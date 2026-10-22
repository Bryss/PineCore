#!/usr/bin/env python3
"""End-to-end test of PineCore without hardware.

Runs two PineCore companions in one process, joined by a simulated LoRa
channel, and drives them through their TCP companion ports with the official
`meshcore` Python client (the same protocol the MeshCore app speaks):

  * device query / self info over TCP
  * advert exchange and automatic contact creation
  * direct message with ACK, and a Public channel message
  * a DM that arrives while no app is connected is kept in the offline
    queue, written to disk on shutdown and delivered after a restart
  * contacts survive a restart
  * the OpenSSL AES shim matches pycryptodome on random keys and blocks

Usage: python3 tests/sim_mesh.py   (needs: pip install meshcore pynacl,
       plus the openhop_core and pyusb wheels in PINECORE_WHEELS or on sys.path)
"""

import asyncio
import os
import random
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "MeshCore"))

import pinecore  # noqa: E402


class SimAir:
    """Every transmitted frame reaches every other radio after its airtime."""

    def __init__(self):
        self.radios = []


class SimRadio:
    spreading_factor = 8
    bandwidth = 62500
    coding_rate = 8
    preamble_length = 32

    def __init__(self, air):
        self.air = air
        self.callback = None
        air.radios.append(self)

    def begin(self):
        return True

    def set_rx_callback(self, callback):
        self.callback = callback

    async def send(self, data):
        await asyncio.sleep(0.05)
        loop = asyncio.get_running_loop()
        for other in self.air.radios:
            if other is not self and other.callback:
                loop.call_soon(other.callback, bytes(data), -60, 9.5)
        return {"airtime_ms": 50}

    async def wait_for_rx(self):
        await asyncio.Event().wait()

    def sleep(self):
        pass

    def check_radio_health(self):
        return True

    def get_last_rssi(self):
        return -60

    def get_last_snr(self):
        return 9.5

    def cleanup(self):
        pass


class Node:
    def __init__(self, name, home, port, air):
        self.name, self.home, self.port, self.air = name, home, port, air
        self.paths = pinecore.Paths(home)
        self.radio = None
        self.task = None
        self.stop = None
        os.makedirs(os.path.join(home, "lib"), exist_ok=True)
        for whl in os.environ.get("PINECORE_WHEELS", "").split(os.pathsep):
            if whl:
                shutil.copy(whl, os.path.join(home, "lib"))
        assert pinecore.main(["--home", home, "set-radio", "EU/UK (Narrow)"]) == 0
        assert pinecore.main(["--home", home, "set-name", name]) == 0

    async def start(self):
        if self.radio is None:
            self.radio = SimRadio(self.air)
        self.stop = asyncio.Event()
        self.task = asyncio.ensure_future(
            pinecore.run_daemon(self.paths, radio=self.radio, port=self.port,
                                bind="127.0.0.1", stop=self.stop))
        for _ in range(100):
            if os.path.exists(self.paths.status):
                return
            if self.task.done():
                raise RuntimeError("node %s exited: %r" % (self.name, self.task.result()))
            await asyncio.sleep(0.05)
        raise RuntimeError("node %s did not start" % self.name)

    async def shutdown(self):
        self.stop.set()
        assert await self.task == 0
        self.radio.callback = None


async def connect(port):
    from meshcore import MeshCore

    mc = await MeshCore.create_tcp("127.0.0.1", port, default_timeout=5)
    assert mc is not None, "TCP connect failed"
    return mc


async def wait_for(predicate, timeout=10.0, what="condition"):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(0.2)
    raise AssertionError("timed out waiting for " + what)


async def fetch_messages(mc):
    from meshcore import EventType

    msgs = []
    while True:
        ev = await mc.commands.get_msg()
        if ev.type == EventType.NO_MORE_MSGS:
            return msgs
        if ev.type == EventType.ERROR:
            raise AssertionError("get_msg error: %r" % ev.payload)
        msgs.append(ev)


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        raise SystemExit(1)


def test_aes_shim():
    from Crypto.Cipher import AES as RealAES

    for _ in range(200):
        key = os.urandom(random.choice((16, 24, 32)))
        data = os.urandom(16 * random.randint(1, 8))
        shim = pinecore._OpenSSLAesEcb(key)
        real = RealAES.new(key, RealAES.MODE_ECB)
        assert shim.encrypt(data) == real.encrypt(data)
        assert shim.decrypt(data) == real.decrypt(data)
    check("OpenSSL AES shim matches pycryptodome (200 random key/data pairs)", True)


async def main():
    from meshcore import EventType

    root = tempfile.mkdtemp(prefix="pinecore-sim-")
    air = SimAir()
    try:
        alice = Node("Alice", os.path.join(root, "alice"), 15001, air)
        bob = Node("Bob", os.path.join(root, "bob"), 15002, air)
        pinecore.setup_environment(alice.paths)
        test_aes_shim()
        # The simulated radio bypasses the real driver, so import it explicitly
        # (an unimportable namespace package once broke only real hardware).
        check("SX1262/CH341 driver modules import from the unpacked wheels",
              len(pinecore.import_radio_driver()) == 3)

        await alice.start()
        await bob.start()
        a = await connect(alice.port)
        b = await connect(bob.port)

        dev = await a.commands.send_device_query()
        check("device query over TCP reports PineCore model",
              dev.type == EventType.DEVICE_INFO and "PineCore" in str(dev.payload.get("model")))
        check("self info carries node name and EU radio settings",
              a.self_info.get("name") == "Alice"
              and abs(float(a.self_info.get("radio_freq", 0)) - 869.618) < 0.001)

        await a.commands.send_advert(flood=True)
        await b.commands.send_advert(flood=True)

        async def has_contact(mc, name):
            await mc.commands.get_contacts()
            return mc.get_contact_by_name(name)

        bob_contact = await wait_for(lambda: has_contact(a, "Bob"), what="Alice to learn Bob")
        await wait_for(lambda: has_contact(b, "Alice"), what="Bob to learn Alice")
        check("adverts exchanged; contacts auto-added on both nodes", True)

        acks = []
        a.subscribe(EventType.ACK, lambda ev: acks.append(ev))
        sent = await a.commands.send_msg(bob_contact, "hello from the pager")
        check("DM accepted by Alice's node", sent.type == EventType.MSG_SENT)

        async def bob_inbox():
            return [m for m in await fetch_messages(b) if m.type == EventType.CONTACT_MSG_RECV]

        got = await wait_for(bob_inbox, what="Bob to receive the DM")
        check("Bob received the DM text", got[0].payload.get("text") == "hello from the pager")
        await wait_for(lambda: acks, what="delivery ACK back at Alice")
        check("Alice got the delivery ACK", True)

        await b.commands.send_chan_msg(0, "public hello")

        async def alice_channel():
            return [m for m in await fetch_messages(a) if m.type == EventType.CHANNEL_MSG_RECV]

        got = await wait_for(alice_channel, what="Alice to receive the channel message")
        check("Public channel message delivered", "public hello" in got[0].payload.get("text", ""))

        # Alice's app goes away; Bob messages her; Alice's daemon restarts.
        await a.disconnect()
        alice_contact = b.get_contact_by_name("Alice")
        await b.commands.send_msg(alice_contact, "while you were away")
        await asyncio.sleep(2.0)
        await alice.shutdown()
        queue = pinecore.read_json(alice.paths.queue, [])
        check("offline DM written to queue.json on shutdown",
              any(m.get("text") == "while you were away" for m in queue))
        contacts = pinecore.read_json(alice.paths.contacts, [])
        check("contacts.json holds Bob", any(c.get("name") == "Bob" for c in contacts))

        await alice.start()
        a = await connect(alice.port)
        await a.commands.get_contacts()
        check("Bob is still a contact after restart", a.get_contact_by_name("Bob") is not None)
        msgs = await fetch_messages(a)
        check("queued DM delivered after restart",
              any(m.payload.get("text") == "while you were away" for m in msgs))
        check("self info name persisted", a.self_info.get("name") == "Alice")
        info = pinecore.read_json(alice.paths.info, {})
        check("info.json matches the running identity",
              info.get("name") == "Alice" and info.get("radio_set") is True
              and info.get("public_key") == a.self_info.get("public_key"))

        await a.disconnect()
        await b.disconnect()
        await alice.shutdown()
        await bob.shutdown()
        print("ALL PASSED")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

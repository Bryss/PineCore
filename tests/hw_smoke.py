#!/usr/bin/env python3
"""Hardware smoke test against a running PineCore on a Pager (172.16.52.1:5000).

TRANSMITS: one flood advert and one message on a random private channel, at
10 dBm. Restores name, TX power, radio settings and channels afterwards.
Disconnects the MeshCore app while it runs (one companion client at a time).
"""

import asyncio, os
from meshcore import MeshCore, EventType

results = []
def check(label, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""), flush=True)

async def stats(mc):
    p = (await mc.commands.get_stats_packets()).payload
    r = (await mc.commands.get_stats_radio()).payload
    c = (await mc.commands.get_stats_core()).payload
    return p, r, c

async def channels(mc):
    out = {}
    for i in range(8):
        ev = await mc.commands.get_channel(i)
        if ev.type != EventType.ERROR and ev.payload.get("channel_name"):
            out[i] = ev.payload
    return out

async def main():
    mc = await MeshCore.create_tcp("172.16.52.1", 5000, default_timeout=15)
    orig = dict(mc.self_info)
    print("ORIGINAL", {k: orig.get(k) for k in ("name","tx_power","radio_freq","radio_bw","radio_sf","radio_cr")}, flush=True)
    chans = await channels(mc)
    print("CHANNELS", {i: c.get("channel_name") for i, c in chans.items()}, flush=True)
    p0, r0, c0 = await stats(mc)
    print("BASELINE", p0, r0, c0, flush=True)

    # T1 TX power via protocol
    ev = await mc.commands.set_tx_power(10)
    await mc.commands.send_appstart()
    check("set TX power 10 dBm via protocol", ev.type == EventType.OK and mc.self_info.get("tx_power") == 10, str(ev.type))

    # T2 first-ever transmission: flood advert
    ev = await mc.commands.send_advert(flood=True)
    check("flood advert accepted", ev.type == EventType.OK, str(ev.type))
    await asyncio.sleep(6)
    p1, r1, c1 = await stats(mc)
    check("advert left the radio (sent/flood_tx counters)", p1["sent"] > p0["sent"] and p1["flood_tx"] > p0["flood_tx"], f"sent {p0['sent']}->{p1['sent']}, tx_air {r1['tx_air_secs']}s")
    check("no core errors after TX", c1["errors"] == 0, str(c1))
    print("RADIO AFTER TX", r1, flush=True)

    # T3 back in RX: noise floor still sampled
    await asyncio.sleep(12)
    _, r2, _ = await stats(mc)
    check("radio back in RX after TX (noise floor sampled)", r2["noise_floor"] < -60, str(r2["noise_floor"]))

    # T4 private test channel message
    free = next(i for i in range(1, 8) if i not in chans)
    secret = os.urandom(16)
    ev = await mc.commands.set_channel(free, "pinecore-test", secret)
    check(f"create private channel {free}", ev.type == EventType.OK, str(ev.type))
    ev = await mc.commands.send_chan_msg(free, "PineCore TX test")
    check("private channel message sent", ev.type in (EventType.OK, EventType.MSG_SENT), str(ev.type))
    await asyncio.sleep(6)
    p3, _, c3 = await stats(mc)
    check("channel message left the radio", p3["flood_tx"] > p1["flood_tx"], f"flood_tx {p1['flood_tx']}->{p3['flood_tx']}")
    ev = await mc.commands.set_channel(free, "", bytes(16))
    check("remove test channel", ev.type == EventType.OK, str(ev.type))

    # T5 out-of-band frequency must be refused
    ev = await mc.commands.set_radio(433.65, 62.5, 8, 8)
    await mc.commands.send_appstart()
    check("433 MHz refused (out of module band)", ev.type == EventType.ERROR and abs(mc.self_info["radio_freq"] - 869.618) < 0.001, str(ev.type))

    # T6 live retune and back
    ev = await mc.commands.set_radio(869.432, 62.5, 7, 5)
    await mc.commands.send_appstart()
    check("live retune to 869.432/SF7/CR5", ev.type == EventType.OK and abs(mc.self_info["radio_freq"] - 869.432) < 0.001 and mc.self_info["radio_sf"] == 7, str(ev.type))
    ev = await mc.commands.set_radio(orig["radio_freq"], orig["radio_bw"], orig["radio_sf"], orig["radio_cr"])
    await mc.commands.send_appstart()
    check("retune back to original", ev.type == EventType.OK and abs(mc.self_info["radio_freq"] - orig["radio_freq"]) < 0.001 and mc.self_info["radio_sf"] == orig["radio_sf"], str(ev.type))
    await asyncio.sleep(12)
    _, r4, _ = await stats(mc)
    check("radio still in RX after retunes", r4["noise_floor"] < -60, str(r4["noise_floor"]))

    # T7 name round trip
    ev = await mc.commands.set_name("PineCore-test")
    await mc.commands.send_appstart()
    ok = mc.self_info.get("name") == "PineCore-test"
    ev2 = await mc.commands.set_name(orig["name"])
    await mc.commands.send_appstart()
    check("rename and restore", ok and mc.self_info.get("name") == orig["name"], mc.self_info.get("name"))

    # restore TX power
    ev = await mc.commands.set_tx_power(orig["tx_power"])
    await mc.commands.send_appstart()
    check("TX power restored", mc.self_info.get("tx_power") == orig["tx_power"], str(mc.self_info.get("tx_power")))
    after = await channels(mc)
    check("channels match the original set", {i: c.get("channel_name") for i, c in after.items()} == {i: c.get("channel_name") for i, c in chans.items()}, str({i: c.get('channel_name') for i, c in after.items()}))
    await mc.disconnect()
    print(f"\n{sum(results)}/{len(results)} passed", flush=True)

asyncio.run(main())

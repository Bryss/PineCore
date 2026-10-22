#!/usr/bin/env python3
"""PineCore: a MeshCore companion radio for the WiFi Pineapple Pager.

WARNING: vibe-coded proof of concept, never tested against the real Mesh Mod
radio hardware. See README.md.

Drives the Glytch Mesh Mod (SX1262 behind a CH341 USB-to-SPI bridge, the same
"Meshtoad" wiring the Meshtastic payload uses) with the openhop_core (pyMC)
MeshCore stack, and serves the standard MeshCore companion protocol on TCP
port 5000. The MeshCore app, meshcore-cli or anything built on meshcore.js /
meshcore_py connects to the Pager exactly as it would to a WiFi companion
radio: 172.16.52.1:5000 over the management AP or USB-C.

Subcommands (the payload menu drives these):
  run                 run the companion daemon (foreground, procd supervises it)
  info                print node name, public key, contact URL and radio settings
  presets             list radio presets usable with this 900 MHz module
  set-radio PRESET    apply a preset (or --freq/--bw/--sf/--cr for custom values)
  set-power DBM       set TX power
  set-name NAME       set the advertised node name
  selftest            check Python modules, AES, libusb and the radio bridge

State lives in $PINECORE_HOME (default /root/.pinecore, on the Pager's eMMC).
"""

import argparse
import asyncio
import ctypes
import ctypes.util
import dataclasses
import glob
import json
import logging
import os
import signal
import sys
import time
import shutil
import types

HOME = os.environ.get("PINECORE_HOME", "/root/.pinecore")
VERSION = "1.0.0"
TCP_PORT = 5000
EXIT_RESTART = 75  # procd respawns us; used after a private-key import
AES_BACKEND = None

# Glytch Mesh Mod: E22 SX1262 module on a CH341 USB-SPI bridge (1a86:5512).
# Pin numbers are CH341 GPIOs and match Meshtastic's lora-usb-meshtoad-e22.yaml.
CH341_VID = 0x1A86
CH341_PID = 0x5512
SX1262_PINS = dict(
    bus_id=0, cs_id=0, cs_pin=0, reset_pin=2, busy_pin=4, irq_pin=6,
    txen_pin=-1, rxen_pin=1, use_dio2_rf=True, use_dio3_tcxo=True,
    dio3_tcxo_voltage=1.8,
)
PREAMBLE_LENGTH = 32
# The CH341 has no interrupt line, so the driver polls the SX1262's IRQ pin over
# USB. Upstream polls every 1 ms (~1000 USB transfers/s), which kept the Pager's
# CPU at ~75% while idle. The SX1262 holds IRQ high until it is cleared, so a
# slower poll only adds latency, which is negligible next to LoRa airtime.
IRQ_POLL_SECONDS = 0.02
# The driver's RX loop also wakes every radio_timing_delay (10 ms upstream) just
# to maybe sample the noise floor; real packets wake it through an event. At
# 30 ms the idle loop costs a third as much CPU and TX setup gains ~80 ms.
RADIO_TIMING_DELAY = 0.03
DEFAULT_TX_POWER = 20
# The SX1262/E22-900 front end is only matched for the sub-GHz ISM bands.
FREQ_MIN_HZ = 850_000_000
FREQ_MAX_HZ = 930_000_000

# MeshCore's published regional presets (api.meshcore.nz/api/v1/config),
# restricted to the 868/915 MHz bands this module can actually use.
# name: (MHz, kHz bandwidth, SF, CR)
PRESETS = {
    "EU/UK (Narrow)": (869.618, 62.5, 8, 8),
    "EU/UK (Deprecated)": (869.525, 250, 11, 5),
    "Switzerland": (869.618, 62.5, 8, 8),
    "Netherlands": (869.618, 62.5, 7, 5),
    "Czech Republic (Narrow)": (869.432, 62.5, 7, 5),
    "Hungary": (869.618, 62.5, 7, 5),
    "Slovakia": (869.618, 62.5, 7, 5),
    "Portugal 868": (869.618, 62.5, 7, 6),
    "USA/Canada (Recommended)": (910.525, 62.5, 7, 5),
    "Australia": (915.800, 250, 10, 5),
    "Australia (Narrow)": (916.575, 62.5, 7, 8),
    "Australia (Mid)": (915.075, 125, 9, 5),
    "Australia: SA, WA": (923.125, 62.5, 8, 8),
    "Australia: QLD": (923.125, 62.5, 8, 5),
    "Brazil": (923.125, 62.5, 8, 8),
    "Costa Rica": (910.525, 125, 11, 5),
    "New Zealand (Narrow)": (917.375, 62.5, 7, 5),
    "New Zealand (Gisborne)": (917.375, 250, 11, 5),
    "Vietnam (Narrow)": (920.250, 62.5, 8, 5),
}
# Presets that also set MeshCore's path hash size (presets list "2B").
PRESET_PATH_HASH_MODE = {"Hungary": 1, "Slovakia": 1, "New Zealand (Narrow)": 1}

BATTERY_VOLTAGE = "/sys/class/power_supply/bq27546-0/voltage_now"
LIB_FALLBACKS = {
    "usb-1.0": ("/usr/lib/libusb-1.0.so.0",),
    "libusb-1.0": ("/usr/lib/libusb-1.0.so.0",),
    "crypto": ("/usr/lib/libcrypto.so.3", "/opt/homebrew/opt/openssl@3/lib/libcrypto.dylib"),
}

log = logging.getLogger("pinecore")


def read_battery_mv():
    try:
        with open(BATTERY_VOLTAGE, "r") as f:
            return max(0, min(int(f.read().strip()) // 1000, 0xFFFF))
    except (OSError, ValueError):
        return 0


class Paths:
    def __init__(self, home):
        self.home = home
        self.lib = os.path.join(home, "lib")
        self.identity = os.path.join(home, "identity.key")
        self.prefs = os.path.join(home, "prefs.json")
        self.contacts = os.path.join(home, "contacts.json")
        self.channels = os.path.join(home, "channels.json")
        self.queue = os.path.join(home, "queue.json")
        self.status = os.path.join(home, "status.json")
        self.info = os.path.join(home, "info.json")


# ---------------------------------------------------------------------------
# Runtime environment: vendored wheels, libusb lookup, AES fallback
# ---------------------------------------------------------------------------

def unpack_wheels(paths):
    """Extract the pinned wheels into lib/site (only when they change).

    Importing straight from the .whl does not work: openhop_core's
    hardware/lora is an implicit namespace package, which zipimport cannot load.
    """
    wheels = sorted(glob.glob(os.path.join(paths.lib, "*.whl")))
    site = os.path.join(paths.lib, "site")
    stamp = "".join("%s %d\n" % (os.path.basename(w), os.path.getsize(w)) for w in wheels)
    stamp_path = os.path.join(site, ".wheels")
    try:
        with open(stamp_path, "r") as f:
            if f.read() == stamp:
                return site
    except OSError:
        pass
    # The payload unpacks with unzip and writes the same stamp; this path is a
    # fallback for dev machines. (zipfile pulls in pathlib -> urllib, which the
    # Pager's minimal Python does not ship.)
    import zipfile

    tmp = site + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, mode=0o700)
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(tmp)
    with open(os.path.join(tmp, ".wheels"), "w") as f:
        f.write(stamp)
    shutil.rmtree(site, ignore_errors=True)
    os.rename(tmp, site)
    return site


def setup_environment(paths):
    """Make the pinned wheels importable and patch what OpenWrt lacks."""
    if glob.glob(os.path.join(paths.lib, "*.whl")):
        site = unpack_wheels(paths)
        if site not in sys.path:
            sys.path.insert(0, site)

    # OpenWrt has no ldconfig cache or compiler, so ctypes.util.find_library
    # returns None for libraries that are present. pyusb resolves libusb
    # through it at backend load time, so a fallback here is enough.
    original = ctypes.util.find_library

    def find_library(name):
        found = original(name)
        if found:
            return found
        for candidate in LIB_FALLBACKS.get(name, ()):
            if os.path.exists(candidate):
                return candidate
        return None

    if getattr(ctypes.util.find_library, "__name__", "") != "find_library_with_fallback":
        find_library.__name__ = "find_library_with_fallback"
        ctypes.util.find_library = find_library

    global AES_BACKEND
    try:
        from Crypto.Cipher import AES  # noqa: F401  (pycryptodome, if present)

        AES_BACKEND = "pycryptodome"
    except ImportError:
        install_aes_shim()
        AES_BACKEND = "OpenSSL libcrypto (self-tested)"


class _OpenSSLAesEcb:
    """The subset of pycryptodome's AES object that openhop_core uses."""

    _lib = None

    @classmethod
    def _libcrypto(cls):
        if cls._lib is None:
            path = ctypes.util.find_library("crypto")
            if not path:
                raise ImportError("libcrypto not found; install pycryptodome or libopenssl3")
            lib = ctypes.CDLL(path)
            vp, cp, ip = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
            lib.EVP_CIPHER_CTX_new.restype = vp
            lib.EVP_CIPHER_CTX_free.argtypes = [vp]
            for name in ("EVP_aes_128_ecb", "EVP_aes_192_ecb", "EVP_aes_256_ecb"):
                getattr(lib, name).restype = vp
            lib.EVP_CipherInit_ex.argtypes = [vp, vp, vp, cp, cp, ip]
            lib.EVP_CIPHER_CTX_set_padding.argtypes = [vp, ip]
            lib.EVP_CipherUpdate.argtypes = [vp, vp, ctypes.POINTER(ip), cp, ip]
            lib.EVP_CipherFinal_ex.argtypes = [vp, vp, ctypes.POINTER(ip)]
            cls._lib = lib
        return cls._lib

    def __init__(self, key):
        ciphers = {16: "EVP_aes_128_ecb", 24: "EVP_aes_192_ecb", 32: "EVP_aes_256_ecb"}
        if len(key) not in ciphers:
            raise ValueError("Incorrect AES key length (%d bytes)" % len(key))
        self._key = bytes(key)
        self._cipher = getattr(self._libcrypto(), ciphers[len(key)])()

    def _run(self, data, encrypt):
        data = bytes(data)
        if len(data) % 16:
            raise ValueError("Data must be aligned to block boundary in ECB mode")
        lib = self._libcrypto()
        ctx = lib.EVP_CIPHER_CTX_new()
        if not ctx:
            raise MemoryError("EVP_CIPHER_CTX_new failed")
        try:
            out = ctypes.create_string_buffer(len(data) + 16)
            addr = ctypes.addressof(out)
            n = ctypes.c_int(0)
            tail = ctypes.c_int(0)
            if (lib.EVP_CipherInit_ex(ctx, self._cipher, None, self._key, None, encrypt) != 1
                    or lib.EVP_CIPHER_CTX_set_padding(ctx, 0) != 1
                    or lib.EVP_CipherUpdate(ctx, addr, ctypes.byref(n), data, len(data)) != 1
                    or lib.EVP_CipherFinal_ex(ctx, addr + n.value, ctypes.byref(tail)) != 1):
                raise ValueError("OpenSSL AES operation failed")
            return out.raw[: n.value + tail.value]
        finally:
            lib.EVP_CIPHER_CTX_free(ctx)

    def encrypt(self, data):
        return self._run(data, 1)

    def decrypt(self, data):
        return self._run(data, 0)


def install_aes_shim():
    """Register a Crypto.Cipher.AES module backed by the Pager's libcrypto.

    MeshCore only needs AES-ECB, and the OpenWrt pycryptodome package (plus
    libgmp) costs ~5 MB of the Pager's small root overlay. The shim is checked
    against the FIPS-197 known-answer vector before anything uses it.
    """
    aes = types.ModuleType("Crypto.Cipher.AES")
    aes.MODE_ECB = 1

    def new(key, mode, *args, **kwargs):
        if mode != aes.MODE_ECB or args or kwargs:
            raise ValueError("PineCore AES shim only implements ECB mode")
        return _OpenSSLAesEcb(key)

    aes.new = new
    vector = _OpenSSLAesEcb(bytes(range(16)))
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    expected = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    if vector.encrypt(plain) != expected or vector.decrypt(expected) != plain:
        raise ImportError("OpenSSL AES self-test failed")

    crypto = types.ModuleType("Crypto")
    cipher = types.ModuleType("Crypto.Cipher")
    crypto.Cipher = cipher
    cipher.AES = aes
    sys.modules.update({"Crypto": crypto, "Crypto.Cipher": cipher, "Crypto.Cipher.AES": aes})


# ---------------------------------------------------------------------------
# Persistence helpers (atomic JSON on the eMMC)
# ---------------------------------------------------------------------------

def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path, value):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(value, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_identity(paths, create=True):
    from openhop_core.protocol.identity import LocalIdentity

    try:
        with open(paths.identity, "r", encoding="ascii") as f:
            seed = bytes.fromhex(f.read().strip())
        return LocalIdentity(seed=seed)
    except FileNotFoundError:
        if not create:
            return None
    identity = LocalIdentity()
    save_identity_seed(paths, identity.signing_key.encode())
    log.info("Generated new MeshCore identity %s", identity.get_public_key().hex()[:16])
    return identity


def save_identity_seed(paths, seed):
    tmp = paths.identity + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="ascii") as f:
        f.write(bytes(seed).hex() + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, paths.identity)


def prefs_to_json(prefs):
    out = dataclasses.asdict(prefs)
    for key, value in out.items():
        if isinstance(value, (bytes, bytearray)):
            out[key] = {"hex": bytes(value).hex()}
    return out


def prefs_from_json(prefs, data):
    fields = {f.name for f in dataclasses.fields(prefs)}
    for key, value in data.items():
        if key not in fields:
            continue
        if isinstance(value, dict) and "hex" in value:
            value = bytes.fromhex(value["hex"])
        setattr(prefs, key, value)


def radio_configured(prefs_data):
    return bool(prefs_data.get("frequency_hz")) and prefs_data.get("pinecore_radio_set") is True


def check_frequency(freq_hz):
    if not FREQ_MIN_HZ <= int(freq_hz) <= FREQ_MAX_HZ:
        raise ValueError(
            "%.3f MHz is outside the Mesh Mod's %d-%d MHz range"
            % (freq_hz / 1e6, FREQ_MIN_HZ // 1_000_000, FREQ_MAX_HZ // 1_000_000)
        )


# ---------------------------------------------------------------------------
# Companion: CompanionRadio + JSON persistence
# ---------------------------------------------------------------------------

def make_companion_class():
    from openhop_core.companion import CompanionRadio
    from openhop_core.companion.constants import STATS_TYPE_CORE, STATS_TYPE_PACKETS, STATS_TYPE_RADIO
    from openhop_core.companion.frame_server import CompanionFrameServer

    class PagerCompanion(CompanionRadio):
        """CompanionRadio whose preferences and identity survive restarts."""

        def __init__(self, paths, *args, **kwargs):
            self._paths = paths
            self.restart_requested = False
            super().__init__(*args, **kwargs)
            self.tx_air_secs = 0.0
            real_send = self._radio.send

            async def send_and_count(data, *a, **kw):
                meta = await real_send(data, *a, **kw)
                if meta:
                    self.tx_air_secs += float(meta.get("airtime_ms", 0)) / 1000.0
                return meta

            self._radio.send = send_and_count

        def get_stats(self, stats_type=STATS_TYPE_PACKETS):
            # Fill in what openhop_core leaves at zero, so the app's stats are real.
            stats = dict(super().get_stats(stats_type))
            if stats_type == STATS_TYPE_CORE:
                stats["battery_mv"] = read_battery_mv()
                stats["errors"] = self.stats.packets.tx_errors
            elif stats_type == STATS_TYPE_RADIO:
                stats["tx_air_secs"] = int(self.tx_air_secs)
            else:
                stats["sent"] = stats.get("flood_tx", 0) + stats.get("direct_tx", 0)
                stats["recv"] = stats.get("flood_rx", 0) + stats.get("direct_rx", 0)
            return stats

        def _load_prefs(self):
            prefs_from_json(self.prefs, read_json(self._paths.prefs, {}))

        def _save_prefs(self):
            # Merge so PineCore's own keys (preset name, radio-set flag) survive.
            data = read_json(self._paths.prefs, {})
            data.update(prefs_to_json(self.prefs))
            data["pinecore_radio_set"] = True
            write_json(self._paths.prefs, data)
            write_info(self._paths, self._identity)

        def set_radio_params(self, freq_hz, bw_hz, sf, cr):
            try:
                check_frequency(freq_hz)
            except ValueError as e:
                log.warning("Rejected radio change from app: %s", e)
                return False
            return super().set_radio_params(freq_hz, bw_hz, sf, cr)

        def _get_radio_stats(self):
            # The SX1262 driver samples RSSI while idle; report it so the app
            # shows a real noise floor instead of 0.
            stats = super()._get_radio_stats()
            get_floor = getattr(self._radio, "get_noise_floor", None)
            floor = get_floor() if callable(get_floor) else None
            if floor is not None:
                stats["noise_floor"] = int(round(floor))
            return stats

        def import_private_key(self, key):
            # CompanionRadio swaps in a new MeshNode without restarting its
            # dispatcher, so persist the key and let procd restart us cleanly.
            from openhop_core.protocol.identity import LocalIdentity

            try:
                LocalIdentity(seed=key)
            except Exception as e:
                log.error("Rejected private key import: %s", e)
                return False
            save_identity_seed(self._paths, key)
            try:
                os.unlink(self._paths.info)
            except FileNotFoundError:
                pass
            log.info("Private key imported; restarting to load the new identity")
            self.restart_requested = True
            return True

    class PagerFrameServer(CompanionFrameServer):
        """Frame server that reports the Pager's battery and eMMC usage."""

        def __init__(self, *args, on_change=None, storage_dir=None, **kwargs):
            super().__init__(*args, **kwargs)
            self._on_change = on_change or (lambda: None)
            self._storage_dir = storage_dir

        def _get_batt_and_storage(self):
            millivolts = read_battery_mv()
            used_kb = total_kb = 0
            if self._storage_dir:
                try:
                    st = os.statvfs(self._storage_dir)
                    total_kb = st.f_blocks * st.f_frsize // 1024
                    used_kb = (st.f_blocks - st.f_bfree) * st.f_frsize // 1024
                except OSError:
                    pass
            return (millivolts, min(used_kb, 0xFFFFFFFF), min(total_kb, 0xFFFFFFFF))

        async def _persist_contact(self, contact):
            self._on_change()

        async def _save_contacts(self):
            self._on_change()

        async def _save_channels(self):
            self._on_change()

    return PagerCompanion, PagerFrameServer


class StateStore:
    """Snapshots contacts, channels and the offline message queue to JSON.

    Push callbacks only exist while an app is connected, so instead of relying
    on them the daemon snapshots its in-memory stores whenever they change.
    Unchanged state is never rewritten, which keeps eMMC writes rare.
    """

    def __init__(self, paths, companion):
        self.paths = paths
        self.companion = companion
        self._last = {}
        self.wake = asyncio.Event()

    def restore(self):
        from openhop_core.companion.constants import DEFAULT_PUBLIC_CHANNEL_SECRET
        from openhop_core.companion.models import QueuedMessage

        c = self.companion
        c.contacts.load_from_dicts(read_json(self.paths.contacts, []))
        for ch in read_json(self.paths.channels, []):
            c.channels.set(int(ch["idx"]), _channel(ch["name"], bytes.fromhex(ch["secret"])))
        if c.get_channel(0) is None:
            c.set_channel(0, "Public", DEFAULT_PUBLIC_CHANNEL_SECRET)
        for m in read_json(self.paths.queue, []):
            for key in ("sender_key", "channel_data_payload", "sender_prefix"):
                m[key] = bytes.fromhex(m.get(key) or "")
            c.message_queue.push(QueuedMessage(**m))
        self._last = self._collect()
        log.info("Restored %d contacts, %d channels, %d queued messages",
                 c.contacts.get_count(), c.channels.get_count(), c.message_queue.count)

    def _collect(self):
        c = self.companion
        channels = []
        for idx in range(c.channels.max_channels):
            ch = c.channels.get(idx)
            if ch is not None:
                channels.append({"idx": idx, "name": ch.name, "secret": ch.secret.hex()})
        queue = []
        for m in list(c.message_queue._queue):
            d = dataclasses.asdict(m)
            for key in ("sender_key", "channel_data_payload", "sender_prefix"):
                d[key] = bytes(d.get(key) or b"").hex()
            queue.append(d)
        return {
            self.paths.contacts: c.contacts.to_dicts(),
            self.paths.channels: channels,
            self.paths.queue: queue,
        }

    def save(self):
        current = self._collect()
        for path, value in current.items():
            if self._last.get(path) != value:
                write_json(path, value)
        self._last = current

    async def run(self, interval=30.0):
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=interval)
                await asyncio.sleep(2.0)  # coalesce bursts of contact updates
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            try:
                self.save()
            except Exception:
                log.exception("Saving state failed")


def _channel(name, secret):
    from openhop_core.companion.models import Channel

    return Channel(name=name[:32], secret=secret)


# ---------------------------------------------------------------------------
# Radio
# ---------------------------------------------------------------------------

def import_radio_driver():
    from openhop_core.hardware.lora.LoRaRF.SX126x import set_spi_transport
    from openhop_core.hardware.sx1262_wrapper import SX1262Radio
    from openhop_core.hardware.transports.ch341_spi_transport import CH341SPITransport

    return set_spi_transport, SX1262Radio, CH341SPITransport


def slow_down_irq_polling():
    """Make the CH341 IRQ poll loop sleep IRQ_POLL_SECONDS instead of 1 ms.

    The interval is a local inside the upstream poll loop, so give that one
    module a clock whose sleep() has a floor; its only sleeps are in that loop.
    """
    from openhop_core.hardware.ch341 import ch341_gpio_manager

    real_time = time

    class PollClock:
        time = staticmethod(real_time.time)
        monotonic = staticmethod(real_time.monotonic)

        @staticmethod
        def sleep(seconds):
            real_time.sleep(max(seconds, IRQ_POLL_SECONDS))

    ch341_gpio_manager.time = PollClock


def build_radio(prefs_data):
    set_spi_transport, SX1262Radio, CH341SPITransport = import_radio_driver()
    slow_down_irq_polling()

    check_frequency(prefs_data["frequency_hz"])
    spi = CH341SPITransport(vid=CH341_VID, pid=CH341_PID, auto_setup_gpio=True)
    set_spi_transport(spi)
    radio = SX1262Radio(
        **SX1262_PINS,
        frequency=int(prefs_data["frequency_hz"]),
        tx_power=int(prefs_data.get("tx_power_dbm", DEFAULT_TX_POWER)),
        spreading_factor=int(prefs_data["spreading_factor"]),
        bandwidth=int(prefs_data["bandwidth_hz"]),
        coding_rate=int(prefs_data["coding_rate"]),
        preamble_length=PREAMBLE_LENGTH,
        radio_timing_delay=RADIO_TIMING_DELAY,
        sync_word=0x12,  # MeshCore private sync word (the SX1262 reset default)
        spi_transport=spi,
        gpio_manager=spi.gpio_manager,
    )
    if not radio.begin():
        raise RuntimeError("SX1262 did not initialise; check the Mesh Mod connection")
    return radio


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

async def run_daemon(paths, radio=None, port=TCP_PORT, bind="0.0.0.0", stop=None):
    """Run the companion until SIGTERM/SIGINT (or `stop` is set). Returns the exit code.

    `radio` and `stop` let tests run several nodes in one process on a
    simulated radio; normally the daemon builds the Mesh Mod radio itself.
    """
    prefs_data = read_json(paths.prefs, {})
    if not radio_configured(prefs_data):
        log.error("No radio region selected; choose one in the payload menu (Radio region)")
        return 2
    identity = load_identity(paths)
    PagerCompanion, PagerFrameServer = make_companion_class()

    owns_radio = radio is None
    if owns_radio:
        radio = build_radio(prefs_data)
        log.info("sx1262 init success (%.3f MHz, BW %.1f kHz, SF%d, CR%d, %d dBm)",
                 prefs_data["frequency_hz"] / 1e6, prefs_data["bandwidth_hz"] / 1e3,
                 prefs_data["spreading_factor"], prefs_data["coding_rate"],
                 prefs_data.get("tx_power_dbm", DEFAULT_TX_POWER))

    companion = PagerCompanion(
        paths,
        radio,
        identity,
        node_name=prefs_data.get("node_name") or default_node_name(identity),
        radio_config={
            "frequency": int(prefs_data["frequency_hz"]),
            "bandwidth": int(prefs_data["bandwidth_hz"]),
            "spreading_factor": int(prefs_data["spreading_factor"]),
            "coding_rate": int(prefs_data["coding_rate"]),
            "tx_power": int(prefs_data.get("tx_power_dbm", DEFAULT_TX_POWER)),
            "max_tx_power_dbm": 22,
        },
    )
    store = StateStore(paths, companion)
    store.restore()

    pubkey = identity.get_public_key()
    server = PagerFrameServer(
        companion,
        "0x%02x" % pubkey[0],
        port=port,
        bind_address=bind,
        device_model="PineCore (WiFi Pineapple Pager)",
        build_date="PineCore " + VERSION,
        on_change=store.wake.set,
        storage_dir=paths.home,
    )

    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        def advert_now():
            log.info("Sending flood advert (SIGUSR1)")
            asyncio.ensure_future(companion.advertise(flood=True))

        loop.add_signal_handler(signal.SIGUSR1, advert_now)

    await companion.start()
    await server.start()
    saver = asyncio.ensure_future(store.run())
    write_info(paths, identity)
    write_json(paths.status, {
        "pid": os.getpid(),
        "started": int(time.time()),
        "port": port,
        "name": companion.prefs.node_name,
        "public_key": pubkey.hex(),
        "version": VERSION,
    })
    log.info("PineCore %s ready: '%s' %s on tcp/%d", VERSION, companion.prefs.node_name,
             pubkey.hex()[:16], port)

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        if companion.restart_requested:
            break

    log.info("Shutting down")
    saver.cancel()
    try:
        await server.stop()
        await companion.stop()
    finally:
        store.save()
        try:
            os.unlink(paths.status)
        except FileNotFoundError:
            pass
        if owns_radio:
            try:
                radio.cleanup()
            except Exception:
                log.debug("Radio cleanup failed", exc_info=True)
    return EXIT_RESTART if companion.restart_requested else 0


def default_node_name(identity):
    return "Pager-" + identity.get_public_key()[:2].hex().upper()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def update_prefs(paths, **changes):
    data = read_json(paths.prefs, {})
    data.update(changes)
    write_json(paths.prefs, data)
    write_info(paths)
    return data


def write_info(paths, identity=None):
    """Refresh info.json, the cheap summary the payload menu reads with jsonfilter.

    Starting Python and importing the MeshCore stack takes many seconds on the
    Pager's MIPS core, so the menu never calls us just to draw a label. The
    public key is cached here; only the very first call loads the identity.
    """
    data = read_json(paths.prefs, {})
    pub = identity.get_public_key().hex() if identity else read_json(paths.info, {}).get("public_key")
    if not pub:
        pub = load_identity(paths).get_public_key().hex()
    name = data.get("node_name") or "Pager-" + pub[:4].upper()
    info = {
        "name": name,
        "public_key": pub,
        "contact_url": contact_url(name, pub),
        "radio_set": radio_configured(data),
        "frequency_mhz": (data.get("frequency_hz") or 0) / 1e6,
        "bandwidth_khz": (data.get("bandwidth_hz") or 0) / 1e3,
        "spreading_factor": data.get("spreading_factor") or 8,
        "coding_rate": data.get("coding_rate") or 8,
        "tx_power_dbm": data.get("tx_power_dbm", DEFAULT_TX_POWER),
        "preset": data.get("pinecore_preset", ""),
    }
    write_json(paths.info, info)
    return info


def _url_quote(text):
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    return "".join(chr(b) if b in safe else "%%%02X" % b for b in text.encode("utf-8"))


def contact_url(name, pubkey_hex):
    """The link the MeshCore app's own contact QR codes carry (type 1 = chat node)."""
    return "meshcore://contact/add?name=%s&public_key=%s&type=1" % (_url_quote(name), pubkey_hex)


def cmd_info(paths, args):
    info = write_info(paths, load_identity(paths))
    if args.field:
        print(info[args.field])
    else:
        print(json.dumps(info, indent=1))
    return 0


def cmd_presets(paths, args):
    for name in PRESETS:
        print(name)
    return 0


def cmd_set_radio(paths, args):
    if args.preset:
        if args.preset not in PRESETS:
            print("Unknown preset: %s" % args.preset, file=sys.stderr)
            return 1
        mhz, khz, sf, cr = PRESETS[args.preset]
        hash_mode = PRESET_PATH_HASH_MODE.get(args.preset, 0)
        name = args.preset
    else:
        if None in (args.freq, args.bw, args.sf, args.cr):
            print("Give a preset, or all of --freq --bw --sf --cr", file=sys.stderr)
            return 1
        mhz, khz, sf, cr = args.freq, args.bw, args.sf, args.cr
        hash_mode = read_json(paths.prefs, {}).get("path_hash_mode", 0)
        name = "Custom"
    freq_hz = int(round(mhz * 1_000_000))
    try:
        check_frequency(freq_hz)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if not (5 <= sf <= 12 and 5 <= cr <= 8):
        print("SF must be 5-12 and CR 5-8", file=sys.stderr)
        return 1
    data = read_json(paths.prefs, {})
    data.setdefault("tx_power_dbm", DEFAULT_TX_POWER)
    update_prefs(paths, frequency_hz=freq_hz, bandwidth_hz=int(round(khz * 1000)),
                 spreading_factor=int(sf), coding_rate=int(cr), path_hash_mode=hash_mode,
                 tx_power_dbm=data["tx_power_dbm"], pinecore_preset=name,
                 pinecore_radio_set=True)
    print("%s: %.3f MHz / BW%g / SF%d / CR%d" % (name, mhz, khz, sf, cr))
    return 0


def cmd_set_power(paths, args):
    if not 0 <= args.dbm <= 22:
        print("TX power must be 0-22 dBm", file=sys.stderr)
        return 1
    update_prefs(paths, tx_power_dbm=args.dbm)
    print("TX power: %d dBm" % args.dbm)
    return 0


def cmd_set_name(paths, args):
    name = args.name.strip().encode("utf-8")[:31].decode("utf-8", errors="ignore")
    if not name:
        print("Name cannot be empty", file=sys.stderr)
        return 1
    update_prefs(paths, node_name=name)
    print("Name: %s" % name)
    return 0


def cmd_selftest(paths, args):
    ok = True

    def check(label, fn):
        nonlocal ok
        try:
            detail = fn()
            print("OK   %s%s" % (label, (": " + detail) if detail else ""))
        except Exception as e:
            ok = False
            print("FAIL %s: %s" % (label, e))

    check("python", lambda: sys.version.split()[0])
    check("openhop_core", lambda: __import__("openhop_core").__version__)
    check("pynacl", lambda: __import__("nacl").__version__)
    check("aes", lambda: AES_BACKEND)
    check("companion", lambda: make_companion_class() and "")
    check("radio driver", lambda: import_radio_driver() and "SX1262 over CH341")

    def usb():
        import usb.backend.libusb1
        import usb.core

        backend = usb.backend.libusb1.get_backend()
        if backend is None:
            raise RuntimeError("libusb-1.0 backend unavailable")
        found = list(usb.core.find(find_all=True, idVendor=CH341_VID, idProduct=CH341_PID,
                                   backend=backend))
        return "%d Mesh Mod bridge(s) attached" % len(found)

    check("libusb", usb)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pinecore", description=__doc__.split("\n")[0])
    parser.add_argument("--home", default=HOME)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--port", type=int, default=TCP_PORT)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--debug", action="store_true")
    p = sub.add_parser("info")
    p.add_argument("field", nargs="?")
    sub.add_parser("presets")
    p = sub.add_parser("set-radio")
    p.add_argument("preset", nargs="?")
    p.add_argument("--freq", type=float, help="MHz")
    p.add_argument("--bw", type=float, help="kHz")
    p.add_argument("--sf", type=int)
    p.add_argument("--cr", type=int)
    p = sub.add_parser("set-power")
    p.add_argument("dbm", type=int)
    p = sub.add_parser("set-name")
    p.add_argument("name")
    sub.add_parser("selftest")
    args = parser.parse_args(argv)

    # openhop_core logs every packet at INFO; the Pager's logread ring buffer is
    # small, so keep the stack at WARNING unless --debug is given.
    debug = getattr(args, "debug", False)
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not debug:
        for name in ("pinecore", "CompanionFrameServer", "CompanionRadio"):
            logging.getLogger(name).setLevel(logging.INFO)
    paths = Paths(args.home)
    os.makedirs(paths.home, mode=0o700, exist_ok=True)
    setup_environment(paths)

    if args.cmd == "run":
        try:
            code = asyncio.run(run_daemon(paths, port=args.port, bind=args.bind))
        except Exception as e:
            log.error("PineCore failed: %s", e, exc_info=getattr(args, "debug", False))
            code = 1
        # State is saved and the radio released by now. Skip interpreter
        # teardown: pyusb's finalizers race libusb there and trip
        # "Assertion failed: refcnt >= 2" (an abort) on every stop.
        logging.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    handlers = {
        "info": cmd_info,
        "presets": cmd_presets,
        "set-radio": cmd_set_radio,
        "set-power": cmd_set_power,
        "set-name": cmd_set_name,
        "selftest": cmd_selftest,
    }
    return handlers[args.cmd](paths, args)


if __name__ == "__main__":
    sys.exit(main())

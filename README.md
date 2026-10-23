# PineCore

**MeshCore for the WiFi Pineapple Pager + Glytch Mesh Mod, in Python.**

> [!IMPORTANT]
> **Deprecated: use [PineCore Native](https://github.com/Bryss/PineCore-Native) instead.**
> PineCore Native runs MeshCore's own C++ companion firmware on the Pager. It uses about 5% CPU
> instead of about 45%, needs 1 MB of RAM instead of 20 MB, installs offline in half a megabyte,
> and starts in seconds. This Python version stays up for reference and as a hackable prototype,
> but **it probably won't get further work**. Fork it if you want to take it somewhere.

> [!CAUTION]
> **VIBE-CODED PROOF OF CONCEPT. NOT A FINISHED, PROPERLY TESTED PRODUCT.**
>
> An AI assistant wrote this in one session. **It has never exchanged a packet with another MeshCore
> node.** Only one Mesh Mod was available, and no other MeshCore nodes were in range.
>
> Checked on one Pager with the Mesh Mod attached:
> - SPI to the SX1262 over the CH341 works: registers read back correctly, the chip sits in RX, and the noise floor is live
> - the MeshCore phone app connects over TCP 5000 and shows the node (one user, one phone)
> - 16 protocol/hardware checks: two transmissions (flood advert + private-channel message at 10 dBm)
>   completed with the chip's TX-done interrupt; the radio returns to RX; live retune; out-of-band
>   frequencies refused; settings persist; procd respawns it after `kill -9`
> - a 15-check two-node simulation (adverts, DMs + ACKs, channels, offline queue across restarts)
>
> Not verified: receiving real packets, interop with real nodes, actual RF output and antenna path,
> which E22 variant the Mod uses, long-term stability. Check your radio settings are legal where you
> are before transmitting. Use at your own risk; no support, no warranty.

## Why this exists

Glytch's [Mesh Mod](https://github.com/glytchtech/Pager-Mod-Docs/tree/main/Mesh) turns the Pager into
a LoRa node and ships with a Meshtastic payload. Plenty of local meshes are moving to
[MeshCore](https://github.com/meshcore-dev/MeshCore), so PineCore is the same idea for MeshCore: the
Pager joins the mesh, and the **MeshCore app** connects to it over Wi-Fi, the way it connects to a WiFi
companion radio.

```
MeshCore app / meshcore-cli ──TCP 172.16.52.1:5000──▶ Pager ──USB──▶ Mesh Mod (CH341 + SX1262) ))) MeshCore mesh
```

## Comparison

All measured on the same Pager model and Mesh Mod at idle, averaged over 30 s. The Pager itself idles at about 4% CPU.

| | **PineCore (this, Python)** | [PineCore Native](https://github.com/Bryss/PineCore-Native) (C++) | Glytch Meshtastic (reference) |
|---|---|---|---|
| Stack | [openhop_core](https://github.com/pyMC-dev/pyMC_core) 1.1.3 (Python MeshCore) | MeshCore companion firmware ([meshcore-linux](https://github.com/l5yth/meshcore-linux)) | `meshtasticd` 2.7.26 |
| Daemon CPU, idle | **~45%** (74% before tuning) | ~5% | ~2% |
| Memory (RSS) | 20 MB | 1 MB | 7.5 MB |
| Install footprint | ~7.5 MB on the overlay (Python runtime) | ~0.5 MB, static binary | opkg packages |
| Needs internet to install | yes (opkg + PyPI) | no | yes |
| Time to start | tens of seconds (Python imports on the MIPS CPU) | ~6 s | |
| Phone app | MeshCore app, TCP 5000 | MeshCore app, TCP 5000 | Meshtastic app, TCP 4403 |

Battery: on one Pager the Python version ran at about 0.9 A discharge even on laptop USB (cable/port may have
played a part). Pager 2 with the native build kept charging throughout testing.

**Why so heavy?** The CH341 has no interrupt line, so the SX1262's IRQ pin has to be polled over USB.
openhop_core polls every 1 ms from Python. PineCore slows that to 20 ms and the radio loop from 10 to
30 ms, which still leaves about 45%. Each USB round trip from Python on a 580 MHz MIPS core is simply expensive.

## How it works

- **Radio:** openhop_core's `SX1262Radio` over its CH341 USB-SPI transport (pyusb + the Pager's
  libusb). It uses the same pins as Meshtastic's `lora-usb-meshtoad-e22` profile: CS 0, RXEN 1, RESET 2,
  BUSY 4, IRQ 6, DIO2 RF switch, DIO3 TCXO 1.8 V. PineCore detaches the Pager's `mphsi-ch34x` kernel driver from the bridge.
- **Node:** openhop_core's `CompanionRadio` (contacts, DMs with ACKs, channels, offline queue, paths,
  signing, telemetry) behind its standard companion frame server on **TCP 5000**.
- **PineCore's own parts (`MeshCore/pinecore.py`):**
  - JSON persistence on the eMMC (identity, prefs, contacts, channels, offline messages), snapshotted on change
  - real stats openhop_core leaves at zero: battery from the Pager's fuel gauge, sent/recv, TX airtime, noise floor
  - an 850–930 MHz band guard for this module (the app can't push it to 433 MHz)
  - an OpenSSL-backed AES-ECB shim (FIPS-197 self-test), so pycryptodome and libgmp (about 5 MB) aren't needed
  - wheels unpacked to `lib/site` (openhop_core has a namespace package that zipimport can't load)
  - `info.json`, so the Pager menu doesn't start Python just to draw a label (about 30 s per start on this CPU)

## Install

1. Pager charged, on current firmware, and **online** (Wi-Fi client) for the first install.
2. Attach the Mesh Mod **with its antenna on**. Never transmit without an antenna. The Mod uses the
   Pager's only USB port, so use the Pager's **management Wi-Fi AP** for SSH and the app.
3. Copy the payload folder:
   ```sh
   scp -O -r MeshCore root@172.16.52.1:/root/payloads/user/general/
   ```
4. On the Pager: **Payloads → General → MeshCore → Install / Update** (a few minutes). Pick your
   **radio region** when asked. It must match your local mesh (e.g. *EU/UK (Narrow)* 869.618 MHz SF8
   BW62.5 CR8, or *USA/Canada (Recommended)* 910.525 MHz SF7 BW62.5).
5. **Start server**, then in the MeshCore app connect via **TCP** to `172.16.52.1` port `5000`.

`meshcore-cli` works too: `meshcore-cli -t 172.16.52.1 -p 5000 infos`.

### Payload menu

| Item | What it does |
|---|---|
| Start server / Stop server | run or stop the companion (procd-supervised, auto-respawn) |
| Radio region | MeshCore's regional presets (868/915 MHz ones), or custom freq/BW/SF/CR |
| Node name, TX power | advertised name; 0–22 dBm (default 20) |
| Show contact QR | a `meshcore://contact/add…` QR on the Pager screen to scan from the app |
| Send advert | flood advert now (via SIGUSR1, without disconnecting the app) |
| Status | running state, region, key, Mesh Mod detection |
| Install / Update | installs/updates runtime and PineCore; settings are kept |
| Enable at Boot | default OFF |
| Uninstall | removes PineCore and, optionally, the Python packages it added; keeps identity/contacts |

Command line: `payload.sh --install | --start | --stop | --region "EU/UK (Narrow)" | --name NAME | --advert | --boot-on | --boot-off | --status | --uninstall`.

## Known issues

- **CPU and battery:** about 45% CPU while idle (see above). That's the main reason this version is deprecated.
- **Battery %:** the app derives its own percentage from the voltage, which differs from the Pager's
  fuel gauge at mid charge (e.g. 22% vs 30%). Trust the Pager's figure.
- **Noise floor** read about -103 dBm with the Pager on USB next to a laptop. That's high and likely local interference.
- **TX power:** default 20 dBm. A high-power E22-…M30S adds about 8 dB of PA gain, so lower it to stay legal
  (EU 869.4–869.65 MHz: 500 mW ERP).
- Only one of Meshtastic, PineCore and PineCore Native can own the Mesh Mod at a time; Start checks.

Logs: `logread | grep python3`. Debug: stop the server, then
`PINECORE_HOME=/root/.pinecore python3 /root/.pinecore/pinecore.py run --debug`. Self-test:
`python3 /root/.pinecore/pinecore.py selftest`.

## Development

- `tests/sim_mesh.py`: two PineCore nodes on a simulated radio, driven over TCP by the official
  `meshcore` Python client (needs `pip install meshcore pynacl` plus the openhop_core/pyusb wheels; see the script header).
- `tests/hw_smoke.py`: 16 checks against a running Pager. **It transmits** (advert + private-channel message at 10 dBm).

## Contributing

This was built in one sitting to get MeshCore onto the Pager. It's here so the community can pick it
up. **Fork it, break it, improve it**. That said, the
[native version](https://github.com/Bryss/PineCore-Native) is the better base for almost anything.
Good reasons to work on *this* one: you want a quick Python playground for MeshCore on the Pager, or
you want to improve [openhop_core](https://github.com/pyMC-dev/pyMC_core)'s CH341 path (say, fewer
USB round trips), which benefits every pyMC user.

## Credits

- [Glytch](https://github.com/glytchtech/Pager-Mod-Docs) for the Mesh Mod and the Meshtastic payload this mirrors
- [pyMC / openHop](https://github.com/pyMC-dev/pyMC_core) (MIT) for the Python MeshCore stack and CH341 driver
- [MeshCore](https://github.com/meshcore-dev/MeshCore)

MIT licensed.

# PineCore

> [!CAUTION]
> **VIBE-CODED PROOF OF CONCEPT. NOT A WORKING, TESTED PRODUCT.**
>
> An AI assistant wrote this in one session. It has **never been run against the real Glytch Mesh
> Mod radio**: the module wasn't attached, and no LoRa packet has ever been sent or received through
> this code. The Mod and its antenna share the Pager's only USB port with the USB-C link used during
> development, so it was never plugged in. What *was* tested: two simulated nodes on a fake radio
> on a Mac, plus installing the Python runtime and self-test on one Pager with no Mod attached. That's it.
>
> Expect it to be broken in ways nobody has found yet. Radio init, pin mapping, TX power, timing,
> app compatibility: all unverified on hardware. Don't rely on it for anything, don't trust it for
> emergency comms, and check that your radio settings are legal in your country before you transmit.
> Use at your own risk; no support, no warranty.

**MeshCore for the WiFi Pineapple Pager + Glytch Mesh Mod (proof of concept).**

Glytch's [Mesh Mod](https://github.com/glytchtech/Pager-Mod-Docs/tree/main/Mesh) ships with a
Meshtastic payload. PineCore is its MeshCore twin: same hardware, same payload menu, but the Pager
joins your local **MeshCore** mesh instead, and the **MeshCore app** connects to it over Wi-Fi/USB
the way it connects to a WiFi companion radio.

```
 MeshCore app / meshcore-cli ──TCP 172.16.52.1:5000──▶ Pager ──USB──▶ Mesh Mod (CH341 + SX1262) ))) MeshCore mesh
```

## How it works

| | Glytch Meshtastic payload | PineCore |
|---|---|---|
| Firmware/stack | `meshtasticd` (C++) | [openhop_core](https://github.com/pyMC-dev/pyMC_core) (pyMC, Python MeshCore stack) |
| Radio access | userspace CH341 SPI | userspace CH341 SPI (pyusb + libusb) |
| Pins (CH341 GPIO) | CS0 RST2 BUSY4 IRQ6 RXEN1, DIO2 RF switch, DIO3 TCXO | identical (`lora-usb-meshtoad-e22` wiring) |
| Phone link | Meshtastic app, TCP 4403 | MeshCore companion protocol, TCP **5000** |
| State | `/root/.meshtasticd` | `/root/.pinecore` (eMMC: identity, contacts, channels, offline messages) |
| Boot start | opt-in, default OFF | opt-in, default OFF |

`pinecore.py` runs openhop_core's `CompanionRadio` (contacts, DMs with ACKs, channels, offline
queue, paths, signing, telemetry…) on the Mesh Mod and exposes it with openhop_core's companion
frame server. PineCore adds the Pager-specific parts: JSON persistence on the eMMC, battery and
storage reporting, MeshCore region presets limited to the module's 850–930 MHz range, a contact QR
on the Pager screen, and a small OpenSSL-backed AES shim so the runtime fits the Pager's 30 MB
overlay (no pycryptodome/libgmp needed).

The Python runtime comes from the official OpenWrt 24.10 feed (`python3-light`, `-asyncio`,
`-logging`, `-ctypes`, `-pynacl`, about 17 MB). openhop_core 1.1.3 and pyusb 1.3.1 are downloaded from
PyPI, pinned by SHA-256, and imported directly from their wheels.

## Install

1. Pager charged, on current firmware, and **online** (Wi-Fi client) for the first install.
2. Attach the Mesh Mod **with its antenna**. Never transmit without an antenna, or you can damage the PA.
   The Mod uses the Pager's only USB port, so you can't be connected over USB-C at the same time.
   Use the Pager's **management Wi-Fi AP** for SSH and for the MeshCore app.
3. Copy the payload folder to the Pager:
   ```sh
   scp -O -r MeshCore root@172.16.52.1:/root/payloads/user/general/
   ```
4. On the Pager: **Payloads → General → MeshCore → Install / Update** (about 3–5 min). Pick your
   **radio region** when asked. It must match the MeshCore nodes around you (e.g. *EU/UK (Narrow)*
   869.618 MHz SF8 BW62.5, or *USA/Canada (Recommended)* 910.525 MHz SF7 BW62.5).
5. **Start server**.
6. Join the Pager's management Wi-Fi AP from your phone, then in the MeshCore app connect via
   **TCP/IP** to `172.16.52.1` port `5000`. Set your name in the app, or use **Node name** in the payload. Then send an advert.

`meshcore-cli` works too: `meshcore-cli -t 172.16.52.1 -p 5000 infos`.

## Payload menu

| Item | What it does |
|---|---|
| Start server / Stop server | run or stop the companion (procd-supervised, auto-respawn) |
| Radio region | MeshCore regional presets, or custom freq/BW/SF/CR |
| Node name, TX power | advertised name; 0–22 dBm (default 20) |
| Show contact QR | `meshcore://contact/add…` QR on the Pager screen; scan it from the app to add the Pager |
| Send advert | flood advert right now (without disconnecting the app) |
| Status | running state, region, key, Mesh Mod detection |
| Install / Update | installs or updates runtime and PineCore; settings are kept |
| Enable at Boot | default OFF |
| Uninstall | removes PineCore and, optionally, the Python packages it added; keeps identity/contacts |

Command line: `payload.sh --install | --start | --stop | --region "EU/UK (Narrow)" | --name NAME | --advert | --boot-on | --boot-off | --status | --uninstall`.

Radio changes made from the MeshCore app (frequency, SF, BW, CR, TX power, name, location) are
applied to the radio live and persisted. Frequencies outside 850–930 MHz are rejected.

## Notes

- **TX power:** the default is 20 dBm. If your Mesh Mod uses a high-power E22-…M30S module, the
  external PA adds about 8 dB, so lower the setting to stay within your region's limit
  (EU 869.4–869.65 MHz: 500 mW ERP).
- **Meshtastic and PineCore** can both be installed, but only one can own the Mesh Mod at a time.
  Start refuses while `meshtasticd` runs.
- Logs: `logread | grep python3`. Debug run: stop the server, then
  `PINECORE_HOME=/root/.pinecore python3 /root/.pinecore/pinecore.py run --debug`.
- Runtime check: `python3 /root/.pinecore/pinecore.py selftest`.

## Development

`tests/sim_mesh.py` runs two PineCore nodes on a simulated radio and drives them through TCP with
the official `meshcore` Python client. It covers adverts, DMs + ACKs, channel messages, offline
queue persistence across restart, and the AES shim against pycryptodome.

```sh
python3 -m venv venv && venv/bin/pip install meshcore pynacl
curl -LO <openhop_core-1.1.3 wheel URL> && curl -LO <pyusb-1.3.1 wheel URL>   # see payload.sh
PINECORE_WHEELS=openhop_core-1.1.3-py3-none-any.whl:pyusb-1.3.1-py3-none-any.whl venv/bin/python tests/sim_mesh.py
```

## Credits

- [Glytch](https://github.com/glytchtech/Pager-Mod-Docs) for the Mesh Mod and the Meshtastic payload this mirrors
- [pyMC / openHop](https://github.com/pyMC-dev/pyMC_core) (MIT) for the Python MeshCore stack and CH341 driver
- [MeshCore](https://github.com/meshcore-dev/MeshCore)

MIT licensed.

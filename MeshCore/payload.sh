#!/bin/bash

# Title: MeshCore
# Description: UNTESTED vibe-coded PoC - MeshCore companion for the Glytch Mesh Mod. Start/Stop, radio region, contact QR; Enable at Boot defaults OFF
# Author: Bryss
# Version: 1

# WARNING: vibe-coded proof of concept. Never tested against the real Mesh Mod
# radio. Expect breakage; see README.md before using.

# The MeshCore twin of Glytch's Meshtastic payload. It runs PineCore
# (pinecore.py, next to this file) on top of the openhop_core MeshCore stack,
# talking to the Mesh Mod's SX1262 through its CH341 USB-SPI bridge, and
# serves the MeshCore companion protocol on 172.16.52.1:5000 for the MeshCore
# app or meshcore-cli. Nothing here touches Meshtastic; stop it if it is
# using the Mesh Mod.

set -u
if ! command -v LOG >/dev/null 2>&1; then LOG() { printf '%s\n' "$*"; }; fi

PAYLOAD_DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR='/root/.pinecore'
DAEMON="$HOME_DIR/pinecore.py"
SERVICE='/etc/init.d/pinecore'
UCI_FILE='/etc/config/pinecore'
LOCK_DIR='/tmp/pinecore-payload.lock'
OWNED_PACKAGES="$HOME_DIR/opkg-installed"
PORT=5000
PAGER_IP='172.16.52.1'
PYTHON='/usr/bin/python3'

# Python runtime from the official OpenWrt 24.10 feed (~17 MB on the overlay).
# AES comes from the Pager's own libcrypto, so pycryptodome is not needed.
PACKAGES=(python3-light python3-asyncio python3-logging python3-ctypes python3-pynacl)
# Pure-Python wheels, pinned by SHA-256 and imported straight from the .whl.
WHEELS=(
  'openhop_core-1.1.3-py3-none-any.whl|https://files.pythonhosted.org/packages/a6/dd/ac8167d3addcfe1d669ef1a76ef7f402c9754df7bcd24142f73d1bd1febb/openhop_core-1.1.3-py3-none-any.whl|fb38aab1bf6e6254bdb682ae213f0ba8b306721e5afc701f10070e19676e92da'
  'pyusb-1.3.1-py3-none-any.whl|https://files.pythonhosted.org/packages/28/b8/27e6312e86408a44fe16bd28ee12dd98608b39f7e7e57884a24e8f29b573/pyusb-1.3.1-py3-none-any.whl|bf9b754557af4717fe80c2b07cc2b923a9151f5c08d17bdb5345dac09d6a0430'
)

ACTION='menu'
INTERACTIVE=0
lock_owned=0
WORK=''

cleanup() {
  [ -z "$WORK" ] || rm -rf "$WORK"
  if [ "$lock_owned" -eq 1 ] && [ "$(cat "$LOCK_DIR/pid" 2>/dev/null)" = "$$" ]; then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

fail() {
  LOG "FAIL: $*"
  if [ "$INTERACTIVE" -eq 1 ] && command -v ERROR_DIALOG >/dev/null 2>&1; then ERROR_DIALOG "$*" >/dev/null 2>&1 || true; fi
  exit 1
}

lock_action() {
  (umask 077; mkdir "$LOCK_DIR") 2>/dev/null || fail "Another MeshCore action is running (stale? remove $LOCK_DIR)"
  lock_owned=1
  printf '%s\n' "$$" > "$LOCK_DIR/pid" || fail 'Cannot record the MeshCore action lock'
}

confirmed() {
  # Firmware supplies its positive-response constant; 1 is documented.
  [ -n "$1" ] && [ "$1" != 0 ] && { [ "$1" = 1 ] || [ "$1" = "${DUCKYSCRIPT_USER_CONFIRMED:-CONFIRMED}" ]; }
}

spinner_start() { command -v START_SPINNER >/dev/null 2>&1 && START_SPINNER "$1" 2>/dev/null || true; }
spinner_stop() { [ -z "${1:-}" ] || STOP_SPINNER "$1" >/dev/null 2>&1 || true; }

# ---------------------------------------------------------------------------
# State queries
# ---------------------------------------------------------------------------

is_installed_pkg() { opkg status "$1" 2>/dev/null | grep -q '^Status: .* installed$'; }

core_installed() {
  [ -x "$PYTHON" ] && [ -f "$DAEMON" ] && [ -x "$SERVICE" ] || return 1
  local w
  for w in "${WHEELS[@]}"; do [ -f "$HOME_DIR/lib/${w%%|*}" ] || return 1; done
}

daemon_pid() {
  local pid
  pid="$(sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' "$HOME_DIR/status.json" 2>/dev/null | head -n1)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && grep -q 'pinecore.py' "/proc/$pid/cmdline" 2>/dev/null || return 1
  printf '%s\n' "$pid"
}

service_registered() {
  local def
  def="$(ubus -S call service list '{"name":"pinecore"}' 2>/dev/null)" || return 1
  [ "$(printf '%s' "$def" | tr -d ' \t\r\n')" != '{}' ]
}

port_listening() { netstat -lnt 2>/dev/null | awk -v p=":$PORT" '$1 ~ /^tcp/ && $4 ~ p"$" && $6 == "LISTEN" {ok=1} END {exit !ok}'; }

boot_enabled() {
  [ "$(uci -q get pinecore.settings.boot 2>/dev/null)" = 1 ] && [ -L /etc/rc.d/S99pinecore ]
}

mesh_mod_count() {
  local p n=0
  for p in /sys/bus/usb/devices/*/idVendor; do
    [ -f "$p" ] || continue
    [ "$(cat "$p")" = 1a86 ] && [ "$(cat "${p%/idVendor}/idProduct" 2>/dev/null)" = 5512 ] && n=$((n + 1))
  done
  printf '%s\n' "$n"
}

pc() { PINECORE_HOME="$HOME_DIR" "$PYTHON" "$DAEMON" "$@"; }
# pinecore.py keeps info.json current; reading it with jsonfilter avoids a
# multi-second Python start on the Pager for every menu label.
info_field() {
  local v
  if [ -f "$HOME_DIR/info.json" ] && v="$(jsonfilter -i "$HOME_DIR/info.json" -e "@.$1" 2>/dev/null)"; then
    printf '%s\n' "$v"
    return 0
  fi
  pc info "$1" 2>/dev/null
}
radio_is_set() { case "$(info_field radio_set)" in true|True) return 0 ;; esac; return 1; }

# ---------------------------------------------------------------------------
# Install / Update
# ---------------------------------------------------------------------------

write_init_script() {
  local tmp
  tmp="$(mktemp "$SERVICE.XXXXXX")" || fail 'Cannot stage the PineCore init script'
  cat > "$tmp" <<'PINECORE_INIT'
#!/bin/sh /etc/rc.common
# PineCore MeshCore companion. Installed by the MeshCore payload; boot start
# only happens when "Enable at Boot" is ON (uci pinecore.settings.boot=1).

START=99
STOP=10
USE_PROCD=1

boot() {
	[ "$(uci -q get pinecore.settings.boot)" = 1 ] || return 0
	start "$@"
}

start_service() {
	[ -f /root/.pinecore/pinecore.py ] || return 1
	procd_open_instance
	procd_set_param command /usr/bin/python3 /root/.pinecore/pinecore.py run
	procd_set_param env PINECORE_HOME=/root/.pinecore
	# Respawn after crashes and after a private-key import (exit 75).
	procd_set_param respawn 3600 10 5
	procd_set_param term_timeout 15
	procd_set_param stdout 1
	procd_set_param stderr 1
	procd_close_instance
}
PINECORE_INIT
  chmod 755 "$tmp" && mv "$tmp" "$SERVICE" || { rm -f "$tmp"; fail 'Cannot install the PineCore init script'; }
}

fetch_wheels() {
  local entry name url sha f keep
  mkdir -p "$HOME_DIR/lib" && chmod 700 "$HOME_DIR" "$HOME_DIR/lib" || fail "Cannot create $HOME_DIR/lib"
  for entry in "${WHEELS[@]}"; do
    name="${entry%%|*}"; url="${entry#*|}"; url="${url%%|*}"; sha="${entry##*|}"
    if [ -f "$HOME_DIR/lib/$name" ] && printf '%s  %s\n' "$sha" "$HOME_DIR/lib/$name" | sha256sum -c - >/dev/null 2>&1; then
      continue
    fi
    LOG "Downloading $name"
    curl -q --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 20 --max-time 180 \
      --silent --show-error --output "$WORK/$name" "$url" || fail "Download failed: $name (check internet/TLS)"
    printf '%s  %s\n' "$sha" "$WORK/$name" | sha256sum -c - >/dev/null 2>&1 || fail "Checksum mismatch: $name"
    mv "$WORK/$name" "$HOME_DIR/lib/$name" || fail "Cannot store $name"
  done
  # Drop wheels from older payload versions so only pinned code is importable.
  for f in "$HOME_DIR"/lib/*.whl; do
    [ -e "$f" ] || continue
    keep=0
    for entry in "${WHEELS[@]}"; do [ "${f##*/}" = "${entry%%|*}" ] && keep=1; done
    [ "$keep" -eq 1 ] || rm -f "$f"
  done
  unpack_wheels
}

# Python can't import openhop_core straight from its wheel (hardware/lora is a
# namespace package, which zipimport can't load), so unpack into lib/site.
# The stamp format matches unpack_wheels() in pinecore.py.
unpack_wheels() {
  local site="$HOME_DIR/lib/site" stamp='' f
  for f in "$HOME_DIR"/lib/*.whl; do
    [ -e "$f" ] || continue
    stamp="$stamp${f##*/} $(wc -c < "$f" | tr -d ' ')
"
  done
  [ "$(cat "$site/.wheels" 2>/dev/null; printf x)" = "${stamp}x" ] && return 0
  rm -rf "$site.tmp" && mkdir -p "$site.tmp" || fail 'Cannot stage Python libraries'
  for f in "$HOME_DIR"/lib/*.whl; do
    unzip -q -o "$f" -d "$site.tmp" || fail "Cannot unpack ${f##*/}"
  done
  printf '%s' "$stamp" > "$site.tmp/.wheels" && rm -rf "$site" && mv "$site.tmp" "$site" ||
    fail 'Cannot install Python libraries'
}

do_install() {
  local missing=() p before after avail was_running=0 spin
  opkg print-architecture | grep -q 'mipsel_24kc' || fail 'Unsupported architecture; mipsel_24kc is required'
  case "$(awk -F= '$1=="DISTRIB_RELEASE" {gsub(/["\047]/,"",$2);print $2}' /etc/openwrt_release)" in
    24.10.*) ;;
    *) fail 'Unsupported Pager firmware base; this payload targets OpenWrt 24.10' ;;
  esac
  [ -f "$PAYLOAD_DIR/pinecore.py" ] || fail "pinecore.py must sit next to payload.sh in $PAYLOAD_DIR"
  [ "$(readlink -f /root 2>/dev/null)" = /mmc/root ] && [ -w /mmc/root ] || fail 'Persistent eMMC home (/mmc/root) is unavailable'

  if daemon_pid >/dev/null || service_registered; then was_running=1; stop_server quiet; fi
  WORK="$(umask 077; mktemp -d /tmp/pinecore-install.XXXXXX)" || fail 'Cannot create a staging directory'

  for p in "${PACKAGES[@]}"; do is_installed_pkg "$p" || missing+=("$p"); done
  if [ "${#missing[@]}" -gt 0 ]; then
    avail="$(df -k /overlay 2>/dev/null | awk 'NR==2 {print $4}')"
    [ -n "$avail" ] && [ "$avail" -ge 20000 ] || fail "Need ~20 MB free on /overlay for Python (have ${avail:-?} KB)"
    curl -q --silent --head --fail --connect-timeout 10 --max-time 20 https://downloads.openwrt.org/ >/dev/null 2>&1 ||
      fail 'No internet: connect the Pager to Wi-Fi (Settings > Wi-Fi client) and retry'
    spin="$(spinner_start 'Installing Python (~3 min)')"
    LOG 'Running opkg update'
    opkg update >/tmp/pinecore-opkg.log 2>&1 || { spinner_stop "$spin"; fail 'opkg update failed; see /tmp/pinecore-opkg.log'; }
    before="$(opkg list-installed | awk '{print $1}' | sort)"
    LOG "Installing ${missing[*]}"
    opkg install "${missing[@]}" >>/tmp/pinecore-opkg.log 2>&1 || { spinner_stop "$spin"; fail 'opkg install failed; see /tmp/pinecore-opkg.log'; }
    after="$(opkg list-installed | awk '{print $1}' | sort)"
    spinner_stop "$spin"
    mkdir -p "$HOME_DIR" && chmod 700 "$HOME_DIR"
    # Remember exactly what this payload added so Uninstall removes only that.
    # (BusyBox on the Pager has no comm/tac, so awk does the set difference.)
    { cat "$OWNED_PACKAGES" 2>/dev/null
      awk 'NR == FNR {had[$0] = 1; next} !had[$0]' <(printf '%s\n' "$before") <(printf '%s\n' "$after")
    } | awk 'NF && !seen[$0]++' > "$OWNED_PACKAGES.tmp" && mv "$OWNED_PACKAGES.tmp" "$OWNED_PACKAGES"
  fi
  for p in "${PACKAGES[@]}"; do is_installed_pkg "$p" || fail "Package $p is not fully installed"; done

  fetch_wheels
  cp "$PAYLOAD_DIR/pinecore.py" "$DAEMON.tmp" && chmod 600 "$DAEMON.tmp" && mv "$DAEMON.tmp" "$DAEMON" ||
    fail 'Cannot install pinecore.py'
  write_init_script
  [ -f "$UCI_FILE" ] || printf "config settings 'settings'\n\toption boot '0'\n" > "$UCI_FILE"

  LOG 'Checking the MeshCore runtime'
  pc selftest || fail 'Runtime self-test failed (see above)'
  pc info >/dev/null || fail 'Cannot create the MeshCore identity'
  LOG "PineCore installed. Data: $HOME_DIR"

  if ! radio_is_set; then
    if [ "$INTERACTIVE" -eq 1 ]; then
      PROMPT 'Choose your MeshCore radio region next. It must match the other MeshCore nodes around you.' >/dev/null 2>&1 || true
      pick_region
    else
      LOG 'Next: pick a radio region (menu > Radio region, or payload.sh --region "EU/UK (Narrow)")'
    fi
  fi
  if [ "$was_running" -eq 1 ]; then start_server; fi
}

# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------

start_server() {
  local n pid spin attempt
  core_installed || fail 'PineCore is not installed; choose Install / Update first'
  radio_is_set || fail 'Pick a radio region before starting (menu > Radio region)'
  if pid="$(daemon_pid)"; then LOG "PineCore already running (PID $pid) on $PAGER_IP:$PORT"; return 0; fi
  n="$(mesh_mod_count)"
  [ "$n" -gt 0 ] || fail 'Glytch Mesh Mod (USB 1a86:5512) not detected; attach it (and its antenna!) and retry'
  [ "$n" -eq 1 ] || fail 'More than one CH341 radio is attached; leave only the Mesh Mod connected'
  [ -z "$(pidof meshtasticd 2>/dev/null || true)" ] || fail 'Meshtastic is using the Mesh Mod; stop it in the Meshtastic payload first'

  spin="$(spinner_start 'Starting MeshCore')"
  "$SERVICE" start || { spinner_stop "$spin"; fail 'Service start failed'; }
  # Python on the Pager's MIPS core needs a while to import the stack.
  for ((attempt = 0; attempt < 60; attempt++)); do
    if pid="$(daemon_pid)" && port_listening; then
      spinner_stop "$spin"
      LOG "PineCore running (PID $pid). MeshCore app: connect via TCP to $PAGER_IP port $PORT"
      return 0
    fi
    service_registered || break
    sleep 2
  done
  spinner_stop "$spin"
  logread 2>/dev/null | grep 'python3\[' | tail -n 8 | while IFS= read -r line; do LOG "$line"; done
  "$SERVICE" stop >/dev/null 2>&1 || true
  fail 'PineCore did not come up; see the log lines above (logread | grep python3)'
}

stop_server() {
  local attempt
  if [ -x "$SERVICE" ]; then "$SERVICE" stop >/dev/null 2>&1 || true; fi
  for ((attempt = 0; attempt < 15; attempt++)); do
    if ! daemon_pid >/dev/null && ! service_registered; then
      [ "${1:-}" = quiet ] || LOG 'PineCore stopped'
      return 0
    fi
    sleep 1
  done
  fail 'PineCore did not stop; check: ps | grep pinecore'
}

# Settings changes only reach the radio on (re)start.
restart_if_running() {
  if daemon_pid >/dev/null; then stop_server quiet; start_server; fi
}

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

pick_region() {
  local presets=() choice current freq bw sf cr
  core_installed || fail 'Install / Update first'
  mapfile -t presets < <(pc presets)
  [ "${#presets[@]}" -gt 0 ] || fail 'Cannot list radio presets'
  current="$(info_field preset)"
  choice="$(LIST_PICKER 'Radio region' "${presets[@]}" 'Custom...' 'Back' "${current:-${presets[0]}}")" || return 0
  case "$choice" in
    ''|'Back') return 0 ;;
    'Custom...')
      freq="$(TEXT_PICKER 'Frequency MHz' "$(info_field frequency_mhz)")" || return 0
      bw="$(TEXT_PICKER 'Bandwidth kHz (62.5/125/250)' "$(info_field bandwidth_khz)")" || return 0
      sf="$(NUMBER_PICKER 'Spreading factor (5-12)' "$(info_field spreading_factor)")" || return 0
      cr="$(NUMBER_PICKER 'Coding rate 4/x (5-8)' "$(info_field coding_rate)")" || return 0
      pc set-radio --freq "$freq" --bw "$bw" --sf "$sf" --cr "$cr" || fail 'Invalid radio settings'
      ;;
    *) pc set-radio "$choice" || fail "Cannot apply $choice" ;;
  esac
  LOG "Radio: $(info_field preset) $(info_field frequency_mhz) MHz"
  restart_if_running
}

pick_name() {
  local name
  core_installed || fail 'Install / Update first'
  name="$(TEXT_PICKER 'Node name (max 31 bytes)' "$(info_field name)")" || return 0
  [ -n "$name" ] || return 0
  pc set-name "$name" || fail 'Cannot set node name'
  restart_if_running
}

pick_power() {
  local dbm
  core_installed || fail 'Install / Update first'
  dbm="$(NUMBER_PICKER 'TX power dBm (0-22)' "$(info_field tx_power_dbm)")" || return 0
  pc set-power "$dbm" || fail 'TX power must be 0-22 dBm'
  restart_if_running
}

show_qr() {
  local url name
  core_installed || fail 'Install / Update first'
  url="$(info_field contact_url)" && name="$(info_field name)" || fail 'Cannot read the node identity'
  LOG "$url"
  PROMPT_QR "$url" "MeshCore: scan to add $name" >/dev/null 2>&1 || true
}

send_advert() {
  local pid
  pid="$(daemon_pid)" || fail 'Start the server first'
  kill -USR1 "$pid" && LOG 'Flood advert sent; nearby nodes will list this Pager as a contact'
}

boot_on() {
  core_installed || fail 'Install / Update first'
  [ -f "$UCI_FILE" ] || printf "config settings 'settings'\n" > "$UCI_FILE"
  uci set pinecore.settings=settings && uci set pinecore.settings.boot=1 && uci commit pinecore || fail 'Cannot save the boot choice'
  "$SERVICE" enable || fail 'Boot enable failed'
  LOG 'Enable at Boot: ON. The current server session is unchanged.'
}

boot_off() {
  if [ -f "$UCI_FILE" ]; then uci set pinecore.settings.boot=0 && uci commit pinecore; fi
  [ ! -x "$SERVICE" ] || "$SERVICE" disable || true
  rm -f /etc/rc.d/S99pinecore /etc/rc.d/K10pinecore
  LOG 'Enable at Boot: OFF. Manual Start still works; the current session is unchanged.'
}

show_status() {
  local pid lines
  if pid="$(daemon_pid)"; then lines="Running (PID $pid) on $PAGER_IP:$PORT"; else lines='Stopped'; fi
  if core_installed; then
    lines="$lines
Name: $(info_field name)
Radio: $(info_field preset) $(info_field frequency_mhz) MHz SF$(info_field spreading_factor) @ $(info_field tx_power_dbm) dBm
Key: $(info_field public_key | cut -c1-16)..."
  else
    lines="$lines
Not installed"
  fi
  if boot_enabled; then lines="$lines
Boot: ON"; else lines="$lines
Boot: OFF"; fi
  lines="$lines
Mesh Mod attached: $(mesh_mod_count)"
  printf '%s\n' "$lines" | while IFS= read -r l; do LOG "$l"; done
  [ "$INTERACTIVE" -eq 0 ] || PROMPT "$lines" >/dev/null 2>&1 || true
}

do_uninstall() {
  local p pkgs=() resp
  stop_server quiet
  boot_off >/dev/null
  rm -f "$SERVICE" "$UCI_FILE"
  rm -rf "$HOME_DIR/lib" "$DAEMON" "$HOME_DIR/status.json"
  if [ -s "$OWNED_PACKAGES" ]; then
    resp=1
    if [ "$INTERACTIVE" -eq 1 ]; then
      resp="$(CONFIRMATION_DIALOG 'Also remove the Python packages this payload installed?')" || resp=0
    fi
    if confirmed "$resp"; then
      # Newest first, so dependents go before what they depend on.
      mapfile -t pkgs < <(awk '{l[NR] = $0} END {for (i = NR; i > 0; i--) print l[i]}' "$OWNED_PACKAGES")
      for p in "${pkgs[@]}"; do
        is_installed_pkg "$p" || continue
        opkg remove "$p" >>/tmp/pinecore-opkg.log 2>&1 || LOG "Kept $p (another package needs it)"
      done
      rm -f "$OWNED_PACKAGES"
    fi
  fi
  LOG "PineCore uninstalled. Identity, contacts and messages kept in $HOME_DIR"
}

# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

menu() {
  local choice boot radio resp
  command -v LIST_PICKER >/dev/null 2>&1 || fail 'Pager menu unavailable; use a command-line action (--help)'
  INTERACTIVE=1
  while :; do
    boot='OFF'; boot_enabled && boot='ON'
    radio='not set'
    core_installed && radio_is_set && radio="$(info_field preset)"
    choice="$(LIST_PICKER 'MeshCore' 'Start server' 'Stop server' "Radio region: $radio" 'Node name' 'TX power' \
      'Show contact QR' 'Send advert' 'Status' 'Install / Update' "Enable at Boot: $boot" 'Uninstall' 'Exit' 'Start server')" || exit 0
    case "$choice" in
      'Start server') ACTION=start ;;
      'Stop server') ACTION=stop ;;
      "Radio region: $radio") ACTION=region ;;
      'Node name') ACTION=name ;;
      'TX power') ACTION=power ;;
      'Show contact QR') ACTION=qr ;;
      'Send advert') ACTION=advert ;;
      'Status') ACTION=status ;;
      'Install / Update') ACTION=install ;;
      "Enable at Boot: $boot")
        choice="$(LIST_PICKER "Enable at Boot: $boot" 'ON - start at boot' 'OFF - manual start only' 'Back' 'Back')" || exit 0
        case "$choice" in
          'ON - start at boot') ACTION=boot-on ;;
          'OFF - manual start only') ACTION=boot-off ;;
          *) continue ;;
        esac ;;
      'Uninstall')
        resp="$(CONFIRMATION_DIALOG 'Uninstall MeshCore? Identity, contacts and messages stay.')" || continue
        confirmed "$resp" || continue
        ACTION=uninstall ;;
      'Exit'|'') exit 0 ;;
      *) continue ;;
    esac
    return 0
  done
}

usage() {
  cat <<'USAGE'
Usage: payload.sh [action]
  (no action)        Pager menu
  --install|--update install or update PineCore and its Python runtime
  --start|--stop     start or stop the MeshCore companion (TCP 5000)
  --region NAME      set radio preset, e.g. --region "EU/UK (Narrow)"
  --name NAME        set the advertised node name
  --advert           send a flood advert now
  --boot-on|--boot-off
  --status
  --uninstall        remove PineCore (identity/contacts are kept)
USAGE
}

[ "$(cat /tmp/sysinfo/board_name 2>/dev/null)" = hak5,wifi-pineapple-pager ] || fail 'This payload targets the WiFi Pineapple Pager'

ARG=''
case "${1-}" in
  '') ;;
  --install|--update) ACTION=install ;;
  --start) ACTION=start ;;
  --stop) ACTION=stop ;;
  --region) ACTION=region-cli; ARG="${2-}" ;;
  --name) ACTION=name-cli; ARG="${2-}" ;;
  --advert) ACTION=advert ;;
  --boot-on) ACTION=boot-on ;;
  --boot-off) ACTION=boot-off ;;
  --status) ACTION=status ;;
  --uninstall) ACTION=uninstall ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 1 ;;
esac

[ "$ACTION" != menu ] || menu
[ "$ACTION" = status ] || lock_action
case "$ACTION" in
  install) do_install ;;
  start) start_server ;;
  stop) stop_server ;;
  region) pick_region ;;
  region-cli)
    core_installed || fail 'Install / Update first'
    pc set-radio "$ARG" || fail "Unknown preset; choose from: $(pc presets | tr '\n' ',' )"
    restart_if_running ;;
  name) pick_name ;;
  name-cli)
    core_installed || fail 'Install / Update first'
    pc set-name "$ARG" || fail 'Cannot set node name'
    restart_if_running ;;
  power) pick_power ;;
  qr) show_qr ;;
  advert) send_advert ;;
  status) show_status ;;
  boot-on) boot_on ;;
  boot-off) boot_off ;;
  uninstall) do_uninstall ;;
esac
exit 0

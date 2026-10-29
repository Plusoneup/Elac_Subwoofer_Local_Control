#!/usr/bin/env bash
# Install or update the ELAC SUB3030 web controller as a service on Debian (e.g. a Proxmox VM).
#
#   sudo ./deploy/install_debian.sh            # install, or update after `git pull`
#   sudo ./deploy/install_debian.sh --https    # also create a self-signed certificate (AutoEQ mic on a phone)
#   sudo ./deploy/install_debian.sh --port 8800
#   sudo ./deploy/install_debian.sh --uninstall
#
# Re-running keeps the existing token, settings (config.json) and certificate.
set -euo pipefail

ROOT_PREFIX="${ELAC_INSTALL_ROOT:-}"          # only for testing the script; leave empty
APP_DIR="$ROOT_PREFIX/opt/elac-sub3030"
DATA_DIR="$ROOT_PREFIX/var/lib/elac-sub3030"
ETC_DIR="$ROOT_PREFIX/etc/elac-sub3030"
ENV_FILE="$ETC_DIR/elac.env"
UNIT_FILE="$ROOT_PREFIX/etc/systemd/system/elac-sub3030.service"
DBUS_POLICY="$ROOT_PREFIX/etc/dbus-1/system.d/elac-sub3030.conf"
SERVICE_USER="elac"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PORT=8765
HTTPS=0
UNINSTALL=0

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --https) HTTPS=1 ;;
    --port) [ $# -ge 2 ] || die "--port needs a number"; PORT="$2"; shift ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) usage 0 ;;
    *) warn "unknown option: $1"; usage 1 ;;
  esac
  shift
done
case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
{ [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ]; } || die "--port must be between 1 and 65535"

[ -n "$ROOT_PREFIX" ] || [ "$(id -u)" -eq 0 ] || die "run with sudo"

if [ "$UNINSTALL" -eq 1 ]; then
  say "Stopping and removing the service (settings in $ETC_DIR and $DATA_DIR are kept)"
  systemctl disable --now elac-sub3030.service 2>/dev/null || true
  rm -f "$UNIT_FILE" "$DBUS_POLICY"
  systemctl daemon-reload
  rm -rf "$APP_DIR"
  say "Done. Remove $ETC_DIR and $DATA_DIR yourself if you want the token and settings gone too."
  exit 0
fi

[ -f "$SRC_DIR/run.py" ] && [ -f "$SRC_DIR/app.py" ] || die "run this script from inside the elac-sub3030-control checkout"

# ---------------------------------------------------------------- packages ----
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv bluez avahi-daemon usbutils openssl rsync >/dev/null
if lsusb 2>/dev/null | grep -qi ' 8087:'; then
  # Intel Bluetooth needs its firmware from the non-free-firmware component. The ibt-* files are in
  # firmware-iwlwifi (Debian 12) and may sit in firmware-intel-misc (Debian 13), so try both.
  FW_OK=0
  for pkg in firmware-iwlwifi firmware-intel-misc; do
    apt-get install -y -qq "$pkg" >/dev/null 2>&1 && FW_OK=1
  done
  [ "$FW_OK" -eq 1 ] || warn "could not install Intel firmware; enable 'non-free-firmware' in the apt sources and re-run"
fi
if lsusb 2>/dev/null | grep -qi ' 0bda:'; then
  # Realtek dongles (e.g. RTL8761B, sold as TP-Link UB500 and similar) load firmware too.
  apt-get install -y -qq firmware-realtek >/dev/null 2>&1 \
    || warn "could not install firmware-realtek; enable 'non-free-firmware' in the apt sources and re-run"
fi

# ------------------------------------------------------------------- users ----
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "Creating system user '$SERVICE_USER'"
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
getent group bluetooth >/dev/null && usermod -aG bluetooth "$SERVICE_USER"

# BlueZ only answers D-Bus calls from allowed users; allow the service user explicitly.
mkdir -p "$(dirname "$DBUS_POLICY")"
cat > "$DBUS_POLICY" <<EOF
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="$SERVICE_USER">
    <allow send_destination="org.bluez"/>
  </policy>
</busconfig>
EOF
systemctl reload dbus 2>/dev/null || true

# -------------------------------------------------------------------- code ----
say "Copying the program to $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR" "$ETC_DIR"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude 'config.json' --exclude 'config.tmp' \
  --exclude 'elac_app.log' --exclude 'research' --exclude 'legacy' \
  "$SRC_DIR/" "$APP_DIR/"
chown -R root:root "$APP_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"

say "Installing Python packages into $APP_DIR/.venv"
[ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# -------------------------------------------------------- token + settings ----
new_token() {  # 4 groups of 5 unambiguous characters, ~100 bits
  python3 -c 'import secrets; a="abcdefghjkmnpqrstuvwxyz23456789"; print("-".join("".join(secrets.choice(a) for _ in range(5)) for _ in range(4)))'
}
set_env() {  # set_env KEY VALUE: replace or append a line in the env file
  local key="$1" value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

if [ ! -f "$ENV_FILE" ]; then
  say "Writing $ENV_FILE with a new access token"
  cat > "$ENV_FILE" <<EOF
# ELAC SUB3030 web controller settings. After editing: sudo systemctl restart elac-sub3030
ELAC_HOST=0.0.0.0
ELAC_PORT=$PORT
ELAC_TOKEN=$(new_token)
ELAC_DATA_DIR=$DATA_DIR
# ELAC_ADDRESS=AA:BB:CC:DD:EE:FF
# ELAC_IDLE_DISCONNECT_S=300
EOF
else
  say "Keeping existing settings in $ENV_FILE"
  set_env ELAC_PORT "$PORT"
fi
grep -q '^ELAC_TOKEN=.\{12,\}' "$ENV_FILE" || set_env ELAC_TOKEN "$(new_token)"

if [ "$HTTPS" -eq 1 ]; then
  CERT="$ETC_DIR/tls.crt"; KEY="$ETC_DIR/tls.key"
  if [ ! -f "$CERT" ]; then
    say "Creating a self-signed certificate (valid 10 years)"
    HOST_SHORT="$(hostname -s)"
    SAN="DNS:$HOST_SHORT,DNS:$HOST_SHORT.local,DNS:localhost,IP:127.0.0.1"
    for ip in $(hostname -I 2>/dev/null); do SAN="$SAN,IP:$ip"; done
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$HOST_SHORT" \
      -addext "subjectAltName=$SAN" -keyout "$KEY" -out "$CERT" 2>/dev/null
  fi
  set_env ELAC_TLS_CERT "$CERT"
  set_env ELAC_TLS_KEY "$KEY"
  chown "root:$SERVICE_USER" "$KEY" "$CERT"
  chmod 640 "$KEY"
  chmod 644 "$CERT"
fi
chown "root:$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

# ----------------------------------------------------------------- service ----
say "Installing and starting the service"
mkdir -p "$(dirname "$UNIT_FILE")"
install -m 644 "$SRC_DIR/deploy/elac-sub3030.service" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable --now bluetooth.service >/dev/null 2>&1 || warn "could not start bluetooth.service"
command -v rfkill >/dev/null && rfkill unblock bluetooth 2>/dev/null || true
systemctl enable elac-sub3030.service >/dev/null
systemctl restart elac-sub3030.service

# ------------------------------------------------------------------ checks ----
if ! bluetoothctl list 2>/dev/null | grep -q 'Controller'; then
  warn "no Bluetooth controller visible in this machine. In Proxmox pass the Bluetooth USB device to this VM (Hardware → Add → USB Device), then: sudo systemctl restart bluetooth elac-sub3030"
fi
if dmesg 2>/dev/null | grep -qi 'CSR clone'; then
  warn "the kernel reports an unbranded CSR clone dongle (0a12:0001). Many work with workarounds; if the subwoofer is not found or the link drops, use a Realtek RTL8761B or Intel based adapter instead."
fi
sleep 2
systemctl is-active --quiet elac-sub3030.service \
  || warn "the service is not running; see: journalctl -u elac-sub3030 -n 50"

SCHEME=http; grep -q '^ELAC_TLS_CERT=.' "$ENV_FILE" && SCHEME=https
TOKEN="$(sed -n 's/^ELAC_TOKEN=//p' "$ENV_FILE")"
echo
say "Ready. Open on your phone (same Wi-Fi):"
for ip in $(hostname -I 2>/dev/null); do
  case "$ip" in *:*) ;; *) echo "      $SCHEME://$ip:$PORT" ;; esac
done
echo "      $SCHEME://$(hostname -s).local:$PORT   (if your phone resolves .local names)"
echo
say "Access token: $TOKEN"
echo "      (stored in $ENV_FILE; show it again with: sudo grep TOKEN $ENV_FILE)"
if [ "$SCHEME" = https ]; then echo "      The certificate is self-signed: accept the browser warning once on each device."; fi

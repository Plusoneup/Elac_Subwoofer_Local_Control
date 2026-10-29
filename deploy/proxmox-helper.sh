#!/usr/bin/env bash
# ELAC SUB3030 web controller: one-step setup on a Proxmox VE host (in the spirit of community-scripts.org).
#
# Run in the Proxmox node shell (as root):
#   bash -c "$(curl -fsSL '<raw URL of this file>')"
#
# First run: finds the Bluetooth adapter, creates a small Debian 13 VM with it passed through, installs the
# controller inside via cloud-init, waits until it is ready and prints the address and access token.
# Later runs: update the existing VM to the latest version from GitHub.
#
# Every question has a default; set these to answer them in advance:
#   ELAC_VMID, ELAC_STORAGE, ELAC_BRIDGE, ELAC_BT (vendor:product), ELAC_HTTPS (1/0),
#   ELAC_PASSWORD (console password for user "debian"), ELAC_ASSUME_YES=1
set -euo pipefail

REPO="${ELAC_REPO:-Plusoneup/Elac_Subwoofer_Local_Control}"   # owner/name of the GitHub repository to install
VM_NAME="elac-sub3030"
IMAGE_URL="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2"
IMAGE_CACHE="${ELAC_IMAGE_CACHE:-/var/lib/vz/template/cache}/debian-13-generic-amd64.qcow2"
KEY_DIR="${ELAC_KEY_DIR:-/root/.elac-sub3030}"
SYSFS_USB="${ELAC_SYSFS_USB:-/sys/bus/usb/devices}"
INSTALL_TIMEOUT_S="${ELAC_INSTALL_TIMEOUT_S:-1500}"

c_info=$'\033[1;36m'; c_ok=$'\033[1;32m'; c_warn=$'\033[1;33m'; c_err=$'\033[1;31m'; c_off=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$c_info" "$c_off" "$*"; }
ok()   { printf '%s ✓ %s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '%sWARNING:%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
die()  { printf '%sERROR:%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }

ask() {  # ask VAR "question" "default": uses an existing value of VAR, else prompts (Enter = default)
  local var="$1" question="$2" default="$3" answer
  if [ -n "${!var:-}" ]; then return; fi
  if [ "${ELAC_ASSUME_YES:-0}" = 1 ] || [ ! -t 0 ]; then printf -v "$var" '%s' "$default"; return; fi
  read -r -p "$question [$default]: " answer
  printf -v "$var" '%s' "${answer:-$default}"
}
confirm() {  # confirm "question" -> 0 for yes (default yes)
  [ "${ELAC_ASSUME_YES:-0}" = 1 ] || [ ! -t 0 ] && return 0
  local a; read -r -p "$1 [T/n]: " a
  case "${a,,}" in n|nie|no) return 1 ;; *) return 0 ;; esac
}
json() {  # json '<python expression on d>' <<< JSON
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"
}
guest_exec() {  # guest_exec VMID cmd... -> prints stdout; returns the command's exit code
  local vmid="$1"; shift
  local out code
  out="$(qm guest exec "$vmid" --timeout 120 -- "$@" 2>/dev/null)" || return 125
  code="$(json 'd.get("exitcode", 1)' <<<"$out")"
  json 'd.get("out-data", "")' <<<"$out"
  return "$code"
}

cat <<'BANNER'

   ███████╗██╗      █████╗  ██████╗    ███████╗██╗   ██╗██████╗ ██████╗  ██████╗ ██████╗  ██████╗
   ██╔════╝██║     ██╔══██╗██╔════╝    ██╔════╝██║   ██║██╔══██╗╚════██╗██╔═████╗╚════██╗██╔═████╗
   █████╗  ██║     ███████║██║         ███████╗██║   ██║██████╔╝ █████╔╝██║██╔██║ █████╔╝██║██╔██║
   ██╔══╝  ██║     ██╔══██║██║         ╚════██║██║   ██║██╔══██╗ ╚═══██╗████╔╝██║ ╚═══██╗████╔╝██║
   ███████╗███████╗██║  ██║╚██████╗    ███████║╚██████╔╝██████╔╝██████╔╝╚██████╔╝██████╔╝╚██████╔╝
   ╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝    ╚══════╝ ╚═════╝ ╚═════╝ ╚═════╝  ╚═════╝ ╚═════╝  ╚═════╝
                        web controller · Proxmox helper

BANNER

# ------------------------------------------------------------------ checks ----
command -v qm >/dev/null && command -v pvesm >/dev/null || die "run this in the Proxmox VE node shell"
[ "$(id -u)" -eq 0 ] || die "run as root"
for tool in curl python3 openssl ssh-keygen; do command -v "$tool" >/dev/null || die "missing tool: $tool"; done

# ---------------------------------------------------------- existing VM? ----
EXISTING="$(qm list 2>/dev/null | awk -v n="$VM_NAME" '$2 == n {print $1; exit}' || true)"
if [ -n "$EXISTING" ]; then
  say "Found VM $EXISTING ($VM_NAME)."
  confirm "Update it to the latest version from GitHub?" || exit 0
  [ "$(qm status "$EXISTING" | awk '{print $2}')" = running ] || { say "Starting VM $EXISTING"; qm start "$EXISTING"; sleep 20; }
  say "Updating (git pull + installer)…"
  if OUT="$(guest_exec "$EXISTING" /usr/local/sbin/elac-sub3030-update)"; then
    printf '%s\n' "$OUT" | sed 's/\x1b\[[0-9;]*m//g' | tail -n 12
    ok "Updated."
  else
    printf '%s\n' "$OUT" | tail -n 20
    die "update failed (is the QEMU guest agent running in VM $EXISTING?)"
  fi
  exit 0
fi

# ------------------------------------------------------- Bluetooth adapter ----
say "Looking for Bluetooth adapters (USB interface class e0/01/01)"
BT_IDS=(); BT_NAMES=()
for dev in "$SYSFS_USB"/*; do
  [ -f "$dev/idVendor" ] || continue
  is_bt=0
  for itf in "$dev"/*:*; do
    [ -f "$itf/bInterfaceClass" ] || continue
    if [ "$(cat "$itf/bInterfaceClass")" = e0 ] && [ "$(cat "$itf/bInterfaceSubClass")" = 01 ] \
       && [ "$(cat "$itf/bInterfaceProtocol")" = 01 ]; then is_bt=1; fi
  done
  [ "$is_bt" = 1 ] || continue
  dev_id="$(cat "$dev/idVendor"):$(cat "$dev/idProduct")"
  name="$(cat "$dev/product" 2>/dev/null || true)"; [ -n "$name" ] || name="Bluetooth device"
  BT_IDS+=("$dev_id"); BT_NAMES+=("$name")
done
if [ -z "${ELAC_BT:-}" ]; then
  case "${#BT_IDS[@]}" in
    0) die "no Bluetooth adapter found on this host (lsusb shows none with a Bluetooth interface)" ;;
    1) ELAC_BT="${BT_IDS[0]}"; ok "Bluetooth: ${BT_NAMES[0]} (${BT_IDS[0]})" ;;
    *) for i in "${!BT_IDS[@]}"; do echo "   $((i + 1))) ${BT_NAMES[$i]} (${BT_IDS[$i]})"; done
       ask CHOICE "Which adapter should the VM get?" 1
       case "$CHOICE" in ''|*[!0-9]*) die "not a number: $CHOICE" ;; esac
       [ "$CHOICE" -ge 1 ] && [ "$CHOICE" -le "${#BT_IDS[@]}" ] || die "no adapter $CHOICE"
       ELAC_BT="${BT_IDS[$((CHOICE - 1))]}" ;;
  esac
fi
[[ "$ELAC_BT" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$ ]] || die "ELAC_BT must look like 8087:0029"
echo "   The host gives this adapter to the VM; the host itself will not use Bluetooth."

# ----------------------------------------------------------- repo access ----
CLONE_URL="https://github.com/$REPO.git"
DEPLOY_KEY=""
if [ "$(curl -s -o /dev/null -w '%{http_code}' "https://github.com/$REPO")" != 200 ]; then
  say "The repository is private: the VM needs a read-only deploy key"
  mkdir -p "$KEY_DIR"; chmod 700 "$KEY_DIR"
  [ -f "$KEY_DIR/deploy_key" ] || ssh-keygen -q -t ed25519 -N "" -C "elac-sub3030-vm" -f "$KEY_DIR/deploy_key"
  deploy_key_ok() {  # GitHub answers "successfully authenticated" and exits 1 (no shell): check the text
    local out
    out="$(ssh -i "$KEY_DIR/deploy_key" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
             -o UserKnownHostsFile="$KEY_DIR/known_hosts" -T git@github.com 2>&1 || true)"
    [[ "$out" == *"successfully authenticated"* ]]
  }
  until deploy_key_ok; do
    echo
    echo "   Add this key on GitHub: $REPO → Settings → Deploy keys → Add deploy key"
    echo "   (title: elac-sub3030-vm, leave \"Allow write access\" unticked):"
    echo
    echo "   $(cat "$KEY_DIR/deploy_key.pub")"
    echo
    [ -t 0 ] || die "deploy key not accepted by GitHub yet"
    read -r -p "Press Enter once the key is added (Ctrl+C to stop)… " _
  done
  ok "GitHub accepts the deploy key."
  CLONE_URL="git@github.com:$REPO.git"
  DEPLOY_KEY="$(cat "$KEY_DIR/deploy_key")"
else
  ok "The repository is public."
fi

# ---------------------------------------------------------------- settings ----
DEFAULT_STORAGE="$(pvesm status -content images 2>/dev/null | awk 'NR > 1 && $3 == "active" {print $1; exit}' || true)"
NEXT_ID="$(pvesh get /cluster/nextid)"
ask ELAC_VMID "VM ID (a number; the VM name is always $VM_NAME)" "$NEXT_ID"
while ! [[ "$ELAC_VMID" =~ ^[0-9]+$ ]] || qm status "$ELAC_VMID" >/dev/null 2>&1; do
  [ -t 0 ] && [ "${ELAC_ASSUME_YES:-0}" != 1 ] || die "VM ID must be a free number"
  echo "   '$ELAC_VMID' is not a free VM ID; type a number or press Enter for $NEXT_ID."
  ELAC_VMID=""; ask ELAC_VMID "VM ID" "$NEXT_ID"
done
ask ELAC_STORAGE "Disk storage for the VM (Enter = default)" "${DEFAULT_STORAGE:-local-lvm}"
ask ELAC_BRIDGE "Network bridge" "vmbr0"
ask ELAC_HTTPS "HTTPS for the AutoEQ microphone on a phone (1 = yes, 0 = no)" "1"
while ! [[ "$ELAC_HTTPS" =~ ^[01]$ ]]; do
  [ -t 0 ] && [ "${ELAC_ASSUME_YES:-0}" != 1 ] || die "HTTPS must be 1 or 0"
  ELAC_HTTPS=""; ask ELAC_HTTPS "Please type 1 (yes) or 0 (no)" "1"
done
case "$ELAC_VMID" in ''|*[!0-9]*) die "VM ID must be a number" ;; esac
case "$ELAC_HTTPS" in 0|1) ;; *) die "HTTPS must be 1 or 0" ;; esac
qm status "$ELAC_VMID" >/dev/null 2>&1 && die "VM ID $ELAC_VMID is already in use"
GENERATED_PASSWORD=0
if [ -z "${ELAC_PASSWORD:-}" ]; then
  if [ -t 0 ] && [ "${ELAC_ASSUME_YES:-0}" != 1 ]; then
    read -r -s -p "Console password for user 'debian' (Enter = generate one): " ELAC_PASSWORD; echo
  fi
  if [ -z "${ELAC_PASSWORD:-}" ]; then
    ELAC_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"; GENERATED_PASSWORD=1
  fi
fi
PASS_HASH="$(openssl passwd -6 "$ELAC_PASSWORD")"
HTTPS_FLAG=""; [ "$ELAC_HTTPS" = 1 ] && HTTPS_FLAG="--https"

# A storage that can hold cloud-init snippets ("local" gets the content type added if none has it).
SNIP_STORE="$(pvesm status -content snippets 2>/dev/null | awk 'NR > 1 && $3 == "active" {print $1; exit}' || true)"
if [ -z "$SNIP_STORE" ]; then
  CURRENT="$(pvesh get /storage/local --output-format json | json 'd.get("content", "")')"
  say "Allowing cloud-init snippets on storage 'local' (content: $CURRENT,snippets)"
  pvesm set local --content "$CURRENT,snippets"
  SNIP_STORE=local
fi
SNIPPET_VOL="$SNIP_STORE:snippets/$VM_NAME-$ELAC_VMID.yaml"
SNIPPET_PATH="$(pvesm path "$SNIPPET_VOL")"
mkdir -p "$(dirname "$SNIPPET_PATH")"

echo
say "Summary"
echo "   VM $ELAC_VMID '$VM_NAME' · 1 vCPU · 1 GB RAM · 8 GB on $ELAC_STORAGE · $ELAC_BRIDGE (DHCP)"
echo "   Bluetooth $ELAC_BT · HTTPS $([ "$ELAC_HTTPS" = 1 ] && echo on || echo off) · Debian 13"
confirm "Create the VM?" || exit 0

# -------------------------------------------------------------- cloud-init ----
indent() { sed 's/^/      /'; }
{
  cat <<EOF
#cloud-config
hostname: $VM_NAME
manage_etc_hosts: true
users:
  - name: debian
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: false
    passwd: "$PASS_HASH"
ssh_pwauth: true
package_update: true
packages: [qemu-guest-agent, git]
write_files:
  - path: /usr/local/sbin/elac-sub3030-update
    permissions: "0755"
    content: |
      #!/bin/sh
      set -e
      cd /root/elac-sub3030-control
      git pull --ff-only
      ./deploy/install_debian.sh $HTTPS_FLAG
  - path: /usr/local/sbin/elac-sub3030-firstboot
    permissions: "0755"
    content: |
      #!/bin/sh
      # Runs once from cloud-init: enable non-free firmware, fetch the controller, install it.
      sed -i 's/^Components: main\$/Components: main non-free-firmware/' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
      {
        git clone $CLONE_URL /root/elac-sub3030-control &&
        cd /root/elac-sub3030-control &&
        ./deploy/install_debian.sh $HTTPS_FLAG
      } > /var/log/elac-sub3030-install.log 2>&1
      echo \$? > /var/log/elac-sub3030-install.status
EOF
  if [ -n "$DEPLOY_KEY" ]; then
    cat <<EOF
  - path: /root/.ssh/elac_deploy
    permissions: "0600"
    content: |
$(printf '%s\n' "$DEPLOY_KEY" | indent)
  - path: /root/.ssh/config
    permissions: "0600"
    content: |
      Host github.com
        IdentityFile /root/.ssh/elac_deploy
        IdentitiesOnly yes
        StrictHostKeyChecking accept-new
EOF
  fi
  cat <<EOF
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]
  - [/usr/local/sbin/elac-sub3030-firstboot]
EOF
} > "$SNIPPET_PATH"
chmod 600 "$SNIPPET_PATH"

# --------------------------------------------------------------------- VM ----
if [ ! -s "$IMAGE_CACHE" ] || [ -n "$(find "$IMAGE_CACHE" -mtime +14 2>/dev/null)" ]; then
  say "Downloading Debian 13 cloud image"
  mkdir -p "$(dirname "$IMAGE_CACHE")"
  curl -fL --progress-bar -o "$IMAGE_CACHE.part" "$IMAGE_URL"
  mv "$IMAGE_CACHE.part" "$IMAGE_CACHE"
fi

say "Creating VM $ELAC_VMID"
qm create "$ELAC_VMID" --name "$VM_NAME" --ostype l26 --memory 1024 --cores 1 --cpu host \
  --net0 "virtio,bridge=$ELAC_BRIDGE" --scsihw virtio-scsi-single --agent enabled=1 --onboot 1 \
  --serial0 socket --vga serial0 --tags elac --description "ELAC SUB3030 web controller (proxmox-helper.sh)"
qm set "$ELAC_VMID" --scsi0 "$ELAC_STORAGE:0,import-from=$IMAGE_CACHE,discard=on,ssd=1" >/dev/null
qm resize "$ELAC_VMID" scsi0 8G >/dev/null
qm set "$ELAC_VMID" --ide2 "$ELAC_STORAGE:cloudinit" --boot order=scsi0 --ipconfig0 ip=dhcp \
  --cicustom "user=$SNIPPET_VOL" --usb0 "host=$ELAC_BT" >/dev/null
qm start "$ELAC_VMID"
ok "VM $ELAC_VMID started."

# ------------------------------------------------------------ wait + report ----
say "Installing inside the VM (usually 3–6 minutes)…"
START=$(date +%s)
until qm agent "$ELAC_VMID" ping >/dev/null 2>&1; do
  [ $(( $(date +%s) - START )) -lt "$INSTALL_TIMEOUT_S" ] || die "the VM did not come up; check its console in Proxmox"
  sleep 10
done
ok "VM is up."
STATUS=""
until STATUS="$(guest_exec "$ELAC_VMID" cat /var/log/elac-sub3030-install.status 2>/dev/null)"; do
  [ $(( $(date +%s) - START )) -lt "$INSTALL_TIMEOUT_S" ] || die "installation did not finish; see /var/log/elac-sub3030-install.log in the VM"
  sleep 10
done
if [ "$(printf '%s' "$STATUS" | tr -d '[:space:]')" != 0 ]; then
  guest_exec "$ELAC_VMID" tail -n 25 /var/log/elac-sub3030-install.log | sed 's/\x1b\[[0-9;]*m//g' || true
  die "the installer reported an error (log above)"
fi
ok "Controller installed."

TOKEN="$(guest_exec "$ELAC_VMID" sed -n 's/^ELAC_TOKEN=//p' /etc/elac-sub3030/elac.env | tr -d '[:space:]' || true)"
IP="$(qm guest cmd "$ELAC_VMID" network-get-interfaces 2>/dev/null | json '" ".join(a["ip-address"] for i in d if i.get("name") != "lo" for a in i.get("ip-addresses", []) if a.get("ip-address-type") == "ipv4")' | awk '{print $1}' || true)"
SCHEME=http; [ "$ELAC_HTTPS" = 1 ] && SCHEME=https
if guest_exec "$ELAC_VMID" bluetoothctl list 2>/dev/null | grep -q Controller; then
  ok "Bluetooth adapter works inside the VM."
else
  warn "no Bluetooth controller visible in the VM yet; try rebooting it once (qm reboot $ELAC_VMID)"
fi

cat <<EOF

${c_ok}Ready.${c_off} On your phone (same Wi-Fi) open:

      $SCHEME://${IP:-<VM address>}:8765

   Access token:      ${TOKEN:-(see /etc/elac-sub3030/elac.env in the VM)}
   VM console login:  debian / $([ "$GENERATED_PASSWORD" = 1 ] && echo "$ELAC_PASSWORD" || echo "(the password you chose)")
EOF
[ "$ELAC_HTTPS" = 1 ] && echo "   The certificate is self-signed: accept the browser warning once on each device."
cat <<EOF
   Give the VM a fixed address in your router (DHCP reservation) so the link keeps working.
   To update later, run this helper again on the Proxmox host.

EOF

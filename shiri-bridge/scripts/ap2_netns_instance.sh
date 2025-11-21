#!/usr/bin/env bash
# ap2_netns_instance.sh
# Spin up a single AirPlay 2-capable shairport-sync instance in its own
# network namespace with its own IP, private /run, dbus, avahi and nqptp.
# This is a scripted version of the manual steps you just verified.

set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
  echo "Run this script as root (sudo)." >&2
  exit 1
fi

REQUIRED_CMDS=(ip dhclient dbus-daemon avahi-daemon shairport-sync nqptp unshare)
for cmd in "${REQUIRED_CMDS[@]}"; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: missing required command '$cmd'. Install it first." >&2
    exit 1
  fi
done

PARENT_IF="${1:-}"
SPEAKER_NAME="${2:-}"

if [[ -z "$PARENT_IF" ]]; then
  # Interactive selection if not provided
  mapfile -t INTERFACES < <(ip -o link show | awk -F': ' '($2!="lo") {print $2"|"$3}')
  if [[ ${#INTERFACES[@]} -eq 0 ]]; then
    echo "No candidate interfaces found." >&2
    exit 1
  fi
  echo "Available network interfaces:" >&2
  for i in "${!INTERFACES[@]}"; do
    name=${INTERFACES[$i]%|*}
    info=${INTERFACES[$i]#*|}
    printf "  [%d] %s  (%s)\n" "$i" "$name" "$info"
  done
  read -rp "Select parent interface index: " choice
  if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 0 || choice >= ${#INTERFACES[@]} )); then
    echo "Invalid selection" >&2
    exit 1
  fi
  PARENT_IF=${INTERFACES[$choice]%|*}
fi

if [[ -z "$SPEAKER_NAME" ]]; then
  read -rp "Speaker display name [ap2-instance]: " SPEAKER_NAME
  SPEAKER_NAME=${SPEAKER_NAME:-ap2-instance}
fi

suffix=$(date +%s)
id_token=$(printf "%08x" "$suffix")
NS_NAME="ap2n_${id_token}"
MV_IF="ap2m_${id_token}"

echo "[info] Parent interface : $PARENT_IF"
echo "[info] Namespace        : $NS_NAME"
echo "[info] Macvlan IF       : $MV_IF"
echo "[info] Speaker name     : $SPEAKER_NAME"

echo "[info] Creating network namespace..."
ip netns add "$NS_NAME"
cleanup() {
  set +e
  echo "\n[cleanup] tearing down namespace $NS_NAME and interface $MV_IF"
  ip netns delete "$NS_NAME" 2>/dev/null || true
  ip link delete "$MV_IF" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Create macvlan on parent and move into namespace
ip link add "$MV_IF" link "$PARENT_IF" type macvlan
ip link set "$MV_IF" netns "$NS_NAME"

echo "[info] Bringing interface up and requesting DHCP inside namespace..."

# Run the full sequence inside the netns using a single unshare -m bash -c
# so that we get a private /run plus dbus+avahi+nqptp+shairport-sync.
ip netns exec "$NS_NAME" env MV_IF="$MV_IF" SPEAKER_NAME="$SPEAKER_NAME" unshare -m bash -c "
  set -e
  echo '[ns] Using interface:' "\"\$MV_IF\""

  # 1. Bring up interfaces and get IP from router
  ip link set lo up
  ip link set \"\$MV_IF\" up
  ip addr show dev \"\$MV_IF\"

  echo '[ns] Running DHCP on' \"\$MV_IF\" '...'
  dhclient -v \"\$MV_IF\"
  ip addr show dev \"\$MV_IF\"

  # 2. Private /run + dbus + avahi
  echo '[ns-mnt] Mounting private /run and starting dbus+avahi+nqptp+shairport-sync...'
  mount -t tmpfs tmpfs /run
  mkdir -p /run/dbus /run/avahi-daemon

  echo '[ns-mnt] Starting system dbus-daemon...'
  dbus-daemon --system --fork --nopidfile
  sleep 1

  echo '[ns-mnt] Starting avahi-daemon...'
  avahi-daemon --daemonize --no-chroot --no-drop-root --file /etc/avahi/avahi-daemon.conf --no-rlimits
  sleep 1

  echo '[ns-mnt] Starting nqptp...'
  nqptp &
  sleep 1

  echo '[ns-mnt] Starting shairport-sync as' "\"\$SPEAKER_NAME\"" '...'
  exec shairport-sync -a \"\$SPEAKER_NAME\" --statistics
"

# When shairport-sync exits, control returns here, and the trap will clean up the ns+interface.
echo "[info] shairport-sync exited; namespace will be cleaned up."

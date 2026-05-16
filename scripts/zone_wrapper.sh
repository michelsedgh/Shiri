#!/usr/bin/env bash
set -Eeuo pipefail

MV_IF="$1"
GRP_DIR="$2"
GRP="$3"

STATE_DIR="$GRP_DIR/state"
DHCLIENT_LEASE="/run/dhclient-$GRP.leases"
DHCLIENT_PID="/run/dhclient-$GRP.pid"
NQPTP_PID=""
SHAIRPORT_PID=""
OWNTONE_PID=""

cleanup() {
  trap - EXIT INT TERM HUP
  echo "[zone:$GRP] Supervisor cleaning up all processes..."

  if [[ -n "${DHCLIENT_LEASE:-}" && -n "${DHCLIENT_PID:-}" ]]; then
    dhclient -r -lf "$DHCLIENT_LEASE" -pf "$DHCLIENT_PID" "$MV_IF" >/dev/null 2>&1 || true
  fi

  for pid in "${OWNTONE_PID:-}" "${SHAIRPORT_PID:-}" "${NQPTP_PID:-}"; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done

  sleep 1

  for pid in "${OWNTONE_PID:-}" "${SHAIRPORT_PID:-}" "${NQPTP_PID:-}"; do
    if [[ -n "$pid" ]]; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM HUP

ip link set lo up
ip link set "$MV_IF" up

# Private /run, /tmp, AND /dev/shm for COMPLETE PTP ISOLATION
# CRITICAL: Private /dev/shm gives each zone its own /dev/shm/nqptp
# This prevents one zone's teardown from poisoning another zone's PTP timing.
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp
mount -t tmpfs tmpfs /dev/shm
mkdir -p /run/dbus /run/avahi-daemon
echo "$DHCLIENT_LEASE" > "$STATE_DIR/dhclient_lease_path.txt"
echo "$DHCLIENT_PID" > "$STATE_DIR/dhclient_pid_path.txt"

echo "[zone:$GRP] Running DHCP on $MV_IF ..."
rm -f "$DHCLIENT_LEASE" "$DHCLIENT_PID"
if ! timeout 45 dhclient -1 -v \
  -lf "$DHCLIENT_LEASE" \
  -pf "$DHCLIENT_PID" \
  "$MV_IF"; then
  echo "[zone:$GRP] FATAL: DHCP failed on $MV_IF" >&2
  exit 1
fi
sleep 2

# Get and save the IP address
MY_IP=$(ip -4 addr show "$MV_IF" | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
if [[ -z "$MY_IP" ]]; then
  echo "[zone:$GRP] FATAL: $MV_IF has no IPv4 address after DHCP" >&2
  exit 1
fi
echo "$MY_IP" > "$GRP_DIR/state/owntone_ip.txt"
echo "$MY_IP" > "$GRP_DIR/state/shairport_ip.txt"
cat "/sys/class/net/$MV_IF/address" > "$GRP_DIR/state/macvlan_mac.txt"
echo "[zone:$GRP] Got IP: $MY_IP"

echo "[zone:$GRP] Starting dbus..."
dbus-daemon --system --fork --nopidfile
sleep 1

# Create per-instance avahi config to avoid mDNS conflicts
cat > /tmp/avahi-daemon.conf <<AVAHI_EOF
[server]
host-name=$(hostname)-shiri-$GRP
use-ipv4=yes
use-ipv6=yes
allow-interfaces=$MV_IF
deny-interfaces=lo
ratelimit-interval-usec=1000000
ratelimit-burst=1000

[wide-area]
enable-wide-area=no

[publish]
publish-hinfo=no
publish-workstation=no

[reflector]
enable-reflector=no

[rlimits]
AVAHI_EOF

echo "[zone:$GRP] Starting avahi..."
avahi-daemon --daemonize --no-chroot --no-drop-root --file /tmp/avahi-daemon.conf --no-rlimits 2>/dev/null || true
sleep 1

# Start per-zone nqptp with ISOLATED /dev/shm/nqptp
# Each zone gets its own PTP timing — one zone's disconnect
# cannot corrupt another zone's clock
echo "[zone:$GRP] Starting per-zone nqptp (isolated PTP timing)..."
setsid nqptp &
NQPTP_PID=$!
echo "$NQPTP_PID" > "$GRP_DIR/state/nqptp.pid"
sleep 1

if ! kill -0 "$NQPTP_PID" 2>/dev/null; then
  echo "[zone:$GRP] FATAL: nqptp failed to start" >&2
  exit 1
fi
if [[ ! -e /dev/shm/nqptp ]]; then
  echo "[zone:$GRP] FATAL: /dev/shm/nqptp not created" >&2
  exit 1
fi
echo "[zone:$GRP] nqptp ready (pid $NQPTP_PID, private /dev/shm/nqptp)"

# Start shairport-sync (uses this zone's private /dev/shm/nqptp for PTP)
# Writes to ALSA loopback on host (ALSA is kernel-level, works across namespaces)
echo "[zone:$GRP] Starting shairport-sync..."
setsid chrt -f 50 shairport-sync -c "$GRP_DIR/config/shairport-sync.conf" --statistics &>"$GRP_DIR/logs/shairport.log" &
SHAIRPORT_PID=$!
echo "$SHAIRPORT_PID" > "$GRP_DIR/state/shairport.pid"
sleep 1
echo "[zone:$GRP] shairport-sync started (pid $SHAIRPORT_PID)"

# Start OwnTone in background (NOT exec — keeps wrapper as supervisor)
echo "[zone:$GRP] Starting OwnTone with Real-Time priority..."
chrt -f 50 owntone -f -c "$GRP_DIR/config/owntone.conf" &
OWNTONE_PID=$!
echo "$OWNTONE_PID" > "$GRP_DIR/state/owntone.pid"
echo "[zone:$GRP] OwnTone started (pid $OWNTONE_PID)"

# ---- Process supervisor loop ----
# Monitor all three critical processes. If any dies, kill the rest and exit.
# The Shiri daemon will detect our exit via the wrapper PID.

while true; do
  sleep 5
  for label_pid in "nqptp:$NQPTP_PID" "shairport-sync:$SHAIRPORT_PID" "owntone:$OWNTONE_PID"; do
    label="${label_pid%%:*}"
    pid="${label_pid##*:}"
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[zone:$GRP] FATAL: $label (pid $pid) died! Shutting down zone." >&2
      exit 1
    fi
  done
done

#!/usr/bin/env bash
set -e

MV_IF="$1"
GRP_DIR="$2"
GRP="$3"

ip link set lo up
ip link set "$MV_IF" up

echo "[zone:$GRP] Running DHCP on $MV_IF ..."
dhclient -v "$MV_IF" \
  -lf "/run/dhclient.leases" \
  -pf "/run/dhclient.pid" \
  2>&1 | head -20 || true
sleep 2

# Get and save the IP address
MY_IP=$(ip -4 addr show "$MV_IF" | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
echo "$MY_IP" > "$GRP_DIR/state/owntone_ip.txt"
echo "$MY_IP" > "$GRP_DIR/state/shairport_ip.txt"
echo "[zone:$GRP] Got IP: $MY_IP"

# Private /run, /tmp, AND /dev/shm for COMPLETE PTP ISOLATION
# CRITICAL: Private /dev/shm gives each zone its own /dev/shm/nqptp
# This prevents one zone's teardown from poisoning another zone's PTP timing
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp
mount -t tmpfs tmpfs /dev/shm
mkdir -p /run/dbus /run/avahi-daemon

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
cleanup() {
  echo "[zone:$GRP] Supervisor cleaning up all processes..."
  kill "$NQPTP_PID" "$SHAIRPORT_PID" "$OWNTONE_PID" 2>/dev/null
  sleep 1
  kill -9 "$NQPTP_PID" "$SHAIRPORT_PID" "$OWNTONE_PID" 2>/dev/null
}
trap cleanup EXIT

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

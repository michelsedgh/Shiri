#!/usr/bin/env bash
###############################################################################
# dual_zone_demo.sh
#
# Proof-of-concept script that spins up independent AirPlay 2 endpoints.
# Each zone runs in its own network namespace with macvlan, containing:
#   - shairport-sync (AirPlay 2 receiver)
#   - OwnTone (audio router to speakers)
# This gives each zone its own IP, avoiding port conflicts.
#
# AUDIO FLOW (using ALSA loopback for PTP synchronization):
#   shairport-sync → ALSA Loopback → arecord → audio.pipe → OwnTone
#
# Synchronization is handled by shairport-sync's native PTP clock sync:
#   1. shairport-sync uses ALSA backend (not pipe) to enable PTP sync
#   2. Each instance syncs to the system clock using PTP from iOS
#   3. ALSA loopback captures the synced audio for OwnTone
#
# For zones to stay synchronized when grouped on iOS:
#   - Group all zones BEFORE starting playback
#   - iOS will send the same audio stream to all grouped zones
#
# Target OS: Ubuntu (tested on 22.04+)
# Requires: snd-aloop kernel module (modprobe snd-aloop pcm_substreams=16)
# Must be run as root (sudo).
###############################################################################
set -euo pipefail

#------------------------------------------------------------------------------
# Configuration
#------------------------------------------------------------------------------
BASE_DIR="/var/lib/shiri"

# Each script instance runs ONE zone only - for full isolation
# Generate unique zone ID so multiple instances don't conflict
INSTANCE_ID="${RANDOM}${RANDOM}"
ZONES=("zone${INSTANCE_ID}")

# OwnTone ports (same for all instances since each is in its own namespace)
OWNTONE_LIB_PORT=3689

# Parent interface for macvlan (set via CLI or interactive prompt)
PARENT_IF=""

# Will hold PIDs and IPs for cleanup/access
declare -A SHAIRPORT_PIDS
declare -A OWNTONE_PIDS
declare -a NETNS_NAMES
declare -a MACVLAN_IFS
declare -a GROUP_NAMES
declare -A SHAIRPORT_IPS  # IP address of each shairport-sync namespace
declare -A OWNTONE_IPS    # IP address of each OwnTone namespace
declare -A SHAIRPORT_NETNS  # Namespace name per shairport instance
declare -A OWNTONE_NETNS    # Namespace name per OwnTone instance

#------------------------------------------------------------------------------
# Helpers
#------------------------------------------------------------------------------
log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

exec_in_owntone_netns() {
  local grp="$1"
  shift
  local ns="${OWNTONE_NETNS[$grp]:-}"
  if [[ -z "$ns" ]]; then
    log "WARNING: No OwnTone namespace recorded for $grp"
    return 1
  fi
  ip netns exec "$ns" "$@"
}

require_root() {
  [[ $(id -u) -eq 0 ]] || die "This script must be run as root (sudo)."
}

check_deps() {
  local missing=()
  for cmd in ip dhclient unshare dbus-daemon avahi-daemon shairport-sync nqptp owntone jq curl mkfifo python3 arecord; do
    command -v "$cmd" &>/dev/null || missing+=("$cmd")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "Missing required commands: ${missing[*]}\nInstall them first (apt install iproute2 isc-dhcp-client util-linux dbus avahi-daemon shairport-sync owntone-server jq curl coreutils python3)."
  fi
}

#------------------------------------------------------------------------------
# Setup ALSA loopback for clock-synchronized multi-zone audio
# Each zone gets a unique subdevice (0-15) to avoid conflicts
#------------------------------------------------------------------------------
LOOPBACK_LOCK_DIR="/var/lib/shiri/loopback"
ALLOCATED_SUBDEVICE=""

setup_alsa_loopback() {
  # Load snd-aloop with 16 subdevices if not already loaded
  if ! lsmod | grep -q snd_aloop; then
    log "Loading snd-aloop kernel module with 16 subdevices..."
    modprobe snd-aloop pcm_substreams=16 2>/dev/null || {
      log "WARNING: Failed to load snd-aloop. Trying without options..."
      modprobe snd-aloop 2>/dev/null || {
        log "ERROR: Cannot load snd-aloop module. Install linux-modules-extra-$(uname -r)"
        return 1
      }
    }
    sleep 1
  fi
  
  # Verify loopback card exists
  if ! aplay -l 2>/dev/null | grep -q Loopback; then
    log "ERROR: ALSA Loopback card not found after loading module"
    return 1
  fi
  
  log "ALSA Loopback ready"
  return 0
}

# Allocate a unique loopback subdevice using file-based locking
allocate_loopback_subdevice() {
  mkdir -p "$LOOPBACK_LOCK_DIR"
  
  for i in {0..15}; do
    local lock_file="$LOOPBACK_LOCK_DIR/subdev_$i.lock"
    # Try to create lock file exclusively
    if ( set -o noclobber; echo "$$" > "$lock_file" ) 2>/dev/null; then
      ALLOCATED_SUBDEVICE="$i"
      log "Allocated loopback subdevice $i"
      return 0
    fi
    # Check if the PID in the lock file is still running
    local pid
    pid=$(cat "$lock_file" 2>/dev/null)
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
      # Stale lock, remove and claim
      rm -f "$lock_file"
      if ( set -o noclobber; echo "$$" > "$lock_file" ) 2>/dev/null; then
        ALLOCATED_SUBDEVICE="$i"
        log "Allocated loopback subdevice $i (reclaimed from stale lock)"
        return 0
      fi
    fi
  done
  
  log "ERROR: No free loopback subdevices available (all 16 in use)"
  return 1
}

release_loopback_subdevice() {
  if [[ -n "$ALLOCATED_SUBDEVICE" ]]; then
    rm -f "$LOOPBACK_LOCK_DIR/subdev_$ALLOCATED_SUBDEVICE.lock"
    log "Released loopback subdevice $ALLOCATED_SUBDEVICE"
  fi
}

select_parent_interface() {
  if [[ -n "$PARENT_IF" ]]; then
    return
  fi
  mapfile -t INTERFACES < <(ip -o link show | awk -F': ' '($2!="lo"){print $2}')
  if [[ ${#INTERFACES[@]} -eq 0 ]]; then
    die "No candidate network interfaces found."
  fi
  echo "Available network interfaces:"
  for i in "${!INTERFACES[@]}"; do
    printf "  [%d] %s\n" "$i" "${INTERFACES[$i]}"
  done
  read -rp "Select parent interface index: " choice
  if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 0 || choice >= ${#INTERFACES[@]} )); then
    die "Invalid selection."
  fi
  PARENT_IF="${INTERFACES[$choice]}"
  log "Using parent interface: $PARENT_IF"
}

#------------------------------------------------------------------------------
# Ask user for AirPlay name (what iOS/macOS will see)
#------------------------------------------------------------------------------
prompt_group_names() {
  GROUP_NAMES=()
  local default_name="Shiri Zone"
  read -rp "AirPlay receiver name [$default_name]: " name
  name=${name:-$default_name}
  GROUP_NAMES[0]="$name"
  log "This zone will appear as '$name' on your devices"
}

#------------------------------------------------------------------------------
# Cleanup on exit
#------------------------------------------------------------------------------
cleanup() {
  set +e
  log "Cleaning up..."
  
  # Release loopback subdevice
  release_loopback_subdevice

  # Kill ALL processes in each namespace before deleting it
  for ns in "${NETNS_NAMES[@]}"; do
    if ip netns list 2>/dev/null | grep -qw "$ns"; then
      log "Killing processes in netns $ns"
      # Get all PIDs in the namespace and kill them
      for pid in $(ip netns pids "$ns" 2>/dev/null); do
        kill -9 "$pid" 2>/dev/null || true
      done
      sleep 0.5
    fi
  done

  # Also kill wrapper PIDs if still running
  for grp in "${ZONES[@]}"; do
    pid="${SHAIRPORT_PIDS[$grp]:-}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      log "Stopping shairport-sync wrapper for $grp (pid $pid)"
      kill -9 "$pid" 2>/dev/null || true
    fi
    pid="${OWNTONE_PIDS[$grp]:-}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      log "Stopping OwnTone wrapper for $grp (pid $pid)"
      kill -9 "$pid" 2>/dev/null || true
    fi
  done

  sleep 1

  # Tear down network namespaces
  for ns in "${NETNS_NAMES[@]}"; do
    if ip netns list 2>/dev/null | grep -qw "$ns"; then
      log "Deleting netns $ns"
      ip netns delete "$ns" 2>/dev/null || true
    fi
  done
  for mv in "${MACVLAN_IFS[@]}"; do
    if ip link show "$mv" &>/dev/null; then
      log "Deleting macvlan $mv"
      ip link delete "$mv" 2>/dev/null || true
    fi
  done

  log "Cleanup complete."
}
trap cleanup EXIT INT TERM

#------------------------------------------------------------------------------
# Kill any lingering processes from previous runs
#------------------------------------------------------------------------------
cleanup_previous_run() {
  log "Cleaning up any previous run state..."
  
  # Kill any existing owntone processes (including system-wide)
  pkill -9 owntone 2>/dev/null || true
  pkill -9 -f "owntone" 2>/dev/null || true
  pkill -9 -f "shairport-sync.*shiri" 2>/dev/null || true
  
  # Delete any existing netns that match our patterns
  for ns in $(ip netns list 2>/dev/null | grep -E "^(shairport_|owntone_|zone)" | awk '{print $1}'); do
    log "Deleting old netns: $ns"
    # Kill all processes in it first
    for pid in $(ip netns pids "$ns" 2>/dev/null); do
      kill -9 "$pid" 2>/dev/null || true
    done
    ip netns delete "$ns" 2>/dev/null || true
  done
  
  # Remove old state files (IP files, etc.) - including old indexed groups (0, 1, etc.)
  rm -f "$BASE_DIR"/groups/*/state/*.txt 2>/dev/null || true
  rm -f "$BASE_DIR"/groups/*/state/*.db 2>/dev/null || true
  rm -rf "$BASE_DIR"/groups/*/state/cache 2>/dev/null || true
  rm -f "$BASE_DIR"/groups/*/logs/*.log 2>/dev/null || true
  
  # Also remove old index-based group directories if they exist
  for old_grp in "$BASE_DIR"/groups/[0-9]*; do
    if [[ -d "$old_grp" ]]; then
      log "Removing old group directory: $old_grp"
      rm -rf "$old_grp"
    fi
  done
  
  sleep 1
}

#------------------------------------------------------------------------------
# Directory / FIFO setup
#------------------------------------------------------------------------------
setup_directories() {
  for grp in "${ZONES[@]}"; do
    local grp_dir="$BASE_DIR/groups/$grp"
    
    # Create directories with proper permissions
    mkdir -p "$grp_dir"/{pipes,config,logs,state}
    chmod 755 "$grp_dir" "$grp_dir"/{pipes,config,logs,state}
    
    # Clear ALL stale state files (IP files, leases, db, etc.)
    rm -f "$grp_dir/state/"* 2>/dev/null || true
    rm -f "$grp_dir/logs/"* 2>/dev/null || true

    local audio_pipe="$grp_dir/pipes/audio.pipe"       # OwnTone reads from this
    local meta_pipe="$grp_dir/pipes/audio.pipe.metadata"
    local format_file="$grp_dir/pipes/audio.pipe.format"

    # Remove stale FIFOs (if exist) and recreate
    rm -f "$audio_pipe" "$meta_pipe" "$format_file"
    mkfifo "$audio_pipe"
    mkfifo "$meta_pipe"
    chmod 666 "$audio_pipe" "$meta_pipe"

    # OwnTone REQUIRES a format file to know the pipe's audio format!
    # Format: <bits_per_sample>,<sample_rate>,<channels>
    # Must match shairport-sync output: S16_LE @ 44100Hz stereo
    echo "16,44100,2" > "$format_file"

    log "Created directories and FIFOs for $grp"
  done
}

#------------------------------------------------------------------------------
# Generate shairport-sync config (pipe backend)
#------------------------------------------------------------------------------
generate_shairport_config() {
  local grp="$1"
  local idx="$2"
  local grp_dir="$BASE_DIR/groups/$grp"
  local conf="$grp_dir/config/shairport-sync.conf"
  local display_name="${GROUP_NAMES[$idx]:-Shiri $grp}"
  
  # Use ALSA Loopback for perfect clock synchronization
  # ALLOCATED_SUBDEVICE is unique across all script instances (0-15)
  local alsa_device="hw:Loopback,0,$ALLOCATED_SUBDEVICE"
  
  # Generate unique identifiers for this instance
  # Each instance MUST have unique: device_id, port bases, and MAC-like identifier
  local instance_hash
  instance_hash=$(echo -n "$grp" | md5sum | cut -c1-12)
  local unique_device_id="${instance_hash}"
  
  # Generate unique port base from hash (not idx, which is always 0 in single-zone mode)
  # Convert first 4 hex chars to decimal and use modulo to get a port offset
  local hash_num=$((16#${instance_hash:0:4}))
  local port_base=$((6001 + (hash_num % 500) * 10))

  cat > "$conf" <<EOF
// shairport-sync.conf for $grp
general =
{
  name = "$display_name";
  interpolation = "soxr";  // High-quality resampling for sync
  output_backend = "alsa"; // ALSA backend enables PTP clock sync
  mdns_backend = "avahi";
  udp_port_base = $port_base;
  udp_port_range = 100;
  // Explicitly match arecord -f cd format
  output_format = "S16_LE";
  output_rate = 44100;
  
  audio_backend_latency_offset_in_seconds = -3.0;  // Compensate for OwnTone's AirPlay buffer
  // Unique device identifiers
  device_id = "$unique_device_id";
};

alsa =
{
  output_device = "$alsa_device";
};

// AirPlay 2 specific settings
airplay =
{
  // Enable AirPlay 2 operation (requires nqptp running)
  // Each instance has isolated nqptp in private /dev/shm
};

metadata =
{
  enabled = "yes";
  include_cover_art = "no";
  pipe_name = "$grp_dir/pipes/audio.pipe.metadata";
  pipe_timeout = 5000;
};
EOF
  log "Generated shairport-sync config for $grp (ALSA backend)"
}

#------------------------------------------------------------------------------
# Generate OwnTone config (runs in namespace, uses standard ports)
#------------------------------------------------------------------------------
generate_owntone_config() {
  local grp="$1"
  local idx="$2"
  local grp_dir="$BASE_DIR/groups/$grp"
  local conf="$grp_dir/config/owntone.conf"
  local display_name="${GROUP_NAMES[$idx]:-Shiri $grp}"

  cat > "$conf" <<EOF
# OwnTone config for $grp (runs in network namespace)

general {
	uid = "root"
	db_path = "$grp_dir/state/songs3.db"
	logfile = "$grp_dir/logs/owntone.log"
	loglevel = log
	admin_password = ""
	websocket_port = 3688
	cache_dir = "$grp_dir/state/cache"
	cache_daap_threshold = 1000
	speaker_autoselect = no
	high_resolution_clock = yes
}

library {
	name = "$display_name Library"
	port = 3689
	directories = { "$grp_dir/pipes" }
	follow_symlinks = false
	filescan_disable = false
	pipe_autostart = true
	clear_queue_on_stop_disable = true
}

audio {
	type = "disabled"
}

mpd {
	port = 6600
}

streaming {
	sample_rate = 44100
	bit_rate = 192
}
EOF
  log "Generated OwnTone config for $grp"
}

#------------------------------------------------------------------------------
# Start shairport-sync in its own netns
#------------------------------------------------------------------------------
start_shairport_in_netns() {
  local grp="$1"
  local idx="$2"
  local grp_dir="$BASE_DIR/groups/$grp"

  local suffix
  suffix=$(date +%s%N | tail -c 9)
  local ns_name="shairport_${grp}_${suffix}"
  local mv_if="sp_${grp:0:3}${suffix:0:5}"

  NETNS_NAMES+=("$ns_name")
  MACVLAN_IFS+=("$mv_if")

  log "Creating shairport-sync netns $ns_name for $grp"

  ip netns add "$ns_name"
  ip link add "$mv_if" link "$PARENT_IF" type macvlan mode bridge
  ip link set "$mv_if" netns "$ns_name"

  SHAIRPORT_NETNS[$grp]="$ns_name"

  # Wrapper script for shairport-sync
  local wrapper="$grp_dir/config/shairport_wrapper.sh"
  # Calculate loopback capture device: Card Loopback (1), Subdevice matches ALLOCATED_SUBDEVICE
  # Loopback 0 is Playback (used by shairport), Loopback 1 is Capture (used by arecord)
  local capture_dev="hw:Loopback,1,$ALLOCATED_SUBDEVICE"

  cat > "$wrapper" <<WRAPPER_EOF
#!/usr/bin/env bash
set -e

MV_IF="\$1"
GRP_DIR="\$2"
GRP="\$3"

ip link set lo up
ip link set "\$MV_IF" up

echo "[shairport:\$GRP] Running DHCP on \$MV_IF ..."
# Use per-instance lease and PID files in /run (tmpfs) to avoid AppArmor/permission issues
dhclient -v "\$MV_IF" \\
  -lf "/run/shairport_dhclient.leases" \\
  -pf "/run/shairport_dhclient.pid" \\
  2>&1 | head -20 || true
sleep 2

# Get and save the IP address
MY_IP=\$(ip -4 addr show "\$MV_IF" | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
echo "\$MY_IP" > "\$GRP_DIR/state/shairport_ip.txt"
echo "[shairport:\$GRP] Got IP: \$MY_IP"

# Private /run, /tmp, AND /dev/shm for complete isolation
# CRITICAL: nqptp uses /dev/shm for shared memory - must be isolated!
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp
mount -t tmpfs tmpfs /dev/shm
mkdir -p /run/dbus /run/avahi-daemon

echo "[shairport:\$GRP] Starting dbus..."
dbus-daemon --system --fork --nopidfile
sleep 1

# Create per-instance avahi config to avoid mDNS conflicts
cat > /tmp/avahi-daemon.conf <<AVAHI_EOF
[server]
host-name=\$(hostname)-shairport-\$GRP
use-ipv4=yes
use-ipv6=no
allow-interfaces=\$MV_IF
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

echo "[shairport:\$GRP] Starting avahi..."
avahi-daemon --daemonize --no-chroot --no-drop-root --file /tmp/avahi-daemon.conf --no-rlimits 2>/dev/null || true
sleep 1

echo "[shairport:\$GRP] Starting nqptp..."
# nqptp binds to ports 319/320 - in separate network namespaces with separate IPs, this is isolated
nqptp &
sleep 1

echo "[shairport:\$GRP] Starting audio capture pipeline..."
# DIRECT PIPE: shairport (ALSA Loopback 0) -> arecord (ALSA Loopback 1) -> audio.pipe -> OwnTone
# 
# IMPORTANT: We removed clock_sync_buffer because shairport-sync with ALSA backend
# already does PTP synchronization to the system clock. Adding our own re-timing
# was BREAKING the sync.
#
# For zones to be synced, iOS must send them the same audio stream (same frame positions).
# This happens when zones are properly grouped BEFORE playback starts.

arecord -D $capture_dev -f cd -c 2 -t raw 2>/dev/null > "\$GRP_DIR/pipes/audio.pipe" &
ARECORD_PID=\$!
echo "\$ARECORD_PID" > "\$GRP_DIR/state/arecord.pid"

echo "[shairport:\$GRP] Starting shairport-sync with Real-Time priority..."
# Use chrt (FIFO priority 50) to minimize scheduling jitter
exec chrt -f 50 shairport-sync -c "\$GRP_DIR/config/shairport-sync.conf" --statistics
WRAPPER_EOF
  chmod +x "$wrapper"

  # Launch wrapper inside netns
  ip netns exec "$ns_name" unshare -m bash "$wrapper" "$mv_if" "$grp_dir" "$grp" &>"$grp_dir/logs/shairport_wrapper.log" &
  SHAIRPORT_PIDS[$grp]=$!
  log "Started shairport-sync for $grp (pid ${SHAIRPORT_PIDS[$grp]})"
}

#------------------------------------------------------------------------------
# Start OwnTone in its own netns (separate from shairport-sync)
#------------------------------------------------------------------------------
start_owntone_in_netns() {
  local grp="$1"
  local grp_dir="$BASE_DIR/groups/$grp"

  local suffix
  suffix=$(date +%s%N | tail -c 9)
  local ns_name="owntone_${grp}_${suffix}"
  local mv_if="ot_${grp:0:3}${suffix:0:5}"

  NETNS_NAMES+=("$ns_name")
  MACVLAN_IFS+=("$mv_if")

  log "Creating OwnTone netns $ns_name for $grp"

  ip netns add "$ns_name"
  ip link add "$mv_if" link "$PARENT_IF" type macvlan mode bridge
  ip link set "$mv_if" netns "$ns_name"

  OWNTONE_NETNS[$grp]="$ns_name"

  # Ensure cache dir exists
  mkdir -p "$grp_dir/state/cache"

  # Wrapper script for OwnTone
  local wrapper="$grp_dir/config/owntone_wrapper.sh"
  cat > "$wrapper" <<'WRAPPER_EOF'
#!/usr/bin/env bash
set -e

MV_IF="$1"
GRP_DIR="$2"
GRP="$3"

ip link set lo up
ip link set "$MV_IF" up

echo "[owntone:$GRP] Running DHCP on $MV_IF ..."
# Use per-instance lease and PID files in /run (tmpfs) to avoid AppArmor/permission issues
dhclient -v "$MV_IF" \
  -lf "/run/dhclient.leases" \
  -pf "/run/dhclient.pid" \
  2>&1 | head -20 || true
sleep 2

# Get and save the IP address
MY_IP=$(ip -4 addr show "$MV_IF" | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
echo "$MY_IP" > "$GRP_DIR/state/owntone_ip.txt"
echo "[owntone:$GRP] Got IP: $MY_IP"

# Private /run, /tmp, AND /dev/shm for complete isolation
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp
mount -t tmpfs tmpfs /dev/shm
mkdir -p /run/dbus /run/avahi-daemon

echo "[owntone:$GRP] Starting dbus..."
dbus-daemon --system --fork --nopidfile
sleep 1

# Create per-instance avahi config to avoid mDNS conflicts
cat > /tmp/avahi-daemon.conf <<AVAHI_EOF
[server]
host-name=$(hostname)-owntone-$GRP
use-ipv4=yes
use-ipv6=no
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

echo "[owntone:$GRP] Starting avahi..."
avahi-daemon --daemonize --no-chroot --no-drop-root --file /tmp/avahi-daemon.conf --no-rlimits 2>/dev/null || true
sleep 1

echo "[owntone:$GRP] Starting OwnTone with Real-Time priority..."
# Use chrt (FIFO priority 50) to minimize scheduling jitter
exec chrt -f 50 owntone -f -c "$GRP_DIR/config/owntone.conf"
WRAPPER_EOF
  chmod +x "$wrapper"

  # Launch wrapper inside netns
  ip netns exec "$ns_name" unshare -m bash "$wrapper" "$mv_if" "$grp_dir" "$grp" &>"$grp_dir/logs/owntone_wrapper.log" &
  OWNTONE_PIDS[$grp]=$!
  log "Started OwnTone for $grp (pid ${OWNTONE_PIDS[$grp]})"
}

#------------------------------------------------------------------------------
# Wait for shairport-sync to get IP
#------------------------------------------------------------------------------
wait_for_shairport() {
  local grp="$1"
  local grp_dir="$BASE_DIR/groups/$grp"
  local ip_file="$grp_dir/state/shairport_ip.txt"

  log "Waiting for shairport-sync $grp to get IP..."
  
  for _ in {1..60}; do
    if [[ -f "$ip_file" ]]; then
      local ip
      ip=$(cat "$ip_file")
      if [[ -n "$ip" ]]; then
        SHAIRPORT_IPS[$grp]="$ip"
        log "shairport-sync $grp has IP: $ip"
        return 0
      fi
    fi
    sleep 1
  done

  log "WARNING: shairport-sync $grp did not get an IP address in time."
  return 1
}

#------------------------------------------------------------------------------
# Wait for OwnTone to be ready (check for IP file and API)
#------------------------------------------------------------------------------
wait_for_owntone() {
  local grp="$1"
  local grp_dir="$BASE_DIR/groups/$grp"
  local ip_file="$grp_dir/state/owntone_ip.txt"

  log "Waiting for OwnTone $grp to get IP..."
  
  # Wait for IP file to appear
  for _ in {1..60}; do
    if [[ -f "$ip_file" ]]; then
      local ip
      ip=$(cat "$ip_file")
      if [[ -n "$ip" ]]; then
        OWNTONE_IPS[$grp]="$ip"
        log "OwnTone $grp has IP: $ip"
        break
      fi
    fi
    sleep 1
  done

  if [[ -z "${OWNTONE_IPS[$grp]:-}" ]]; then
    log "WARNING: OwnTone $grp did not get an IP address in time."
    return 1
  fi

  local owntone_ip="${OWNTONE_IPS[$grp]}"
  local url="http://$owntone_ip:3689/api/config"

  log "Waiting for OwnTone API at $owntone_ip:3689 ..."
  for i in {1..60}; do
    # Try curl from inside the namespace (localhost should work since OwnTone binds to 0.0.0.0)
    if exec_in_owntone_netns "$grp" curl -s --connect-timeout 2 "http://127.0.0.1:3689/api/config" >/dev/null 2>&1; then
      log "OwnTone $grp is ready at $owntone_ip"
      return 0
    fi
    # Show progress every 10 seconds
    if (( i % 10 == 0 )); then
      log "Still waiting for OwnTone API... ($i seconds)"
    fi
    sleep 1
  done
  log "WARNING: OwnTone $grp API did not become ready in time."
  return 1
}

#------------------------------------------------------------------------------
# Trigger OwnTone library rescan to discover pipes
#------------------------------------------------------------------------------
trigger_library_rescan() {
  local grp="$1"
  local owntone_ip="${OWNTONE_IPS[$grp]:-}"

  if [[ -z "$owntone_ip" ]]; then
    log "WARNING: No IP for OwnTone $grp, cannot trigger rescan"
    return 1
  fi

  log "Triggering library rescan for OwnTone $grp at $owntone_ip ..."
  exec_in_owntone_netns "$grp" curl -s --connect-timeout 5 -X PUT "http://$owntone_ip:3689/api/update" >/dev/null 2>&1 || true
  sleep 3
}

#------------------------------------------------------------------------------
# Check if OwnTone found the pipe and show its status
#------------------------------------------------------------------------------
verify_pipe_discovery() {
  local grp="$1"
  local owntone_ip="${OWNTONE_IPS[$grp]:-}"

  if [[ -z "$owntone_ip" ]]; then
    log "WARNING: No IP for OwnTone $grp, cannot verify pipe"
    return 1
  fi

  log "Checking if OwnTone $grp discovered the audio pipe..."

  # Get all tracks from the library
  local tracks
  tracks=$(exec_in_owntone_netns "$grp" curl -s --connect-timeout 5 "http://$owntone_ip:3689/api/library/tracks?limit=100" 2>/dev/null)

  if [[ -z "$tracks" ]]; then
    log "WARNING: Could not query tracks from OwnTone $grp"
    return 1
  fi

  local pipe_count
  pipe_count=$(echo "$tracks" | jq '[.items[] | select(.path | contains("audio.pipe"))] | length' 2>/dev/null || echo "0")

  if [[ "$pipe_count" -gt 0 ]]; then
    log "SUCCESS: OwnTone $grp found the audio pipe!"
    echo "$tracks" | jq -r '.items[] | select(.path | contains("audio.pipe")) | "  Track: \(.title) | Path: \(.path) | ID: \(.id)"'
    return 0
  else
    log "WARNING: OwnTone $grp has NOT discovered the audio pipe yet."
    log "  Available tracks:"
    echo "$tracks" | jq -r '.items[] | "    - \(.title) (\(.path))"' | head -5
    return 1
  fi
}

#------------------------------------------------------------------------------
# List speakers from OwnTone and let user pick one
#------------------------------------------------------------------------------
select_speaker_for_group() {
  local grp="$1"
  local owntone_ip="${OWNTONE_IPS[$grp]:-}"

  if [[ -z "$owntone_ip" ]]; then
    log "WARNING: No IP for OwnTone $grp, cannot select speaker"
    return 1
  fi

  local url="http://$owntone_ip:3689/api/outputs"

  log "Fetching available outputs from OwnTone $grp at $owntone_ip ..."
  local outputs
  outputs=$(exec_in_owntone_netns "$grp" curl -s --connect-timeout 5 "$url")

  local count
  count=$(echo "$outputs" | jq '.outputs | length' 2>/dev/null || echo "0")
  if [[ "$count" -eq 0 ]]; then
    log "No outputs found for $grp. Skipping speaker selection."
    return
  fi

  echo ""
  echo "=== Available speakers for $grp (OwnTone at $owntone_ip) ==="
  echo "$outputs" | jq -r '.outputs | to_entries[] | "  [\(.key)] \(.value.name) (\(.value.type)) - id: \(.value.id)"'
  echo ""

  read -rp "Select speaker index for $grp (or 'skip'): " choice
  if [[ "$choice" == "skip" ]]; then
    log "Skipping speaker selection for $grp"
    return
  fi

  if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 0 || choice >= count )); then
    log "Invalid selection for $grp, skipping."
    return
  fi

  local speaker_id
  speaker_id=$(echo "$outputs" | jq -r ".outputs[$choice].id")
  local speaker_name
  speaker_name=$(echo "$outputs" | jq -r ".outputs[$choice].name")

  log "Enabling speaker '$speaker_name' (id $speaker_id) for $grp"

  # Use PUT /api/outputs/set to enable ONLY this speaker
  exec_in_owntone_netns "$grp" curl -s --connect-timeout 5 -X PUT "http://$owntone_ip:3689/api/outputs/set" \
    -H "Content-Type: application/json" \
    -d "{\"outputs\":[\"$speaker_id\"]}" >/dev/null

  log "Speaker '$speaker_name' enabled for $grp"
}

#------------------------------------------------------------------------------
# Main
#------------------------------------------------------------------------------
main() {
  require_root
  check_deps
  
  # Setup ALSA loopback for clock synchronization
  setup_alsa_loopback || die "Failed to setup ALSA loopback"
  allocate_loopback_subdevice || die "Failed to allocate loopback subdevice"
  
  select_parent_interface
  prompt_group_names

  log "Starting instance with groups: ${ZONES[*]}"
  log "Setting up directories and FIFOs..."
  setup_directories

  log "Generating configs..."
  for i in "${!ZONES[@]}"; do
    generate_shairport_config "${ZONES[$i]}" "$i"
    generate_owntone_config "${ZONES[$i]}" "$i"
  done

  # Start OwnTone instances FIRST (each in its own namespace with its own IP)
  # This way OwnTone can discover and watch the pipes before shairport writes to them
  log "Starting OwnTone instances in separate namespaces..."
  for grp in "${ZONES[@]}"; do
    start_owntone_in_netns "$grp"
  done

  # Wait for OwnTone instances to be ready
  for grp in "${ZONES[@]}"; do
    wait_for_owntone "$grp" || true
  done

  # Trigger library rescan so OwnTone finds the pipes
  log "Triggering library rescan to discover pipes..."
  for grp in "${ZONES[@]}"; do
    trigger_library_rescan "$grp" || true
  done
  
  # Wait a bit for pipe watching to be set up
  sleep 3

  # Verify pipes were discovered
  echo ""
  echo "========================================"
  echo "  Pipe Discovery Status"
  echo "========================================"
  for grp in "${ZONES[@]}"; do
    verify_pipe_discovery "$grp" || true
  done

  # NOW start shairport-sync instances (each in its own namespace with its own IP)
  log "Starting shairport-sync instances in separate namespaces..."
  for i in "${!ZONES[@]}"; do
    start_shairport_in_netns "${ZONES[$i]}" "$i"
  done

  # Wait for shairport-sync to get IPs
  for grp in "${ZONES[@]}"; do
    wait_for_shairport "$grp" || true
  done

  # Let user select speakers
  echo ""
  echo "========================================"
  echo "  Speaker Selection"
  echo "========================================"
  for grp in "${ZONES[@]}"; do
    select_speaker_for_group "$grp"
  done

  echo ""
  log "Demo is running!"
  echo ""
  echo "IPs assigned:"
  for grp in "${ZONES[@]}"; do
    echo "  - $grp shairport-sync: ${SHAIRPORT_IPS[$grp]:-unknown}"
    echo "  - $grp OwnTone:        ${OWNTONE_IPS[$grp]:-unknown}"
  done
  echo ""
  echo "You should now see these AirPlay endpoints on your iPhone:"
  for i in "${!ZONES[@]}"; do
    echo "  - ${GROUP_NAMES[$i]}"
  done
  echo ""
  echo "When you play audio to an AirPlay endpoint, OwnTone should auto-start"
  echo "playback to the speaker you selected (pipe_autostart is enabled)."
  echo ""
  echo "OwnTone Web UIs (access from your browser):"
  for grp in "${ZONES[@]}"; do
    echo "  - $grp: http://${OWNTONE_IPS[$grp]:-<ip>}:3689"
  done
  echo ""
  echo "TROUBLESHOOTING:"
  echo "  1. Check OwnTone wrapper logs:"
  for grp in "${ZONES[@]}"; do
    echo "     cat $BASE_DIR/groups/$grp/logs/owntone_wrapper.log"
  done
  echo ""
  echo "  2. Check shairport-sync wrapper logs:"
  for grp in "${ZONES[@]}"; do
    echo "     cat $BASE_DIR/groups/$grp/logs/shairport_wrapper.log"
  done
  echo ""
  echo "  3. Check OwnTone logs for 'pipe' messages:"
  for grp in "${ZONES[@]}"; do
    echo "     grep -i pipe $BASE_DIR/groups/$grp/logs/owntone.log"
  done
  echo ""
  echo "  4. Check if pipes exist and are FIFOs:"
  for grp in "${ZONES[@]}"; do
    echo "     ls -la $BASE_DIR/groups/$grp/pipes/"
  done
  echo ""
  echo "Press Ctrl+C to stop the demo and clean up."

  # Wait indefinitely (cleanup on signal)
  while true; do
    sleep 60
  done
}

main "$@"

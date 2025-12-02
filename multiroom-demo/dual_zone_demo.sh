#!/usr/bin/env bash
###############################################################################
# dual_zone_demo.sh
#
# Proof-of-concept script that spins up independent AirPlay 2 endpoints.
#
# CRITICAL ARCHITECTURE FOR MULTI-ROOM SYNC:
#   All shairport-sync instances run on the HOST (same IP) sharing ONE nqptp.
#   This ensures all instances use the SAME PTP timing calculation.
#   OwnTone runs in network namespaces (separate IPs) for speaker routing.
#
# Why this works:
#   - nqptp receives PTP timing from iOS and calculates local_to_master_time_offset
#   - All shairport-sync instances read from the SAME /dev/shm/nqptp
#   - They all output audio at exactly the same time = PERFECT SYNC
#   - OwnTone doesn't need PTP sync - it receives already-synchronized audio
#
# AUDIO FLOW:
#   shairport-sync (HOST) → ALSA Loopback → arecord → audio.pipe → OwnTone (namespace)
#
# Each shairport-sync instance uses:
#   - Different port (7000, 7001, etc.)
#   - Different airplay_device_id_offset (1, 2, etc.)
#   - Different ALSA loopback subdevice (0, 1, etc.)
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
 

# Parent interface for macvlan (set via CLI or interactive prompt)
PARENT_IF=""

# Will hold PIDs and IPs for cleanup/access
declare -A SHAIRPORT_PIDS
declare -A OWNTONE_PIDS
declare -a NETNS_NAMES
declare -a MACVLAN_IFS
declare -a GROUP_NAMES
declare -A SHAIRPORT_IPS  # IP address of each shairport-sync instance
declare -A OWNTONE_IPS    # IP address of each OwnTone namespace
declare -A SHAIRPORT_NETNS  # "HOST" for host-based, or namespace name
declare -A OWNTONE_NETNS    # Namespace name per OwnTone instance

# Host process tracking (for cleanup)
HOST_NQPTP_PID=""
declare -a HOST_SHAIRPORT_PIDS
declare -a HOST_ARECORD_PIDS

#------------------------------------------------------------------------------
# Helpers
#------------------------------------------------------------------------------
log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# Forward declaration for cleanup - actual logic is later
stop_host_nqptp() {
  if [[ -n "$HOST_NQPTP_PID" ]]; then
    log "Stopping host nqptp (pid $HOST_NQPTP_PID)"
    kill -TERM "$HOST_NQPTP_PID" 2>/dev/null || true
    sleep 1
    kill -9 "$HOST_NQPTP_PID" 2>/dev/null || true
  fi
}

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
  for cmd in ip dhclient unshare dbus-daemon avahi-daemon shairport-sync nqptp owntone jq curl mkfifo arecord; do
    command -v "$cmd" &>/dev/null || missing+=("$cmd")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "Missing required commands: ${missing[*]}\nInstall them first (apt install iproute2 isc-dhcp-client util-linux dbus avahi-daemon shairport-sync owntone-server jq curl coreutils)."
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

  # STEP 1: Stop HOST processes (shairport-sync, arecord, nqptp)
  log "Stopping host shairport-sync instances..."
  for pid in "${HOST_SHAIRPORT_PIDS[@]}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      log "Stopping shairport-sync (pid $pid)"
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  
  log "Stopping host arecord instances..."
  for pid in "${HOST_ARECORD_PIDS[@]}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      log "Stopping arecord (pid $pid)"
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  
  # Stop host nqptp (only if we started it)
  stop_host_nqptp

  # STEP 2: Gracefully release DHCP leases and stop services in OwnTone namespaces
  for ns in "${NETNS_NAMES[@]}"; do
    if ip netns list 2>/dev/null | grep -qw "$ns"; then
      log "Releasing DHCP lease in netns $ns..."
      
      # Find the interface in the namespace (ot_*)
      local iface
      iface=$(ip netns exec "$ns" ip -o link show 2>/dev/null | grep -oP 'ot_\w+' | head -1)
      
      if [[ -n "$iface" ]]; then
        ip netns exec "$ns" dhclient -r "$iface" 2>/dev/null || true
        log "Released DHCP lease on $iface in $ns"
      fi
      
      # Stop avahi-daemon gracefully
      ip netns exec "$ns" pkill -TERM avahi-daemon 2>/dev/null || true
      
      # Stop owntone gracefully
      ip netns exec "$ns" pkill -TERM owntone 2>/dev/null || true
    fi
  done
  
  # Give services time to gracefully shutdown
  sleep 2

  # STEP 3: Force kill remaining host processes
  for pid in "${HOST_SHAIRPORT_PIDS[@]}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${HOST_ARECORD_PIDS[@]}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done

  # STEP 4: Force kill remaining namespace processes
  for ns in "${NETNS_NAMES[@]}"; do
    if ip netns list 2>/dev/null | grep -qw "$ns"; then
      log "Force killing remaining processes in netns $ns"
      for pid in $(ip netns pids "$ns" 2>/dev/null); do
        kill -9 "$pid" 2>/dev/null || true
      done
      sleep 0.3
    fi
  done

  # Also kill wrapper PIDs if still running
  for grp in "${ZONES[@]}"; do
    pid="${SHAIRPORT_PIDS[$grp]:-}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      log "Stopping shairport-sync for $grp (pid $pid)"
      kill -9 "$pid" 2>/dev/null || true
    fi
    pid="${OWNTONE_PIDS[$grp]:-}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      log "Stopping OwnTone wrapper for $grp (pid $pid)"
      kill -9 "$pid" 2>/dev/null || true
    fi
  done

  sleep 1

  # STEP 5: Tear down network namespaces (OwnTone only now)
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
  # CRITICAL FOR AIRPLAY 2 SYNC: Each instance MUST have:
  #   - Unique airplay_device_id_offset (so iOS sees them as distinct devices)
  #   - Unique port (to avoid any confusion)
  # 
  # Using ALLOCATED_SUBDEVICE (0-15) ensures uniqueness across all instances
  local device_offset=$((ALLOCATED_SUBDEVICE + 1))  # 1-16 to avoid 0
  local port=$((7000 + ALLOCATED_SUBDEVICE))  # 7000-7015
  local udp_port_base=$((6001 + ALLOCATED_SUBDEVICE * 100))  # 6001, 6101, 6201, etc.

  # Volume bridge script path (for instant volume control via OwnTone)
  local volume_bridge_script="$(dirname "$(readlink -f "$0")")/volume_bridge.sh"
  
  # Ensure volume bridge script is executable
  chmod +x "$volume_bridge_script" 2>/dev/null || true

  # Create pipe reset script - CRITICAL FOR MULTI-ROOM SYNC
  # This script kills arecord and flushes the pipe to reset ALL buffer state
  # Without this, pipes accumulate different amounts of data = DRIFT
  local flush_script="$grp_dir/config/reset_audio_pipe.sh"
  cat > "$flush_script" <<FLUSH_EOF
#!/bin/bash
# Reset audio pipe completely for sync
# Called by shairport-sync on play start/stop
# This kills arecord, flushes the pipe, and lets arecord restart fresh

PIPE="$grp_dir/pipes/audio.pipe"
ARECORD_PID_FILE="$grp_dir/state/arecord.pid"
LOG="$grp_dir/logs/sync_reset.log"
TIMESTAMP=\$(date '+%H:%M:%S.%3N')

echo "" >> "\$LOG"
echo "[\$TIMESTAMP] ========== SYNC RESET TRIGGERED ==========" >> "\$LOG"
echo "[\$TIMESTAMP] Called with args: \$@" >> "\$LOG"

# Step 1: Kill arecord to stop writing to pipe
if [[ -f "\$ARECORD_PID_FILE" ]]; then
  ARECORD_PID=\$(cat "\$ARECORD_PID_FILE")
  echo "[\$TIMESTAMP] Found arecord PID file: \$ARECORD_PID" >> "\$LOG"
  if kill -0 "\$ARECORD_PID" 2>/dev/null; then
    echo "[\$TIMESTAMP] Killing arecord (pid \$ARECORD_PID)" >> "\$LOG"
    kill -TERM "\$ARECORD_PID" 2>/dev/null || true
    sleep 0.1
    echo "[\$TIMESTAMP] arecord killed" >> "\$LOG"
  else
    echo "[\$TIMESTAMP] arecord (pid \$ARECORD_PID) not running" >> "\$LOG"
  fi
else
  echo "[\$TIMESTAMP] WARNING: arecord PID file not found!" >> "\$LOG"
fi

# Step 2: Drain any buffered data from the pipe
if [[ -p "\$PIPE" ]]; then
  BEFORE_SIZE=\$(timeout 0.1 stat -c%s "\$PIPE" 2>/dev/null || echo "unknown")
  echo "[\$TIMESTAMP] Draining pipe (size before: \$BEFORE_SIZE)" >> "\$LOG"
  # Non-blocking drain - read whatever is buffered
  DRAINED=\$(timeout 0.3 dd if="\$PIPE" of=/dev/null bs=65536 iflag=nonblock 2>&1 | grep -oP '\d+ bytes' || echo "0 bytes")
  echo "[\$TIMESTAMP] Drained: \$DRAINED" >> "\$LOG"
else
  echo "[\$TIMESTAMP] WARNING: Pipe \$PIPE is not a FIFO!" >> "\$LOG"
fi

echo "[\$TIMESTAMP] Reset complete - arecord will restart fresh" >> "\$LOG"
echo "[\$TIMESTAMP] ==========================================" >> "\$LOG"
FLUSH_EOF
  chmod +x "$flush_script"

  cat > "$conf" <<EOF
// shairport-sync.conf for $grp
general =
{
  name = "$display_name";
  interpolation = "soxr";  // High-quality resampling for sync
  output_backend = "alsa"; // ALSA backend enables PTP clock sync
  mdns_backend = "avahi";
  
  // CRITICAL FOR MULTI-INSTANCE SYNC:
  // Each instance needs unique port and device ID offset
  // so iOS properly identifies them as separate synchronized receivers
  port = $port;  // Unique RTSP port per instance
  udp_port_base = $udp_port_base;
  udp_port_range = 100;
  airplay_device_id_offset = $device_offset;  // Makes each instance have unique AirPlay ID
  
  // Tighter sync tolerances for multi-room
  drift_tolerance_in_seconds = 0.001;  // Tighter than default 0.002
  resync_threshold_in_seconds = 0.025;  // Faster resync when out of sync
  resync_recovery_time_in_seconds = 0.050;  // Quick recovery
  
  // INSTANT VOLUME CONTROL via OwnTone:
  // Keep shairport-sync at 100% volume (no delayed volume changes)
  // Volume changes are intercepted and applied instantly via OwnTone API
  ignore_volume_control = "yes";
  
  // Hook called when iOS sends volume change - passes volume to OwnTone
  // NOTE: trailing space is REQUIRED so shairport appends volume as argument
  run_this_when_volume_is_set = "$volume_bridge_script $grp_dir ";
};

alsa =
{
  output_device = "$alsa_device";
  // Disable standby to prevent timing glitches on resume
  disable_standby_mode = "always";
};

// CRITICAL: Session control hooks to flush pipes on play start/stop
// This resets buffer state so all instances start synchronized
sessioncontrol =
{
  run_this_before_play_begins = "$flush_script";
  run_this_after_play_ends = "$flush_script";
  wait_for_completion = "yes";  // Wait for flush to complete before continuing
};

// Enable statistics for debugging sync issues
diagnostics =
{
  statistics = "yes";
  log_verbosity = 1;
};

// AirPlay 2 specific settings
airplay =
{
  // Enable AirPlay 2 operation (requires nqptp running)
};

metadata =
{
  enabled = "yes";
  include_cover_art = "no";
  pipe_name = "$grp_dir/pipes/audio.pipe.metadata";
  pipe_timeout = 5000;
};
EOF
  log "Generated shairport-sync config for $grp (ALSA backend + pipe flush hooks)"
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
# Host nqptp management - ONE instance shared by ALL shairport-sync
# This is CRITICAL for multi-room sync - all instances must use same timing
#------------------------------------------------------------------------------
start_host_nqptp() {
  # Check if nqptp is already running on the host
  if pgrep -x nqptp >/dev/null 2>&1; then
    HOST_NQPTP_PID=$(pgrep -x nqptp | head -1)
    log "nqptp already running on host (pid $HOST_NQPTP_PID) - reusing for shared timing"
    return 0
  fi
  
  log "Starting shared nqptp on HOST (CRITICAL for multi-room sync)..."
  # nqptp MUST run on host to:
  # 1. Receive PTP timing packets from iOS on ports 319/320
  # 2. Write timing data to /dev/shm/nqptp
  # 3. All shairport-sync instances read the SAME timing = PERFECT SYNC
  nqptp &
  HOST_NQPTP_PID=$!
  sleep 1
  
  if ! kill -0 "$HOST_NQPTP_PID" 2>/dev/null; then
    die "Failed to start nqptp on host - check if ports 319/320 are available"
  fi
  
  # Verify shared memory was created
  if [[ ! -e /dev/shm/nqptp ]]; then
    die "nqptp started but /dev/shm/nqptp not created"
  fi
  
  log "Host nqptp started (pid $HOST_NQPTP_PID) - /dev/shm/nqptp ready for all instances"
}

#------------------------------------------------------------------------------
# Start shairport-sync on HOST (NOT in namespace) for shared PTP timing
# 
# CRITICAL FOR MULTI-ROOM SYNC:
#   All shairport-sync instances MUST share the same nqptp via /dev/shm/nqptp.
#   Running in separate namespaces with private /dev/shm causes each instance 
#   to have independent PTP calculations → DRIFT over time.
#
#   By running on HOST:
#   - All instances share ONE nqptp = SAME local_to_master_time_offset
#   - Different ports + airplay_device_id_offset = iOS sees separate devices
#   - Audio flows through ALSA loopback to pipes → OwnTone in namespaces
#------------------------------------------------------------------------------
start_shairport_on_host() {
  local grp="$1"
  local grp_dir="$BASE_DIR/groups/$grp"

  log "Starting shairport-sync on HOST for $grp (shared nqptp = perfect sync)"

  # Get host IP for display
  local host_ip
  host_ip=$(ip -4 addr show "$PARENT_IF" | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1 || echo "unknown")
  echo "$host_ip" > "$grp_dir/state/shairport_ip.txt"
  SHAIRPORT_IPS[$grp]="$host_ip"
  SHAIRPORT_NETNS[$grp]="HOST"  # Mark as running on host

  # Loopback devices for this instance
  local playback_dev="hw:Loopback,0,$ALLOCATED_SUBDEVICE"
  local capture_dev="hw:Loopback,1,$ALLOCATED_SUBDEVICE"

  # Start arecord supervisor in background
  # Uses small buffer (2048 frames = ~46ms) to minimize latency/drift
  # The supervisor loop auto-restarts arecord when killed by sync reset
  log "Starting arecord supervisor for $grp (loopback subdevice $ALLOCATED_SUBDEVICE)"
  (
    while true; do
      # Clear stale loopback data before each session
      timeout 0.1 arecord -D "$capture_dev" -f cd -c 2 -t raw -d 1 2>/dev/null >/dev/null || true
      
      # Start arecord with small buffer for low latency
      # --buffer-size=2048 = ~46ms buffer (was 8192 = ~185ms)
      # This reduces the time window for drift accumulation
      arecord -D "$capture_dev" -f cd -c 2 -t raw --buffer-size=2048 --period-size=512 2>/dev/null > "$grp_dir/pipes/audio.pipe" &
      ARECORD_INNER_PID=\$!
      
      # Write the actual arecord PID (not supervisor PID) for sync reset script
      echo "\$ARECORD_INNER_PID" > "$grp_dir/state/arecord.pid"
      
      # Wait for arecord to exit (killed by sync reset or pipe close)
      wait \$ARECORD_INNER_PID 2>/dev/null || true
      
      echo "[\$(date '+%H:%M:%S')] arecord exited, restarting in 0.3s..." >&2
      sleep 0.3
    done
  ) &>"$grp_dir/logs/arecord.log" &
  local supervisor_pid=$!
  HOST_ARECORD_PIDS+=("$supervisor_pid")
  log "Started arecord supervisor for $grp (supervisor pid $supervisor_pid)"

  # Start shairport-sync on host with real-time priority
  log "Starting shairport-sync for $grp on host..."
  chrt -f 50 shairport-sync -c "$grp_dir/config/shairport-sync.conf" --statistics \
    &>"$grp_dir/logs/shairport.log" &
  local shairport_pid=$!
  SHAIRPORT_PIDS[$grp]=$shairport_pid
  HOST_SHAIRPORT_PIDS+=("$shairport_pid")
  
  log "Started shairport-sync for $grp (pid $shairport_pid) - using shared nqptp"
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
  
  # Save netns name so volume_bridge.sh can use it
  echo "$ns_name" > "$grp_dir/state/owntone_netns.txt"

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
  
  # Start nqptp on HOST FIRST - CRITICAL for multi-room sync
  # All shairport-sync instances will share this ONE nqptp
  start_host_nqptp
  
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

  # NOW start shairport-sync instances ON HOST (sharing nqptp for perfect sync)
  log "Starting shairport-sync instances on HOST (shared nqptp = perfect multi-room sync)..."
  for i in "${!ZONES[@]}"; do
    start_shairport_on_host "${ZONES[$i]}"
  done

  # Brief pause to let shairport-sync register with avahi
  sleep 2

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
  echo "========================================"
  echo "  SYNC ARCHITECTURE (Perfect Multi-Room)"
  echo "========================================"
  echo ""
  echo "  nqptp (HOST):        PID $HOST_NQPTP_PID → /dev/shm/nqptp"
  echo "  All shairport-sync:  Running on HOST, sharing nqptp timing"
  echo ""
  echo "========================================"
  echo "  INSTANT VOLUME CONTROL"
  echo "========================================"
  echo ""
  echo "  Volume changes from your phone are now INSTANT!"
  echo "  - shairport-sync stays at 100% (no delayed volume)"
  echo "  - volume_bridge.sh intercepts volume events"
  echo "  - OwnTone applies volume to speakers immediately"
  echo ""
  echo "IPs assigned:"
  for grp in "${ZONES[@]}"; do
    echo "  - $grp shairport-sync: ${SHAIRPORT_IPS[$grp]:-unknown} (HOST)"
    echo "  - $grp OwnTone:        ${OWNTONE_IPS[$grp]:-unknown} (namespace)"
  done
  echo ""
  echo "========================================"
  echo "  AirPlay Endpoints"
  echo "========================================"
  echo ""
  echo "You should see these AirPlay endpoints on your iPhone:"
  for i in "${!ZONES[@]}"; do
    echo "  - ${GROUP_NAMES[$i]}"
  done
  echo ""
  echo "FOR SYNCHRONIZED PLAYBACK:"
  echo "  1. Open Control Center on your iPhone"
  echo "  2. Long-press the audio card (top-right)"
  echo "  3. Tap the AirPlay icon"
  echo "  4. Select your zone(s)"
  echo "  5. Play music!"
  echo ""
  echo "  ✓ All zones now share ONE nqptp = PERFECT SYNC"
  echo "  ✓ No more drift over time!"
  echo ""
  echo "When you play audio, OwnTone should auto-start playback to your speakers."
  echo ""
  echo "OwnTone Web UIs (access from your browser):"
  for grp in "${ZONES[@]}"; do
    echo "  - $grp: http://${OWNTONE_IPS[$grp]:-<ip>}:3689"
  done
  echo ""
  echo "TROUBLESHOOTING:"
  echo "  1. Verify nqptp shared memory exists:"
  echo "     ls -la /dev/shm/nqptp"
  echo ""
  echo "  2. Check shairport-sync logs:"
  for grp in "${ZONES[@]}"; do
    echo "     cat $BASE_DIR/groups/$grp/logs/shairport.log"
  done
  echo ""
  echo "  3. Check OwnTone wrapper logs:"
  for grp in "${ZONES[@]}"; do
    echo "     cat $BASE_DIR/groups/$grp/logs/owntone_wrapper.log"
  done
  echo ""
  echo "  4. Check arecord logs:"
  for grp in "${ZONES[@]}"; do
    echo "     cat $BASE_DIR/groups/$grp/logs/arecord.log"
  done
  echo ""
  echo "  5. Check volume bridge logs (instant volume):"
  for grp in "${ZONES[@]}"; do
    echo "     cat $BASE_DIR/groups/$grp/logs/volume_bridge.log"
  done
  echo ""
  echo "  6. Check if pipes exist and are FIFOs:"
  for grp in "${ZONES[@]}"; do
    echo "     ls -la $BASE_DIR/groups/$grp/pipes/"
  done
  echo ""
  echo "  7. SYNC VERIFICATION - All instances should show same timing offset:"
  echo "     grep 'offset' $BASE_DIR/groups/*/logs/shairport.log | tail -20"
  echo ""
  echo "Press Ctrl+C to stop the demo and clean up."

  # Wait indefinitely (cleanup on signal)
  while true; do
    sleep 60
  done
}

main "$@"

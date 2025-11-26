#!/usr/bin/env bash
###############################################################################
# dual_zone_demo.sh
#
# Proof-of-concept script that spins up TWO independent AirPlay 2 endpoints.
# Each zone runs in its own network namespace with macvlan, containing:
#   - shairport-sync (AirPlay 2 receiver)
#   - OwnTone (audio router to speakers)
# This gives each zone its own IP, avoiding port conflicts.
#
# Each shairport-sync outputs PCM to a named pipe; each OwnTone reads from
# that pipe and streams to the speaker(s) you select via its JSON API.
#
# Target OS: Ubuntu (tested on 22.04+)
# Must be run as root (sudo).
###############################################################################
set -euo pipefail

#------------------------------------------------------------------------------
# Configuration
#------------------------------------------------------------------------------
BASE_DIR="/var/lib/shiri"
GROUPS=("zone1" "zone2")

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
  for cmd in ip dhclient unshare dbus-daemon avahi-daemon shairport-sync nqptp owntone jq curl mkfifo; do
    command -v "$cmd" &>/dev/null || missing+=("$cmd")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "Missing required commands: ${missing[*]}\nInstall them first (apt install iproute2 isc-dhcp-client util-linux dbus avahi-daemon shairport-sync owntone-server jq curl coreutils)."
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
# Ask user for AirPlay names per group (what iOS/macOS will see)
#------------------------------------------------------------------------------
prompt_group_names() {
  GROUP_NAMES=()
  for i in "${!GROUPS[@]}"; do
    local id="${GROUPS[$i]}"
    local default_name="Shiri $id"
    read -rp "AirPlay name for $id [$default_name]: " name
    name=${name:-$default_name}
    GROUP_NAMES[$i]="$name"
  done
}

#------------------------------------------------------------------------------
# Cleanup on exit
#------------------------------------------------------------------------------
cleanup() {
  set +e
  log "Cleaning up..."

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
  for grp in "${GROUPS[@]}"; do
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
  for grp in "${GROUPS[@]}"; do
    local grp_dir="$BASE_DIR/groups/$grp"
    mkdir -p "$grp_dir"/{pipes,config,logs,state}

    local audio_pipe="$grp_dir/pipes/audio.pipe"
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

  cat > "$conf" <<EOF
// shairport-sync.conf for $grp
general =
{
  name = "$display_name";
  interpolation = "basic";
  output_backend = "pipe";
  mdns_backend = "avahi";
  udp_port_base = 6001;
  udp_port_range = 100;
  audio_backend_buffer_desired_length_in_seconds = 1.0;
  output_format = "S16_LE";  // PCM16 little-endian for OwnTone
  output_rate = 44100;
};

metadata =
{
  enabled = "yes";
  include_cover_art = "no";
  pipe_name = "$grp_dir/pipes/audio.pipe.metadata";
  pipe_timeout = 5000;
};

pipe =
{
  name = "$grp_dir/pipes/audio.pipe";
};
EOF
  log "Generated shairport-sync config for $grp"
}

#------------------------------------------------------------------------------
# Generate OwnTone config (runs in namespace, uses standard ports)
#------------------------------------------------------------------------------
generate_owntone_config() {
  local grp="$1"
  local idx="$2"
  local grp_dir="$BASE_DIR/groups/$grp"
  local conf="$grp_dir/config/owntone.conf"

  cat > "$conf" <<EOF
# OwnTone config for $grp (runs in network namespace)

general {
	uid = "root"
	db_path = "$grp_dir/state/songs3.db"
	logfile = "$grp_dir/logs/owntone.log"
	loglevel = debug
	admin_password = ""
	websocket_port = 3688
	cache_dir = "$grp_dir/state/cache"
	cache_daap_threshold = 1000
	speaker_autoselect = no
	high_resolution_clock = yes
}

library {
	name = "Shiri $grp"
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
  cat > "$wrapper" <<'WRAPPER_EOF'
#!/usr/bin/env bash
set -e

MV_IF="$1"
GRP_DIR="$2"
GRP="$3"

ip link set lo up
ip link set "$MV_IF" up

echo "[shairport:$GRP] Running DHCP on $MV_IF ..."
dhclient -v "$MV_IF" 2>&1 | head -20 || true
sleep 2

# Get and save the IP address
MY_IP=$(ip -4 addr show "$MV_IF" | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
echo "$MY_IP" > "$GRP_DIR/state/shairport_ip.txt"
echo "[shairport:$GRP] Got IP: $MY_IP"

# Private /run for dbus/avahi/nqptp
mount -t tmpfs tmpfs /run
mkdir -p /run/dbus /run/avahi-daemon

echo "[shairport:$GRP] Starting dbus..."
dbus-daemon --system --fork --nopidfile
sleep 1

echo "[shairport:$GRP] Starting avahi..."
avahi-daemon --daemonize --no-chroot --no-drop-root --file /etc/avahi/avahi-daemon.conf --no-rlimits 2>/dev/null || true
sleep 1

echo "[shairport:$GRP] Starting nqptp..."
nqptp &
sleep 1

echo "[shairport:$GRP] Starting shairport-sync..."
exec shairport-sync -c "$GRP_DIR/config/shairport-sync.conf" --statistics
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
dhclient -v "$MV_IF" 2>&1 | head -20 || true
sleep 2

# Get and save the IP address
MY_IP=$(ip -4 addr show "$MV_IF" | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
echo "$MY_IP" > "$GRP_DIR/state/owntone_ip.txt"
echo "[owntone:$GRP] Got IP: $MY_IP"

# Private /run for dbus/avahi (OwnTone needs avahi for speaker discovery)
mount -t tmpfs tmpfs /run
mkdir -p /run/dbus /run/avahi-daemon

echo "[owntone:$GRP] Starting dbus..."
dbus-daemon --system --fork --nopidfile
sleep 1

echo "[owntone:$GRP] Starting avahi..."
avahi-daemon --daemonize --no-chroot --no-drop-root --file /etc/avahi/avahi-daemon.conf --no-rlimits 2>/dev/null || true
sleep 1

echo "[owntone:$GRP] Starting OwnTone..."
exec owntone -f -c "$GRP_DIR/config/owntone.conf"
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
  for _ in {1..30}; do
    if exec_in_owntone_netns "$grp" curl -s --connect-timeout 2 "$url" >/dev/null 2>&1; then
      log "OwnTone $grp is ready at $owntone_ip"
      return 0
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
  select_parent_interface
  prompt_group_names

  # Clean up any state from previous runs
  cleanup_previous_run

  log "Setting up directories and FIFOs..."
  setup_directories

  log "Generating configs..."
  for i in "${!GROUPS[@]}"; do
    generate_shairport_config "${GROUPS[$i]}" "$i"
    generate_owntone_config "${GROUPS[$i]}" "$i"
  done

  # Start OwnTone instances FIRST (each in its own namespace with its own IP)
  # This way OwnTone can discover and watch the pipes before shairport writes to them
  log "Starting OwnTone instances in separate namespaces..."
  for grp in "${GROUPS[@]}"; do
    start_owntone_in_netns "$grp"
  done

  # Wait for OwnTone instances to be ready
  for grp in "${GROUPS[@]}"; do
    wait_for_owntone "$grp" || true
  done

  # Trigger library rescan so OwnTone finds the pipes
  log "Triggering library rescan to discover pipes..."
  for grp in "${GROUPS[@]}"; do
    trigger_library_rescan "$grp" || true
  done
  
  # Wait a bit for pipe watching to be set up
  sleep 3

  # Verify pipes were discovered
  echo ""
  echo "========================================"
  echo "  Pipe Discovery Status"
  echo "========================================"
  for grp in "${GROUPS[@]}"; do
    verify_pipe_discovery "$grp" || true
  done

  # NOW start shairport-sync instances (each in its own namespace with its own IP)
  log "Starting shairport-sync instances in separate namespaces..."
  for grp in "${GROUPS[@]}"; do
    start_shairport_in_netns "$grp"
  done

  # Wait for shairport-sync to get IPs
  for grp in "${GROUPS[@]}"; do
    wait_for_shairport "$grp" || true
  done

  # Let user select speakers
  echo ""
  echo "========================================"
  echo "  Speaker Selection"
  echo "========================================"
  for grp in "${GROUPS[@]}"; do
    select_speaker_for_group "$grp"
  done

  echo ""
  log "Demo is running!"
  echo ""
  echo "IPs assigned:"
  for grp in "${GROUPS[@]}"; do
    echo "  - $grp shairport-sync: ${SHAIRPORT_IPS[$grp]:-unknown}"
    echo "  - $grp OwnTone:        ${OWNTONE_IPS[$grp]:-unknown}"
  done
  echo ""
  echo "You should now see these AirPlay endpoints on your iPhone:"
  for i in "${!GROUPS[@]}"; do
    echo "  - ${GROUP_NAMES[$i]}"
  done
  echo ""
  echo "When you play audio to an AirPlay endpoint, OwnTone should auto-start"
  echo "playback to the speaker you selected (pipe_autostart is enabled)."
  echo ""
  echo "OwnTone Web UIs (access from your browser):"
  for grp in "${GROUPS[@]}"; do
    echo "  - $grp: http://${OWNTONE_IPS[$grp]:-<ip>}:3689"
  done
  echo ""
  echo "TROUBLESHOOTING:"
  echo "  1. Check OwnTone wrapper logs:"
  for grp in "${GROUPS[@]}"; do
    echo "     cat $BASE_DIR/groups/$grp/logs/owntone_wrapper.log"
  done
  echo ""
  echo "  2. Check shairport-sync wrapper logs:"
  for grp in "${GROUPS[@]}"; do
    echo "     cat $BASE_DIR/groups/$grp/logs/shairport_wrapper.log"
  done
  echo ""
  echo "  3. Check OwnTone logs for 'pipe' messages:"
  for grp in "${GROUPS[@]}"; do
    echo "     grep -i pipe $BASE_DIR/groups/$grp/logs/owntone.log"
  done
  echo ""
  echo "  4. Check if pipes exist and are FIFOs:"
  for grp in "${GROUPS[@]}"; do
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

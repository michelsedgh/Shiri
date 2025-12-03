#!/bin/bash
###############################################################################
# pause_bridge.sh
#
# Monitors shairport-sync metadata pipe for pause/resume events and instantly
# mutes OwnTone to eliminate the ~2.5 second buffer delay on pause/stop.
#
# PROBLEM: AirPlay 1 has a ~2.5 second audio buffer. When you pause, the audio
#          in the buffer continues playing for ~2.5 seconds before stopping.
#
# SOLUTION: shairport-sync sends 'pfls' (flush) metadata immediately when iOS
#           pauses. We catch this and INSTANTLY mute OwnTone. Then we restore
#           volume after the buffer clears.
#
# METADATA EVENTS:
#   pfls = Flush (pause/stop) - iOS told shairport to stop
#   prsm = Resume - iOS told shairport to resume playing
#   pbeg = Play begin - New playback session started
#   pend = Play end - Playback session ended
#
# USAGE: This script runs in the background, started by dual_zone_demo.sh
#   ./pause_bridge.sh <grp_dir>
#
###############################################################################

GRP_DIR="$1"

if [[ -z "$GRP_DIR" ]]; then
  echo "Usage: $0 <grp_dir>"
  exit 1
fi

LOG="$GRP_DIR/logs/pause_bridge.log"
META_PIPE="$GRP_DIR/pipes/pause_bridge.metadata"
OWNTONE_IP_FILE="$GRP_DIR/state/owntone_ip.txt"
OWNTONE_NETNS_FILE="$GRP_DIR/state/owntone_netns.txt"
SAVED_VOLUME_FILE="$GRP_DIR/state/saved_volume.txt"
LAST_VOL_FILE="$GRP_DIR/state/master_volume_last.txt"  # Written by volume_bridge.sh

# Buffer delay - how long to stay muted after pause
BUFFER_DELAY_SECS=2.5

# PID of the delayed restore job (to cancel if needed)
RESTORE_PID=""

log() {
  echo "[$(date '+%H:%M:%S.%3N')] $*" >> "$LOG"
}

log "========== PAUSE BRIDGE STARTED =========="
log "Monitoring: $META_PIPE"

# Wait for metadata pipe to exist
while [[ ! -p "$META_PIPE" ]]; do
  log "Waiting for metadata pipe..."
  sleep 1
done

# Wait for OwnTone IP
while [[ ! -f "$OWNTONE_IP_FILE" ]]; do
  log "Waiting for OwnTone IP file..."
  sleep 1
done

OWNTONE_IP=$(cat "$OWNTONE_IP_FILE")
log "OwnTone IP: $OWNTONE_IP"

# Read netns name for curl
OWNTONE_NETNS=""
if [[ -f "$OWNTONE_NETNS_FILE" ]]; then
  OWNTONE_NETNS=$(cat "$OWNTONE_NETNS_FILE")
  log "OwnTone netns: $OWNTONE_NETNS"
fi

# Function to run curl (either in netns or on host)
run_curl() {
  if [[ -n "$OWNTONE_NETNS" ]] && ip netns list 2>/dev/null | grep -qw "$OWNTONE_NETNS"; then
    ip netns exec "$OWNTONE_NETNS" curl "$@"
  else
    curl "$@"
  fi
}

# Get current master volume
# Prefer the last value persisted by volume_bridge.sh, fall back to /api/player.
get_current_volume() {
  local vol
  if [[ -f "$LAST_VOL_FILE" ]]; then
    vol=$(cat "$LAST_VOL_FILE" 2>/dev/null)
    if [[ -n "$vol" ]]; then
      echo "$vol"
      return
    fi
  fi

  vol=$(run_curl -s --connect-timeout 2 "http://$OWNTONE_IP:3689/api/player" 2>/dev/null | jq -r '.volume // empty')
  if [[ -n "$vol" ]]; then
    echo "$vol"
  else
    # Last resort: assume 100 but do NOT persist it
    echo "100"
  fi
}

# Set master volume
set_volume() {
  local vol="$1"
  run_curl -s --connect-timeout 2 -X PUT "http://$OWNTONE_IP:3689/api/player/volume?volume=$vol" >/dev/null 2>&1
}

# Cancel any pending restore
cancel_restore() {
  if [[ -n "$RESTORE_PID" ]] && kill -0 "$RESTORE_PID" 2>/dev/null; then
    log "Cancelling pending volume restore (pid $RESTORE_PID)"
    kill "$RESTORE_PID" 2>/dev/null || true
    RESTORE_PID=""
  fi
}

# Instant mute on pause
do_mute() {
  cancel_restore
  
  # Save current volume before muting
  local current_vol
  current_vol=$(get_current_volume)
  
  # Only save if not already muted (avoid saving 0)
  if [[ "$current_vol" -gt 0 ]]; then
    echo "$current_vol" > "$SAVED_VOLUME_FILE"
    log "Saved volume: $current_vol"
  fi
  
  log "MUTING instantly (pause detected)"
  set_volume 0
  
  # Schedule restore after buffer clears
  (
    sleep "$BUFFER_DELAY_SECS"
    
    # Only restore if still muted and not playing
    local saved_vol
    if [[ -f "$SAVED_VOLUME_FILE" ]]; then
      saved_vol=$(cat "$SAVED_VOLUME_FILE")
      log "Restoring volume to $saved_vol after buffer delay"
      set_volume "$saved_vol"
      rm -f "$SAVED_VOLUME_FILE"
    fi
  ) &
  RESTORE_PID=$!
  log "Scheduled restore in ${BUFFER_DELAY_SECS}s (pid $RESTORE_PID)"
}

# Handle resume - cancel restore, audio will naturally start
do_resume() {
  cancel_restore
  
  # Restore volume immediately on resume if we have a saved value
  if [[ -f "$SAVED_VOLUME_FILE" ]]; then
    local saved_vol
    saved_vol=$(cat "$SAVED_VOLUME_FILE")
    log "UNMUTING on resume (restoring to $saved_vol)"
    set_volume "$saved_vol"
    rm -f "$SAVED_VOLUME_FILE"
  else
    # If we have no saved volume, leave current master volume unchanged
    log "Resume detected but no saved volume - leaving volume unchanged"
  fi
}

# Handle play begin - make sure we're unmuted
do_play_begin() {
  cancel_restore
  
  if [[ -f "$SAVED_VOLUME_FILE" ]]; then
    local saved_vol
    saved_vol=$(cat "$SAVED_VOLUME_FILE")
    log "Play begin - restoring volume to $saved_vol"
    set_volume "$saved_vol"
    rm -f "$SAVED_VOLUME_FILE"
  fi
}

# Metadata pipe format (XML):
#   <item><type>73736e63</type><code>70666c73</code><length>N</length></item>
# 
# Type 'ssnc' = 73736e63 (hex for ASCII 'ssnc')
# Codes we care about:
#   'pfls' = 70666c73 (flush/pause)
#   'prsm' = 7072736d (resume)
#   'pbeg' = 70626567 (play begin)
#   'pend' = 70656e64 (play end)

# Outer loop to restart if pipe closes
while true; do
  log "Starting metadata monitor loop..."
  
  # Wait for pipe to exist
  while [[ ! -p "$META_PIPE" ]]; do
    sleep 0.5
  done
  
  # Keep pipe open and read continuously line by line
  while IFS= read -r line; do
    # Skip empty lines
    [[ -z "$line" ]] && continue
    
    # Check for ssnc type (73736e63)
    if [[ "$line" == *"<type>73736e63</type>"* ]]; then
      
      # Log SSNC code for debugging pause behavior
      code_hex=$(echo "$line" | sed -n 's/.*<code>\([0-9a-fA-F]*\)<\/code>.*/\1/p')
      if [[ -n "$code_hex" ]]; then
        log "SSNC event: code=$code_hex"
      fi
      
      # Buffered audio stream paused (paus) - code 70617573
      if [[ "$line" == *"<code>70617573</code>"* ]]; then
        log ">>> PAUS (pause) detected!"
        do_mute
      fi

      # Active mode end (aend) - code 61656e64 (log only)
      if [[ "$line" == *"<code>61656e64</code>"* ]]; then
        log ">>> AEND (active mode end) detected!"
      fi

      # Flush (pause/stop) - code 70666c73 (log only)
      if [[ "$line" == *"<code>70666c73</code>"* ]]; then
        log ">>> PFLS (flush/pause) detected!"
      fi
      
      # Resume - code 7072736d
      if [[ "$line" == *"<code>7072736d</code>"* ]]; then
        log ">>> PRSM (resume) detected!"
        do_resume
      fi
      
      # Play begin - code 70626567
      if [[ "$line" == *"<code>70626567</code>"* ]]; then
        log ">>> PBEG (play begin) detected!"
        do_play_begin
      fi
      
      # Play end - code 70656e64 (log only)
      if [[ "$line" == *"<code>70656e64</code>"* ]]; then
        log ">>> PEND (play end) detected!"
      fi
    fi
  done < "$META_PIPE"
  
  log "Metadata pipe closed, restarting in 1s..."
  sleep 1
done

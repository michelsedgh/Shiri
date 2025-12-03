#!/bin/bash
###############################################################################
# volume_bridge.sh
#
# Called by shairport-sync when AirPlay volume changes.
# Converts AirPlay volume to OwnTone volume and applies it instantly.
#
# USAGE: This script is called by shairport-sync with the volume appended:
#   /path/to/volume_bridge.sh <grp_dir> <volume>
#
# VOLUME SCALES:
#   AirPlay/iOS:  0.0 (max) to -30.0 (min), -144.0 = mute
#   OwnTone API:  100 (max) to 0 (min)
#
# VOLUME MAPPING ANALYSIS:
#   After analyzing shairport-sync source (common.c) and OwnTone source
#   (outputs/airplay.c, outputs/alsa.c), here's what I found:
#
#   1. AirPlay volume (-30 to 0) is already in dB (logarithmic/perceptual)
#   2. iOS slider is designed so equal movements = equal perceived loudness
#   3. OwnTone uses LINEAR conversion for AirPlay outputs:
#        airplay_vol = -30 + (owntone_pct * 30 / 100)
#   4. For ALSA outputs, OwnTone applies its own perceptual curve
#
#   Therefore: LINEAR MAPPING is correct. It preserves iOS's intent.
#
# WHY THIS WORKS:
#   shairport-sync volume changes happen after the audio buffer delay.
#   By setting ignore_volume_control=yes, audio stays at 100%.
#   This script intercepts volume events and applies them INSTANTLY via OwnTone.
###############################################################################

GRP_DIR="$1"
AIRPLAY_VOL="$2"

LOG="$GRP_DIR/logs/volume_bridge.log"
OWNTONE_IP_FILE="$GRP_DIR/state/owntone_ip.txt"
OWNTONE_NETNS_FILE="$GRP_DIR/state/owntone_netns.txt"

log() {
  echo "[$(date '+%H:%M:%S.%3N')] $*" >> "$LOG"
}

# Read OwnTone IP
if [[ ! -f "$OWNTONE_IP_FILE" ]]; then
  log "ERROR: OwnTone IP file not found: $OWNTONE_IP_FILE"
  exit 1
fi
OWNTONE_IP=$(cat "$OWNTONE_IP_FILE")

# Read netns name (for curl exec)
OWNTONE_NETNS=""
if [[ -f "$OWNTONE_NETNS_FILE" ]]; then
  OWNTONE_NETNS=$(cat "$OWNTONE_NETNS_FILE")
fi

log "Volume event: AirPlay volume = $AIRPLAY_VOL"

# Handle mute (-144.0 is AirPlay mute)
if [[ "$AIRPLAY_VOL" == "-144"* ]]; then
  log "MUTE detected"
  OWNTONE_VOL=0
else
  # Convert AirPlay volume (-30 to 0) to OwnTone volume (0 to 100)
  #
  # LINEAR MAPPING (matches OwnTone's internal conversion):
  #   AirPlay -30 (silent) -> OwnTone 0
  #   AirPlay -20          -> OwnTone 33
  #   AirPlay -15 (middle) -> OwnTone 50
  #   AirPlay -10          -> OwnTone 67
  #   AirPlay -5           -> OwnTone 83
  #   AirPlay 0 (max)      -> OwnTone 100
  #
  # This is correct because OwnTone converts back linearly for AirPlay speakers:
  #   airplay = -30 + (owntone * 30 / 100)
  #
  OWNTONE_VOL=$(awk -v av="$AIRPLAY_VOL" 'BEGIN {
    # Clamp to valid AirPlay range
    if (av < -30) av = -30
    if (av > 0) av = 0
    
    # Linear mapping
    vol = ((av + 30) / 30) * 100
    
    # Round to nearest integer
    printf "%.0f", vol
  }')
fi

log "Mapped to OwnTone volume: $OWNTONE_VOL"

# Function to run curl (either in netns or on host)
run_curl() {
  if [[ -n "$OWNTONE_NETNS" ]] && ip netns list 2>/dev/null | grep -qw "$OWNTONE_NETNS"; then
    ip netns exec "$OWNTONE_NETNS" curl "$@"
  else
    curl "$@"
  fi
}

# Set MASTER volume (preserves ratio between individual speakers)
# Using /api/player/volume without output_id changes the master volume
# This is different from setting individual output volumes which would break the ratio
log "Setting OwnTone MASTER volume to $OWNTONE_VOL (preserves speaker ratio)"

RESULT=$(run_curl -s --connect-timeout 2 -X PUT \
  "http://$OWNTONE_IP:3689/api/player/volume?volume=$OWNTONE_VOL" 2>&1)

if [[ $? -eq 0 ]]; then
  log "SUCCESS: Master volume set to $OWNTONE_VOL"
else
  log "ERROR: Failed to set master volume: $RESULT"
  exit 1
fi

log "Volume bridge complete"

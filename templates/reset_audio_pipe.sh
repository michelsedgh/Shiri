#!/bin/bash
# Reset audio pipeline completely for sync
# Called by shairport-sync BEFORE play begins
# CRITICAL: Must flush OwnTone buffers to prevent cumulative drift!

PIPE="%%GRP_DIR%%/pipes/audio.pipe"
LOG="%%GRP_DIR%%/logs/sync_reset.log"
TIMESTAMP=$(date '+%H:%M:%S.%3N')

echo "" >> "$LOG"
echo "[$TIMESTAMP] ========== SYNC RESET TRIGGERED ==========" >> "$LOG"

# Step 1: STOP OwnTone playback to flush its internal buffers
# This is CRITICAL - without this, OwnTone accumulates delay each reconnect
echo "[$TIMESTAMP] Stopping OwnTone playback (flush buffers)" >> "$LOG"
curl -s -X PUT "http://127.0.0.1:%%OWNTONE_PORT%%/api/player/stop" --connect-timeout 1 >> "$LOG" 2>&1 || true
echo "[$TIMESTAMP] OwnTone stopped" >> "$LOG"

# Step 2: Leave the GStreamer mixer running.
# The mixer owns FIFO reconnects. Killing or draining the active FIFO writer here
# races with OwnTone and can cause choppy playback or a stuck pipe.
if [[ -p "$PIPE" ]]; then
  echo "[$TIMESTAMP] Mixer FIFO left intact for GStreamer reconnect" >> "$LOG"
fi

echo "[$TIMESTAMP] Reset complete - pipeline flushed" >> "$LOG"
echo "[$TIMESTAMP] ==========================================" >> "$LOG"

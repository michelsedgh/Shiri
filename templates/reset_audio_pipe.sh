#!/bin/bash
# Reset audio pipeline completely for sync
# Called by shairport-sync BEFORE play begins
# CRITICAL: Must flush OwnTone buffers to prevent cumulative drift!

PIPE="%%GRP_DIR%%/pipes/audio.pipe"
ARECORD_PID_FILE="%%GRP_DIR%%/state/arecord.pid"
LOG="%%GRP_DIR%%/logs/sync_reset.log"
TIMESTAMP=$(date '+%H:%M:%S.%3N')

echo "" >> "$LOG"
echo "[$TIMESTAMP] ========== SYNC RESET TRIGGERED ==========" >> "$LOG"

# Step 1: STOP OwnTone playback to flush its internal buffers
# This is CRITICAL - without this, OwnTone accumulates delay each reconnect
echo "[$TIMESTAMP] Stopping OwnTone playback (flush buffers)" >> "$LOG"
curl -s -X PUT "http://127.0.0.1:%%OWNTONE_PORT%%/api/player/stop" --connect-timeout 1 >> "$LOG" 2>&1 || true
echo "[$TIMESTAMP] OwnTone stopped" >> "$LOG"

# Step 2: Kill arecord to stop writing to pipe
if [[ -f "$ARECORD_PID_FILE" ]]; then
  ARECORD_PID=$(cat "$ARECORD_PID_FILE")
  echo "[$TIMESTAMP] Found arecord PID file: $ARECORD_PID" >> "$LOG"
  if kill -0 "$ARECORD_PID" 2>/dev/null; then
    echo "[$TIMESTAMP] Killing arecord (pid $ARECORD_PID)" >> "$LOG"
    kill -TERM "$ARECORD_PID" 2>/dev/null || true
    sleep 0.1
    echo "[$TIMESTAMP] arecord killed" >> "$LOG"
  else
    echo "[$TIMESTAMP] arecord (pid $ARECORD_PID) not running" >> "$LOG"
  fi
else
  echo "[$TIMESTAMP] WARNING: arecord PID file not found!" >> "$LOG"
fi

# Step 3: Drain any buffered data from the pipe
if [[ -p "$PIPE" ]]; then
  DRAINED=$(timeout 0.2 dd if="$PIPE" of=/dev/null bs=65536 iflag=nonblock 2>&1 | grep -oP '\d+ bytes' || echo "0 bytes")
  echo "[$TIMESTAMP] Drained pipe: $DRAINED" >> "$LOG"
fi

echo "[$TIMESTAMP] Reset complete - pipeline flushed" >> "$LOG"
echo "[$TIMESTAMP] ==========================================" >> "$LOG"

#!/bin/bash
while true; do
  # Clear stale loopback data before each session
  timeout 0.1 arecord -D "%%CAPTURE_DEV%%" -f cd -c 2 -t raw -d 1 2>/dev/null >/dev/null || true

  # Start arecord with small buffer for low latency
  arecord -D "%%CAPTURE_DEV%%" -f cd -c 2 -t raw --buffer-size=2048 --period-size=512 2>/dev/null > "%%GRP_DIR%%/pipes/audio.pipe" &
  ARECORD_INNER_PID=$!

  # Write the actual arecord PID for sync reset script
  echo "$ARECORD_INNER_PID" > "%%GRP_DIR%%/state/arecord.pid"

  # Wait for arecord to exit (killed by sync reset or pipe close)
  wait $ARECORD_INNER_PID 2>/dev/null || true

  echo "[$(date '+%H:%M:%S')] arecord exited, restarting in 0.3s..." >&2
  sleep 0.3
done

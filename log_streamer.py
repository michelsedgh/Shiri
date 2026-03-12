"""
log_streamer.py — Real-time log file tailing for Shiri zones.

Watches all log files in /var/lib/shiri/groups/<zone>/logs/ and streams
new lines to connected browsers via Flask-SocketIO.
"""

import logging
import os
import threading
import time

log = logging.getLogger("shiri.logs")

BASE_DIR = "/var/lib/shiri"

# Log types that can exist per zone
LOG_TYPES = [
    "shairport",
    "owntone",
    "owntone_wrapper",
    "arecord",
    "pause_bridge",
    "volume_bridge",
    "sync_reset",
]


class LogStreamer:
    """Tails zone log files and emits lines via SocketIO."""

    def __init__(self, socketio):
        self.socketio = socketio
        self._watchers = {}  # zone_id -> thread
        self._stop_events = {}  # zone_id -> Event
        self._lock = threading.Lock()

    def start_watching(self, zone_id):
        """Start tailing logs for a zone."""
        with self._lock:
            if zone_id in self._watchers:
                return  # Already watching

            stop_event = threading.Event()
            self._stop_events[zone_id] = stop_event

            t = threading.Thread(
                target=self._watch_zone_logs,
                args=(zone_id, stop_event),
                daemon=True,
                name=f"logwatch-{zone_id}",
            )
            self._watchers[zone_id] = t
            t.start()
            log.info("Started log watcher for %s", zone_id)

    def stop_watching(self, zone_id):
        """Stop tailing logs for a zone."""
        with self._lock:
            stop_event = self._stop_events.pop(zone_id, None)
            self._watchers.pop(zone_id, None)

        if stop_event:
            stop_event.set()
            log.info("Stopped log watcher for %s", zone_id)

    def stop_all(self):
        """Stop all log watchers."""
        with self._lock:
            zone_ids = list(self._stop_events.keys())
        for zid in zone_ids:
            self.stop_watching(zid)

    def get_recent_logs(self, zone_id, log_type, lines=100):
        """Read the last N lines from a log file."""
        log_path = os.path.join(BASE_DIR, "groups", zone_id, "logs", f"{log_type}.log")
        if not os.path.exists(log_path):
            return []
        try:
            with open(log_path, "r", errors="replace") as f:
                all_lines = f.readlines()
            return [l.rstrip("\n") for l in all_lines[-lines:]]
        except IOError:
            return []

    def _watch_zone_logs(self, zone_id, stop_event):
        """Tail all log files for a zone, emit new lines via SocketIO."""
        logs_dir = os.path.join(BASE_DIR, "groups", zone_id, "logs")

        # Track file positions
        positions = {}

        while not stop_event.is_set():
            if not os.path.isdir(logs_dir):
                time.sleep(2)
                continue

            for log_type in LOG_TYPES:
                log_path = os.path.join(logs_dir, f"{log_type}.log")
                if not os.path.exists(log_path):
                    continue

                try:
                    file_size = os.path.getsize(log_path)
                    last_pos = positions.get(log_path, 0)

                    # If file was truncated/recreated, reset position
                    if file_size < last_pos:
                        last_pos = 0

                    if file_size > last_pos:
                        with open(log_path, "r", errors="replace") as f:
                            f.seek(last_pos)
                            new_data = f.read()
                            new_pos = f.tell()

                        positions[log_path] = new_pos

                        for line in new_data.splitlines():
                            if line.strip():
                                self.socketio.emit("zone_log", {
                                    "zone_id": zone_id,
                                    "log_type": log_type,
                                    "line": line,
                                    "timestamp": time.time(),
                                })
                except IOError:
                    pass

            # Poll interval — 500ms for responsive log viewing
            stop_event.wait(0.5)

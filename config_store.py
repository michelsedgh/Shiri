"""
config_store.py — Persistent zone configuration storage.

Saves/loads zone definitions to /var/lib/shiri/config.json so zones
survive daemon restarts.
"""

import json
import os
import threading
import time

CONFIG_PATH = "/var/lib/shiri/config.json"


class ConfigStore:
    """Thread-safe JSON config store for zone definitions."""

    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = {"zones": {}, "settings": {"default_interface": ""}}
        self._load()

    def _load(self):
        """Load config from disk."""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        # Ensure structure
        self._data.setdefault("zones", {})
        self._data.setdefault("settings", {"default_interface": ""})

    def _save(self):
        """Write config to disk."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    # -- Zone CRUD --

    def list_zones(self):
        """Return dict of all zone configs keyed by zone_id."""
        with self._lock:
            return dict(self._data["zones"])

    def get_zone(self, zone_id):
        """Return config for a single zone, or None."""
        with self._lock:
            return self._data["zones"].get(zone_id)

    def save_zone(self, zone_id, config):
        """Create or update a zone config."""
        with self._lock:
            self._data["zones"][zone_id] = config
            self._save()

    def delete_zone(self, zone_id):
        """Remove a zone config."""
        with self._lock:
            self._data["zones"].pop(zone_id, None)
            self._save()

    # -- Settings --

    def get_settings(self):
        with self._lock:
            return dict(self._data.get("settings", {}))

    def update_settings(self, settings):
        with self._lock:
            self._data["settings"].update(settings)
            self._save()

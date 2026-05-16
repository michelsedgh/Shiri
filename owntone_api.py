"""
owntone_api.py — OwnTone REST API client for Shiri zones.

All zone OwnTone instances now run on host-network ports. Calls go directly
to the host listener for the zone.
"""

import json
import subprocess
import logging

log = logging.getLogger("shiri.owntone")


def _run_curl(url, method="GET", data=None, timeout=5):
    """Run curl against a host-network OwnTone API endpoint."""
    curl_cmd = ["curl", "-s", "--connect-timeout", str(timeout)]

    if method == "PUT":
        curl_cmd += ["-X", "PUT"]
    elif method == "POST":
        curl_cmd += ["-X", "POST"]

    if data is not None:
        curl_cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]

    curl_cmd.append(url)

    try:
        result = subprocess.run(
            curl_cmd, capture_output=True, text=True, timeout=timeout + 5
        )
        if result.returncode != 0:
            log.warning("curl failed (rc=%d): %s", result.returncode, result.stderr)
            return None
        if not result.stdout.strip():
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        log.warning("curl timed out: %s", url)
        return None
    except json.JSONDecodeError:
        log.warning("Invalid JSON from %s: %s", url, result.stdout[:200])
        return None
    except Exception as e:
        log.error("curl error: %s", e)
        return None


class OwnToneAPI:
    """OwnTone REST API client for a single host-network zone."""

    def __init__(self, owntone_ip, port=3689):
        self.owntone_ip = owntone_ip
        self.port = int(port)
        self.base_url = f"http://{owntone_ip}:{self.port}"

    def _api(self, path, method="GET", data=None):
        """Call OwnTone API endpoint."""
        url = f"{self.base_url}{path}"
        return _run_curl(url, method=method, data=data)

    # -- Config / Health --

    def is_ready(self):
        """Check if OwnTone API is responding."""
        result = self._api("/api/config")
        return result is not None

    # -- Outputs / Speakers --
    # Same logic as select_speaker_for_group() in dual_zone_demo.sh

    def get_outputs(self):
        """
        List all discovered outputs (speakers).
        Returns list of dicts with id, name, type, selected, has_video, volume.
        """
        result = self._api("/api/outputs")
        if not result or "outputs" not in result:
            return []
        return result["outputs"]

    def set_outputs(self, output_ids):
        """
        Enable specific outputs (same as /api/outputs/set in dual_zone_demo.sh).
        Disables all outputs not in the list.
        """
        data = {"outputs": [str(oid) for oid in output_ids]}
        return self._api("/api/outputs/set", method="PUT", data=data)

    def enable_output(self, output_id):
        """Enable a single output (speaker)."""
        return self._api(f"/api/outputs/{output_id}", method="PUT",
                         data={"selected": True})

    def disable_output(self, output_id):
        """Disable a single output (speaker)."""
        return self._api(f"/api/outputs/{output_id}", method="PUT",
                         data={"selected": False})

    def set_output_volume(self, output_id, volume):
        """Set volume for a specific output (0-100)."""
        return self._api(f"/api/outputs/{output_id}", method="PUT",
                         data={"volume": int(volume)})

    # -- Player --

    def get_player_status(self):
        """Get player status (play_status, volume, etc.)."""
        return self._api("/api/player")

    def get_volume(self):
        """Get current master volume (0-100)."""
        result = self._api("/api/player")
        if result:
            return result.get("volume", 0)
        return 0

    def set_volume(self, volume):
        """
        Set master volume (preserves speaker ratio).
        Same as volume_bridge.sh's curl call.
        """
        return self._api(f"/api/player/volume?volume={int(volume)}", method="PUT")

    def play(self):
        """Ask OwnTone to play the current pipe/queue."""
        return self._api("/api/player/play", method="PUT")

    # -- Library --
    # Same as trigger_library_rescan() in dual_zone_demo.sh

    def rescan_library(self):
        """Trigger library rescan to discover pipes."""
        return self._api("/api/update", method="PUT")

    def get_tracks(self, limit=100):
        """Get library tracks (to verify pipe discovery)."""
        result = self._api(f"/api/library/tracks?limit={limit}")
        if result and "items" in result:
            return result["items"]
        return []

    def verify_pipe(self):
        """
        Check if OwnTone discovered the audio pipe.
        Same logic as verify_pipe_discovery() in dual_zone_demo.sh.
        """
        tracks = self.get_tracks()
        pipe_tracks = [t for t in tracks if "audio.pipe" in t.get("path", "")]
        return len(pipe_tracks) > 0, pipe_tracks

    # -- Queue --

    def get_queue(self):
        """Get current playback queue."""
        return self._api("/api/queue")

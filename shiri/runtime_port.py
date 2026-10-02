"""Backend boundary: control logic does not manipulate Linux resources."""
from pathlib import Path

from shiri.rpc import call_rpc, RpcError


class SocketRuntime:
    def __init__(self, socket_path: Path):
        self.socket_path = socket_path

    async def call(self, operation: str, payload: dict | None = None):
        return await call_rpc(self.socket_path, operation, payload)


class SimulatedRuntime:
    """Explicitly labelled development backend; never accesses a physical speaker."""
    def __init__(self):
        self.rooms = {}
        self.selected = {}
        self.sessions = {}

    async def call(self, operation: str, payload: dict | None = None):
        payload = payload or {}
        if operation == "reconcile":
            self.rooms = {room["id"]: room for room in payload["rooms"]}
            self.selected = {room["id"]: [s["id"] for s in room["speakers"]] for room in payload["rooms"]}
        if operation in {"health", "reconcile"}:
            return {"ready": True, "simulation": True, "rooms": [
                {"room_id": room["id"], "status": "running" if room["enabled"] else "stopped",
                 "error": None, "receiver_ip": None, "owntone_url": None,
                 "message": "Simulation; no hardware audio"} for room in self.rooms.values()]}
        if operation == "interfaces":
            return {"interfaces": ["sim0"]}
        if operation == "local_devices":
            return {"devices": [], "simulation": True}
        if operation == "bind_local_device":
            raise RpcError("unsupported", "Simulation cannot verify or bind physical audio hardware")
        room_id = payload.get("room_id")
        room = self.rooms.get(room_id)
        if not room:
            raise RpcError("not_found", "Room is not known to the runtime")
        if operation == "outputs":
            return {"outputs": [{"id": sid, "name": name, "protocol": protocol,
                                 "selected": sid in self.selected.get(room_id, []), "available": True,
                                 "volume": room["volume"] * next((s.get("balance_percent", 100) for s in room["speakers"] if s["id"] == sid), 100) // 100,
                                 "balance_percent": next((s.get("balance_percent", 100) for s in room["speakers"] if s["id"] == sid), 100),
                                 "sync_quality": quality,
                                 "offset_ms": next((s["offset_ms"] for s in room["speakers"] if s["id"] == sid), 0),
                                 "assignable": sid != "0" or room.get("local_audio_device") is not None}
                                for sid, name, protocol, quality in [
                                    ("101", "Simulated AirPlay", "airplay2", "protocol_timed"),
                                    ("202", "Simulated Google Cast", "chromecast", "approximate"),
                                    ("0", "Simulated local audio", "alsa", "device_dependent")]]}
        if operation == "set_outputs":
            self.selected[room_id] = [speaker["id"] for speaker in payload["speakers"]]
            return {"ok": True}
        if operation in {"player", "set_volume"}:
            return {"ok": True, "volume": room["volume"], "play_status": "playing"}
        if operation == "speech":
            if payload.get("action") == "close":
                return {"ok": True}
            raise RpcError("unsupported", "Simulation does not negotiate or play real audio")
        if operation == "diagnostics":
            return {"simulation": True, "entries": []}
        raise RpcError("unsupported", "Unknown runtime operation")

"""OwnTone control stays in OwnTone; every mutation requires an HTTP acknowledgment."""

from __future__ import annotations

import httpx
import asyncio

from shiri.deadline import bounded

from .system import RuntimeFailure

PROTOCOLS = {
    "AirPlay": "airplay1",
    "AirPlay 1": "airplay1",
    "AirPlay 2": "airplay2",
    "Chromecast": "chromecast",
    "ALSA": "alsa",
    "Pulseaudio": "pulseaudio",
    "PulseAudio": "pulseaudio",
}


def family(protocol: str):
    return "airplay" if protocol in {"airplay1", "airplay2"} else protocol


def normalize_output(raw: dict, excluded_names: set[str]):
    output_type = raw.get("type")
    if not isinstance(output_type, str):
        return None
    protocol = PROTOCOLS.get(output_type)
    try:
        output_id = str(raw["id"])
        if not output_id.isdigit() or not 0 <= int(output_id) <= (1 << 64) - 1:
            return None
        output_id = str(int(output_id))
    except (KeyError, ValueError, TypeError):
        return None
    name = raw.get("name")
    if (not isinstance(name, str) or not 1 <= len(name) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        return None
    for key in ("selected", "requires_auth", "needs_auth_key", "has_password"):
        if key in raw and type(raw[key]) is not bool:
            return None
    if raw.get("volume") is not None and (type(raw["volume"]) is not int or not 0 <= raw["volume"] <= 100):
        return None
    if "offset_ms" in raw and (type(raw["offset_ms"]) is not int or not -2000 <= raw["offset_ms"] <= 2000):
        return None
    if "balance_percent" in raw and (type(raw["balance_percent"]) is not int or not 0 <= raw["balance_percent"] <= 100):
        return None
    formats = raw.get("supported_formats", [])
    if (not isinstance(raw.get("format", ""), str) or not isinstance(formats, list) or len(formats) > 32
            or any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in formats)):
        return None
    assignable = protocol is not None and name not in excluded_names
    reason = (
        ""
        if assignable
        else (
            "Shiri room receiver cannot be its own output"
            if name in excluded_names
            else "Unsupported backend output"
        )
    )
    sync = "native" if family(protocol or "") == "airplay" else "approximate"
    return {
        "id": output_id,
        "name": name,
        "protocol": protocol,
        "selected": bool(raw.get("selected")),
        "volume": raw.get("volume"),
        "offset_ms": raw.get("offset_ms", 0),
        "balance_percent": raw.get("balance_percent", 100),
        "assignable": assignable,
        "reason": reason,
        "synchronization": sync,
        "requires_auth": bool(raw.get("requires_auth") or raw.get("needs_auth_key")),
        "format": raw.get("format", ""),
        "supported_formats": raw.get("supported_formats", []),
    }


class OwnToneRejected(RuntimeFailure):
    """Typed backend rejection; carries no credentials or response contents."""
    def __init__(self, method: str, path: str, status_code: int):
        self.method, self.path, self.status_code = method, path, status_code
        super().__init__(f"OwnTone rejected {method} {path} (HTTP {status_code})")


class OwnToneClient:
    def __init__(self, base_url: str, *, transport=None, password: str | None = None, deadline=5.0):
        self.base_url = base_url
        self.deadline = deadline
        self.client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(4, connect=2),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            trust_env=False,
            transport=transport,
            auth=("admin", password) if password else None,
        )

    async def close(self):
        await self.client.aclose()

    async def request(self, method: str, path: str, *, json=None, params=None):
        # httpx read timeouts measure idle time between chunks. A slow trickle
        # must not keep a room actor or the health monitor waiting indefinitely.
        try:
            return await bounded(
                self._exchange(method, path, json=json, params=params), self.deadline
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("OwnTone exceeded its total control request deadline") from exc

    async def _exchange(self, method: str, path: str, *, json=None, params=None):
        try:
            async with self.client.stream(method, path, json=json, params=params) as response:
                if not 200 <= response.status_code < 300:
                    raise OwnToneRejected(method, path, response.status_code)
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        raise RuntimeFailure("OwnTone response exceeded its size limit")
                if response.status_code == 204 or not content:
                    return {"ok": True}
                import json as json_module

                try:
                    result = json_module.loads(content)
                except ValueError as exc:
                    raise RuntimeFailure("OwnTone returned invalid JSON") from exc
                if not isinstance(result, dict):
                    raise RuntimeFailure("OwnTone response is not an object")
                return result
        except httpx.HTTPError as exc:
            raise RuntimeFailure(
                f"OwnTone is unavailable at its private control endpoint: {exc.__class__.__name__}"
            ) from exc

    async def outputs(self, excluded_names: set[str]):
        result = await self.request("GET", "/api/outputs")
        rows = result.get("outputs")
        if not isinstance(rows, list) or len(rows) > 512:
            raise RuntimeFailure("OwnTone did not return an output list")
        outputs, identities = [], set()
        for raw in rows:
            output = normalize_output(raw, excluded_names) if isinstance(raw, dict) else None
            if output is None or output["id"] in identities:
                raise RuntimeFailure("OwnTone returned malformed or duplicate output identities; selection is unconfirmed")
            identities.add(output["id"])
            outputs.append(output)
        return outputs

    async def select(self, speakers: list, outputs: list[dict]):
        by_id = {output["id"]: output for output in outputs}
        for speaker in speakers:
            output = by_id.get(speaker.id)
            if (
                not output
                or not output["assignable"]
                or output["name"] != speaker.name
                or family(output["protocol"]) != family(speaker.protocol)
            ):
                raise RuntimeFailure(f"Saved speaker '{speaker.name}' is unavailable or its identity changed")
            if output.get("requires_auth"):
                raise RuntimeFailure(f"Speaker '{speaker.name}' requires OwnTone device authorization")
        changed = [speaker for speaker in speakers if by_id[speaker.id].get("offset_ms") != speaker.offset_ms]
        resume = False
        try:
            if changed:
                player = await self.request("GET", "/api/player")
                if player.get("state") == "play":
                    await self.request("PUT", "/api/player/pause")
                    resume = True
                    for _ in range(10):
                        if (await self.request("GET", "/api/player")).get("state") == "pause":
                            break
                        await asyncio.sleep(0.1)
                    else:
                        raise RuntimeFailure("OwnTone did not pause; calibration remains unapplied")
            # OwnTone 29.3 stops each paused output session when changing its
            # offset. This makes the setting effective on resume, not just saved.
            for speaker in changed:
                await self.request("PUT", f"/api/outputs/{speaker.id}", json={"offset_ms": speaker.offset_ms})
                readback = await self.request("GET", f"/api/outputs/{speaker.id}")
                if readback.get("offset_ms") != speaker.offset_ms:
                    raise RuntimeFailure(
                        f"OwnTone did not retain the requested timing offset for '{speaker.name}'"
                    )
            await self.request(
                "PUT", "/api/outputs/set", json={"outputs": [speaker.id for speaker in speakers]}
            )
            observed = await self.outputs(set())
            selected = {output["id"] for output in observed if output["selected"]}
            if selected != {speaker.id for speaker in speakers}:
                raise RuntimeFailure("OwnTone did not retain the requested speaker selection")
        finally:
            if resume:
                await self.request("PUT", "/api/player/play")
        return {"ok": True, "playback_restarted": resume, "offsets_applied": True}

    async def volume(self, value: int):
        await self.request("PUT", "/api/player/volume", params={"volume": value})
        return {"ok": True}

    async def volume_settings(self, value: int, speakers: list):
        # Stage trims before selection, so an output cannot start at an old gain.
        if type(value) is not int or not 0 <= value <= 100:
            raise RuntimeFailure("Room volume must be an integer from 0 to 100")
        body = {"volume": value, "outputs": [{"id": speaker.id, "balance_percent": speaker.balance_percent}
                                            for speaker in speakers]}
        reply = await self.request("POST", "/api/player/shiri-volume-settings", json=body)
        if reply != body:
            raise RuntimeFailure("OwnTone did not acknowledge the requested room volume and speaker balances")
        player = await self.request("GET", "/api/player")
        if player.get("volume") != value:
            raise RuntimeFailure("OwnTone did not retain the requested room volume")
        observed = {output["id"]: output for output in await self.outputs(set())}
        if any(speaker.id not in observed or observed[speaker.id]["balance_percent"] != speaker.balance_percent
               for speaker in speakers):
            raise RuntimeFailure("OwnTone did not retain the requested speaker balances")
        return {"ok": True}

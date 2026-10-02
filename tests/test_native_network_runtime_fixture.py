"""Exercise actual fixture payloads through the production speech dispatcher.

Peer and spectrum doubles isolate the request/cleanup contract. No namespaces,
network sockets, services, or real PCM are created; this is not a gate receipt.
"""
import ast
import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

from aiortc import RTCConfiguration, RTCSessionDescription
import pytest

from shiri.rpc import RpcError
from shiri.runtime.audio import AudioWorker
from shiri.runtime.system import RuntimeFailure


PATH = Path(__file__).with_name("linux")/"check_native_network.py"
TREE = ast.parse(PATH.read_text(), filename=str(PATH))
NODES = [node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
         and node.name in {"Rig", "speech_request", "eventually"}]
assert {node.name for node in NODES} == {"Rig", "speech_request", "eventually"}


def require(condition, message):
    if not condition:
        raise RuntimeFailure(message)


NAMESPACE = {"AudioWorker": AudioWorker, "asyncio": asyncio, "uuid4": uuid4, "time": time,
             "sys": sys, "RuntimeFailure": RuntimeFailure, "require": require}
exec(compile(ast.Module(body=NODES, type_ignores=[]), str(PATH), "exec"), NAMESPACE)
Rig, speech_request = NAMESPACE["Rig"], NAMESPACE["speech_request"]

SDP = "\r\n".join([
    "v=0", "o=- 1 1 IN IP4 127.0.0.1", "s=-", "t=0 0", "a=group:BUNDLE 0",
    "m=audio 9 UDP/TLS/RTP/SAVPF 111", "c=IN IP4 0.0.0.0", "a=mid:0",
    "a=sendonly", "a=rtpmap:111 opus/48000/2", "",
])


class Mixer:
    def close(self):
        pass


class Peer:
    def __init__(self):
        self.connectionState = "new"
        self.handlers = {}
        self.close_calls = 0
        self.remote_error = None
        self.localDescription = SimpleNamespace(sdp=SDP, type="offer")

    def on(self, name):
        def register(callback):
            self.handlers[name] = callback
            return callback
        return register

    def addTransceiver(self, _track, *, direction):
        assert direction == "sendonly"
        return SimpleNamespace(setCodecPreferences=lambda _codecs: None)

    async def createOffer(self):
        return SimpleNamespace(sdp=SDP, type="offer")

    async def createAnswer(self):
        return SimpleNamespace(sdp="inert-answer", type="answer")

    async def setLocalDescription(self, description):
        self.localDescription = description

    async def setRemoteDescription(self, _description):
        if self.remote_error:
            raise self.remote_error

    async def close(self):
        self.close_calls += 1
        self.connectionState = "closed"


class Broker:
    def __init__(self, worker):
        self.worker = worker
        self.requests = []
        self.close_error = None
        self.stopped = []
        self.network = SimpleNamespace(manifest={"processes": {}})

    async def speech(self, _room, payload):
        self.requests.append(dict(payload))
        # Both offer and every retirement traverse the real validator/parser/
        # ownership dispatcher; only the RTC peer is inert.
        AudioWorker.identity(payload)
        if payload["action"] == "close" and self.close_error:
            raise self.close_error
        return await self.worker.dispatch("speech", payload)

    async def _stop_reserved_units(self, owner):
        self.stopped.append(owner)


def prepare_rig(tmp_path, monkeypatch, state="play"):
    server, producer = Peer(), Peer()
    worker = AudioWorker(Mixer(), peer_factory=lambda: server)
    broker = Broker(worker)
    rig = Rig(broker, tmp_path/"rig", "inert-fixture")
    rig.room = object()
    owner = None if state == "stop" else "exact-music-owner"
    health = {"source": {"owner": owner}, "native_generation": 7, "native_blocks": 10}
    calls = 0

    async def source_request(method, path):
        nonlocal calls
        assert (method, path) == ("GET", "/api/player")
        calls += 1
        return {"state": state, "item_id": None if state == "stop" else 17,
                "item_progress_ms": calls*1000}

    async def healthy():
        return health

    async def client_close():
        pass

    rig.health = rig.healthy = healthy
    rig.source_client = SimpleNamespace(request=source_request, close=client_close)
    rig.observer = SimpleNamespace(registry=SimpleNamespace(spectrum=lambda **_: {
        "speech_880_amplitude": 100, "music_440_amplitude": 100 if state == "play" else 0,
        "minimum_presentation_lead_ns": 10_000_000}))
    monkeypatch.setitem(NAMESPACE, "RTCPeerConnection", lambda _config: producer)
    monkeypatch.setitem(NAMESPACE, "RTCConfiguration", RTCConfiguration)
    monkeypatch.setitem(NAMESPACE, "RTCSessionDescription", RTCSessionDescription)
    monkeypatch.setitem(NAMESPACE, "SpeechTone", lambda: SimpleNamespace(audible=True))
    monkeypatch.setitem(NAMESPACE, "RTCRtpSender", SimpleNamespace(getCapabilities=lambda _kind:
                        SimpleNamespace(codecs=[SimpleNamespace(mimeType="audio/opus")])))
    return rig, broker, worker, server, producer


def test_request_helper_creates_distinct_valid_operation_identities():
    payloads = [speech_request("network-session", action) for action in ["offer", "close", "close"]]
    assert len({payload["request_id"] for payload in payloads}) == 3
    assert all(AudioWorker.identity(payload) == 0.28 for payload in payloads)
    assert all(payload["session_id"] == "network-session" for payload in payloads)


@pytest.mark.parametrize("session", ["", "bad session", "x"*129])
def test_invalid_session_fails_actual_worker_identity_before_dispatch(session):
    with pytest.raises(RpcError, match="session_id"):
        speech_request(session, "close")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["play", "pause", "stop"])
async def test_full_fixture_speech_offer_and_close_dispatch_actual_worker(tmp_path, monkeypatch, state):
    rig, broker, worker, server, producer = prepare_rig(tmp_path, monkeypatch, state)
    try:
        result = await rig.speech(label="inert contract", paused=state != "play", baseline=400)
        assert result["source_state"] == state and result["source_owner_preserved"] is True
        assert [payload["action"] for payload in broker.requests] == ["offer", "close"]
        offer, close = broker.requests
        assert offer["session_id"] == close["session_id"]
        assert offer["request_id"] != close["request_id"]
        assert offer["sdp"] == SDP and offer["type"] == "offer"
        assert server.close_calls == producer.close_calls == 1
        assert worker.session is None and not worker._disposals and not rig.peers
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_phase_failure_preserved_when_exact_close_also_fails(tmp_path, monkeypatch):
    rig, broker, worker, _server, producer = prepare_rig(tmp_path, monkeypatch)
    original = ValueError("original negotiation phase failure")
    producer.remote_error = original
    broker.close_error = RpcError("invalid_request", "controlled worker close rejection")
    try:
        with pytest.raises(ValueError) as failure:
            await rig.speech(label="inert contract", baseline=400)
        assert failure.value is original
        assert isinstance(failure.value.__cause__, RuntimeFailure)
        assert "controlled worker close rejection" in str(failure.value.__cause__)
        assert producer.close_calls == 1 and len(rig.peers) == 1
        assert "controlled worker close rejection" in rig.speech_cleanup_errors[0]["error"]
        assert all("request_id" in payload for payload in broker.requests)
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_final_fixture_cleanup_uses_valid_close_and_retires_peer(tmp_path, monkeypatch):
    rig, broker, worker, _server, producer = prepare_rig(tmp_path, monkeypatch)
    rig.observer = None
    rig.peers["network-interrupted-session"] = producer
    try:
        await rig.close()
        assert len(broker.requests) == 1 and broker.requests[0]["action"] == "close"
        assert AudioWorker.identity(broker.requests[0]) == 0.28
        assert producer.close_calls == 1 and not rig.peers
        assert set(broker.stopped) == {rig.source_id, rig.terminal_id}
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_final_close_rejection_reported_after_remaining_units_are_attempted(tmp_path, monkeypatch):
    rig, broker, worker, _server, producer = prepare_rig(tmp_path, monkeypatch)
    rig.observer = None
    rig.peers["network-interrupted-session"] = producer
    broker.close_error = RpcError("invalid_request", "controlled close protocol rejection")
    try:
        with pytest.raises(RuntimeFailure, match="controlled close protocol rejection"):
            await rig.close()
        assert producer.close_calls == 1 and len(rig.peers) == 1
        assert set(broker.stopped) == {rig.source_id, rig.terminal_id}
        assert len(rig.speech_cleanup_errors) == 1
    finally:
        await worker.close()

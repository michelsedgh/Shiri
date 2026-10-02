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
from shiri.domain import SpeakerRef
from shiri.runtime.backend import OwnToneClient
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
             "sys": sys, "RuntimeFailure": RuntimeFailure, "require": require, "SpeakerRef": SpeakerRef}
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
@pytest.mark.parametrize('pcm_peak', [0, 1000])
async def test_deadline_failure_records_exact_window_before_original_assert_and_closes_peer(tmp_path, monkeypatch, pcm_peak):
    rig, broker, worker, server, producer = prepare_rig(tmp_path, monkeypatch, 'pause')
    spectrum = {'speech_880_amplitude': 100, 'music_440_amplitude': 0,
                'minimum_presentation_lead_ns': -7_000_000}
    window_calls = []
    def deadline_observation(*, since_ns):
        window_calls.append(since_ns)
        return {'since_ns': since_ns, 'late_blocks': 1, 'late_frames': 384,
                'minimum_presentation_lead_ns': -7_000_000,
                'late_packets': [{'pcm_peak': pcm_peak}], 'sample_limit': 64}
    rig.observer.registry.spectrum = lambda **_: dict(spectrum)
    rig.observer.registry.deadline_observation = deadline_observation
    report = {}
    try:
        with pytest.raises(RuntimeFailure, match='Actual terminal speech PCM missed its original presentation deadline'):
            await rig.speech(label='failed paused deadline', paused=True, report=report)
        observation, = report['speech_observations']
        assert observation['label'] == 'failed paused deadline' and observation['paused'] is True
        assert observation['first_voice_spectrum'] == observation['steady_spectrum'] == spectrum
        assert observation['window_start_ns'] == window_calls[0]
        assert observation['deadline_window']['late_packets'][0]['pcm_peak'] == pcm_peak
        assert observation['deadline_window']['minimum_presentation_lead_ns'] == -7_000_000
        assert server.close_calls == producer.close_calls == 1
        assert [request['action'] for request in broker.requests] == ['offer', 'close']
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


def cold_release_fixture(tmp_path, monkeypatch, states):
    """Model only observed tail retirement; selection recreates a live pipe."""
    rig = Rig(object(), tmp_path/"cold-release", "inert-fixture")
    registry = SimpleNamespace(current=None)
    rig.observer = SimpleNamespace(registry=registry)
    cursor, restored = [0], []

    async def source_request(method, path):
        assert (method, path) == ("PUT", "/api/player/stop")
        return {}

    async def outputs(_excluded):
        return []

    async def select(_speakers, _outputs):
        return {}

    async def health():
        return states[cursor[0]]["health"]

    async def zone_request(method, path):
        assert (method, path) == ("GET", "/api/player")
        return {"state": states[cursor[0]]["player"]}

    async def restore(_room):
        restored.append(cursor[0])
        # Production speaker_set full START is selected by PLAY_PLAYING.
        if states[cursor[0]]["player"] == "play":
            registry.current = "unheld-idle-tail-transport"

    async def select_source():
        return None

    async def polls(action, _description, *, timeout):
        assert timeout == 20  # The original cold release bound is unchanged.
        while True:
            result = await action()
            if result is not None:
                return result
            if cursor[0] == len(states)-1:
                raise RuntimeFailure("Controlled cold-release retirement never completed")
            cursor[0] += 1

    rig.source_client = SimpleNamespace(request=source_request, outputs=outputs, select=select)
    rig.room = SimpleNamespace(client=SimpleNamespace(request=zone_request, outputs=outputs, select=select))
    rig.broker = SimpleNamespace(_restore_outputs=restore)
    rig.health, rig.select_source = health, select_source
    monkeypatch.setitem(NAMESPACE, "eventually", polls)
    return rig, restored, registry


def retired_idle_state(**changes):
    health = {"ready": True, "source": {"owner": None, "pending_revocations": 0},
              "audio_active": False, "fifo_reader": False, "speech_session_id": None,
              "speech_cleanup_pending": 0, "speech_cleanup_error": None}
    health.update(changes)
    return {"health": health, "player": "stop"}


@pytest.mark.asyncio
async def test_cold_release_waits_idle_fifo_and_player_retirement_before_reselection(tmp_path, monkeypatch):
    idle_tail = retired_idle_state(audio_active=True, fifo_reader=True)
    idle_tail["player"] = "play"
    pending_eof = retired_idle_state(fifo_reader=False)
    pending_eof["player"] = "play"
    rig, restored, registry = cold_release_fixture(tmp_path, monkeypatch,
        [idle_tail, pending_eof, retired_idle_state()])
    await rig.release()
    assert restored == [2] and registry.current is None
    # The original release predicate returned at the first observation:
    # ownerNone/currentNone alone certify neither an idle pipe nor a cold START.
    assert idle_tail["health"]["source"]["owner"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"fifo_reader": True}, {"audio_active": True}, {"speech_session_id": "retained-voice"},
    {"speech_cleanup_pending": 1}, {"speech_cleanup_error": "retained-close-failure"},
    {"source": {"owner": None, "pending_revocations": 1}},
])
async def test_cold_release_refuses_unretired_tail_or_cleanup(tmp_path, monkeypatch, changes):
    rig, restored, _registry = cold_release_fixture(tmp_path, monkeypatch, [retired_idle_state(**changes)])
    with pytest.raises(RuntimeFailure, match="retirement never completed"):
        await rig.release()
    assert not restored


def test_held_setup_attempt_evidence_survives_recovery_exception():
    phases = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "exercise")
    fault = next(n for n in phases.body if isinstance(n, ast.If)
                 and isinstance(n.test, ast.Name) and n.test.id == "fault_case")
    arm = next(i for i, n in enumerate(fault.body) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Subscript) and ast.unparse(t) == "report['held_setup_attempt']"
                       for t in n.targets))
    recovery = next(i for i, n in enumerate(fault.body) if isinstance(n, ast.Try)
                    and any(isinstance(x, ast.Await) and "eventually" in ast.unparse(x.value)
                            for x in ast.walk(n)))
    assert arm < recovery
    cleanup = fault.body[recovery].finalbody
    update = next(n.value for n in cleanup if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                  and ast.unparse(n.value.func) == "attempt.update")
    fake = {"attempt": {}, "queue_error": "ControlledQueueFailure", "deadline_failure": None, "failures": [],
            "rig": SimpleNamespace(observer=SimpleNamespace(held_begins=[], hold_next_begin_seconds=4.5,
                registry=SimpleNamespace(snapshot=lambda: {"sessions": 5})))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.Expr(value=update)], type_ignores=[])), str(PATH), "exec"), fake)
    assert fake["attempt"]["held_begins"] == [] and fake["attempt"]["hold_still_armed"] == 4.5
    assert fake["attempt"]["terminal_sessions_after"] == 5
    assert fake["attempt"]["queue_error_type"] == "ControlledQueueFailure"


@pytest.mark.asyncio
async def test_exhausted_idle_pipe_pause_is_cold_only_after_full_quiescence(tmp_path, monkeypatch):
    tail = retired_idle_state(fifo_reader=True)
    tail["player"] = "pause"
    retired = retired_idle_state()
    retired["player"] = "pause"
    rig, restored, registry = cold_release_fixture(tmp_path, monkeypatch, [tail, retired])
    await rig.release()
    assert restored == [1] and registry.current is None
    attempt = rig.release_observations[-1]
    assert attempt["quiescence_confirmed"] is attempt["selection_restored"] is True
    assert [s["fifo_reader"] for s in attempt["observations"]] == [True, False]
    assert all(s["player_state"] == "pause" for s in attempt["observations"])


@pytest.mark.asyncio
async def test_playing_pipe_cannot_reselect_even_with_retired_worker_fields(tmp_path, monkeypatch):
    playing = retired_idle_state()
    playing["player"] = "play"
    rig, restored, registry = cold_release_fixture(tmp_path, monkeypatch, [playing])
    with pytest.raises(RuntimeFailure, match="retirement never completed"):
        await rig.release()
    assert not restored and registry.current is None
    attempt = rig.release_observations[-1]
    assert attempt["quiescence_confirmed"] is attempt["selection_restored"] is False
    assert attempt["finished_ns"] >= attempt["started_ns"]
    assert attempt["observations"][-1]["player_state"] == "play"


@pytest.mark.asyncio
async def test_failed_release_retains_bounded_exact_worker_and_player_observations(tmp_path, monkeypatch):
    retained = retired_idle_state(speech_cleanup_pending=1)
    retained["player"] = "pause"
    rig, restored, _registry = cold_release_fixture(tmp_path, monkeypatch, [retained]*20)
    with pytest.raises(RuntimeFailure, match="retirement never completed"):
        await rig.release()
    assert not restored
    attempt = rig.release_observations[-1]
    assert len(attempt["observations"]) == 16
    last = attempt["observations"][-1]
    assert last["ready"] is True and last["source_owner_none"] is True
    assert last["fifo_reader"] is last["audio_active"] is False
    assert last["pending_revocations"] == 0 and last["speech_session_none"] is True
    assert last["speech_cleanup_pending"] == 1 and last["player_state"] == "pause"
    assert last["terminal_none"] is True
    assert attempt["quiescence_confirmed"] is attempt["selection_restored"] is False


def rediscovery_fixture(tmp_path, catalogs):
    rig = Rig(object(), tmp_path/"source-rediscovery", "inert-fixture")
    rig.room_name = "Exact original receiver"
    rig.source_target = {"id": "2930199551404", "name": rig.room_name, "protocol": "airplay2"}
    rig.room = SimpleNamespace(receiver={"mac": "02:AA:3D:80:DD:AC"})
    client = OwnToneClient("http://inert.invalid")
    cursor, writes = [0], []

    async def outputs(_excluded):
        value = catalogs[min(cursor[0], len(catalogs)-1)]
        cursor[0] += 1
        if isinstance(value, BaseException):
            raise value
        return value

    async def request(method, path, *, json=None):
        assert (method, path) == ("PUT", "/api/outputs/set")
        writes.append(json)
        for value in catalogs[-1]:
            value["selected"] = value["id"] in json["outputs"]
        return {"ok": True}

    client.outputs, client.request = outputs, request
    rig.source_client = client
    return rig, writes, cursor


def original_source_output(**changes):
    output = {"id": "2930199551404", "name": "Exact original receiver", "protocol": "airplay2",
              "assignable": True, "requires_auth": False, "selected": False, "offset_ms": 0}
    output.update(changes)
    return output


@pytest.mark.asyncio
async def test_recovered_zone_waits_only_for_exact_original_sender_discovery(tmp_path):
    exact = original_source_output()
    rig, writes, cursor = rediscovery_fixture(tmp_path, [[], [exact]])
    original = dict(rig.source_target)
    try:
        assert await rig.select_rediscovered_source() is None
        assert not writes
        assert await rig.select_rediscovered_source() is True
        assert writes == [{"outputs": [original["id"]]}]
        assert rig.source_target == original and cursor[0] == 3
        absent, discovered = rig.source_rediscovery_observations
        assert absent["catalog_count"] == 0 and absent["related"] == []
        assert discovered["related"] == [{k: exact[k] for k in
            ("id", "name", "protocol", "assignable", "requires_auth")}]
    finally:
        await rig.source_client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"id": "2930199551405"}, {"name": "Changed receiver"},
    {"protocol": "airplay1"}, {"requires_auth": True}, {"assignable": False}])
async def test_rediscovery_identity_or_admission_change_fails_without_retry_or_selection(tmp_path, changes):
    rig, writes, cursor = rediscovery_fixture(tmp_path, [[original_source_output(**changes)]])
    try:
        with pytest.raises(ValueError, match="exact identity or admission contract"):
            await NAMESPACE["eventually"](rig.select_rediscovered_source, "exact rediscovery", timeout=0.01)
        assert cursor[0] == 1 and not writes
    finally:
        await rig.source_client.close()


@pytest.mark.asyncio
async def test_catalog_failure_is_not_swallowed_by_recovery_poll(tmp_path):
    original = RuntimeFailure("OwnTone returned malformed or duplicate output identities")
    rig, writes, cursor = rediscovery_fixture(tmp_path, [original])
    try:
        with pytest.raises(ValueError, match="malformed or duplicate") as captured:
            await NAMESPACE["eventually"](rig.select_rediscovered_source, "exact rediscovery", timeout=0.01)
        assert captured.value.__cause__ is original
        assert cursor[0] == 1 and not writes
    finally:
        await rig.source_client.close()


def test_source_rediscovery_is_inside_original_bounded_paired_recovery():
    exercise = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "exercise")
    fault = next(n for n in exercise.body if isinstance(n, ast.If) and ast.unparse(n.test) == "fault_case")
    recovered = next(n for n in fault.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "recovered")
    assert any(isinstance(n, ast.Await) and ast.unparse(n.value) == "rig.select_rediscovered_source()"
               for n in ast.walk(recovered))
    recovery_call = next(n for n in ast.walk(fault) if isinstance(n, ast.Call)
                         and ast.unparse(n.func) == "eventually")
    assert ast.unparse(recovery_call.args[0]) == "recovered"
    assert next(k.value.value for k in recovery_call.keywords if k.arg == "timeout") == 45
    assert not any(isinstance(n, ast.Await) and ast.unparse(n.value) == "rig.select_source()"
                   for n in ast.walk(fault))


@pytest.mark.asyncio
async def test_paired_recovery_catalog_wait_cannot_consume_a_fresh_deadline(monkeypatch):
    exercise = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "exercise")
    fault = next(n for n in exercise.body if isinstance(n, ast.If) and ast.unparse(n.test) == "fault_case")
    recovery = next(n for n in fault.body if isinstance(n, ast.Try)
                    and any(isinstance(v, ast.Call) and ast.unparse(v.func) == "asyncio.wait_for"
                            for v in ast.walk(n)))
    expression = recovery.body[0].value.value
    assert ast.unparse(expression.func) == "asyncio.wait_for"
    stalled, cancelled = [False], [False]

    async def recovered():
        stalled[0] = True
        try:
            await asyncio.Event().wait()
        finally:
            cancelled[0] = True

    frozen_clock = 7_000_000_000
    namespace = {"asyncio": asyncio, "eventually": NAMESPACE["eventually"], "recovered": recovered,
                 "fault_started": frozen_clock-44_990_000_000,
                 "time": SimpleNamespace(monotonic_ns=lambda: frozen_clock)}
    # Execute the actual recovery expression with only10ms of the SAME45s
    # attempt budget remaining. A freshly-added45s discovery wait would hang.
    expr = ast.Expression(body=expression)
    coroutine = eval(compile(ast.fix_missing_locations(expr), str(PATH), "eval"), namespace)
    began = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await coroutine
    assert time.monotonic()-began < 0.2 and stalled[0] and cancelled[0]

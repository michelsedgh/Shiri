"""Independent recorded-PCM fixtures and authenticated, real-store workflows."""
import asyncio
import io
import wave

import httpx
import pytest

np = pytest.importorskip("numpy")

from shiri.api import create_app  # noqa: E402
from shiri.calibration import (CalibrationSessions, analyze_wav, probe_pcm, probe_wav,  # noqa: E402
                               read_wav, summary)
from shiri.domain import Conflict, NotFound, Room, SpeakerRef, ValidationIssue  # noqa: E402
from shiri.runtime_port import SimulatedRuntime  # noqa: E402
from shiri.settings import Settings  # noqa: E402

TOKEN = "calibration-admin-" * 4
ROOM_ID = "00000000-0000-4000-8000-000000000001"
SEED = "independent-fixture-v1"


def wav(channels, rate=48000):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(np.rint(np.clip(channels, -1, 32767 / 32768) * 32768).astype("<i2").tobytes())
    return output.getvalue()


def recording(seed=SEED, *, delay_ms=23.0, ppm=0, polarity=1, rate=48000, take=0,
              noise=1e-4, echo=None, dropout=False, clipping=False):
    """Generate known arrival times via independent index interpolation.

    No estimator/helper lag calculation is used to create expected results.
    Delayed clock means target arrival at source time t is delay + t*(1+ppm).
    """
    if rate == 48000:
        program = probe_pcm(seed).astype(float) / 32768
    else:
        original = probe_pcm(seed).astype(float) / 32768
        program = np.interp(np.arange(round(len(original) * rate / 48000)) * 48000 / rate,
                            np.arange(len(original)), original)
    reference = np.pad(program, (round(.25 * rate), round(.25 * rate)))
    positions = np.arange(len(reference))
    shifted = (positions - delay_ms * rate / 1000 - .25 * rate) / (1 + ppm / 1e6)
    target = np.interp(shifted, np.arange(len(program)), program, left=0, right=0) * polarity
    rng = np.random.default_rng(710 + take)
    reference += rng.normal(scale=noise, size=len(reference))
    target += rng.normal(scale=noise, size=len(target))
    if echo:
        shifted_echo = shifted - echo[0] * rate / 1000
        target += np.interp(shifted_echo, np.arange(len(program)), program, left=0, right=0) * echo[1]
    if dropout:
        target[round(1.33 * rate):round(1.38 * rate)] = 0
    if clipping:
        target *= 50
    return wav(np.column_stack((reference, target)), rate)


@pytest.mark.parametrize("delay_ms,polarity", [(23, 1), (-41, 1), (137, -1), (0, 1)])
async def test_integer_delay_sign_and_inverted_polarity_recovered_within_one_sample(delay_ms, polarity):
    result = analyze_wav(recording(delay_ms=delay_ms, polarity=polarity), SEED)
    valid = [m for m in result["markers"] if m["accepted"]]
    assert len(valid) == 8, result
    assert all(abs(m["lag_ms"] - delay_ms) <= 1000 / 48000 for m in valid)
    assert all(m["target_polarity"] == polarity for m in valid)
    assert result["drift"]["ms_per_minute"] == pytest.approx(0, abs=.01)


async def test_independent_capture_rate_and_geometry_correction():
    result = analyze_wav(recording(rate=44100, delay_ms=11), SEED, geometry_correction_ms=2.5)
    valid = [m for m in result["markers"] if m["accepted"]]
    assert len(valid) >= 7
    assert np.median([m["lag_ms"] for m in valid]) == pytest.approx(8.5, abs=.05)


async def test_candidate_requires_repetitions_and_never_changes_profile_during_analysis():
    records = [analyze_wav(recording(take=take), SEED) for take in range(3)]
    assert summary(records[:2], 100)["status"] == "insufficient_evidence"
    result = summary(records, 100)
    assert result["status"] == "candidate_correction", result
    assert result["accepted_markers"] == 21  # First marker is startup evidence.
    assert result["candidate_offset_ms"] == 77  # Target late => remove delay.
    assert result["minimum_confidence"] > .99


async def test_verification_is_a_measurement_and_never_proposes_an_out_of_range_second_correction():
    records = [analyze_wav(recording(delay_ms=.625, take=take), SEED) for take in range(3)]
    candidate = summary(records, -2000)
    assert candidate["status"] == "insufficient_evidence" and candidate["candidate_offset_ms"] is None
    verified = summary(records, -2000, verification=True)
    assert verified["status"] == "stable_measurement"
    assert verified["median_lag_ms"] == pytest.approx(.625)
    assert verified["candidate_offset_ms"] is None


@pytest.mark.parametrize("changes,reason", [({"echo": (40, .95)}, "multiple"),
                                          ({"clipping": True}, "clips"),
                                          ({"noise": .16}, "weak"),
                                          ({"dropout": True}, "silence")])
async def test_bad_recordings_keep_rejections_instead_of_fabricating_confidence(changes, reason):
    result = analyze_wav(recording(**changes), SEED)
    invalid = [marker for marker in result["markers"] if not marker["accepted"]]
    assert invalid, result
    assert any(reason in marker["reason"] for marker in invalid), invalid


async def test_jitter_and_clock_drift_are_different_rejections():
    varying = [analyze_wav(recording(delay_ms=delay, take=index), SEED) for index, delay in enumerate([20, 23, 26])]
    result = summary(varying, 0)
    assert result["candidate_offset_ms"] is None
    assert any("jitter" in reason for reason in result["reasons"])
    drifting = [analyze_wav(recording(ppm=500, take=index), SEED) for index in range(3)]
    result = summary(drifting, 0)
    assert any("clock drift" in reason for reason in result["reasons"]), result
    assert drifting[0]["drift"]["ms_per_minute"] == pytest.approx(30, abs=2)


async def test_truncated_wrong_format_oversized_and_unrelated_pcm_rejected():
    raw = recording()
    with pytest.raises(ValidationIssue, match="truncated"):
        read_wav(raw[:-7])
    with pytest.raises(ValidationIssue, match="2 MiB"):
        read_wav(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValidationIssue, match="stereo"):
        read_wav(probe_wav(SEED))
    result = analyze_wav(wav(np.zeros((4 * 48000, 2))), SEED)
    assert not any(marker["accepted"] for marker in result["markers"])


def definition():
    return Room(id=ROOM_ID, slot=0, name="Kitchen", airplay_name="Kitchen", interface="eth0",
                speakers=[SpeakerRef(id="101", name="Reference", protocol="airplay2"),
                          SpeakerRef(id="202", name="Target", protocol="chromecast")])


async def test_exact_room_bound_expiring_bounded_sessions_and_duplicate_pcm():
    sessions = CalibrationSessions(capacity=1)
    room = definition()
    arguments = dict(target_id="202", reference_id="101", capture_device="Shared ADC",
                     geometry="Equal distance near-field microphones", max_lag_ms=500, geometry_correction_ms=0)
    session = sessions.create(room, **arguments)
    with pytest.raises(NotFound):
        sessions.get("another-room", session.id)
    with pytest.raises(Conflict, match="Too many"):
        sessions.create(room, **arguments)
    data = recording(session.seed)
    result = analyze_wav(data, session.seed)
    sessions.add_result(session, result)
    # A container chunk alone must not turn duplicate PCM into another take.
    altered_container = data + b"JUNK\x00\x00\x00\x00"
    with pytest.raises(Conflict, match="PCM recording"):
        sessions.add_result(session, analyze_wav(altered_container, session.seed))
    session.deadline = 0
    with pytest.raises(NotFound, match="expired"):
        sessions.get(room.id, session.id)
    assert len(sessions.sessions) == 0


@pytest.fixture
async def api(tmp_path):
    app = create_app(Settings(state_dir=tmp_path), runtime=SimulatedRuntime(), token=TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shiri.test",
                                 headers={"Authorization": "Bearer " + TOKEN}) as client:
        r = (await client.post("/api/v1/rooms", json={"name": "Room", "interface": "sim0"})).json()["room"]
        r = (await client.patch(f"/api/v1/rooms/{r['id']}", json={"expected_revision": r["revision"], "changes": {"enabled": True}})).json()["room"]
        r = (await client.put(f"/api/v1/rooms/{r['id']}/speakers", json={"expected_revision": r["revision"], "speaker_ids": ["101", "202"]})).json()["room"]
        yield app, client, r
    app.state.service.store.close()


async def begin(client, r):
    endpoint = f"/api/v1/rooms/{r['id']}/calibration"
    created = await client.post(endpoint, json={"expected_revision": r["revision"], "reference_id": "101", "target_id": "202",
                                                "capture_device": "Shared USB ADC", "geometry": "Matched distances; channel 1 reference, channel 2 target"})
    assert created.status_code == 201, created.text
    return endpoint + "/" + created.json()["id"], created.json()


async def import_take(app, client, endpoint, session_id, take, *, delay_ms=23, verification=False):
    seed = app.state.service.calibrations.sessions[session_id].seed
    result = await client.post(endpoint + "/recordings" + ("?verification=true" if verification else ""),
                               content=recording(seed, take=take, delay_ms=delay_ms), headers={"Content-Type": "audio/wav"})
    assert result.status_code == 200, result.text
    return result.json()


async def test_api_exact_identity_auth_validation_candidate_apply_verify_and_rollback(api):
    app, client, r = api
    endpoint, session = await begin(client, r)
    unauthenticated = await client.get(endpoint, headers={"Authorization": ""})
    assert unauthenticated.status_code == 401
    assert (await client.post(endpoint + "/apply", json={"expected_revision": r["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]}, headers={"Origin": "https://evil.test"})).status_code == 403
    assert (await client.get(endpoint.replace(r["id"], ROOM_ID))).status_code == 404
    assert (await client.get(endpoint + "/probe.wav")).headers["content-type"] == "audio/wav"
    assert (await client.post(endpoint + "/recordings", content=b"invalid", headers={"Content-Type": "text/plain"})).status_code == 415
    assert (await client.post(endpoint + "/apply", json={"expected_revision": r["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})).status_code == 409
    for take in range(3):
        session = await import_take(app, client, endpoint, session["id"], take)
    assert session["status"] == "candidate_correction"
    assert session["result"]["candidate_offset_ms"] == -23
    # Analysis does not touch any room intent or runtime output.
    assert app.state.service.store.get_room(r["id"]).revision == r["revision"]
    assert (await client.post(endpoint + "/apply", json={"expected_revision": r["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})).status_code == 409
    r = (await client.patch(f"/api/v1/rooms/{r['id']}", json={"expected_revision": r["revision"], "changes": {"enabled": False}})).json()["room"]
    assert (await client.post(endpoint + "/apply", json={"expected_revision": r["revision"] - 1, "expected_generation": (await client.get(endpoint)).json()["generation"]})).status_code == 409
    applied = await client.post(endpoint + "/apply", json={"expected_revision": r["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})
    assert applied.status_code == 200, applied.text
    r = applied.json()["room"]
    assert r["speakers"][1]["offset_ms"] == -23
    assert applied.json()["calibration"]["status"] == "offset_saved"
    assert (await client.post(endpoint + "/apply", json={"expected_revision": r["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})).status_code == 409
    assert (await client.get(endpoint.rsplit("/", 1)[0])).json()["sessions"][0]["id"] == session["id"]
    r = (await client.patch(f"/api/v1/rooms/{r['id']}", json={"expected_revision": r["revision"], "changes": {"enabled": True}})).json()["room"]
    for take in range(3, 6):
        session = await import_take(app, client, endpoint, session["id"], take, delay_ms=0, verification=True)
    assert session["status"] == "measured_at_this_setup"
    assert session["verification"]["accepted_markers"] == 21
    export_response = await client.get(endpoint + "/export")
    exported = export_response.json()
    assert len(export_response.content) < 64 * 1024  # Six 1.17 MiB takes never survive in the evidence.
    def raw_capture(value):
        if isinstance(value, (bytes, bytearray, memoryview, np.ndarray)):
            return True
        if isinstance(value, dict):
            return any(raw_capture(item) for item in value.values())
        if isinstance(value, list):
            return any(raw_capture(item) for item in value)
        return False
    assert not raw_capture(vars(app.state.service.calibrations.sessions[session["id"]]))
    assert exported["previous_offset_ms"] == 0
    assert exported["verification_backend"][0]["reported_offsets"] == {"202": -23, "101": 0}
    r = (await client.patch(f"/api/v1/rooms/{r['id']}", json={"expected_revision": r["revision"], "changes": {"enabled": False}})).json()["room"]
    rolled_back = await client.post(endpoint + "/rollback", json={"expected_revision": r["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["room"]["speakers"][1]["offset_ms"] == 0
    assert rolled_back.json()["calibration"]["status"] == "rolled_back"
    assert (await client.delete(endpoint)).status_code == 200
    assert (await client.get(endpoint)).status_code == 404


async def test_stale_speaker_offset_prevents_import_apply_and_rollback(api):
    app, client, r = api
    endpoint, session = await begin(client, r)
    for take in range(3):
        session = await import_take(app, client, endpoint, session["id"], take)
    r = (await client.patch(f"/api/v1/rooms/{r['id']}/speakers/101/offset", json={"expected_revision": r["revision"], "offset_ms": 50})).json()["room"]
    r = (await client.patch(f"/api/v1/rooms/{r['id']}", json={"expected_revision": r["revision"], "changes": {"enabled": False}})).json()["room"]
    response = await client.post(endpoint + "/apply", json={"expected_revision": r["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})
    assert response.status_code == 409 and "stale" in response.json()["error"]
    response = await client.post(endpoint + "/recordings", content=recording(app.state.service.calibrations.sessions[session["id"]].seed, take=7), headers={"Content-Type": "audio/wav"})
    assert response.status_code == 409
    assert app.state.service.store.get_room(r["id"]).speakers[1].offset_ms == 0


async def test_cancelled_analysis_cannot_append_late_evidence(api, monkeypatch):
    app, client, r = api
    endpoint, session = await begin(client, r)
    service = app.state.service
    entered, release = asyncio.Event(), asyncio.Event()
    original = asyncio.to_thread
    async def deferred(*args, **kwargs):
        if args[0] is not analyze_wav:
            return await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr("shiri.service.asyncio.to_thread", deferred)
    task = asyncio.create_task(service.calibration_recording(r["id"], session["id"], recording(service.calibrations.sessions[session["id"]].seed)))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and service._calibration_analysis.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.calibrations.sessions[session["id"]].recordings == []


async def prepared_correction(api):
    app, client, room = api
    endpoint, session = await begin(client, room)
    for take in range(3):
        session = await import_take(app, client, endpoint, session["id"], take)
    room = (await client.patch(f"/api/v1/rooms/{room['id']}", json={"expected_revision": room["revision"], "changes": {"enabled": False}})).json()["room"]
    return endpoint, session, room


async def test_cancelled_apply_retains_commit_and_exact_rollback_receipt(api, monkeypatch):
    app, _client, _room = api
    _endpoint, session, room = await prepared_correction(api)
    service = app.state.service
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.reconcile
    async def delayed_reconcile():
        entered.set()
        await release.wait()
        return await original()
    monkeypatch.setattr(service, "reconcile", delayed_reconcile)
    task = asyncio.create_task(service.calibration_apply(room["id"], session["id"], room["revision"]))
    await entered.wait()
    # SQLite has committed before the request disappears.
    assert service.store.get_room(room["id"]).speakers[1].offset_ms == -23
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and service._mutation.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    retained = service.calibrations.get(room["id"], session["id"])
    assert retained.previous_offset_ms == 0
    assert retained.applied_offset_ms == -23
    assert retained.applied_revision == service.store.get_room(room["id"]).revision
    assert not service._mutation.locked()


async def test_backend_failure_saved_intent_distinct_from_verification_and_stale_rollback(api, monkeypatch):
    app, client, _room = api
    endpoint, session, room = await prepared_correction(api)
    original = app.state.service.runtime.call
    async def failed_reconcile(operation, payload=None):
        if operation == "reconcile":
            from shiri.rpc import RpcError
            raise RpcError("unavailable", "Backend offline")
        return await original(operation, payload)
    monkeypatch.setattr(app.state.service.runtime, "call", failed_reconcile)
    applied = (await client.post(endpoint + "/apply", json={"expected_revision": room["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})).json()
    assert applied["runtime_accepted"] is False
    assert applied["calibration"]["status"] == "offset_saved"
    assert applied["calibration"]["verification"] is None
    assert "Backend offline" in applied["calibration"]["application"]["pending_reason"]
    room = applied["room"]
    # A later manual edit is protected from an old rollback, including when
    # the caller supplies the latest revision.
    edited = (await client.patch(f"/api/v1/rooms/{room['id']}/speakers/202/offset", json={"expected_revision": room["revision"], "offset_ms": 77})).json()["room"]
    rolled_back = await client.post(endpoint + "/rollback", json={"expected_revision": edited["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})
    assert rolled_back.status_code == 409 and "stale" in rolled_back.json()["error"]
    assert app.state.service.store.get_room(room["id"]).speakers[1].offset_ms == 77


async def test_verification_requires_active_speakers_exact_backend_readback_and_fresh_pcm(api, monkeypatch):
    app, client, _room = api
    endpoint, session, room = await prepared_correction(api)
    applied = (await client.post(endpoint + "/apply", json={"expected_revision": room["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})).json()
    room = applied["room"]
    seed = app.state.service.calibrations.sessions[session["id"]].seed
    data = recording(seed, delay_ms=0, take=11)
    verification_endpoint = endpoint + "/recordings?verification=true"
    response = await client.post(verification_endpoint, content=data, headers={"Content-Type": "audio/wav"})
    assert response.status_code == 409 and "Enable" in response.json()["error"]
    room = (await client.patch(f"/api/v1/rooms/{room['id']}", json={"expected_revision": room["revision"], "changes": {"enabled": True}})).json()["room"]
    original = app.state.service.discover
    async def wrong_readback(room_id):
        outputs = await original(room_id)
        for output in outputs:
            if output["id"] == "202":
                output["offset_ms"] = 0
        return outputs
    monkeypatch.setattr(app.state.service, "discover", wrong_readback)
    response = await client.post(verification_endpoint, content=data, headers={"Content-Type": "audio/wav"})
    assert response.status_code == 409 and "offsets reported" in response.json()["error"]
    assert app.state.service.calibrations.sessions[session["id"]].verification_recordings == []
    monkeypatch.setattr(app.state.service, "discover", original)
    old_capture = recording(seed, delay_ms=23, take=0)
    response = await client.post(verification_endpoint, content=old_capture, headers={"Content-Type": "audio/wav"})
    assert response.status_code == 409 and "already analyzed" in response.json()["error"]


async def test_unexpected_reconcile_error_after_commit_cannot_lose_previous_offset(api, monkeypatch):
    app, client, _room = api
    endpoint, session, room = await prepared_correction(api)
    service = app.state.service
    original = service.reconcile
    async def broken_runtime_metadata():
        from shiri.rpc import RpcError
        raise RpcError("invalid_response", "Invalid phone metadata in runtime acknowledgment")
    monkeypatch.setattr(service, "reconcile", broken_runtime_metadata)
    response = await client.post(endpoint + "/apply", json={"expected_revision": room["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})
    assert response.status_code == 503
    assert service.store.get_room(room["id"]).speakers[1].offset_ms == -23
    retained = (await client.get(endpoint)).json()
    assert retained["status"] == "offset_saved"
    assert retained["previous_offset_ms"] == 0 and retained["applied_offset_ms"] == -23
    assert retained["application"]["runtime_accepted"] is False
    monkeypatch.setattr(service, "reconcile", original)
    response = await client.post(endpoint + "/rollback", json={"expected_revision": retained["applied_revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})
    assert response.status_code == 200, response.text
    assert response.json()["room"]["speakers"][1]["offset_ms"] == 0


async def test_room_deletion_releases_unreachable_evidence_and_calibration_capacity(api):
    app, client, room = api
    endpoint, session = await begin(client, room)
    room = (await client.patch(f"/api/v1/rooms/{room['id']}", json={"expected_revision": room["revision"], "changes": {"enabled": False}})).json()["room"]
    response = await client.delete(f"/api/v1/rooms/{room['id']}?expected_revision={room['revision']}")
    assert response.status_code == 200, response.text
    assert session["id"] not in app.state.service.calibrations.sessions
    assert (await client.get(endpoint)).status_code == 404


async def test_concurrent_import_is_rejected_without_starting_another_fft(api, monkeypatch):
    app, client, room = api
    endpoint, session = await begin(client, room)
    service = app.state.service
    entered, release = asyncio.Event(), asyncio.Event()
    original = asyncio.to_thread
    async def blocked(*args, **kwargs):
        if args[0] is not analyze_wav:
            return await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr("shiri.service.asyncio.to_thread", blocked)
    data = recording(service.calibrations.sessions[session["id"]].seed)
    first = asyncio.create_task(service.calibration_recording(room["id"], session["id"], data))
    await entered.wait()
    with pytest.raises(Conflict, match="Another recording"):
        await service.calibration_recording(room["id"], session["id"], data)
    release.set()
    await first
    assert len(service.calibrations.sessions[session["id"]].recordings) == 1


async def test_stable_wrong_post_change_delay_is_explicit_verification_failure(api):
    app, client, _room = api
    endpoint, session, room = await prepared_correction(api)
    applied = (await client.post(endpoint + "/apply", json={"expected_revision": room["revision"], "expected_generation": (await client.get(endpoint)).json()["generation"]})).json()
    room = applied["room"]
    room = (await client.patch(f"/api/v1/rooms/{room['id']}", json={"expected_revision": room["revision"], "changes": {"enabled": True}})).json()["room"]
    for take in range(3, 6):
        session = await import_take(app, client, endpoint, session["id"], take, delay_ms=5, verification=True)
    assert session["verification"]["status"] == "stable_measurement"
    assert session["verification"]["alignment_status"] == "not_aligned"
    assert session["verification"]["median_lag_ms"] == pytest.approx(5)
    assert session["status"] == "verification_failed"
    assert session["applied_offset_ms"] == -23  # Analysis does not blind-apply another correction.


async def cross_local_rooms(api):
    app, client, target = api
    target = (await client.patch(f"/api/v1/rooms/{target['id']}", json={"expected_revision": target["revision"], "changes": {
        "local_audio_device": "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"}})).json()["room"]
    target = (await client.put(f"/api/v1/rooms/{target['id']}/speakers", json={"expected_revision": target["revision"], "speaker_ids": ["0"]})).json()["room"]
    reference = (await client.post("/api/v1/rooms", json={"name": "Reference Wi-Fi zone", "interface": "sim0",
        "local_audio_device": "hw:CARD=ReferenceUSB,DEV=0"})).json()["room"]
    reference = (await client.patch(f"/api/v1/rooms/{reference['id']}", json={"expected_revision": reference["revision"], "changes": {"enabled": True}})).json()["room"]
    reference = (await client.put(f"/api/v1/rooms/{reference['id']}/speakers", json={"expected_revision": reference["revision"], "speaker_ids": ["0"]})).json()["room"]
    assert target["speakers"][0]["id"] == reference["speakers"][0]["id"] == "0"
    body = {"expected_revision": target["revision"], "target_id": "0", "reference_id": "0",
            "reference_room_id": reference["id"], "expected_reference_revision": reference["revision"],
            "playback_context": "Operator declares iPhone native group: target Bluetooth + reference Wi-Fi, common probe",
            "capture_device": "Synthetic shared ADC", "geometry": "Matched near-field capture distances"}
    response = await client.post(f"/api/v1/rooms/{target['id']}/calibration", json=body)
    assert response.status_code == 201, response.text
    session = response.json()
    return app, client, target, reference, body, f"/api/v1/rooms/{target['id']}/calibration/{session['id']}", session


async def cross_candidate(api):
    app, client, target, reference, body, endpoint, session = await cross_local_rooms(api)
    for take in range(3):
        session = await import_take(app, client, endpoint, session["id"], take)
    target = (await client.patch(f"/api/v1/rooms/{target['id']}", json={"expected_revision": target["revision"], "changes": {"enabled": False}})).json()["room"]
    return app, client, target, reference, body, endpoint, session


async def test_cross_zone_duplicate_local_ids_measure_apply_verify_and_exact_target_rollback(api):
    app, client, target, reference, _body, endpoint, session = await cross_candidate(api)
    reference_before = app.state.service.store.get_room(reference["id"])
    assert session["reference_room_id"] == reference["id"]
    assert session["reference_configuration"]["local_audio_device"] == "hw:CARD=ReferenceUSB,DEV=0"
    assert session["result"]["candidate_offset_ms"] == -23
    applied = await client.post(endpoint + "/apply", json={"expected_revision": target["revision"],
        "expected_generation": session["generation"], "expected_reference_revision": reference["revision"]})
    assert applied.status_code == 200, applied.text
    target = applied.json()["room"]
    assert target["speakers"][0]["offset_ms"] == -23
    assert app.state.service.store.get_room(reference["id"]) == reference_before
    assert (await client.get(endpoint.replace(target["id"], reference["id"]))).status_code == 404
    target = (await client.patch(f"/api/v1/rooms/{target['id']}", json={"expected_revision": target["revision"], "changes": {"enabled": True}})).json()["room"]
    for take in range(3, 6):
        session = await import_take(app, client, endpoint, session["id"], take, verification=True, delay_ms=0)
    assert session["status"] == "measured_at_this_setup"
    backend = session["verification_backend"][0]
    assert "reported_offsets" not in backend  # Two ID0 outputs cannot share a dictionary key.
    assert backend["reported_endpoints"] == [
        {"room_id": target["id"], "output_id": "0", "offset_ms": -23},
        {"room_id": reference["id"], "output_id": "0", "offset_ms": 0}]
    evidence = (await client.get(endpoint + "/export")).json()
    assert evidence["playback_context"].startswith("Operator declares iPhone")
    assert "not observed" in evidence["grouping_provenance"]
    assert evidence["recordings"][0]["environment_at_import"]["reference_room_id"] == reference["id"]
    target = (await client.patch(f"/api/v1/rooms/{target['id']}", json={"expected_revision": target["revision"], "changes": {"enabled": False}})).json()["room"]
    restored = await client.post(endpoint + "/rollback", json={"expected_revision": target["revision"],
        "expected_generation": session["generation"], "expected_reference_revision": reference["revision"]})
    assert restored.status_code == 200, restored.text
    assert restored.json()["room"]["speakers"][0]["offset_ms"] == 0
    assert app.state.service.store.get_room(reference["id"]) == reference_before


async def test_cross_zone_requires_declared_context_reference_revision_and_distinct_endpoint(api):
    _app, client, target, reference, body, _endpoint, _session = await cross_local_rooms(api)
    path = f"/api/v1/rooms/{target['id']}/calibration"
    for field in ["playback_context", "expected_reference_revision"]:
        missing = dict(body)
        del missing[field]
        result = await client.post(path, json=missing)
        assert result.status_code == 400 and "Cross-zone" in result.json()["error"]
    same_pair = {**body, "reference_room_id": target["id"], "expected_reference_revision": target["revision"]}
    assert (await client.post(path, json=same_pair)).status_code == 400
    stale = {**body, "expected_reference_revision": reference["revision"] - 1}
    assert (await client.post(path, json=stale)).status_code == 409
    assert (await client.post(path, json={**body, "reference_room_id": "not-a-canonical-uuid"})).status_code == 422


async def test_reference_volume_change_needs_fresh_revision_but_preserves_configuration_guard(api):
    app, client, target, reference, _body, endpoint, session = await cross_candidate(api)
    current_reference = (await client.patch(f"/api/v1/rooms/{reference['id']}", json={"expected_revision": reference["revision"], "changes": {"volume": 74}})).json()["room"]
    action = {"expected_revision": target["revision"], "expected_generation": session["generation"]}
    assert (await client.post(endpoint + "/apply", json=action)).status_code == 409
    assert (await client.post(endpoint + "/apply", json={**action, "expected_reference_revision": reference["revision"]})).status_code == 409
    result = await client.post(endpoint + "/apply", json={**action, "expected_reference_revision": current_reference["revision"]})
    assert result.status_code == 200, result.text
    assert app.state.service.store.get_room(reference["id"]).volume == 74
    assert result.json()["calibration"]["reference_revision"] == reference["revision"]


async def test_reference_profile_edit_blocks_baseline_apply_and_import(api):
    app, client, target, reference, _body, endpoint, session = await cross_candidate(api)
    changed = (await client.patch(f"/api/v1/rooms/{reference['id']}/speakers/0/offset", json={"expected_revision": reference["revision"], "offset_ms": 7})).json()["room"]
    result = await client.post(endpoint + "/apply", json={"expected_revision": target["revision"], "expected_generation": session["generation"],
        "expected_reference_revision": changed["revision"]})
    assert result.status_code == 409 and "Reference" in result.json()["error"]
    result = await client.post(endpoint + "/recordings", content=recording(app.state.service.calibrations.sessions[session["id"]].seed, take=7), headers={"Content-Type": "audio/wav"})
    assert result.status_code == 409 and "Reference" in result.json()["error"]
    assert app.state.service.store.get_room(target["id"]).speakers[0].offset_ms == 0


async def test_cross_verification_requires_reference_enabled_selected_and_exact_own_readback(api, monkeypatch):
    app, client, target, reference, _body, endpoint, session = await cross_candidate(api)
    applied = (await client.post(endpoint + "/apply", json={"expected_revision": target["revision"], "expected_generation": session["generation"],
        "expected_reference_revision": reference["revision"]})).json()
    target = applied["room"]
    await client.patch(f"/api/v1/rooms/{target['id']}", json={"expected_revision": target["revision"], "changes": {"enabled": True}})
    original = app.state.service.runtime.call
    async def wrong_reference(operation, payload=None):
        result = await original(operation, payload)
        if operation == "outputs" and payload["room_id"] == reference["id"]:
            for output in result["outputs"]:
                if output["id"] == "0":
                    output["offset_ms"] = 13
        return result
    monkeypatch.setattr(app.state.service.runtime, "call", wrong_reference)
    data = recording(app.state.service.calibrations.sessions[session["id"]].seed, take=3, delay_ms=0)
    result = await client.post(endpoint + "/recordings?verification=true", content=data, headers={"Content-Type": "audio/wav"})
    assert result.status_code == 409 and "Both measured speakers" in result.json()["error"]
    monkeypatch.setattr(app.state.service.runtime, "call", original)
    await client.patch(f"/api/v1/rooms/{reference['id']}", json={"expected_revision": reference["revision"], "changes": {"enabled": False}})
    result = await client.post(endpoint + "/recordings?verification=true", content=data, headers={"Content-Type": "audio/wav"})
    assert result.status_code == 409 and "both measured rooms" in result.json()["error"]
    assert (await client.get(endpoint)).json()["verification_recordings"] == []


async def test_deleted_reference_keeps_export_but_blocks_apply_and_import(api):
    app, client, target, reference, _body, endpoint, session = await cross_candidate(api)
    reference = (await client.patch(f"/api/v1/rooms/{reference['id']}", json={"expected_revision": reference["revision"], "changes": {"enabled": False}})).json()["room"]
    assert (await client.delete(f"/api/v1/rooms/{reference['id']}?expected_revision={reference['revision']}")).status_code == 200
    exported = await client.get(endpoint + "/export")
    assert exported.status_code == 200 and exported.json()["reference_room_id"] == reference["id"]
    rejected = await client.post(endpoint + "/apply", json={"expected_revision": target["revision"], "expected_generation": session["generation"],
        "expected_reference_revision": reference["revision"]})
    assert rejected.status_code == 409 and "reference room was removed" in rejected.json()["error"]
    result = await client.post(endpoint + "/recordings", content=recording(app.state.service.calibrations.sessions[session["id"]].seed, take=7), headers={"Content-Type": "audio/wav"})
    assert result.status_code == 409 and "reference room was removed" in result.json()["error"]

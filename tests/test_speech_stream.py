"""Persistent speech admission preserves room ownership and bounded PCM delivery."""

import asyncio
from contextlib import asynccontextmanager
import struct
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from shiri.readiness import transport_fingerprint
from shiri.rpc import AdmissionRefused, RpcError, StreamReply, serve_rpc, write_message
from shiri.speech_stream import HEADER, PCM, open_speech, read_packet
from test_pcm_speech import ready_worker as ready_worker
from test_runtime import broker, room, runtime_room


@asynccontextmanager
async def endpoints(worker, tmp_path, *, relayed=True):
    # macOS Unix addresses have a shorter limit than pytest's temporary paths.
    with TemporaryDirectory(prefix="ss-", dir="/tmp") as directory:
        directory = Path(directory)
        audio = await serve_rpc(directory / "audio.sock", worker.dispatch)
        control = broker(tmp_path)
        zone = runtime_room(tmp_path, room(enabled=True, speakers=[{"id": "10", "name": "Speaker", "protocol": "airplay2"}]))
        zone.client, zone.status, zone.launch_generation = object(), "running", uuid4().hex
        zone.selected_ids = ["10"]
        zone.worker_sockets["audio"] = directory / "audio.sock"
        control.rooms[zone.desired.id] = zone
        api = await serve_rpc(directory / "broker.sock", control.dispatch) if relayed else None
        payload = {"room_id": zone.desired.id, "session_id": "text-speech", "request_id": "request-1",
                   "duck_on_prepare": True, "transport_fingerprint": transport_fingerprint(zone.desired)}
        try:
            yield directory / ("broker.sock" if relayed else "audio.sock"), payload, control, zone
        finally:
            if api:
                api.close()
                await api.wait_closed()
            audio.close()
            await audio.wait_closed()


async def test_binary_two_hop_stream_paces_in_worker_and_finishes_exactly(ready_worker, tmp_path, monkeypatch):
    worker, _native, _writer, _client, overlay = ready_worker
    async with endpoints(worker, tmp_path) as (socket, payload, control, zone):
        connections = []
        connect = asyncio.open_unix_connection

        async def counted_connect(path, **kwargs):
            connections.append(path)
            return await connect(path, **kwargs)

        monkeypatch.setattr(asyncio, "open_unix_connection", counted_connect)
        session = await open_speech(socket, payload)
        assert session.prepared["launch_generation"] == zone.launch_generation
        assert session.prepared["transport_fingerprint"] == payload["transport_fingerprint"]
        assert control.sessions == {payload["session_id"]: zone.desired.id}
        # Every subsequent sample/EOF travels on the two admitted links.
        rpc = AsyncMock(side_effect=AssertionError("Per-frame control RPC is forbidden"))
        monkeypatch.setattr("shiri.runtime.broker.call_rpc", rpc)
        pcm = struct.pack("<h", 321) * 960
        first = await session.send_pcm(pcm, 1, 0)
        for index in range(1, 150):
            last = await session.send_pcm(pcm, index + 1, index * 960)
        assert last["last_pcm_admitted_monotonic_ns"] - first["first_pcm_admitted_monotonic_ns"] >= 2_978_000_000
        result = await session.finish(150, 144000)
        assert result["finished"] and result["admitted_frames"] == 144000
        assert len(overlay.sent) == 150 and overlay.sent[0][0] == pcm
        assert len(connections) == 2
        assert worker.session is None and session.writer.transport.is_closing()
        assert await session.close() == result
        await asyncio.sleep(0)
        assert not control.sessions and not zone.speech_streams
        rpc.assert_not_called()


@pytest.mark.parametrize("change", ["route", "launch", "client", "disabled", "removed"])
async def test_admitted_route_is_revoked_before_next_frame(ready_worker, tmp_path, change):
    worker, _native, _writer, _client, overlay = ready_worker
    async with endpoints(worker, tmp_path) as (socket, payload, control, zone):
        session = await open_speech(socket, payload)
        if change == "route":
            zone.desired = zone.desired.model_copy(update={"interface": "eth1"})
        elif change == "launch":
            zone.launch_generation = uuid4().hex
        elif change == "client":
            zone.client = object()
        elif change == "disabled":
            zone.desired = zone.desired.model_copy(update={"enabled": False})
        else:
            zone.removing = True
        with pytest.raises(RpcError, match="retired"):
            await session.send_pcm(b"\x01\x01" * 960, 1, 0)
        await asyncio.sleep(0.02)
        assert not overlay.sent and worker.session is None
        assert not control.sessions and not zone.speech_streams


async def test_gain_edits_do_not_revoke_frozen_transport(ready_worker, tmp_path):
    worker, *_ = ready_worker
    async with endpoints(worker, tmp_path) as (socket, payload, _control, zone):
        session = await open_speech(socket, payload)
        zone.desired = zone.desired.model_copy(update={"volume": 19, "duck_gain": 0.31})
        await session.send_pcm(b"\0\0" * 2, 1, 0)
        await session.finish(1, 2)


@pytest.mark.parametrize("action", ["offer", "control", "close"])
async def test_rtc_control_cannot_revoke_a_binary_stream_indirectly(ready_worker, tmp_path, action):
    worker, *_ = ready_worker
    async with endpoints(worker, tmp_path) as (socket, payload, control, _zone):
        session = await open_speech(socket, payload)
        generation = control.session_generations[payload["session_id"]]
        with pytest.raises(RpcError, match="binary connection"):
            await control.dispatch("speech", {**payload, "action": action, "request_id": "other-request"})
        assert control.session_generations[payload["session_id"]] is generation
        await session.send_pcm(b"\0\0", 1, 0)
        await session.finish(1, 1)


async def test_bad_frame_retires_its_owner_and_allows_a_fresh_job(ready_worker, tmp_path):
    worker, *_ = ready_worker
    async with endpoints(worker, tmp_path) as (socket, payload, control, zone):
        session = await open_speech(socket, payload)
        with pytest.raises(RpcError, match="position"):
            await session.send_pcm(b"\0\0", 3, 0)
        assert (await session.close())["admitted_frames"] == 0
        assert worker.session is None and not control.sessions and not zone.speech_streams
        successor = await open_speech(socket, {**payload, "request_id": "next-request"})
        await successor.send_pcm(b"\0\0", 1, 0)
        await successor.finish(1, 1)


async def test_unconfirmed_retirement_keeps_reconfiguration_fenced(ready_worker, tmp_path, monkeypatch):
    from shiri.runtime.system import RuntimeFailure

    worker, *_ = ready_worker
    async with endpoints(worker, tmp_path) as (socket, payload, control, zone):
        session = await open_speech(socket, payload)
        release = worker._release
        monkeypatch.setattr(worker, "_release", AsyncMock(side_effect=RpcError("audio_unavailable", "Lost retirement")))
        try:
            with pytest.raises(RpcError):
                await session.close()
            for _ in range(2):
                with pytest.raises(RuntimeFailure, match="unconfirmed"):
                    await control._retire_speech_streams(zone)
                assert len(zone.speech_streams) == 1
        finally:
            monkeypatch.setattr(worker, "_release", release)


async def test_lost_initial_admission_reply_keeps_output_reassignment_fenced(ready_worker, tmp_path, monkeypatch):
    import shiri.rpc as rpc
    from shiri.runtime.latency import latency_plan, room_buffer_ms
    from shiri.runtime.system import RuntimeFailure

    worker, *_ = ready_worker
    admitted, release_reply, broker_ended, worker_retired = (asyncio.Event() for _ in range(4))
    write = rpc.write_message

    async def held_reply(writer, reply):
        result = reply.get("result", {})
        if result.get("stream_id") and "launch_generation" not in result:
            admitted.set()
            await release_reply.wait()
        await write(writer, reply)

    monkeypatch.setattr(rpc, "write_message", held_reply)
    retire = worker._release

    async def observed_retirement(*args, **kwargs):
        await retire(*args, **kwargs)
        worker_retired.set()

    monkeypatch.setattr(worker, "_release", observed_retirement)
    async with endpoints(worker, tmp_path) as (socket, payload, control, zone):
        opening = control.open_speech

        async def observed_open(*args):
            try:
                return await opening(*args)
            finally:
                broker_ended.set()

        monkeypatch.setattr(control, "open_speech", observed_open)
        caller = asyncio.create_task(open_speech(socket, payload))
        try:
            await asyncio.wait_for(admitted.wait(), 1)
            assert worker.session is not None
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            await asyncio.wait_for(broker_ended.wait(), 1)
            assert control.sessions == {payload["session_id"]: zone.desired.id}
            assert len(zone.speech_streams) == 1
            zone.timing = (room_buffer_ms(zone.desired), latency_plan([zone.desired]).common_horizon_ms)
            successor = zone.desired.speakers[0].model_copy(update={"id": "11"})
            with pytest.raises(RuntimeFailure, match="unconfirmed"):
                await control.set_outputs(zone, [successor])
            assert zone.selected_ids == ["10"]
            release_reply.set()
            await asyncio.wait_for(worker_retired.wait(), 1)
            assert worker.session is None
            # EOF retires the worker, but the lost acknowledgement cannot be
            # invented by the broker. Its exact launch remains the final fence.
            with pytest.raises(RuntimeFailure, match="unconfirmed"):
                await control._retire_speech_streams(zone)
        finally:
            release_reply.set()
            if not caller.done():
                caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.parametrize("refusal", ["invalid_identity", "invalid_request", "existing_owner"])
async def test_proven_admission_refusal_does_not_retain_a_new_room_fence(ready_worker, tmp_path, refusal):
    worker, *_ = ready_worker
    async with endpoints(worker, tmp_path) as (socket, payload, control, zone):
        existing = await open_speech(socket, payload) if refusal == "existing_owner" else None
        rejected = {**payload, "session_id": "refused-attempt"}
        if refusal == "invalid_identity":
            rejected["request_id"] = ""
        elif refusal == "invalid_request":
            rejected["duck_on_prepare"] = 1
        with pytest.raises(AdmissionRefused):
            await open_speech(socket, rejected)
        assert "refused-attempt" not in control.sessions
        assert len(zone.speech_streams) == int(existing is not None)
        if existing is not None:
            await existing.close()
        successor = await open_speech(socket, {**payload, "session_id": "fresh-attempt"})
        await successor.close()


@pytest.mark.parametrize("code", ["runtime_error", "deadline_exceeded", "audio_unavailable"])
async def test_unproven_admission_error_cannot_clear_the_room_fence(ready_worker, tmp_path, monkeypatch, code):
    from shiri.runtime.system import RuntimeFailure

    worker, *_ = ready_worker
    monkeypatch.setattr(worker, "dispatch", AsyncMock(side_effect=RpcError(code, "Uncertain admission")))
    async with endpoints(worker, tmp_path) as (socket, payload, control, zone):
        with pytest.raises(RpcError, match="Uncertain admission") as failure:
            await open_speech(socket, payload)
        assert not isinstance(failure.value, AdmissionRefused)
        assert control.sessions == {payload["session_id"]: zone.desired.id}
        with pytest.raises(RuntimeFailure, match="unconfirmed"):
            await control._retire_speech_streams(zone)


async def test_forged_future_position_rejected_without_pacing_wait(ready_worker, tmp_path):
    worker, *_ = ready_worker
    async with endpoints(worker, tmp_path, relayed=False) as (socket, payload, *_):
        session = await open_speech(socket, payload)
        await session.send_pcm(b"\0\0" * 960, 1, 0)
        with pytest.raises(RpcError, match="position"):
            await asyncio.wait_for(session.send_pcm(b"\0\0", 2, 48_000 * 50), 0.3)
        assert (await session.close())["admitted_frames"] == 960


async def test_disconnect_retires_exact_worker_owner(ready_worker, tmp_path):
    worker, *_ = ready_worker
    async with endpoints(worker, tmp_path) as (socket, payload, control, zone):
        session = await open_speech(socket, payload)
        session.writer.transport.abort()
        for _ in range(100):
            if worker.session is None and not zone.speech_streams:
                break
            await asyncio.sleep(0.005)
        assert worker.session is None and not control.sessions and not zone.speech_streams


async def test_cancel_during_pcm_consumes_ack_before_exact_close(ready_worker, tmp_path):
    worker, *_ = ready_worker
    async with endpoints(worker, tmp_path) as (socket, payload, _control, _zone):
        session = await open_speech(socket, payload)
        await session.send_pcm(b"\0\0" * 960, 1, 0)
        sending = asyncio.create_task(session.send_pcm(b"\0\0" * 960, 2, 960))
        await asyncio.sleep(0.001)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        closed = await session.close()
        assert closed["admitted_frames"] == 1920 and worker.session is None


async def test_output_reconfiguration_waits_for_stream_retirement(ready_worker, tmp_path):
    worker, *_ = ready_worker
    async with endpoints(worker, tmp_path) as (socket, payload, control, zone):
        session = await open_speech(socket, payload)
        await session.send_pcm(b"\0\0" * 960, 1, 0)
        await control._retire_speech_streams(zone)
        assert worker.session is None and not zone.speech_streams and not control.sessions
        with pytest.raises(RpcError):
            await session.send_pcm(b"\0\0" * 960, 2, 960)


async def test_rpc_owns_stream_returned_during_disconnection(tmp_path):
    entered, admitted, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(_operation, _payload):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            admitted.set()
            return StreamReply({}, AsyncMock(), AsyncMock(side_effect=closed.set))

    with TemporaryDirectory(prefix="ss-", dir="/tmp") as directory:
        path = Path(directory) / "stream.sock"
        server = await serve_rpc(path, handler)
        try:
            _reader, writer = await asyncio.open_unix_connection(path)
            await write_message(writer, {"id": "r", "operation": "speech-stream", "payload": {}})
            await entered.wait()
            writer.transport.abort()
            await asyncio.wait_for(closed.wait(), 1)
            assert admitted.is_set()
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.parametrize("header", [HEADER.pack(PCM, 2**32 - 1), HEADER.pack(PCM, 1), HEADER.pack(55, 0)])
async def test_malformed_record_rejected_before_body_read(header):
    reader = asyncio.StreamReader()
    reader.feed_data(header)
    with pytest.raises(RpcError, match="record"):
        await asyncio.wait_for(read_packet(reader), 0.1)

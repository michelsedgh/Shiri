"""Native onset observations at real message/FIFO seams, without house audio."""
import asyncio
from dataclasses import replace
import json
import os
import struct
from uuid import uuid4

import pytest

from shiri.runtime.native import NativeController, NativeHandle, NativeMixer
from shiri.runtime.timing import FLAG_GAP, HEADER_BYTES, Kind, Packet, TimingError
from test_native_audio import Client, begin, controller, pcm
from test_native_ingress_diagnostics import sent, serve, until


def timed(c):
    clock = [15_000_000_500]
    c.mixer.now_ns = lambda: clock[0]
    return clock


def fresh_pcm(token, clock, **changes):
    return pcm(token, frames=240, monotonic_before_ns=clock[0]-500,
               monotonic_after_ns=clock[0]-300, **changes)


@pytest.mark.asyncio
async def test_cold_and_flush_trace_separates_backend_prepare_from_original_pcm_calendar():
    c, writer, client = controller()
    clock = timed(c)
    await c.initialize()
    original = client.request
    async def prepared(method, path, *, json):
        if json["session_id"]:
            clock[0] += 1_250_000_000 if json["generation"] == 1 else 10_000_000
        return await original(method, path, json=json)
    client.request = prepared
    handle = NativeHandle(c)
    try:
        token = await c.begin(begin(), handle)
        cold_input = fresh_pcm(token, clock)
        await c.message(cold_input, handle)
        cold = c.health()["music_startup"]["records"][0]
        assert cold["trigger"] == "begin"
        assert cold["backend_prepare"]["elapsed_ns"] == 1_250_000_000
        assert cold["backend_prepare"]["acknowledged"] is True
        assert cold["grant_ready_monotonic_ns"] == clock[0]
        assert cold["grant_sent_monotonic_ns"] is None  # Direct fixture is not a socket reply.
        assert cold["first_pcm"]["native_presentation_ns"] == cold_input.presentation_ns
        assert cold["first_pcm"]["native_presentation_lead_ns"] == 149_999_600
        assert writer.packets[-1].presentation_ns == clock[0] + 149_999_600 + c.mixer.relay_delay_ns
        assert cold["first_fifo_write"]["output_presentation_monotonic_ns"] == writer.packets[-1].presentation_ns
        assert cold["first_fifo_write"]["written_pcm_bytes"] == 960
        assert len(client.requests) == 2  # Observation performs no extra backend command.

        clock[0] += 40_000_000_000
        resumed = await c.message(replace(token, kind=Kind.FLUSH, generation=2), handle)
        resumed_input = fresh_pcm(resumed, clock)
        await c.message(resumed_input, handle)
        records = c.health()["music_startup"]["records"]
        assert len(records) == 2 and records[0]["closed"] is True
        warm = records[1]
        assert warm["trigger"] == "flush" and warm["native_generation"] == 2
        assert warm["backend_prepare"]["elapsed_ns"] == 10_000_000
        assert warm["first_pcm"]["native_presentation_lead_ns"] == cold["first_pcm"]["native_presentation_lead_ns"]
        assert writer.packets[-1].presentation_ns == clock[0] + 149_999_600 + c.mixer.relay_delay_ns
        assert len(client.requests) == 3
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_actual_socket_reply_and_first_silent_block_are_not_called_audible():
    c, writer, _ = controller()
    clock = timed(c)
    await c.initialize()
    producer, task = serve(c)
    try:
        await sent(producer, begin())
        answer = Packet.decode(await asyncio.wait_for(asyncio.get_running_loop().sock_recv(producer, 4096), 1))
        await until(lambda: c.health()["music_startup"]["records"][0]["grant_sent_monotonic_ns"] is not None)
        await sent(producer, fresh_pcm(answer, clock, value=0))
        await until(lambda: len(writer.packets) == 1)
        first = c.health()["music_startup"]["records"][0]
        assert first["first_pcm"] is not None and first["first_fifo_write"] is not None
        assert first["first_nonzero_input_pcm_monotonic_ns"] is None
        clock[0] += 5_000_000
        await sent(producer, fresh_pcm(answer, clock, sequence=1, frame_index=240))
        await until(lambda: len(writer.packets) == 2)
        after = c.health()["music_startup"]["records"][0]
        assert after["first_nonzero_input_pcm_monotonic_ns"] == clock[0]
        assert after["first_pcm"] == first["first_pcm"]
        assert "physical output are not observed" in c.health()["music_startup"]["scope"]
        text = json.dumps(c.health()["music_startup"])
        assert answer.session_id not in text and answer.incarnation.hex() not in text
        assert "audible_monotonic_ns" not in text and "pcm_payload" not in text
        assert not task.done()
    finally:
        producer.close()
        await c.close()


@pytest.mark.asyncio
async def test_fifo_refusal_records_drop_and_waits_for_actual_success():
    c, writer, _ = controller()
    clock = timed(c)
    await c.initialize()
    handle = NativeHandle(c)
    original = writer.write
    def refused(packet):
        writer.dropped_bytes += len(packet.pcm)
        return False
    writer.write = refused
    try:
        token = await c.begin(begin(), handle)
        await c.message(fresh_pcm(token, clock), handle)
        first = c.health()["music_startup"]["records"][0]
        assert first["first_fifo_write"] is None
        assert first["fifo_dropped_bytes_before_first_write"] == 960
        writer.write = original
        clock[0] += 5_000_000
        await c.message(fresh_pcm(token, clock, sequence=1, frame_index=240, flags=token.flags | FLAG_GAP), handle)
        after = c.health()["music_startup"]["records"][0]
        assert after["first_fifo_write"]["frame_index"] == 240
        assert after["first_fifo_write"]["completed_monotonic_ns"] == clock[0]
        assert after["first_pcm"] == first["first_pcm"]
        assert after["fifo_dropped_bytes_before_first_write"] == 960
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_actual_named_fifo_confirms_recorded_write_and_preserved_first_sample(tmp_path):
    path = tmp_path / "timed.pipe"
    os.mkfifo(path, 0o600)
    reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    mixer = NativeMixer(path, now_ns=lambda: 15_000_000_500)
    c = NativeController(str(uuid4()), mixer, Client())
    clock = timed(c)
    try:
        await c.initialize()
        handle = NativeHandle(c)
        token = await c.begin(begin(), handle)
        original = fresh_pcm(token, clock)
        await c.message(original, handle)
        data = os.read(reader, 4096)
        packets = []
        while data:
            length = HEADER_BYTES + struct.unpack_from("!I", data, 16)[0]
            packets.append(Packet.decode(data[:length]))
            data = data[length:]
        assert sum(packet.frames for packet in packets) == original.frames
        assert b"".join(packet.pcm for packet in packets) == original.pcm
        assert packets[0].frame_index == original.frame_index
        trace = c.health()["music_startup"]["records"][0]
        first = trace["first_fifo_write"]
        assert first["written_pcm_bytes"] == len(original.pcm)
        assert first["output_presentation_monotonic_ns"] == packets[0].presentation_ns
        assert first["output_presentation_monotonic_ns"] == clock[0] + 149_999_600 + mixer.relay_delay_ns
        assert trace["fifo_dropped_bytes_before_first_write"] == 0
    finally:
        await c.close()
        mixer.close()
        os.close(reader)


@pytest.mark.asyncio
async def test_failed_backend_ack_keeps_failure_without_inventing_ready_or_pcm():
    c, writer, client = controller()
    clock = timed(c)
    await c.initialize()
    client.bad_ack = True
    try:
        with pytest.raises(TimingError):
            await c.begin(begin(), NativeHandle(c))
        trace = c.health()["music_startup"]["records"][0]
        assert trace["backend_prepare"]["acknowledged"] is False
        assert trace["status"] == "prepare_failed" and trace["closed"] is True
        assert trace["grant_ready_monotonic_ns"] is None and trace["first_pcm"] is None
        assert trace["first_fifo_write"] is None and not writer.packets
        assert trace["backend_prepare"]["completed_monotonic_ns"] == clock[0]
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_late_callbacks_and_mutated_snapshots_cannot_modify_successor_or_exceed_bound():
    c, _, _ = controller()
    clock = timed(c)
    await c.initialize()
    handle = NativeHandle(c)
    try:
        first = await c.begin(begin(), handle)
        first_token = handle.token
        for generation in range(2, 13):
            clock[0] += 1_000_000
            await c.message(replace(first, kind=Kind.FLUSH, generation=generation), handle)
        trace = c.health()["music_startup"]
        assert trace["retained_limit"] == 8 and len(trace["records"]) == 8
        assert [record["native_generation"] for record in trace["records"]] == list(range(5, 13))
        before = c.health()["music_startup"]
        c.music_startup.grant_sent(first_token, 1, clock[0])
        assert c.health()["music_startup"] == before
        trace["records"][-1]["backend_prepare"]["acknowledged"] = False
        trace["records"].clear()
        assert c.health()["music_startup"] == before
        successor = NativeHandle(c)
        await c.begin(begin(), successor)
        current = c.health()["music_startup"]
        c.music_startup.retire(first_token)
        c.music_startup.grant_ready(first_token, 12, clock[0])
        assert c.health()["music_startup"] == current
        assert not current["records"][-1]["closed"]
    finally:
        await c.close()

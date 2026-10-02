"""Terminal fixture regressions; these are not a real network gate receipt."""
from dataclasses import replace
import asyncio
import importlib.util
from pathlib import Path
import socket
from uuid import uuid4

import numpy as np
import pytest

from shiri.runtime.timing import Clock, FLAG_AIRPLAY2, FLAG_GAP, FLAG_SPEECH_ONLY, Kind, Packet, RATE, TimingError, ZERO_UUID
from shiri.runtime.receiver_volume import BIND, BOUND, Message, SIZE

PATH = Path(__file__).with_name("linux")/"native_network_observer.py"
SPEC = importlib.util.spec_from_file_location("native_network_observer_under_test", PATH)
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


def begin():
    return Packet(Kind.BEGIN, uuid4().bytes, flags=FLAG_AIRPLAY2)


def pcm(grant, *, sequence=1, frame_index=0, now_ns=100_000_000_000, data=None, frames=960):
    data = bytes(frames*4) if data is None else data
    return replace(grant, kind=Kind.PCM, clock=Clock.RAW, sequence=sequence, frame_index=frame_index,
                   frames=frames, pcm=data, presentation_ns=now_ns+150_000_000,
                   clock_sample_ns=now_ns-100, monotonic_before_ns=now_ns-200,
                   monotonic_after_ns=now_ns)


def test_exact_grant_freshness_and_no_timing_rewrite():
    registry, first = observer.CaptureRegistry(), begin()
    grant = registry.begin(first, 1)
    assert grant.kind is Kind.GRANT and grant.session == first.session
    assert grant.incarnation != ZERO_UUID and grant.epoch == 1
    assert grant.presentation_ns == first.presentation_ns == 0
    packet = pcm(grant)
    before = packet.encode()
    registry.message(packet, 1, now_ns=100_000_000_000)
    assert packet.encode() == before
    assert registry.frames == 960 and registry.blocks == 1
    assert registry.ring[-1][3] == packet.presentation_ns


@pytest.mark.parametrize("mutation", [
    {"epoch": 1}, {"incarnation": uuid4().bytes}, {"generation": 2},
    {"flags": FLAG_SPEECH_ONLY}, {"flags": 0}, {"kind": Kind.END},
])
def test_unadmitted_or_wrong_protocol_begin_fenced(mutation):
    registry = observer.CaptureRegistry()
    with pytest.raises(TimingError):
        registry.begin(replace(begin(), **mutation), 1)
    assert registry.current is None and not registry.seen


def test_retired_uuid_and_bounded_session_registry():
    registry = observer.CaptureRegistry(maximum_sessions=2)
    first = begin()
    registry.begin(first, 1)
    registry.disconnected(1)
    with pytest.raises(TimingError):
        registry.begin(first, 2)
    registry.begin(begin(), 2)
    registry.disconnected(2)
    with pytest.raises(TimingError):
        registry.begin(begin(), 3)
    assert len(registry.seen) == 2


@pytest.mark.parametrize("mutation", [{"epoch": 2}, {"incarnation": uuid4().bytes},
                                      {"session": uuid4().bytes}, {"generation": 2}])
def test_wrong_complete_capture_token_cannot_append(mutation):
    registry = observer.CaptureRegistry()
    grant = registry.begin(begin(), 1)
    with pytest.raises(TimingError):
        registry.message(replace(pcm(grant), **mutation), 1, now_ns=100_000_000_000)
    assert registry.blocks == registry.frames == 0


def test_connection_identity_cannot_end_newer_owner():
    registry = observer.CaptureRegistry()
    old = registry.begin(begin(), 1)
    registry.disconnected(1)
    new = registry.begin(begin(), 2)
    registry.disconnected(1)
    assert registry.current == (2, new.session, new.epoch)
    with pytest.raises(TimingError):
        registry.message(replace(old, kind=Kind.END), 1)
    assert registry.current == (2, new.session, new.epoch)


def test_flush_grant_preserves_exact_owner_and_retires_old_pcm():
    registry = observer.CaptureRegistry()
    grant = registry.begin(begin(), 1)
    registry.message(pcm(grant), 1, now_ns=100_000_000_000)
    flush = replace(grant, kind=Kind.FLUSH, generation=2)
    new_grant = registry.message(flush, 1)
    assert new_grant.incarnation == grant.incarnation and new_grant.epoch == grant.epoch
    assert new_grant.generation == 2
    with pytest.raises(TimingError):
        registry.message(pcm(grant, sequence=2, frame_index=960), 1, now_ns=100_000_000_000)
    registry.message(pcm(new_grant), 1, now_ns=100_000_000_000)
    registry.message(replace(new_grant, kind=Kind.END), 1)
    assert registry.current is None and registry.ends == registry.flushes == 1


def test_explicit_missing_frames_required_and_repeated_pcm_rejected():
    registry = observer.CaptureRegistry()
    grant = registry.begin(begin(), 1)
    first = pcm(grant)
    registry.message(first, 1, now_ns=100_000_000_000)
    with pytest.raises(TimingError):
        registry.message(first, 1, now_ns=100_000_000_000)
    missing = pcm(grant, sequence=3, frame_index=1920, now_ns=100_040_000_000)
    with pytest.raises(TimingError):
        registry.message(missing, 1, now_ns=100_040_000_000)
    registry.message(replace(missing, flags=FLAG_AIRPLAY2|FLAG_GAP), 1, now_ns=100_040_000_000)
    assert registry.fence.gaps == 1


def test_stale_native_clock_mapping_never_enters_ring():
    registry = observer.CaptureRegistry()
    grant = registry.begin(begin(), 1)
    with pytest.raises(TimingError):
        registry.message(pcm(grant), 1, now_ns=101_000_000_000)
    assert registry.blocks == 0 and not registry.ring


def test_decoded_frequency_oracle_and_bounded_ring():
    registry = observer.CaptureRegistry(ring_seconds=1)
    grant = registry.begin(begin(), 1)
    for index in range(150):
        sample = np.arange(960)+index*960
        mono = (6000*np.sin(2*np.pi*440*sample/RATE)+1200*np.sin(2*np.pi*880*sample/RATE)).astype("<i2")
        data = np.column_stack((mono, mono)).astype("<i2").tobytes()
        now = 100_000_000_000+index*20_000_000
        registry.message(pcm(grant, sequence=index+1, frame_index=index*960, now_ns=now, data=data), 1, now_ns=now)
    assert registry.ring_bytes <= RATE*4
    assert registry.frames == 150*960
    spectrum = registry.spectrum(since_ns=102_000_000_000)
    assert spectrum["music_440_amplitude"] == pytest.approx(6000, abs=2)
    assert spectrum["speech_880_amplitude"] == pytest.approx(1200, abs=2)
    assert spectrum["channel_error"] == 0
    assert spectrum["minimum_presentation_lead_ns"] == 150_000_000
    assert registry.spectrum(since_ns=103_000_000_000) is None


def test_late_silent_and_audible_packets_are_both_retained_without_timestamp_rewrite():
    registry = observer.CaptureRegistry()
    grant = registry.begin(begin(), 1)
    now = 100_000_000_000
    first = replace(pcm(grant, now_ns=now), presentation_ns=now-1_000_000)
    before = first.encode()
    registry.message(first, 1, now_ns=now)
    now += 20_000_000
    audible = np.full((960, 2), -32768, dtype='<i2').tobytes()
    second = replace(pcm(grant, sequence=2, frame_index=960, now_ns=now, data=audible),
                     presentation_ns=now-2_000_000)
    registry.message(second, 1, now_ns=now)
    snapshot = registry.snapshot()
    assert first.encode() == before
    assert registry.ring[0][3] == first.presentation_ns
    assert snapshot['late_blocks'] == 2 and snapshot['late_frames'] == 1920
    assert snapshot['minimum_presentation_lead_ns'] == -2_000_000
    assert [sample['pcm_peak'] for sample in snapshot['late_packets']] == [0, 32768]
    assert all('pcm' not in sample for sample in snapshot['late_packets'])
    window = registry.deadline_observation(since_ns=now)
    assert window['blocks'] == window['late_blocks'] == 1
    assert window['frames'] == window['late_frames'] == 960
    assert window['late_packets'] == snapshot['late_packets'][-1:]
    assert registry.spectrum(since_ns=100_000_000_000, minimum_frames=1920)['minimum_presentation_lead_ns'] < 0


def test_late_metadata_bound_survives_pcm_ring_eviction_and_includes_zero_lead():
    registry = observer.CaptureRegistry(ring_seconds=1)
    grant = registry.begin(begin(), 1)
    for index in range(100):
        now = 100_000_000_000+index*20_000_000
        packet = replace(pcm(grant, sequence=index+1, frame_index=index*960, now_ns=now),
                         presentation_ns=now)
        registry.message(packet, 1, now_ns=now)
    snapshot = registry.snapshot()
    assert snapshot['late_blocks'] == 100 and snapshot['late_frames'] == 96000
    assert snapshot['minimum_presentation_lead_ns'] == 0
    assert len(snapshot['late_packets']) == 64
    assert [sample['sequence'] for sample in snapshot['late_packets']] == list(range(37, 101))
    assert registry.ring_bytes <= RATE*4
    window = registry.deadline_observation(since_ns=101_500_000_000)
    assert window['blocks'] == window['late_blocks'] == 25
    assert window['sample_limit'] == 64
    assert len(window['late_packets']) == 25


def test_delayed_observer_read_retains_sample_age_and_same_generation_read_gap():
    registry = observer.CaptureRegistry()
    grant = registry.begin(begin(), 1)
    sample = 100_000_000_000
    registry.message(pcm(grant, now_ns=sample-20_000_000), 1, now_ns=sample-20_000_000)
    packet = pcm(grant, sequence=2, frame_index=960, now_ns=sample)
    registry.message(packet, 1, now_ns=sample+200_000_000)
    late = registry.snapshot()['late_packets'][0]
    assert late['lead_ns'] == -50_000_000
    assert late['clock_sample_age_ns'] == 200_000_000
    assert late['receive_gap_ns'] == 220_000_000
    assert late['mapping_uncertainty_ns'] > 0
    assert late['epoch'] == grant.epoch and late['generation'] == grant.generation


def test_new_epoch_does_not_infer_stall_from_previous_session_silence():
    registry = observer.CaptureRegistry()
    old = registry.begin(begin(), 1)
    now = 100_000_000_000
    registry.message(pcm(old, now_ns=now), 1, now_ns=now)
    registry.disconnected(1)
    new = registry.begin(begin(), 2)
    now += 31_000_000_000
    registry.message(replace(pcm(new, now_ns=now), presentation_ns=now-1), 2, now_ns=now)
    late = registry.snapshot()['late_packets'][0]
    assert late['epoch'] == 2 and late['receive_gap_ns'] is None


@pytest.mark.parametrize("lane", ["pcm", "control"])
@pytest.mark.asyncio
async def test_cancel_before_handler_first_instruction_retires_accepted_descriptor(tmp_path, lane):
    sink = observer.NativeObserver(tmp_path/"music.sock", 1000, 1000)
    server, client = socket.socketpair()
    server.setblocking(False)
    task = sink._start_connection(server, 1, lane)
    # No scheduling point has allowed the handler to enter its try/finally.
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert server.fileno() == -1
    assert not sink.connection_sockets and not sink.tasks and sink.registry.current is None
    client.close()


@pytest.mark.asyncio
async def test_close_owns_pending_handler_descriptors_and_maps(tmp_path):
    sink = observer.NativeObserver(tmp_path/"music.sock", 1000, 1000)
    peers, servers = [], []
    for identity, lane in [(1, "pcm"), (2, "control")]:
        server, peer = socket.socketpair()
        server.setblocking(False)
        sink._start_connection(server, identity, lane)
        servers.append(server)
        peers.append(peer)
    await sink.close()
    assert all(server.fileno() == -1 for server in servers)
    assert not sink.connection_sockets and not sink.tasks and not sink.errors
    for peer in peers:
        peer.close()


@pytest.mark.asyncio
async def test_terminal_volume1_exact_default_bind_and_eof(tmp_path):
    sink = observer.NativeObserver(tmp_path/"music.sock", 1000, 1000)
    server, peer = socket.socketpair()
    server.setblocking(False)
    peer.setblocking(False)
    task = sink._start_connection(server, 1, "control")
    loop = asyncio.get_running_loop()
    await loop.sock_sendall(peer, Message(BIND).encode())
    wire = await loop.sock_recv(peer, SIZE+1)
    assert Message.decode(wire) == Message(BOUND, incarnation=sink.registry.incarnation, volume=100)
    assert sink.control_binds == 1 and sink.registry.frames == sink.registry.blocks == 0
    peer.close()
    await asyncio.wait_for(task, 1)
    await asyncio.sleep(0)
    assert not sink.connection_sockets and not sink.errors


@pytest.mark.parametrize("uid,gid", [(0, 1000), (1000, 0), (True, 1000)])
def test_terminal_client_must_be_nonroot_managed_identity(tmp_path, uid, gid):
    with pytest.raises(ValueError):
        observer.NativeObserver(tmp_path/"music.sock", uid, gid)


@pytest.mark.asyncio
async def test_failing_acceptor_joins_waiting_sibling_before_serve_returns(tmp_path):
    sink = observer.NativeObserver(tmp_path/"music.sock", 1000, 1000)
    sink.listeners = {sink.path: object(), sink.path.with_name("volume.sock"): object()}
    waiting_started = asyncio.Event()
    waiting = asyncio.Future()
    tasks, retired = {}, set()

    async def accept(_listener, lane):
        tasks[lane] = asyncio.current_task()
        try:
            if lane == "pcm":
                await waiting_started.wait()
                raise OSError("controlled acceptor failure")
            waiting_started.set()
            await waiting
        finally:
            retired.add(lane)
    sink._accept = accept
    try:
        with pytest.raises(OSError, match="controlled acceptor failure"):
            await sink._serve()
        assert retired == {"pcm", "control"}
        assert all(task.done() for task in tasks.values())
        assert waiting.cancelled()
    finally:
        # A preimage regression must not leak its intentionally uncovered
        # sibling into the test runner after recording the failed assertion.
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_parent_acceptor_cancellation_joins_both_waiting_children(tmp_path):
    sink = observer.NativeObserver(tmp_path/"music.sock", 1000, 1000)
    sink.listeners = {sink.path: object(), sink.path.with_name("volume.sock"): object()}
    both_started = asyncio.Event()
    tasks, pending, retired = {}, {}, set()

    async def accept(_listener, lane):
        tasks[lane] = asyncio.current_task()
        pending[lane] = asyncio.Future()
        if len(tasks) == 2:
            both_started.set()
        try:
            await pending[lane]
        finally:
            retired.add(lane)
    sink._accept = accept
    parent = asyncio.create_task(sink._serve())
    await both_started.wait()
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    assert retired == {"pcm", "control"}
    assert all(task.done() for task in tasks.values())
    assert all(future.cancelled() for future in pending.values())

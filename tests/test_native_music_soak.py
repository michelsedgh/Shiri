"""Pure bounded-soak rejection proofs; no device/unit/VM or elapsed-soak claim."""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
import hashlib
import importlib.util
import json
import signal
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from shiri.runtime.system import RuntimeFailure

pytest.importorskip("numpy")
pytest.importorskip("aiortc")
spec = importlib.util.spec_from_file_location(
    "tested_soak_group", Path(__file__).parent / "linux/check_native_grouping.py"
)
group = importlib.util.module_from_spec(spec)
spec.loader.exec_module(group)
soak = group.load_soak_module()
streaming = soak.streaming
RATE = group.RATE


@pytest.fixture(autouse=True)
def explicit_policy(monkeypatch):
    monkeypatch.setattr(soak, "_policy", None)
    soak.configure(40, 140)


def program(frame, frames=960):
    return soak.bind(group.program_pcm, envelope=soak.envelope)(frame, frames=frames)


class PureCapture(streaming.StreamingEvidence):
    def __init__(self):
        self.pending, self.absolute, self.buffer_metadata = deque(), {}, {}
        self.error = None
        self.capture_dropped = 0
        self.needs_latency = False
        self.sequence = group.observation.PcmSequence()
        self.total = self.discontinuities = 0
        self.max_packet_gap = 0
        self.rate = self.channels = self.format = None
        self.device, self.expected_base, self.clock_offset_ns = "exact-fixture", 1_000_000_000, 0
        self.initialize_streaming()

    def add(self, frame, *, data=None, change=None, rate=RATE):
        data = program(frame) if data is None else data
        frames = len(data) // 4
        anchor = 10_000_000_000 + frame * 1_000_000_000 // RATE
        at = anchor / 1e9
        metadata = {
            "offset": frame,
            "offset_end": frame + frames,
            "pts": anchor - 1_000_000_000,
            "duration": frames * 1_000_000_000 // RATE,
            "discont": frame == 0,
        }
        metadata.update(change or {})
        self.absolute[at] = anchor
        self.buffer_metadata[at] = dict(metadata)
        self.pending.append((at, data, rate, 2, "S16LE", metadata))
        self.poll()


def established():
    capture = PureCapture()
    for frame in range(0, RATE * 2, 960):
        capture.add(frame)
    assert capture.establish_reference(program)
    return capture


@pytest.mark.parametrize(
    "buffer,horizon", [(True, 140), (39, 140), (40, 139), (40, True), (4251, 5000), (40, 5001), (None, None)]
)
def test_explicit_policy_never_lowers_protocol_floor_or_timer_margin(buffer, horizon):
    with pytest.raises(RuntimeFailure):
        soak.Policy(buffer, horizon)


def test_policy_is_immutable_after_explicit_binding():
    assert soak.configure(40, 140) == soak.Policy(40, 140)
    with pytest.raises(RuntimeFailure):
        soak.configure(500, 750)


def test_old_21_second_music_waveform_and_constant_tail_remain_byte_exact():
    raw = group.program_pcm(0, frames=RATE * 21)
    assert (
        hashlib.sha256(raw).hexdigest() == "4de2f3f4c3cdcd0cf44a41308912190829a194005b8de39cd31038fc8693a417"
    )
    assert program(0, frames=RATE * 20) == raw[: RATE * 20 * 4]
    assert any(soak.envelope(frame) != group.envelope(frame) for frame in range(RATE * 20, RATE * 24, 5760))
    assert all(group.envelope(frame) == 1 for frame in range(RATE * 20, RATE * 1830, 5760))


def test_unique_complete_prefix_and_every_later_sample_are_verified():
    capture = established()
    assert capture.music_verified_frames == RATE * 2
    assert capture.music_first_pts_ns == 10_000_000_000
    data = bytearray(program(RATE * 2))
    data[129] ^= 1
    capture.add(RATE * 2, data=bytes(data))
    with pytest.raises(RuntimeFailure, match="immutable source frame calendar"):
        capture.verify_reference()
    with pytest.raises(RuntimeFailure):
        capture.poll()
    assert capture.music_verified_frames == RATE * 2


@pytest.mark.parametrize(
    "change",
    [
        {"offset": 481},
        {"offset_end": 959},
        {"offset": None, "offset_end": None, "pts": None, "duration": None},
        {"discont": True},
        {"duration": 19_000_000},
    ],
)
def test_lifetime_metadata_rejection_cannot_be_evicted_or_retried(change):
    capture = PureCapture()
    capture.add(0)
    with pytest.raises(RuntimeFailure):
        capture.add(960, change=change)
    assert capture.sequence.frames == 960
    assert len(capture.chunks) == 1
    with pytest.raises(RuntimeFailure):
        capture.poll()


def test_bounded_ring_preserves_honest_lifetime_and_local_window_receipts():
    capture = established()
    digest = hashlib.sha256(program(0, frames=RATE * 2))
    for frame in range(RATE * 2, RATE * 16, 960):
        data = program(frame)
        digest.update(data)
        capture.add(frame, data=data)
        capture.verify_reference()
        capture.evict_verified(len(capture.chunks))
    assert capture.music_verified_frames == RATE * 16
    assert capture.music_digest.hexdigest() == digest.hexdigest()
    assert capture.sequence.frames == RATE * 16
    assert capture.chunks.first > 0
    assert capture.retained_bytes <= RATE * 12 * 4 + 3840
    assert capture.peak_retained_bytes < streaming.MAX_BYTES
    snapshot = capture.snapshot(capture.previous_anchor - 8_000_000_000, capture.previous_anchor)
    assert snapshot.frame_continuity["verified_frames"] == sum(len(data) // 4 for data in snapshot.chunks)
    assert snapshot.frame_continuity["verified_blocks"] == len(snapshot.chunks)
    assert snapshot.lifetime["music_verified_frames"] == RATE * 16
    assert snapshot.frame_continuity["verified_frames"] < snapshot.lifetime["music_verified_frames"]
    old = deepcopy(snapshot.frame_continuity)
    capture.add(RATE * 16)
    capture.verify_reference()
    capture.evict_verified(len(capture.chunks))
    assert snapshot.frame_continuity == old
    assert snapshot.lifetime["music_verified_frames"] == RATE * 16
    with pytest.raises(RuntimeFailure):
        _ = capture.chunks[0]


def test_bound_rejects_before_eviction_can_hide_unconsumed_blocks(monkeypatch):
    capture = PureCapture()
    monkeypatch.setattr(streaming, "MAX_BLOCKS", 2)
    capture.add(0)
    capture.add(960)
    with pytest.raises(RuntimeFailure, match="declared bound"):
        capture.add(1920)
    assert len(capture.chunks) == 2
    assert capture.sequence.frames == 1920


def test_analysis_window_cannot_include_new_unverified_pcm():
    capture = established()
    capture.add(RATE * 2)
    with pytest.raises(RuntimeFailure, match="unverified music"):
        capture.snapshot(10_000_000_000, 20_000_000_000)


def test_foreign_rate_or_dropped_queue_never_acquires_sample_authority():
    capture = PureCapture()
    with pytest.raises(RuntimeFailure, match="caps"):
        capture.add(0, rate=44100)
    assert capture.total == 0
    capture = PureCapture()
    capture.capture_dropped = 4
    with pytest.raises(RuntimeFailure, match="dropped"):
        capture.poll()


@pytest.mark.parametrize(
    "change", ["short_mono", "short_wall", "missing_frames", "wrong_zone", "bool_frames"]
)
def test_accelerated_loops_or_partial_music_never_qualify_real_thirty_minutes(change):
    start = 1_000_000_000
    end = start + 1800_000_000_000
    args = [start, end, start, end, {soak.A: RATE * 1800, soak.B: RATE * 1800}]
    if change == "short_mono":
        args[1] -= 1
    elif change == "short_wall":
        args[3] -= 1
    elif change == "missing_frames":
        args[4][soak.A] -= 1
    elif change == "wrong_zone":
        args[4]["other"] = args[4].pop(soak.B)
    else:
        args[4][soak.B] = True
    with pytest.raises(RuntimeFailure, match="thirty real"):
        soak.require_elapsed(*args)
    soak.require_elapsed(start, end, start, end, {soak.A: RATE * 1800, soak.B: RATE * 1800})


@pytest.mark.parametrize("duration", [90, 180, 420, 480, 1829, 1831, True])
def test_soak_only_duration_cannot_adopt_original_producer_modes(duration):
    with pytest.raises(RuntimeFailure, match="separate1830"):
        soak.admit_launch(None, duration, 0, 150_000_000, None, None, lab=object())


@pytest.mark.asyncio
async def test_cancelled_optional_timing_work_never_replaces_primary_failure():
    entered = asyncio.Event()

    async def check():
        entered.set()
        await asyncio.Event().wait()

    context = SimpleNamespace(evidence={}, pcm_guards={})
    observer = soak.SoakObserver(context, check)
    task = asyncio.create_task(observer.sample())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done()


def test_ordinary_modes_cannot_request_soak_duration_before_receiver_mutation():
    async def reject():
        state = SimpleNamespace(desired=SimpleNamespace(id=group.A))
        await group.launch_producer(None, state, None, 0, duration_seconds=1830)

    with pytest.raises(RuntimeFailure, match="undeclared native producer duration"):
        asyncio.run(reject())


def test_guard_respects_immutable_source_on_every_original_buffer():
    capture = established()
    capture.last_packet_at = None
    guard = soak.guard_type(group.FinalPcmGuard)(capture)
    guard.begin(0)
    guard.check(live=False)
    assert guard.checked_blocks == 100
    assert capture.music_verified_frames == RATE * 2
    altered = bytearray(program(RATE * 2))
    altered[1] ^= 1
    altered[3] ^= 1
    capture.add(RATE * 2, data=bytes(altered))
    with pytest.raises(RuntimeFailure, match="immutable source"):
        guard.check(live=False)
    assert capture.music_verified_frames == RATE * 2


@pytest.mark.asyncio
async def test_cancellation_joins_owned_analysis_before_propagating():
    import threading

    entered, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def worker():
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(2)
            return "finished"
        finally:
            finished.set()

    task = asyncio.create_task(soak.analysis(worker))
    try:
        await soak.bounded(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert not [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and t.get_name().startswith("soak-")
        ]
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_observer_primary_rejection_is_preserved_after_owned_watcher_join():
    entered = asyncio.Event()
    primary = RuntimeFailure("primary PCM rejection")

    async def check():
        raise primary

    async def watch():
        entered.set()
        await asyncio.Event().wait()

    context = SimpleNamespace(evidence={}, pcm_guards={})
    observer = soak.SoakObserver(context, check)
    observer.watcher = asyncio.create_task(watch())
    await entered.wait()
    with pytest.raises(RuntimeFailure) as error:
        await observer.close()
    assert error.value is primary
    assert observer.watcher.done() and observer.watcher.cancelled()
    assert not observer.stopped


def test_failed_content_artifact_keeps_the_actual_bounded_pcm_and_lifetime_scope(tmp_path):
    capture = established()
    wrong = bytes([1]) + program(RATE * 2)[1:]
    capture.add(RATE * 2, data=wrong)
    with pytest.raises(RuntimeFailure):
        capture.verify_reference()
    artifact = soak.retain_failed_capture(capture, tmp_path, "rejected")
    raw = Path(artifact["path"]).read_bytes()
    assert raw.endswith(wrong)
    assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
    assert artifact["lifetime"]["music_verified_frames"] == RATE * 2
    assert artifact["lifetime_frame_continuity"]["verified_frames"] == RATE * 2 + 960
    assert artifact["sticky_error"]
    assert "window only" in artifact["scope"]


def test_supervisor_has_explicit_no_bytecode_inner_and_finite_lifetime():
    import ast

    tree = ast.parse((Path(__file__).parent / "linux/run_native_music_soak.py").read_text())
    launch = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create_subprocess_exec"
    )
    assert isinstance(launch.args[4], ast.Constant) and launch.args[4].value == "-B"
    assert any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "bounded"
        and len(n.args) == 2
        and isinstance(n.args[1], ast.Constant)
        and n.args[1].value == 2000
        for n in ast.walk(tree)
    )


@pytest.mark.asyncio
async def test_cancelled_caller_consumes_simultaneous_finished_analysis_error(monkeypatch):
    parent = None
    children = []
    original_create = asyncio.create_task

    def tracked(coroutine, **kwargs):
        child = original_create(coroutine, **kwargs)
        if kwargs.get("name") == "soak-window-analysis":
            children.append(child)
        return child

    async def faulty(_function, *_args, **_kwargs):
        parent.cancel()
        raise ValueError("owned analysis child failed at caller cancellation")

    monkeypatch.setattr(asyncio, "to_thread", faulty)
    monkeypatch.setattr(asyncio, "create_task", tracked)
    parent = original_create(soak.analysis(lambda: None))
    with pytest.raises(asyncio.CancelledError):
        await parent
    assert len(children) == 1 and children[0].done()
    assert children[0]._log_traceback is False


@pytest.mark.asyncio
async def test_new_caller_cancel_during_retirement_propagates_after_join_with_primary_receipt():
    entered = asyncio.Event()
    primary = RuntimeFailure("original observed PCM rejection")
    parent = None

    async def check():
        raise primary

    async def watcher():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            parent.cancel()
            raise ValueError("owned watcher retirement error") from None

    context = SimpleNamespace(evidence={}, pcm_guards={})
    observer = soak.SoakObserver(context, check)
    observer.watcher = asyncio.create_task(watcher())
    await entered.wait()
    parent = asyncio.create_task(observer.close())
    with pytest.raises(asyncio.CancelledError):
        await parent
    # Python 3.10 may replace CancelledError across a Task boundary. The
    # durable observer receipt below preserves the original failure category.
    assert observer.watcher.done() and not observer.stopped
    assert observer.watcher._log_traceback is False
    assert context.evidence["observer_retirement_primary_failure_type"] == "RuntimeFailure"


@pytest.mark.parametrize(
    "frame", [0, 959, 5759, RATE * 20 - 1, RATE * 20, RATE * 1800 + 5760, RATE * 1830 - 960]
)
def test_vectorized_soak_stimulus_matches_original_continuous_bit_mixer_byte_exact(frame):
    expected = soak.bind(group.program_pcm, envelope=soak.envelope)(frame, frames=1920)
    assert soak.program_pcm(frame, frames=1920) == expected



def test_transient_progress_is_complete_private_atomic_and_never_fsyncs(tmp_path, monkeypatch):
    path = tmp_path / 'status.json'
    state = {'stage': 'streaming', 'frames': 960, 'nested': {'exact': True}}
    def forbidden(*_args):
        raise AssertionError('transient progress attempted fsync')
    monkeypatch.setattr(soak.os, 'fsync', forbidden)
    soak.progress_json(path, state)
    assert json.loads(path.read_text()) == {'stage': 'streaming', 'frames': 960, 'nested': {'exact': True}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]
    row = state['progress_publication']
    assert row['passed'] is True and row['count'] == 1
    assert row['finished_ns'] >= row['started_ns'] > 0
    assert row['duration_ns'] == row['max_duration_ns'] == row['finished_ns'] - row['started_ns']
    soak.progress_json(path, state)
    assert json.loads(path.read_text())['progress_publication'] == row
    assert state['progress_publication']['count'] == 2


@pytest.mark.parametrize('value', [float('nan'), 'x' * soak.MAX_PROGRESS_BYTES])
def test_transient_progress_refuses_invalid_or_oversized_snapshot_without_replacing(tmp_path, value):
    path = tmp_path / 'status.json'
    path.write_text('{"previous":true}')
    state = {'value': value}
    with pytest.raises((ValueError, RuntimeFailure)):
        soak.progress_json(path, state)
    assert path.read_text() == '{"previous":true}' and list(tmp_path.iterdir()) == [path]
    assert state['progress_publication']['passed'] is False


@pytest.mark.parametrize('boundary', ['write', 'replace'])
def test_transient_progress_failure_keeps_old_complete_file_and_cleans_temporary(tmp_path, monkeypatch, boundary):
    path = tmp_path / 'status.json'
    path.write_text('{"previous":true}')
    primary = OSError('original ' + boundary + ' failure')
    if boundary == 'replace':
        def fail_replace(*_args):
            raise primary
        monkeypatch.setattr(soak.os, 'replace', fail_replace)
    else:
        opened = soak.os.fdopen
        class PartialWrite:
            def __init__(self, descriptor, mode):
                self.stream = opened(descriptor, mode)
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                self.stream.close()
            def fileno(self):
                return self.stream.fileno()
            def write(self, data):
                self.stream.write(data[:3])
                raise primary
        monkeypatch.setattr(soak.os, 'fdopen', PartialWrite)
    state = {'stage': 'streaming', 'frames': 960}
    with pytest.raises(OSError) as captured:
        soak.progress_json(path, state)
    assert captured.value is primary
    assert path.read_text() == '{"previous":true}' and list(tmp_path.iterdir()) == [path]
    assert state['progress_publication']['passed'] is False


def test_transient_progress_cleanup_failure_preserves_primary(tmp_path, monkeypatch):
    path = tmp_path / 'status.json'
    primary, cleanup = OSError('original replace failure'), OSError('temporary cleanup failure')
    def fail_replace(*_args):
        raise primary
    def fail_cleanup(*_args):
        raise cleanup
    unlink = soak.os.unlink
    monkeypatch.setattr(soak.os, 'replace', fail_replace)
    monkeypatch.setattr(soak.os, 'unlink', fail_cleanup)
    state = {'frames': 960}
    with pytest.raises(OSError) as captured:
        soak.progress_json(path, state)
    assert captured.value is primary and captured.value.__cause__ is cleanup
    assert state['progress_publication']['passed'] is False
    for temporary in tmp_path.iterdir():
        unlink(temporary)


async def run_real_soak_progress(tmp_path, monkeypatch, *, delay_progress=False, fail_progress=False):
    """Real monotonic/asyncio pacing, real status I/O, immutable PCM; no devices."""
    actual_loop = asyncio.get_running_loop()
    callbacks, closed, sent, durable, progress_sync = {}, [], [], [], []
    publication = {'active': False, 'delayed': False}
    durable_sync = []
    path, command = tmp_path / 'status.json', tmp_path / 'command.json'
    original_atomic, original_progress, original_replace, original_sync = (
        soak.atomic_json, soak.progress_json, soak.os.replace, soak.os.fsync)
    current_begin = None
    calendar = {}
    class Connection:
        def setblocking(self, selected):
            assert selected is False
        def close(self):
            closed.append(True)
    connection = Connection()
    class Loop:
        def add_signal_handler(self, name, callback):
            callbacks[name] = callback
        async def sock_connect(self, selected, address):
            assert selected is connection and address == 'pure-fixture'
        async def sock_sendall(self, selected, data):
            nonlocal current_begin
            assert selected is connection
            current_begin = soak.Packet.decode(data)
            assert current_begin.kind is soak.Kind.BEGIN
        async def sock_recv(self, selected, size):
            assert selected is connection and size == 4096
            return soak.Packet(soak.Kind.GRANT, current_begin.session,
                incarnation=bytes.fromhex('01' * 16), group=current_begin.group,
                epoch=1, generation=1, flags=current_begin.flags).encode()
    def durable_write(selected, state):
        durable.append(state['stage'])
        original_atomic(selected, state)
        if state['stage'] == 'ready_before_begin':
            available = time.monotonic_ns()
            calendar.update(generation=2, action='begin', available_ns=available,
                            common_start_ns=available + soak.NATIVE_LEAD_NS)
            command.write_text(json.dumps(calendar))
    def checked_sync(descriptor):
        durable_sync.append(descriptor)
        if publication['active']:
            progress_sync.append(True)
            time.sleep(0.2)
        return original_sync(descriptor)
    def delayed_replace(source, destination):
        if publication['active'] and delay_progress and not publication['delayed']:
            publication['delayed'] = True
            time.sleep(0.2)
        return original_replace(source, destination)
    def progress_write(selected, state):
        publication['active'] = True
        try:
            if fail_progress:
                raise OSError('original progress write failure')
            return original_progress(selected, state)
        finally:
            publication['active'] = False
    def bracket(grant, group_id, frame, sequence, start, wide, frequency, stats):
        assert frequency == 440
        anchor = start + frame * 1_000_000_000 // RATE
        return soak.Packet(soak.Kind.PCM, grant.session, incarnation=grant.incarnation,
            group=group_id, epoch=grant.epoch, generation=grant.generation, sequence=sequence,
            frame_index=frame, presentation_ns=anchor, clock_sample_ns=time.monotonic_ns(),
            monotonic_before_ns=1, monotonic_after_ns=2, flags=grant.flags,
            frames=960, pcm=soak.program_pcm(frame))
    async def send(loop, selected, payload, target, stats):
        assert selected is connection and loop is proxy
        sent.append((soak.Packet.decode(payload), target, time.monotonic_ns()))
        if len(sent) == 4:
            callbacks[signal.SIGTERM]()
        await asyncio.sleep(0)
        return time.monotonic_ns() - target
    proxy = Loop()
    monkeypatch.setattr(soak, 'asyncio', SimpleNamespace(get_running_loop=lambda: proxy,
        Event=asyncio.Event, TimeoutError=asyncio.TimeoutError, sleep=asyncio.sleep))
    monkeypatch.setattr(soak, 'socket', SimpleNamespace(AF_UNIX=soak.socket.AF_UNIX,
        SOCK_SEQPACKET=soak.socket.SOCK_SEQPACKET, socket=lambda *_args: connection))
    monkeypatch.setattr(soak.os, 'geteuid', lambda: 989)
    monkeypatch.setattr(soak.os, 'getegid', lambda: 990)
    monkeypatch.setattr(soak, 'atomic_json', durable_write)
    monkeypatch.setattr(soak, 'progress_json', progress_write)
    monkeypatch.setattr(soak.os, 'replace', delayed_replace)
    monkeypatch.setattr(soak.os, 'fsync', checked_sync)
    config = {'music_soak': soak.producer_profile(), 'common_start_ns': 0,
              'arrival_lead_ns': soak.NATIVE_LEAD_NS, 'duration_seconds': 1830,
              'leader': True, 'wide_bracket': False, 'uid': 989, 'gid': 990,
              'status': str(path), 'command': str(command), 'socket': 'pure-fixture',
              'group': '00000000-0000-0000-0000-000000000001'}
    tools = {'FLAGS': group.FLAGS, 'FLAG_GROUP_LEADER': group.FLAG_GROUP_LEADER,
             'bracketed_packet': bracket, 'send_native_packet': send,
             'retire_producer': group.retire_producer, 'observation': group.observation}
    primary = None
    try:
        await soak.producer(config, tools)
    except BaseException as error:
        primary = error
    assert asyncio.get_running_loop() is actual_loop and actual_loop.is_running()
    state = json.loads(path.read_text())
    assert closed == [True] and state['finished'] is True
    assert durable[:3] == ['ready_before_begin', 'connecting', 'granted']
    assert durable[-1] in {'streaming', 'finished'}, (type(primary).__name__, str(primary))
    assert not progress_sync and len(durable_sync) == 8
    assert list(tmp_path.glob('.status.json.*')) == []
    for index, (packet, target, _observed) in enumerate(sent):
        assert packet.frame_index == index * 960 and packet.sequence == index
        assert target == calendar['available_ns'] + index * 20_000_000
        assert packet.presentation_ns == calendar['common_start_ns'] + index * 20_000_000
        assert packet.pcm == soak.program_pcm(index * 960)
    return primary, state, sent


@pytest.mark.asyncio
async def test_real_progress_avoids_delayed_fsync_and_keeps_original_targets_and_cleanup(tmp_path, monkeypatch):
    primary, state, sent = await run_real_soak_progress(tmp_path, monkeypatch)
    assert primary is None and state['frames'] == 3840 and len(sent) == 4
    assert state['progress_publication']['passed'] is True
    assert all(0 <= observed - target < soak.SEND_LATE_NS for _packet, target, observed in sent)


@pytest.mark.asyncio
async def test_real_slow_transient_io_keeps_original_refusal_and_final_telemetry(tmp_path, monkeypatch):
    primary, state, sent = await run_real_soak_progress(tmp_path, monkeypatch, delay_progress=True)
    assert isinstance(primary, RuntimeFailure) and str(primary) == 'Synthetic producer missed bounded native delivery cadence'
    assert state['frames'] == 960 and len(sent) == 1
    row = state['delivery_observation']
    assert row['stage'] == 'preclock' and row['frame'] == 960
    assert row['target_ns'] == state['available_ns'] + 20_000_000
    assert row['lateness_ns'] >= soak.SEND_LATE_NS
    assert state['progress_publication']['duration_ns'] >= 200_000_000
    assert state['progress_publication']['max_duration_ns'] == state['progress_publication']['duration_ns']


@pytest.mark.asyncio
async def test_progress_failure_retires_connection_and_retains_primary_without_new_frames(tmp_path, monkeypatch):
    primary, state, sent = await run_real_soak_progress(tmp_path, monkeypatch, fail_progress=True)
    assert isinstance(primary, OSError) and str(primary) == 'original progress write failure'
    assert state['frames'] == 960 and len(sent) == 1
    assert state['delivery_observation']['stage'] == 'progress_publish'
    assert state['delivery_observation']['frame'] == 0
    assert state['delivery_observation']['target_ns'] == state['available_ns']

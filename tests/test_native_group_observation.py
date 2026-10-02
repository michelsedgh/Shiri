"""Offline sensitivity tests for the new actual two-zone Linux check.

Importing the manual harness constructs no GStreamer/peer/kernel resources.
No phone, PCM device, namespace, managed UID or VM is created by these tests.
"""
import importlib.util
import json
import os
from collections import deque
from pathlib import Path
import stat
from types import SimpleNamespace
from uuid import uuid4

import pytest

np = pytest.importorskip('numpy', reason='Native PCM observation needs audio extra')
pytest.importorskip('aiortc', reason='Manual speech harness imports audio extra')
spec = importlib.util.spec_from_file_location('native_group_observation_tests',
    Path(__file__).parent/'linux/check_native_grouping.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def test_command_replacement_has_reader_group_before_it_becomes_visible(tmp_path, monkeypatch):
    command = tmp_path/'command.json'
    group = os.getgid()
    actual_chown, actual_replace = os.fchown, os.replace
    admitted = []
    def chown(descriptor, uid, gid):
        assert (uid, gid) == (0, group)
        actual_chown(descriptor, -1, gid)  # portable fixture has no root authority
        admitted.append(os.fstat(descriptor).st_ino)
    def publish(source, destination):
        info = Path(source).stat()
        assert info.st_ino in admitted and info.st_gid == group
        assert stat.S_IMODE(info.st_mode) == 0o640
        actual_replace(source, destination)
    monkeypatch.setattr(harness.os, 'fchown', chown)
    monkeypatch.setattr(harness.os, 'replace', publish)
    for action in ('wait', 'run', 'takeover', 'end'):
        harness.publish_command(command, {'action': action}, {'gid': group})
        assert json.loads(command.read_text()) == {'action': action}
    assert len(admitted) == 4 and len(list(tmp_path.iterdir())) == 1


def test_command_permission_failure_cannot_publish_unreadable_successor(tmp_path, monkeypatch):
    command = tmp_path/'command.json'
    command.write_text('{"action":"wait"}')
    before = command.read_bytes()
    def denied(*_args):
        raise PermissionError('reader group admission failed')
    monkeypatch.setattr(harness.os, 'fchown', denied)
    with pytest.raises(PermissionError, match='reader group'):
        harness.publish_command(command, {'action': 'run'}, {'gid': os.getgid()})
    assert command.read_bytes() == before and list(tmp_path.iterdir()) == [command]


def fixture_capture(*, delay_ms=0, first_frame=0, seconds=5, constant=False, silent=False):
    chunks, at, absolute = [], [], {}
    for index in range(seconds*50):
        frame = first_frame+index*960
        data = harness.program_pcm(frame)
        if constant:
            positions = frame+np.arange(960)
            mono = (8192*np.sin(2*np.pi*440*positions/48000)).astype('<i2')
            data = np.repeat(mono[:, None], 2, axis=1).tobytes()
        if silent:
            data = bytes(len(data))
        observed = 100+index/50
        at.append(observed)
        chunks.append(data)
        absolute[observed] = round((10+frame/48000+delay_ms/1000)*1e9)
    return SimpleNamespace(chunks=chunks, captured_at=at, absolute=absolute)


def series(capture):
    return harness.modulation(capture, 0, 100_000_000_000)


@pytest.mark.parametrize('lag', [0, 1, -1, 2])
def test_actual_coded_pcm_alignment_finds_known_absolute_capture_delay(lag):
    a, b = fixture_capture(), fixture_capture(delay_ms=lag)
    measured = harness.align_series(series(a), series(b))
    assert measured['relative_offset_ms'] == pytest.approx(lag, abs=.01)
    assert measured['correlation'] > .999


@pytest.mark.parametrize('lag', [3, 5, 7, 30, -9])
def test_identifying_the_same_program_cannot_waive_excessive_measured_zone_delay(lag):
    with pytest.raises(harness.RuntimeFailure, match='more than2ms'):
        harness.align_series(series(fixture_capture()), series(fixture_capture(delay_ms=lag)))


def jittered_pair():
    # Rounded microsecond residuals of actual group22 buffers500..523. Both
    # captures contained byte-identical frames, but the first selected B PTS
    # was3.897ms later. Repeat that bounded real-query pattern over generated
    # coded PCM: no private raw recording or hardware path is imported here.
    phase_a = [0, 3848, 1036, 1817, -74, 1066, -71, 217, 935, -65, 79, 1253,
               876, -75, 1256, 955, 870, 53, -20, -15, 651, 307, 1687, -101]
    phase_b = [0, -3317, -2962, 11, -3885, -2852, -3965, -3725, -3505, -3980, -4045, -4015,
               -3761, -4024, -3900, -2978, -3005, -3997, -3998, -4015, -3313, -3975, -3988, -3940]
    pair = [fixture_capture(first_frame=48000*2, seconds=8) for _ in range(2)]
    for capture, phases, first_delta in zip(pair, (phase_a, phase_b), (0, 3897), strict=True):
        for index, at in enumerate(capture.captured_at):
            capture.absolute[at] += (first_delta+phases[index % len(phases)])*1000
    return pair


def verified_snapshot(capture):
    # Use the SAME actual-metadata fence used by OutputCapture.poll, before
    # the frozen snapshot receives its copied continuity receipt.
    capture.sequence = harness.observation.PcmSequence()
    first = capture.absolute[capture.captured_at[0]]
    frame = 0
    for at, data in zip(capture.captured_at, capture.chunks, strict=True):
        frames = len(data)//4
        capture.sequence.push({'offset': frame, 'offset_end': frame+frames,
                               'pts': capture.absolute[at]-first,
                               'duration': round(frames*1_000_000_000/48000), 'discont': frame == 0}, frames, 48000)
        frame += frames
    capture.poll = lambda: None
    return harness.capture_snapshot(capture)


def test_real_query_jitter_cannot_turn_one_first_buffer_anchor_into_a_constant_zone_offset():
    a, b = jittered_pair()
    assert a.chunks == b.chunks
    assert b.absolute[b.captured_at[0]]-a.absolute[a.captured_at[0]] == 3_897_000
    report = {}
    harness.record_group_alignment(report, [series(verified_snapshot(a)), series(verified_snapshot(b))])
    receipt = report['group_alignment']
    assert abs(receipt['relative_offset_ms']) <= 1
    assert receipt['correlation'] > .995
    assert abs(receipt['early']['relative_offset_ms']) <= 1
    assert abs(receipt['late']['relative_offset_ms']) <= 1
    assert abs(receipt['drift_ms']) <= 1


def test_every_actual_window_time_retains_a_genuine_constant_offset_despite_query_jitter():
    a, b = jittered_pair()
    original = series(b)
    for at in b.captured_at:
        b.absolute[at] += 5_000_000
    shifted = series(b)
    np.testing.assert_allclose(shifted[0]-original[0], .005, rtol=0, atol=1e-12)
    np.testing.assert_array_equal(shifted[1], original[1])
    report = {}
    with pytest.raises(harness.RuntimeFailure, match='more than2ms'):
        harness.record_group_alignment(report, [series(a), shifted], frame_continuity={
            'A': {'verified_frames': 384000, 'verified_blocks': 400},
            'B': {'verified_frames': 384000, 'verified_blocks': 400}})
    assert report['group_alignment']['relative_offset_ms'] >= 4
    assert report['group_alignment']['correlation'] > .995
    assert set(report['group_alignment']) >= {'early', 'late', 'drift_ms', 'capture_frame_continuity'}
    assert report['group_alignment']['capture_frame_continuity']['B']['verified_frames'] == 384000


def test_equal_program_with_true_clock_drift_keeps_numeric_receipts_and_fails():
    a, b = [fixture_capture(seconds=10) for _ in range(2)]
    for at, drift in zip(b.captured_at, np.linspace(-4_000_000, 4_000_000, len(b.chunks)), strict=True):
        b.absolute[at] += round(drift)
    report = {}
    with pytest.raises(harness.RuntimeFailure, match='timing drifts|more than2ms'):
        harness.record_group_alignment(report, [series(a), series(b)])
    receipt = report['group_alignment']
    assert abs(receipt['relative_offset_ms']) <= 1
    assert receipt['late']['relative_offset_ms']-receipt['early']['relative_offset_ms'] > 2
    assert receipt['drift_ms'] > 2


@pytest.mark.parametrize('change', ['backwards', 'repeated', 'unbounded-span'])
def test_analysis_never_relabels_invalid_absolute_capture_anchors_as_query_jitter(change):
    capture = fixture_capture()
    previous, current = capture.captured_at[20:22]
    capture.absolute[current] = capture.absolute[previous]+{
        'backwards': -1, 'repeated': 0, 'unbounded-span': 40_000_000}[change]
    with pytest.raises(harness.RuntimeFailure, match='backwards anchor|span/gap'):
        series(capture)


@pytest.mark.parametrize('change', ['missing', 'repeated'])
def test_jittered_snapshot_cannot_hide_missing_or_repeated_actual_verified_pcm(change):
    snapshot = verified_snapshot(jittered_pair()[0])
    index = 30
    if change == 'missing':
        snapshot.chunks = snapshot.chunks[:index]+snapshot.chunks[index+1:]
        snapshot.captured_at = snapshot.captured_at[:index]+snapshot.captured_at[index+1:]
    else:
        snapshot.chunks = snapshot.chunks[:index]+snapshot.chunks[index-1:]
        snapshot.captured_at = snapshot.captured_at[:index]+snapshot.captured_at[index-1:]
    with pytest.raises(harness.RuntimeFailure, match='verified frame continuity receipt'):
        series(snapshot)


@pytest.mark.parametrize('displaced_ns', [1, 50_000_000_000])
def test_invalid_interior_anchor_cannot_filter_verified_pcm_out_of_the_requested_interval(displaced_ns):
    snapshot = verified_snapshot(jittered_pair()[1])
    snapshot.absolute[snapshot.captured_at[30]] = displaced_ns
    assert snapshot.frame_continuity['verified_blocks'] == len(snapshot.chunks)
    assert snapshot.frame_continuity['verified_frames'] == sum(len(data)//4 for data in snapshot.chunks)
    with pytest.raises(harness.RuntimeFailure, match='backwards anchor|span/gap'):
        harness.modulation(snapshot, 11_500_000_000, 20_000_000_000)


@pytest.mark.parametrize('change', ['missing', 'repeated', 'discontinuity'])
def test_actual_poll_metadata_failure_prevents_any_frozen_alignment_snapshot(change):
    capture = harness.observation.OutputCapture.__new__(harness.observation.OutputCapture)
    capture.error, capture.capture_dropped, capture.needs_latency = None, 0, False
    capture.rate = capture.channels = capture.format = None
    capture.total, capture.discontinuities, capture.max_packet_gap, capture.warning = 0, 0, 0, None
    capture.chunks, capture.captured_at, capture.absolute = [], [], {}
    capture.sequence = harness.observation.PcmSequence()
    initial = {'offset': 0, 'offset_end': 960, 'pts': 0, 'duration': 20_000_000, 'discont': True}
    offset = {'missing': 1920, 'repeated': 0, 'discontinuity': 960}[change]
    bad = {**initial, 'offset': offset, 'offset_end': offset+960, 'pts': 20_000_000,
           'discont': change == 'discontinuity'}
    capture.pending = deque((at, harness.program_pcm(index*960), 48000, 2, 'S16LE', metadata)
                            for index, (at, metadata) in enumerate(((100., initial), (100.02, bad))))
    capture.absolute = {100.: 10_000_000_000, 100.02: 10_020_000_000}
    for _ in range(2):
        with pytest.raises(harness.RuntimeFailure, match='lost or repeated|discontinuity'):
            harness.capture_snapshot(capture)
    assert capture.sequence.blocks == len(capture.chunks) == 1


def test_bad_program_quality_still_retains_its_numeric_alignment_receipt():
    first = fixture_capture(seconds=8)
    altered = fixture_capture(first_frame=48000*8, seconds=8)
    altered.absolute = dict(first.absolute)  # Different program on the SAME declared clock interval.
    reference, different = series(first), series(altered)
    report = {}
    with pytest.raises(harness.RuntimeFailure, match='uniquely coded program'):
        harness.record_group_alignment(report, [reference, different])
    assert set(report['group_alignment']) >= {'correlation', 'relative_offset_ms', 'peak_prominence'}
    assert report['group_alignment']['correlation'] < .985


@pytest.mark.parametrize('wrong', ['constant', 'silence', 'different-code'])
def test_missing_or_wrong_program_cannot_be_called_group_synchronization(wrong):
    reference = fixture_capture()
    altered = fixture_capture(constant=wrong == 'constant', silent=wrong == 'silence',
                              first_frame=48000*8 if wrong == 'different-code' else 0)
    with pytest.raises(harness.RuntimeFailure, match='coded program|common final-PCM'):
        harness.align_series(series(reference), series(altered))


def test_snapshots_do_not_drain_an_observer_or_change_when_live_lists_advance():
    live = fixture_capture(seconds=2)
    polls = []
    live.poll = lambda: polls.append('loop-thread')
    snapshot = harness.capture_snapshot(live)
    assert polls == ['loop-thread']
    frozen_length = len(snapshot.chunks)
    live.chunks.append(bytes(3840))
    live.captured_at.append(999)
    live.absolute[999] = 42
    assert len(snapshot.chunks) == frozen_length and 999 not in snapshot.absolute
    assert not hasattr(snapshot, 'poll')


def test_group_pcm_and_packet_serialization_are_deterministic_across_processes(monkeypatch):
    from shiri.runtime.timing import FLAG_AIRPLAY2, FLAG_GROUP_LEADER, Clock, Kind, Packet, map_native_time
    group, session, incarnation = uuid4().bytes, uuid4().bytes, uuid4().bytes
    grant = Packet(Kind.GRANT, session, group=group, incarnation=incarnation, epoch=3,
                   flags=FLAG_AIRPLAY2 | FLAG_GROUP_LEADER)
    # Each receiver has a different real-clock offset/bracket; the explicitly
    # measured mapping still projects to the same common presentation anchor.
    # Recovery adds original-entry/pre-batch/post-batch reads; admitted pair unchanged.
    counter = iter([1_000_000_000, 1_000_000_000, 1_000_000_000, 1_000_000_100, 1_000_000_100])
    monkeypatch.setattr(harness.time, 'monotonic_ns', lambda: next(counter))
    monkeypatch.setattr(harness.time, 'CLOCK_MONOTONIC_RAW', 4, raising=False)
    monkeypatch.setattr(harness.time, 'clock_gettime_ns', lambda _clock: 700_000_000)
    packet = harness.bracketed_packet(grant, group, 960, 1, 1_200_000_000)
    decoded = Packet.decode(packet.encode())
    assert decoded.clock is Clock.RAW and decoded.group == group
    assert decoded.session == session and decoded.incarnation == incarnation and decoded.epoch == 3
    assert decoded.pcm == harness.program_pcm(960)
    assert map_native_time(decoded, now_ns=1_000_000_100).monotonic_ns == 1_220_000_000
    assert decoded.flags & FLAG_GROUP_LEADER


def mock_clock_reads(monkeypatch, monotonic, raw):
    mono_reads, raw_reads = iter(monotonic), iter(raw)
    monkeypatch.setattr(harness.time, 'monotonic_ns', lambda: next(mono_reads))
    monkeypatch.setattr(harness.time, 'CLOCK_MONOTONIC_RAW', 4, raising=False)
    monkeypatch.setattr(harness.time, 'clock_gettime_ns', lambda _clock: next(raw_reads))


def clock_grant():
    from shiri.runtime.timing import FLAG_AIRPLAY2, FLAG_GROUP_LEADER, Kind, Packet
    return Packet(Kind.GRANT, uuid4().bytes, group=uuid4().bytes, incarnation=uuid4().bytes,
                  epoch=3, generation=7, flags=FLAG_AIRPLAY2 | FLAG_GROUP_LEADER)


def test_legacy_actual1347960ns_preemption_retries_entire_mapping_without_changing_pcm_or_source(monkeypatch):
    # Direct legacy inner policy; recovered packet admission is tested separately.
    monkeypatch.setattr(harness, 'recovered_clock_sample', harness.coherent_clock_sample)
    from shiri.runtime.timing import Packet, map_native_time
    grant = clock_grant()
    mock_clock_reads(monkeypatch, [1_000_000_000, 1_001_347_960, 1_001_350_000, 1_001_350_100],
                     [700_000_000, 701_350_050])
    stats = {}
    packet = harness.bracketed_packet(grant, grant.group, 960, 21, 1_200_000_000, stats=stats)
    decoded = Packet.decode(packet.encode())
    assert (decoded.monotonic_before_ns, decoded.clock_sample_ns, decoded.monotonic_after_ns) == (
        1_001_350_000, 701_350_050, 1_001_350_100)
    assert map_native_time(decoded, now_ns=1_001_350_100).monotonic_ns == 1_220_000_000
    assert (decoded.frame_index, decoded.sequence, decoded.frames) == (960, 21, 960)
    assert decoded.pcm == harness.program_pcm(960)
    assert (decoded.session, decoded.incarnation, decoded.epoch, decoded.generation, decoded.group, decoded.flags) == (
        grant.session, grant.incarnation, grant.epoch, grant.generation, grant.group, grant.flags)
    assert stats == {'clock_sample_attempts': 2, 'clock_sample_retries': 1,
                     'max_attempt_clock_bracket_ns': 1_347_960}


def test_legacy_clock_retry_never_introduces_a_native_frame_or_sequence_gap(monkeypatch):
    # Direct legacy inner policy; recovered packet admission is tested separately.
    monkeypatch.setattr(harness, 'recovered_clock_sample', harness.coherent_clock_sample)
    from shiri.runtime.timing import StreamFence, map_native_time
    grant = clock_grant()
    mock_clock_reads(monkeypatch,
        [1_000_000_000, 1_001_347_960, 1_001_350_000, 1_001_350_100, 1_021_350_000, 1_021_350_100],
        [700_000_000, 701_350_050, 721_350_050])
    first = harness.bracketed_packet(grant, grant.group, 0, 0, 1_200_000_000)
    second = harness.bracketed_packet(grant, grant.group, 960, 1, 1_200_000_000)
    fence = StreamFence(grant.session, generation=grant.generation)
    fence.accept(first)
    fence.accept(second)
    assert fence.next_frame == 1920 and fence.sequence == 1 and fence.gaps == 0
    assert map_native_time(second, now_ns=second.monotonic_after_ns).monotonic_ns - map_native_time(
        first, now_ns=first.monotonic_after_ns).monotonic_ns == 20_000_000


@pytest.mark.parametrize('width', [0, 1_000_000])
def test_clock_retry_keeps_the_exact_existing_ingress_bracket_limit(monkeypatch, width):
    mock_clock_reads(monkeypatch, [10_000_000_000, 10_000_000_000+width], [7_000_000_000])
    assert harness.coherent_clock_sample() == (10_000_000_000, 7_000_000_000, 10_000_000_000+width)


def test_legacy_clock_sample_exhaustion_constructs_no_packet_and_advances_no_pcm(monkeypatch):
    # Direct legacy inner policy; recovered packet admission is tested separately.
    monkeypatch.setattr(harness, 'recovered_clock_sample', harness.coherent_clock_sample)
    grant = clock_grant()
    mono = [value for index in range(4) for value in
            (10_000_000_000+index*1_200_000, 10_001_100_000+index*1_200_000)]
    mock_clock_reads(monkeypatch, mono, [7_000_000_000+index*1_200_000 for index in range(4)])
    constructed, generated = [], []
    monkeypatch.setattr(harness, 'Packet', lambda *_a, **_k: constructed.append('invalid'))
    monkeypatch.setattr(harness, 'program_pcm', lambda *_a, **_k: generated.append('invalid'))
    state = {'frames': 1920}
    with pytest.raises(harness.RuntimeFailure, match='exhausted its4'):
        harness.bracketed_packet(grant, grant.group, 1920, 2, 10_200_000_000, stats=state)
    assert constructed == generated == [] and state['frames'] == 1920
    assert (grant.frame_index, grant.sequence, grant.generation) == (0, 0, 7)
    assert state['clock_sample_attempts'] == 4 and state['clock_sample_retries'] == 3
    assert state['max_attempt_clock_bracket_ns'] == 1_100_000


@pytest.mark.parametrize('elapsed', [4_999_999, 5_000_000, 5_500_000])
def test_even_a_fresh_narrow_retry_cannot_waive_total_elapsed_budget(monkeypatch, elapsed):
    after = 10_000_000_000+elapsed
    mock_clock_reads(monkeypatch, [10_000_000_000, 10_001_100_000, after-100, after],
                     [7_000_000_000, 7_000_000_000+elapsed-50])
    if elapsed < 5_000_000:
        assert harness.coherent_clock_sample()[2] == after
    else:
        with pytest.raises(harness.RuntimeFailure, match='exhausted its5ms'):
            harness.coherent_clock_sample()


@pytest.mark.parametrize('monotonic,raw', [
    ([10_000_000_000, 9_999_999_999], [7_000_000_000]),
    ([0, 100], [7_000_000_000]),
    ([10_000_000_000, 10_000_000_100], [0]),
    ([10_000_000_000, 10_001_100_000, 10_001_099_999, 10_001_100_100], [7_000_000_000, 7_001_100_000]),
    ([10_000_000_000, 10_001_100_000, 10_001_200_000, 10_001_200_100], [7_000_000_000, 6_999_999_999]),
])
def test_invalid_or_backwards_clock_reads_are_not_repaired_by_mixing_attempts(monkeypatch, monotonic, raw):
    mock_clock_reads(monkeypatch, monotonic, raw)
    with pytest.raises(harness.RuntimeFailure, match='invalid or backwards clocks'):
        harness.coherent_clock_sample()


def test_every_retry_retains_both75us_sides_of_the_b_stress_bracket(monkeypatch):
    clock, raw_calls = [10_000_000_000], []
    def mono():
        value = clock[0]
        clock[0] += 25_000
        return value
    def raw(_clock):
        if not raw_calls:
            clock[0] += 1_347_960  # Interrupt the first complete sample, as in group23.
        raw_calls.append(clock[0])
        return clock[0]-3_000_000_000
    monkeypatch.setattr(harness.time, 'monotonic_ns', mono)
    monkeypatch.setattr(harness.time, 'CLOCK_MONOTONIC_RAW', 4, raising=False)
    monkeypatch.setattr(harness.time, 'clock_gettime_ns', raw)
    stats = {}
    before, _raw, after = harness.coherent_clock_sample(wide=True, stats=stats)
    assert len(raw_calls) == stats['clock_sample_attempts'] == 2
    assert before+75_000 <= raw_calls[-1] <= after-75_000
    assert 150_000 <= after-before <= 1_000_000
    assert stats['clock_sample_retries'] == 1 and stats['max_attempt_clock_bracket_ns'] > 1_000_000


def test_successor_has_a_distinct_actual_660_probe_not_only_a_changed_playhead():
    first = harness.program_pcm(48000*21, frames=48000)
    second = harness.program_pcm(48000*21, frames=48000, frequency=660)
    mono = np.frombuffer(second, dtype='<i2').reshape(-1, 2)[:, 0].astype(float)
    t = np.arange(len(mono))/48000
    basis = np.column_stack((np.sin(2*np.pi*440*t), np.cos(2*np.pi*440*t),
                             np.sin(2*np.pi*660*t), np.cos(2*np.pi*660*t)))
    fit, *_ = np.linalg.lstsq(basis, mono, rcond=None)
    assert np.hypot(*fit[:2]) < 1 and np.hypot(*fit[2:4]) > 8190
    assert second != first


async def test_receiver_child_wrong_uid_fails_before_connecting_any_native_socket(tmp_path, monkeypatch):
    import json
    import os
    config = tmp_path/'producer.json'
    config.write_text(json.dumps({'uid': os.geteuid()+1, 'gid': os.getegid()}))
    opened = []
    monkeypatch.setattr(harness.socket, 'socket', lambda *_a, **_k: opened.append('forbidden'))
    with pytest.raises(harness.RuntimeFailure, match='credential mismatch'):
        await harness.producer(config)
    assert opened == []


def test_disposable_database_seed_has_exact_two_targets_and_six_disabled_unassigned_rooms(tmp_path):
    from shiri.store import Store
    database = tmp_path/'state.sqlite3'
    harness.seed(database)
    with Store(database) as store:
        rooms = store.list_rooms()
        assert len(rooms) == 8 and all(not room.enabled and not room.speakers for room in rooms)
        assert [(room.id, room.slot) for room in rooms if room.id in harness.ZONES] == [(harness.A, 6), (harness.B, 7)]
        assert all(room.local_audio_device is None for room in rooms)


def test_continuity_observer_still_rejects_lost_frames_and_late_discontinuity():
    fence = harness.observation.PcmSequence()
    initial = {'offset': 0, 'offset_end': 960, 'pts': 0, 'duration': 20_000_000, 'discont': True}
    fence.push(initial, 960, 48000)
    with pytest.raises(harness.RuntimeFailure):
        fence.push({**initial, 'offset': 1920, 'offset_end': 2880, 'pts': 40_000_000, 'discont': False}, 960, 48000)
    with pytest.raises(harness.RuntimeFailure):
        fence.push({**initial, 'offset': 960, 'offset_end': 1920, 'pts': 20_000_000}, 960, 48000)


def test_equal_late_outputs_are_in_sync_but_calendar_displacement_is_still_reported():
    declared = series(fixture_capture())
    late_a, late_b = series(fixture_capture(delay_ms=65)), series(fixture_capture(delay_ms=65))
    assert harness.align_series(late_a, late_b)['relative_offset_ms'] == 0
    measured = harness.align_series(declared, late_a, maximum_delay_ms=None, search_ms=1000)
    assert measured['relative_offset_ms'] == 65
    assert measured['correlation'] > .999
    with pytest.raises(harness.RuntimeFailure, match='more than2ms'):
        harness.align_series(declared, late_a)


def constant_capture(seconds=2):
    capture = fixture_capture(first_frame=48000*21, seconds=seconds)
    capture.last_packet_at = None
    capture.max_packet_gap = 0
    capture.poll = lambda: None
    return capture


@pytest.mark.parametrize('corruption', ['600ms silence', '600ms duck', '20ms speech leak'])
def test_short_untouched_zone_failure_cannot_hide_in_an_accepted_stage_median(corruption):
    capture = constant_capture()
    reference = harness.observation.spectrum(capture.chunks, 48000)
    good = capture.chunks[20]
    if corruption == '600ms silence':
        bad, count, message = bytes(len(good)), 30, 'lost the continuous'
    elif corruption == '600ms duck':
        bad = (np.frombuffer(good, dtype='<i2').astype(float)*.2).astype('<i2').tobytes()
        count, message = 30, 'changed music gain'
    else:
        positions = np.arange(960)/48000
        voice = np.repeat((424*np.sin(2*np.pi*880*positions))[:, None], 2, axis=1).reshape(-1)
        bad = (np.frombuffer(good, dtype='<i2').astype(float)+voice).astype('<i2').tobytes()
        count, message = 1, 'contains target-zone speech'
    capture.chunks[20:20+count] = [bad]*count
    # The old aggregate condition accepts all three altered stages.
    stage = harness.observation.spectrum(capture.chunks, 48000)
    assert .95 < stage['music_440_amplitude']/reference['music_440_amplitude'] < 1.05
    assert stage['speech_880_amplitude'] < 4
    guard = harness.FinalPcmGuard(capture)
    guard.begin(0)
    guard.preserve_reference(reference)
    with pytest.raises(harness.RuntimeFailure, match=message):
        guard.check()
    assert guard.index == guard.checked_blocks == 20
    # Removing the invalid captured buffer must not erase the original failure.
    capture.chunks[20:20+count] = [good]*count
    with pytest.raises(harness.RuntimeFailure, match=message):
        guard.check()
    assert guard.index == 20


def test_initial_onset_index_preserves_already_captured_invalid_tail():
    capture = constant_capture(seconds=1)
    capture.chunks[4] = bytes(3840)
    guard = harness.FinalPcmGuard(capture)
    guard.begin(1)
    with pytest.raises(harness.RuntimeFailure, match='lost the continuous'):
        guard.check()
    assert guard.index == 4 and guard.checked_blocks == 3


def test_window_onset_arms_at_music_after_leading_silence():
    capture = constant_capture(seconds=1)
    capture.chunks = [bytes(3840)]*2+capture.chunks[:3]
    onset = harness.music_onset_index(capture)
    assert onset == 2
    guard = harness.FinalPcmGuard(capture)
    guard.begin(onset)
    guard.check()
    assert guard.checked_blocks == 3


def test_confirmed_onset_cannot_skip_a_silent_buffer_between_music_blocks():
    capture = constant_capture(seconds=1)
    capture.chunks = [capture.chunks[0], bytes(3840), *capture.chunks[2:5]]
    assert harness.music_onset_index(capture) == 0
    guard = harness.FinalPcmGuard(capture)
    guard.begin(0)
    with pytest.raises(harness.RuntimeFailure, match='lost the continuous'):
        guard.check()
    assert guard.failed_block['index'] == 1
    assert guard.failed_block['absolute_pts_monotonic_ns'] == capture.absolute[capture.captured_at[1]]


def test_delayed_confirmation_cannot_forget_an_earlier_audible_onset_and_gap():
    capture = constant_capture(seconds=1)
    capture.chunks = [capture.chunks[0], *([bytes(3840)]*5), *capture.chunks[6:11]]
    onset = harness.music_onset_index(capture)
    assert onset == 0
    guard = harness.FinalPcmGuard(capture)
    guard.begin(onset)
    with pytest.raises(harness.RuntimeFailure, match='lost the continuous'):
        guard.check()
    assert guard.index == 1


@pytest.mark.parametrize('wrong', ['silence', 'different-tone', 'unconfirmed'])
def test_onset_requires_the_expected_music_and_a_confirmed_window(wrong):
    capture = constant_capture(seconds=1)
    if wrong == 'silence':
        capture.chunks = [bytes(3840)]*5
    elif wrong == 'different-tone':
        capture.chunks = [harness.program_pcm(index*960, frequency=660) for index in range(5)]
    else:
        capture.chunks = capture.chunks[:4]
    assert harness.music_onset_index(capture) is None


def test_failed_capture_retains_exact_pcm_timestamps_without_polling(tmp_path):
    import hashlib
    import json
    import stat
    capture = constant_capture(seconds=1)
    capture.poll = lambda: pytest.fail('Sticky capture error must not discard recorded evidence')
    capture.chunks[20] = bytes(3840)
    artifact = harness.retain_failed_capture(capture, tmp_path, 'zone-a')
    raw = Path(artifact['path'])
    assert raw.read_bytes() == b''.join(capture.chunks)
    assert artifact['sha256'] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert artifact['blocks'] == 50 and artifact['bytes'] == 50*3840
    assert stat.S_IMODE(raw.stat().st_mode) == 0o600
    timestamps = json.loads(Path(artifact['timing_path']).read_text())
    assert timestamps[20] == {'frames': 960, 'absolute_pts_monotonic_ns': capture.absolute[capture.captured_at[20]]}
    with pytest.raises(FileExistsError):
        harness.retain_failed_capture(capture, tmp_path, 'zone-a')


def test_failed_capture_with_missing_timestamps_is_rejected_before_writing(tmp_path):
    capture = constant_capture(seconds=1)
    capture.captured_at.pop()
    with pytest.raises(harness.RuntimeFailure, match='complete record'):
        harness.retain_failed_capture(capture, tmp_path, 'zone-a')
    assert list(tmp_path.iterdir()) == []


def test_observer_poll_failure_remains_sticky_when_a_readiness_call_retries():
    capture = constant_capture()
    calls = []
    def poll():
        calls.append('poll')
        if len(calls) == 1:
            raise harness.RuntimeFailure('Lost exact PCM frame sequence')
    capture.poll = poll
    guard = harness.FinalPcmGuard(capture)
    guard.begin(0)
    for _ in range(2):
        with pytest.raises(harness.RuntimeFailure, match='Lost exact PCM frame sequence'):
            guard.check()
    assert calls == ['poll'] and guard.index == 0


def test_recovered_callbacks_cannot_hide_an_already_observed_long_delivery_gap():
    capture = constant_capture()
    capture.max_packet_gap = .6
    capture.last_packet_at = harness.time.monotonic()
    guard = harness.FinalPcmGuard(capture)
    guard.begin(0)
    with pytest.raises(harness.RuntimeFailure, match='Observed final PCM callback gap'):
        guard.check()
    capture.max_packet_gap = 0
    with pytest.raises(harness.RuntimeFailure, match='Observed final PCM callback gap'):
        guard.check()
    assert guard.checked_blocks == 0


async def test_not_ready_control_response_cannot_retry_past_new_bad_pcm():
    capture = constant_capture(seconds=1)
    guard = harness.FinalPcmGuard(capture)
    guard.begin(0)
    requests = []
    async def predicate():
        requests.append('not ready')
        capture.chunks.append(bytes(3840))
        return None
    async def observation_guard():
        guard.check()
    with pytest.raises(harness.RuntimeFailure, match='lost the continuous'):
        await harness.observed_wait(predicate, observation_guard, 'source ownership', timeout=1)
    assert requests == ['not ready'] and guard.index == 50


async def test_readiness_deadline_also_bounds_an_inflight_control_query():
    import asyncio
    requests = []
    async def predicate():
        requests.append('hung')
        await asyncio.Event().wait()
    async def observation_guard():
        pass
    with pytest.raises(asyncio.TimeoutError):
        await harness.observed_wait(predicate, observation_guard, 'source ownership', timeout=.02)
    assert requests == ['hung']


@pytest.fixture
def receiver_replacement(tmp_path, monkeypatch):
    import os
    from unittest.mock import AsyncMock
    from shiri.runtime.network import NetworkManager
    from shiri.runtime import units
    monkeypatch.setattr(units, 'boot_id', lambda: '7832c3ab-9b54-48bc-a814-61dd7beb95a9')
    network = NetworkManager(tmp_path/'state', SimpleNamespace())
    account = {'name': 'shiri-receiver-6', 'uid': os.geteuid(), 'gid': os.getegid()}
    key = f'{harness.A}:shairport'
    def spec(command):
        return units.UnitSpec(units.new_unit(network.installation_tag, harness.A, 'shairport'), 'shairport',
                              account['name'], account['name'], tuple(command))
    group = tmp_path/'receiver-cgroup'
    group.mkdir()
    original = SimpleNamespace(alive=True, process=SimpleNamespace(pid=4242))
    original.entry = spec(('/opt/shiri/sbin/shairport-sync',)).intent()
    original.entry.update(invocation_id='a'*32, control_group='/system.slice/'+original.entry['unit'],
                          cgroup_inode=group.stat().st_ino)
    original.identity = lambda: original.entry.copy()
    current = {'MainPID': 4242, 'ActiveState': 'active', 'InvocationID': 'a'*32}
    kernel = SimpleNamespace(birth='123456', populated=True, original_inode=group.stat().st_ino)
    monkeypatch.setattr(harness, 'process_birth', lambda pid: kernel.birth if pid == 4242 else None)
    def verify(entry, actual):
        if entry['invocation_id'] != actual['InvocationID']:
            raise harness.RuntimeFailure('Daemon invocation was replaced')
    original.manager = SimpleNamespace(
        inspect=AsyncMock(side_effect=lambda _name: current.copy()), verify=verify,
        open_cgroup=lambda _entry: os.open(group, os.O_RDONLY),
        cgroup_events=lambda fd: [f'populated {int(kernel.populated and os.fstat(fd).st_ino == kernel.original_inode)}'])
    network.reserve_unit(key, original.entry)
    events = []
    async def stop():
        events.append('exact original stopped')
        original.alive = False
        current.update(MainPID=0, ActiveState='inactive')
        kernel.populated = False
    original.stop = AsyncMock(side_effect=stop)
    state = SimpleNamespace(desired=SimpleNamespace(id=harness.A, slot=6),
                            directory=tmp_path/'room', processes={'shairport': original})
    async def start(key, role, command, *_args, **_kwargs):
        assert key == f'{harness.A}:shairport' and role == 'shairport'
        assert not original.alive and key not in network.manifest['processes']
        events.append('canonical replacement reserved')
        entry = spec(command).intent()
        network.reserve_unit(key, entry)
        return SimpleNamespace(alive=True, entry=entry)
    async def remember(key, owned):
        assert state.processes['shairport'] is owned
        network.remember_unit(key, owned.entry)
    broker = SimpleNamespace(network=network, _account=lambda *_args: account,
        _worker_socket=lambda _state: tmp_path/'control.sock', _start_process=AsyncMock(side_effect=start),
        _remember_process=AsyncMock(side_effect=remember))
    monkeypatch.setattr(harness, 'call_rpc', AsyncMock(return_value={'source': {'owner': None}}))
    def directory(path, *_args, mode=0o700):
        path.mkdir(parents=True, mode=mode, exist_ok=True)
        return path
    # No system-manager/UID transition is exercised by this file-preparation test.
    monkeypatch.setattr(harness, 'directory', directory)
    monkeypatch.setattr(harness, 'file_owner', lambda *_args, **_kwargs: None)
    chown = os.fchown
    def admitted_group(fd, uid, gid):
        assert (uid, gid) == (0, account['gid'])
        chown(fd, -1, gid)
    monkeypatch.setattr(harness.os, 'fchown', admitted_group)
    return SimpleNamespace(broker=broker, state=state, original=original, events=events, key=key,
                           root=tmp_path/'probe', network=network, current=current, kernel=kernel, group=group)


async def test_canonical_receiver_replacement_manifest_can_be_loaded_after_broker_restart(receiver_replacement):
    from shiri.runtime.network import NetworkManager
    fixture = receiver_replacement
    await harness.launch_producer(fixture.broker, fixture.state, fixture.root, 0)
    assert fixture.events == ['exact original stopped', 'canonical replacement reserved']
    assert fixture.original.manager.inspect.await_count == 3
    recovered = NetworkManager(fixture.network.state_dir, SimpleNamespace())
    entry = recovered.manifest['processes'][fixture.key]
    assert entry['name'] == 'shairport' and fixture.state.processes['shairport'].entry == entry
    # Establish that this test is sensitive to the original watchdog-recovery bug.
    recovered.manifest['processes'][f'{harness.A}:validation-native'] = recovered.manifest['processes'].pop(fixture.key)
    recovered.save()
    with pytest.raises(harness.RuntimeFailure, match='another installation or room'):
        NetworkManager(fixture.network.state_dir, SimpleNamespace())


@pytest.mark.parametrize('blocked', ['receiver carrying program', 'unproven exact stop'])
async def test_receiver_replacement_preserves_prior_reservation_when_not_admitted(receiver_replacement, blocked, monkeypatch):
    from unittest.mock import AsyncMock
    fixture = receiver_replacement
    if blocked == 'receiver carrying program':
        monkeypatch.setattr(harness, 'call_rpc', AsyncMock(return_value={'source': {'owner': {'session_id': 'current'}}}))
    else:
        fixture.original.stop.side_effect = harness.RuntimeFailure('Cannot prove exact owned unit termination')
    with pytest.raises(harness.RuntimeFailure):
        await harness.launch_producer(fixture.broker, fixture.state, fixture.root, 0)
    assert fixture.state.processes['shairport'] is fixture.original
    assert fixture.network.manifest['processes'][fixture.key] == fixture.original.entry
    fixture.broker._start_process.assert_not_awaited()


async def test_startup_failed_receiver_cannot_be_hidden_before_cached_alive_monitor_updates(receiver_replacement):
    fixture = receiver_replacement
    # The unit monitor has not observed the actual startup failure yet.
    assert fixture.original.alive
    fixture.current.update(MainPID=0, ActiveState='failed')
    with pytest.raises(harness.RuntimeFailure, match='exact live process'):
        await harness.launch_producer(fixture.broker, fixture.state, fixture.root, 0)
    harness.call_rpc.assert_not_awaited()
    fixture.original.stop.assert_not_awaited()
    fixture.broker._start_process.assert_not_awaited()
    assert fixture.network.manifest['processes'][fixture.key] == fixture.original.entry


@pytest.mark.parametrize('change', ['exit', 'invocation', 'mainpid', 'pid_birth', 'handle', 'reservation', 'cgroup'])
async def test_original_receiver_change_during_idle_rpc_never_admits_replacement(receiver_replacement, monkeypatch, change):
    from unittest.mock import AsyncMock
    fixture = receiver_replacement
    def health(*_args, **_kwargs):
        if change == 'exit':
            fixture.current.update(MainPID=0, ActiveState='failed')
        elif change == 'invocation':
            fixture.current['InvocationID'] = 'b'*32
        elif change == 'mainpid':
            fixture.current['MainPID'] = 4243
        elif change == 'pid_birth':
            fixture.kernel.birth = 'successor-birth'
        elif change == 'handle':
            fixture.state.processes['shairport'] = SimpleNamespace(alive=True)
        elif change == 'reservation':
            fixture.network.manifest['processes'][fixture.key] = dict(fixture.original.entry, invocation_id='b'*32)
        else:
            fixture.group.rename(fixture.group.with_name('retired-cgroup'))
            fixture.group.mkdir()
        return {'source': {'owner': None}}
    monkeypatch.setattr(harness, 'call_rpc', AsyncMock(side_effect=health))
    with pytest.raises(harness.RuntimeFailure, match='live process|invocation|reservation|cgroup changed'):
        await harness.launch_producer(fixture.broker, fixture.state, fixture.root, 0)
    fixture.original.stop.assert_not_awaited()
    fixture.broker._start_process.assert_not_awaited()
    assert fixture.key in fixture.network.manifest['processes']
    assert not fixture.root.exists()


@pytest.mark.parametrize('change', ['invocation', 'handle', 'reservation', 'populated', 'still_active'])
async def test_receiver_stop_does_not_release_changed_or_unproven_reservation(receiver_replacement, change):
    fixture = receiver_replacement
    exact_stop = fixture.original.stop.side_effect
    async def stop():
        await exact_stop()
        if change == 'invocation':
            fixture.current['InvocationID'] = 'b'*32
        elif change == 'handle':
            fixture.state.processes['shairport'] = SimpleNamespace(alive=True)
        elif change == 'reservation':
            fixture.network.manifest['processes'][fixture.key] = dict(fixture.original.entry, invocation_id='b'*32)
        elif change == 'populated':
            fixture.kernel.populated = True
        else:
            fixture.current.update(MainPID=4242, ActiveState='active')
    fixture.original.stop.side_effect = stop
    with pytest.raises(harness.RuntimeFailure, match='invocation|reservation|termination'):
        await harness.launch_producer(fixture.broker, fixture.state, fixture.root, 0)
    fixture.broker._start_process.assert_not_awaited()
    assert fixture.key in fixture.network.manifest['processes']
    assert not fixture.root.exists()


async def test_receiver_reservation_change_during_fresh_inspection_is_not_ignored(receiver_replacement):
    fixture = receiver_replacement
    def inspect(_name):
        fixture.network.manifest['processes'][fixture.key] = dict(fixture.original.entry, invocation_id='b'*32)
        return fixture.current.copy()
    fixture.original.manager.inspect.side_effect = inspect
    with pytest.raises(harness.RuntimeFailure, match='reservation changed'):
        await harness.launch_producer(fixture.broker, fixture.state, fixture.root, 0)
    fixture.original.stop.assert_not_awaited()
    fixture.broker._start_process.assert_not_awaited()


async def test_receiver_without_committed_invocation_is_not_a_live_replacement_candidate(receiver_replacement):
    fixture = receiver_replacement
    fixture.original.entry.pop('invocation_id')
    with pytest.raises(harness.RuntimeFailure, match='no admitted process identity'):
        await harness.launch_producer(fixture.broker, fixture.state, fixture.root, 0)
    fixture.original.stop.assert_not_awaited()
    fixture.broker._start_process.assert_not_awaited()


@pytest.mark.parametrize('cleared', ['0'*32, [0]*16])
async def test_completed_receiver_with_cleared_invocation_uses_held_original_cgroup_proof(receiver_replacement, cleared):
    fixture = receiver_replacement
    exact_stop = fixture.original.stop.side_effect
    async def stop():
        await exact_stop()
        fixture.current['InvocationID'] = cleared
        # A new name/inode alone cannot prove the old group empty. The guard
        # reads its held FD even if systemd unlinked the original kernel group.
        fixture.group.rmdir()
    fixture.original.stop.side_effect = stop
    handle = await harness.launch_producer(fixture.broker, fixture.state, fixture.root, 0)
    assert fixture.state.processes['shairport'] is handle['unit']
    assert fixture.events == ['exact original stopped', 'canonical replacement reserved']


async def test_empty_successor_cgroup_cannot_hide_live_held_original_group(receiver_replacement):
    fixture = receiver_replacement
    exact_stop = fixture.original.stop.side_effect
    async def stop():
        await exact_stop()
        fixture.kernel.populated = True
        fixture.group.rename(fixture.group.with_name('still-populated-original'))
        fixture.group.mkdir()
    fixture.original.stop.side_effect = stop
    with pytest.raises(harness.RuntimeFailure, match='termination is not proven'):
        await harness.launch_producer(fixture.broker, fixture.state, fixture.root, 0)
    fixture.broker._start_process.assert_not_awaited()
    assert fixture.key in fixture.network.manifest['processes']


def kernel_record(*, spread_ms=0):
    origin = 10_000_000_000
    samples = []
    for index in range(1, 301):
        frames = index*960
        jitter = round((index-1)/299*spread_ms*1e6)
        stamp = origin+(frames-480)*1_000_000_000//48000+jitter
        samples.append({'before_ns': stamp-1000, 'after_ns': stamp+1000, 'timestamp_ns': stamp,
                        'trigger_ns': origin, 'delay_frames': 480, 'submitted_frames': frames,
                        'origin_ns': harness.kernel_probe.queue_origin(stamp, frames, 480, 48000)})
    return {'finished': True, 'closed': True, 'rate': 48000, 'channels': 2, 'format': 'S16LE',
            'submitted_frames': 288000, 'period_frames': 960, 'buffer_frames': 5760, 'anchors': samples}


def test_kernel_queue_reference_and_budget_use_observations_not_desired_write_times():
    record = kernel_record(spread_ms=.3)
    reference = harness.kernel_reference(record)
    assert reference['kernel_origin_monotonic_ns'] == pytest.approx(10_000_150_000, abs=1000)
    assert reference['accepted_anchors'] >= 240
    assert reference['period_uncertainty_ms'] == 20
    assert reference['max_query_bracket_ms'] == .002
    assert reference['horizon_half_width_ms'] == pytest.approx(reference['queue_origin_spread_ms']+22.002)
    # Only this separately established offset can be removed from a group run.
    reference['capture_offset_ms'] = 17
    result = harness.validate_horizon({'relative_offset_ms': 18}, reference)
    assert result['corrected_horizon_error_ms'] == 1
    with pytest.raises(harness.RuntimeFailure, match='missed its declared horizon'):
        harness.validate_horizon({'relative_offset_ms': 65}, reference)


@pytest.mark.parametrize('damage', ['partial delivery', 'missing close', 'cached origin', 'query bracket',
                                   'clock restart', 'wrong queued depth', 'wide period', 'variable anchors'])
def test_unreliable_independent_baseline_is_fatal_before_it_can_expand_horizon_acceptance(damage):
    record = kernel_record()
    if damage == 'partial delivery':
        record['submitted_frames'] -= 960
    elif damage == 'missing close':
        record['closed'] = False
    elif damage == 'cached origin':
        record['anchors'][100]['origin_ns'] += 1
    elif damage == 'query bracket':
        record['anchors'][100]['after_ns'] += 2_000_000
    elif damage == 'clock restart':
        record['anchors'][100]['trigger_ns'] += 20_000_000
    elif damage == 'wrong queued depth':
        record['anchors'][100]['delay_frames'] = 5761
    elif damage == 'wide period':
        record['period_frames'] = 48000
    else:
        record = kernel_record(spread_ms=100)
    with pytest.raises((ValueError, harness.RuntimeFailure)):
        harness.kernel_reference(record)


def test_private_kernel_fixture_matches_the_native_generated_program_without_numpy_in_child():
    for frame in (0, 48000, 123456):
        independent = harness.kernel_probe.pcm_frames(frame, 960)
        native = harness.program_pcm(frame)
        delta = np.frombuffer(independent, dtype='<i2').astype(int)-np.frombuffer(native, dtype='<i2').astype(int)
        assert np.max(np.abs(delta)) <= 1
    assert harness.kernel_probe.queue_origin(12_000_000_000, 100000, 4000, 48000) == 10_000_000_000
    with pytest.raises(ValueError, match='Invalid kernel'):
        harness.kernel_probe.queue_origin(12_000_000_000, 100, 101, 48000)


def test_coded_carrier_bound_accepts_edges_but_rejects_unexpected_voice():
    amplitudes = []
    for phase in (0, 123, 321):
        for edge in (0, 180, 480, 750, 959):
            t = (phase+np.arange(960))/48000
            gain = np.where(np.arange(960) < edge, .65, 1.)
            mono = (8192*gain*np.sin(2*np.pi*440*t)).astype('<i2')
            data = np.repeat(mono[:, None], 2, axis=1).tobytes()
            measured = harness.observation.music_block(data, 48000, 1000)
            amplitudes.append(measured['speech_880_amplitude'])
            assert measured['speech_880_amplitude'] <= harness.coded_880_bound(960)
    assert max(amplitudes) > 4  # A zero-voice floor misclassifies real edges.
    t = np.arange(960)/48000
    mono = (8192*np.sin(2*np.pi*440*t)+500*np.sin(2*np.pi*880*t)).astype('<i2')
    injected = np.repeat(mono[:, None], 2, axis=1).tobytes()
    assert harness.observation.music_block(injected, 48000, 1000)['speech_880_amplitude'] > harness.coded_880_bound(960)


def test_independent_fixture_library_device_open_is_never_attempted_with_wrong_uid(tmp_path, monkeypatch):
    import json
    import os
    path = tmp_path/'fixture.json'
    path.write_text(json.dumps({'uid': os.geteuid()+1, 'gid': os.getegid(), 'manifest': {}, 'command': '', 'status': ''}))
    opened = []
    monkeypatch.setattr(harness.kernel_probe.C, 'CDLL', lambda *_args: opened.append('forbidden'))
    with pytest.raises(ValueError, match='credential mismatch'):
        harness.kernel_probe.run(path)
    assert opened == []


@pytest.mark.parametrize('timestamp,started,age,accepted', [
    (0, False, 1_000_000, True), (0, False, 41_000_000, False),
    (0, True, 1_000_000, False), (9_999_000_000, False, 1_000_000, True),
    (8_000_000_000, False, 1_000_000, False),
])
def test_kernel_startup_zero_never_becomes_a_timing_anchor(monkeypatch, timestamp, started, age, accepted):
    probe = harness.kernel_probe
    pcm = probe.PCM.__new__(probe.PCM)
    pcm.timestamp_started, pcm.period, pcm.buffer = started, 960, 5760
    pcm.handle = pcm.status = None
    pcm.call = lambda *_args: 0
    def stamp(pointer, value):
        pointer._obj.seconds, pointer._obj.nanoseconds = divmod(value, 1_000_000_000)
    pcm.library = SimpleNamespace(
        snd_pcm_status_get_state=lambda *_: 3,
        snd_pcm_status_get_delay=lambda *_: 960,
        snd_pcm_status_get_htstamp=lambda _status, pointer: stamp(pointer, timestamp),
        snd_pcm_status_get_trigger_htstamp=lambda _status, pointer: stamp(pointer, 10_000_000_000-age),
    )
    times = iter((10_000_000_000, 10_000_001_000))
    monkeypatch.setattr(probe.time, 'monotonic_ns', lambda: next(times))
    if accepted:
        anchor = pcm.anchor(960)
        assert (anchor is None) == (timestamp == 0)
        assert pcm.timestamp_started == (started or timestamp > 0)
    else:
        with pytest.raises(ValueError, match='timestamp|monotonic clock'):
            pcm.anchor(960)

"""Abrupt-fault admission/refusal and real observation guards, without a VM.

Kernel directory/descriptor operations are real temporary-file operations.
System-manager/cgroup metadata is explicitly simulated; no test signals a
system service. A Linux-only test signals only its own unreaped direct child.
"""
import asyncio
from collections import deque
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.domain import Room
from shiri.runtime.system import RuntimeFailure, process_birth

HERE = Path(__file__).parent/'linux'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE/filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


faults = load('native_zone_fault_test_helper', 'native_zone_faults.py')


class Manager:
    def __init__(self, directory, pid=54321):
        self.directory, self.pid = directory, pid
        self.inspects = 0
        self.controls, self.descriptors = [], []
        self.changed_invocation = self.changed_members = self.changed_inode = False
        self.inspect_wait = None

    async def inspect(self, name):
        self.inspects += 1
        if self.inspects == 2 and self.inspect_wait is not None:
            await self.inspect_wait.wait()
        return {'MainPID': self.pid, 'ActiveState': 'active', 'Id': name,
                'InvocationID': 'replaced' if self.changed_invocation and self.inspects == 2 else 'original'}

    def verify(self, entry, actual):
        if actual['Id'] != entry['unit'] or actual['InvocationID'] != entry['invocation_id']:
            raise RuntimeFailure('Actual system-manager invocation changed')

    def open_cgroup(self, entry):
        path = self.directory/'replacement' if self.changed_inode and self.inspects == 2 else self.directory
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        self.descriptors.append(descriptor)
        return descriptor

    def cgroup_read(self, descriptor, name):
        assert name == 'cgroup.procs'
        return str(self.pid + (1 if self.changed_members and self.inspects == 2 else 0))+'\n'

    def cgroup_events(self, descriptor):
        assert os.fstat(descriptor).st_ino == self.directory.stat().st_ino
        return ['populated 1', 'frozen 1']

    def write_control(self, descriptor, name, value):
        assert os.fstat(descriptor).st_ino == self.directory.stat().st_ino
        self.controls.append((name, value))
        return True


def target(tmp_path, *, pid=54321):
    group = tmp_path/'group'
    group.mkdir()
    (group/'replacement').mkdir()
    manager = Manager(group, pid)
    room_id = str(uuid4())
    entry = {'unit': 'exact-owned-owntone.service', 'name': 'owntone', 'boot_id': 'test-boot',
             'invocation_id': 'original', 'cgroup_inode': group.stat().st_ino}
    unit = SimpleNamespace(manager=manager, process=SimpleNamespace(pid=pid),
                           identity=lambda: deepcopy(entry))
    async def coherent_identity():
        return unit.identity()
    unit.coherent_identity = coherent_identity
    state = SimpleNamespace(desired=SimpleNamespace(id=room_id), processes={'owntone': unit})
    broker = SimpleNamespace(rooms={room_id: state}, network=SimpleNamespace(
        manifest={'processes': {f'{room_id}:owntone': deepcopy(entry)}}))
    return broker, state, manager


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    broker, state, manager = target(tmp_path)
    signals, pidfds = [], []
    reader, writer = os.pipe()
    def open_pidfd(pid, flags):
        assert pid == manager.pid and flags == 0
        descriptor = os.dup(reader)
        pidfds.append(descriptor)
        return descriptor
    monkeypatch.setattr(faults, 'os', SimpleNamespace(geteuid=lambda: 0, pidfd_open=open_pidfd,
        fstat=os.fstat, close=os.close))
    monkeypatch.setattr(faults, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(faults, 'signal', SimpleNamespace(SIGKILL=signal.SIGKILL,
        pidfd_send_signal=lambda descriptor, value: signals.append((descriptor, value))))
    class Poll:
        def register(self, descriptor, events):
            assert descriptor in pidfds
        def poll(self, timeout):
            return []
    monkeypatch.setattr(faults, 'select', SimpleNamespace(poll=Poll, POLLIN=1))
    monkeypatch.setattr(faults, 'boot_id', lambda: 'test-boot')
    monkeypatch.setattr(faults, 'process_birth', lambda pid: 'birth-original')
    yield broker, state, manager, signals, pidfds
    os.close(reader)
    os.close(writer)


def closed(descriptors):
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.asyncio
async def test_exact_crash_admission_keeps_manifest_and_thaws_closes_real_held_descriptors(admitted):
    broker, state, manager, signals, pidfds = admitted
    before = deepcopy(broker.network.manifest)
    evidence = {}
    await faults.kill_owned_main(broker, state, 'owntone', evidence, expected_room_id=state.desired.id)
    assert len(signals) == 1 and signals[0] == (pidfds[0], signal.SIGKILL)
    assert broker.network.manifest == before and manager.inspects == 2
    assert manager.controls == [('cgroup.freeze', b'1'), ('cgroup.freeze', b'0')]
    assert evidence['signal_sent'] and evidence['reservation_retained']
    assert evidence['held_cgroup_thawed'] and evidence['owned_descriptors_closed']
    closed(manager.descriptors+pidfds)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['invocation', 'members', 'inode', 'birth', 'reservation', 'wrong_room', 'wrong_role'])
async def test_stale_or_wrong_destination_refuses_every_signal_and_cleans_held_authority(admitted, monkeypatch, change):
    broker, state, manager, signals, pidfds = admitted
    expected = state.desired.id
    role = 'owntone'
    if change in {'invocation', 'members', 'inode'}:
        setattr(manager, f'changed_{change}', True)
    elif change == 'birth':
        births = iter(('first', 'replacement'))
        monkeypatch.setattr(faults, 'process_birth', lambda pid: next(births))
    elif change == 'reservation':
        original = manager.inspect
        async def replaced(name):
            result = await original(name)
            if manager.inspects == 2:
                broker.network.manifest['processes'][f'{state.desired.id}:owntone']['invocation_id'] = 'replacement'
            return result
        manager.inspect = replaced
    elif change == 'wrong_room':
        expected = str(uuid4())
    else:
        role = 'shairport'
    evidence = {}
    with pytest.raises(RuntimeFailure):
        await faults.kill_owned_main(broker, state, role, evidence, expected_room_id=expected)
    assert not signals
    if manager.controls:
        assert manager.controls[-1] == ('cgroup.freeze', b'0') and evidence['held_cgroup_thawed']
    closed(manager.descriptors+pidfds)


@pytest.mark.asyncio
async def test_cancel_during_frozen_invocation_read_always_thaws_exact_group(admitted):
    broker, state, manager, signals, pidfds = admitted
    manager.inspect_wait = asyncio.Event()
    evidence = {}
    task = asyncio.create_task(faults.kill_owned_main(broker, state, 'owntone', evidence,
                                                    expected_room_id=state.desired.id))
    for _ in range(100):
        if manager.inspects == 2:
            break
        await asyncio.sleep(0)
    assert manager.inspects == 2 and manager.controls == [('cgroup.freeze', b'1')]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not signals and evidence['held_cgroup_thawed'] and evidence['owned_descriptors_closed']
    assert manager.controls[-1] == ('cgroup.freeze', b'0')
    closed(manager.descriptors+pidfds)


@pytest.mark.asyncio
async def test_reservation_replaced_during_first_await_refuses_even_freezing_old_group(admitted):
    broker, state, manager, signals, pidfds = admitted
    original = manager.inspect
    async def inspect(name):
        result = await original(name)
        broker.network.manifest['processes'][f'{state.desired.id}:owntone']['invocation_id'] = 'replacement'
        return result
    manager.inspect = inspect
    with pytest.raises(RuntimeFailure, match='handle/reservation changed'):
        await faults.kill_owned_main(broker, state, 'owntone', {}, expected_room_id=state.desired.id)
    assert not signals and not manager.controls and not manager.descriptors and not pidfds


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != 'linux' or not hasattr(os, 'pidfd_open'), reason='Owned child pidfd signal requires Linux')
async def test_signal_uses_real_pidfd_for_only_our_unreaped_direct_child(tmp_path, monkeypatch):
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(20)')
    broker, state, manager = target(tmp_path, pid=child.pid)
    monkeypatch.setattr(faults, 'boot_id', lambda: 'test-boot')
    # System-manager/cgroup proof is simulated above. The process birth,
    # pidfd and actual SIGKILL are real and apply only to this test's child.
    monkeypatch.setattr(faults, 'os', SimpleNamespace(geteuid=lambda: 0, pidfd_open=os.pidfd_open,
        fstat=os.fstat, close=os.close))
    assert process_birth(child.pid)
    try:
        evidence = {}
        await faults.kill_owned_main(broker, state, 'owntone', evidence, expected_room_id=state.desired.id)
        assert await asyncio.wait_for(child.wait(), 2) == -signal.SIGKILL
        assert evidence['main_pid'] == child.pid and evidence['signal_sent']
        assert evidence['held_cgroup_thawed'] and evidence['owned_descriptors_closed']
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


@pytest.fixture
def group():
    pytest.importorskip('numpy', reason='PCM fault guards require the audio extra')
    pytest.importorskip('aiortc', reason='Manual grouped fixture imports the audio extra')
    return load('native_zone_fault_group_tests', 'check_native_grouping.py')


def capture(group):
    cls = faults.capture_type(group.Capture)
    value = cls.__new__(cls)
    value.__dict__.update(error=None, capture_dropped=0, needs_latency=False, pending=deque(),
        rate=None, channels=None, format=None, total=0, chunks=[], captured_at=[], absolute={},
        sequence=group.observation.PcmSequence(), discontinuities=1, expected_base=1_000_000_000,
        clock_offset_ns=0, max_packet_gap=0, last_packet_at=None, device='offline-PCM-observer')
    return value


def append(group, value, *, gain=1, dropped=False):
    import numpy as np
    frame = value.sequence.frames + sum(len(item[1])//4 for item in value.pending)
    at = frame/48000
    times = (frame+np.arange(960))/48000
    data = np.repeat((8192*gain*np.sin(2*np.pi*440*times)).astype('<i2')[:, None], 2, axis=1).tobytes()
    first = frame+(960 if dropped else 0)
    metadata = {'offset': first, 'offset_end': first+960, 'pts': int(at*1e9),
                'duration': 20_000_000, 'discont': frame == 0}
    value.absolute[at] = value.expected_base+metadata['pts']
    value.pending.append((at, data, 48000, 2, 'S16LE', metadata))


@pytest.mark.parametrize('violation', ['frame_gap', 'gain', 'malformed', 'caps', 'size'])
def test_fault_capture_and_original_pcm_guard_fail_sticky_without_consuming_invalid_buffer(group, violation):
    value = capture(group)
    append(group, value)
    value.poll()
    guard = group.FinalPcmGuard(value)
    guard.begin(0)
    guard.preserve_reference(group.observation.spectrum(value.chunks, 48000, minimum_seconds=.01))
    guard.check()
    append(group, value, gain=.5 if violation == 'gain' else 1, dropped=violation == 'frame_gap')
    if violation in {'malformed', 'caps'}:
        entry = list(value.pending[0])
        entry[1 if violation == 'malformed' else 2] = b'bad' if violation == 'malformed' else 44100
        value.pending[0] = tuple(entry)
    if violation == 'size':
        value.total = faults.CAPTURE_BYTES
    with pytest.raises(RuntimeFailure):
        guard.check()
    assert guard.index == 1
    append(group, value)
    with pytest.raises(RuntimeFailure):
        guard.check()
    assert guard.error and guard.index == 1


def observer_fixture(group):
    room_id = str(uuid4())
    desired = Room(id=room_id, slot=7, name='Untouched', airplay_name='Untouched', interface='eth0',
                   enabled=True, volume=100, speakers=[])
    value = capture(group)
    append(group, value)
    value.poll()
    guard = group.FinalPcmGuard(value)
    guard.begin(0)
    guard.preserve_reference(group.observation.spectrum(value.chunks, 48000, minimum_seconds=.01))
    unit = SimpleNamespace(identity=lambda: {'invocation_id': 'original'}, alive=True)
    player = {'state': 'play', 'item_id': 'original-item', 'volume': 100, 'item_progress_ms': 100}
    outputs = [{'id': '0', 'selected': True}]
    class Client:
        async def request(self, *args):
            return dict(player)
        async def outputs(self, *args):
            return deepcopy(outputs)
    state = SimpleNamespace(desired=desired, processes={'audio': unit}, status='running',
                            client=Client(), current_volume=100, selected_ids=['0'])
    broker = SimpleNamespace(rooms={room_id: state}, ready=True, sender_processes={'timing': unit},
                             _worker_socket=lambda room: 'owned-private-socket')
    context = SimpleNamespace(untouched=room_id, states={room_id: state}, captures={room_id: value},
                             pcm_guards={room_id: guard}, broker=broker)
    health = {'ready': True, 'error': None, 'source': {'ready': True, 'owner': {'session_id': 'exact-old-source'}},
              'native_generation': 1, 'native_group_id': 'exact-group', 'music_gain': 1,
              'speech_session_id': None, 'dropped_bytes': 0, 'speech_dropped_frames': 0, 'native_blocks': 1}
    health['source']['owner']['epoch'] = 1
    observer = faults.FaultObserver(context, {})
    observer.health, observer.owner, observer.player = deepcopy(health), deepcopy(health['source']['owner']), dict(player)
    return observer, context, health, player, outputs, value


@pytest.mark.asyncio
@pytest.mark.parametrize('violation', ['owner', 'generation', 'group', 'gain', 'drop', 'item', 'selection', 'unit', 'capture', 'backward_npt'])
async def test_original_untouched_control_or_capture_identity_cannot_be_rebased_after_fault(group, monkeypatch, violation):
    observer, context, health, player, outputs, value = observer_fixture(group)
    async def rpc(*args, **kwargs):
        return deepcopy(health)
    monkeypatch.setattr(faults, 'call_rpc', rpc)
    await observer._sample()
    if violation == 'owner':
        health['source']['owner']['session_id'] = 'new-source'
    elif violation == 'generation':
        health['native_generation'] += 1
    elif violation == 'group':
        health['native_group_id'] = 'new-group'
    elif violation == 'gain':
        health['music_gain'] = .2
    elif violation == 'drop':
        health['dropped_bytes'] = 3840
    elif violation == 'item':
        player['item_id'] = 'new-item'
    elif violation == 'selection':
        outputs.append({'id': '42', 'selected': True})
    elif violation == 'unit':
        context.states[context.untouched].processes['audio'] = SimpleNamespace(identity=lambda: {'invocation_id': 'new'}, alive=True)
    elif violation == 'capture':
        context.captures[context.untouched] = capture(group)
    else:
        player['item_progress_ms'] -= 1
    with pytest.raises(RuntimeFailure):
        await observer._sample()
    # Returning a transient bad value to its original value cannot erase the
    # receipt or turn the next readiness retry into a new reference.
    assert observer.error
    with pytest.raises(RuntimeFailure):
        await observer.check()


@pytest.mark.asyncio
@pytest.mark.parametrize('motion', ['forward_seek', 'slow_drift', 'stall'])
async def test_untouched_progress_is_bound_to_original_program_clock(group, monkeypatch, motion):
    observer, _context, health, player, _outputs, _value = observer_fixture(group)
    at = [100.]
    monkeypatch.setattr(faults, 'time', SimpleNamespace(monotonic=lambda: at[0]))
    async def rpc(*args, **kwargs):
        return deepcopy(health)
    monkeypatch.setattr(faults, 'call_rpc', rpc)
    await observer._sample()
    if motion == 'forward_seek':
        at[0] += .01
        player['item_progress_ms'] += 5000
    elif motion == 'slow_drift':
        # Each adjacent sample advances, so the original two-second adjacent
        # stall check alone would accept this near-stopped program indefinitely.
        at[0] += 1
        player['item_progress_ms'] += 1
        await observer._sample()
        at[0] += 1
        player['item_progress_ms'] += 1
    else:
        at[0] += 2.01
    with pytest.raises(RuntimeFailure, match='original program timeline'):
        await observer._sample()
    assert observer.error and len(observer.samples) == (2 if motion == 'slow_drift' else 1)


@pytest.mark.asyncio
async def test_failed_watcher_is_joined_at_close_and_cannot_leave_passed_receipt(group, monkeypatch):
    observer, _context, health, _player, _outputs, _value = observer_fixture(group)
    async def rpc(*args, **kwargs):
        return deepcopy(health)
    monkeypatch.setattr(faults, 'call_rpc', rpc)
    observer.evidence['passed'] = True
    await observer.initialize()
    health['source']['owner']['session_id'] = 'unrelated-successor'
    for _ in range(100):
        if observer.watcher.done():
            break
        await asyncio.sleep(.002)
    assert observer.watcher.done()
    with pytest.raises(RuntimeFailure, match='source/timeline'):
        await observer.close()
    assert observer.evidence['passed'] is False
    assert observer.evidence['untouched']['failure']
    assert observer.watcher.done()


@pytest.mark.asyncio
async def test_close_checks_buffered_pcm_tail_before_accepting_completed_fault_proof(group, monkeypatch):
    observer, _context, _health, _player, _outputs, value = observer_fixture(group)
    observer.evidence['passed'] = True
    # Deliberately no watcher scheduling: a bad block arrives after the last
    # explicit guard and before teardown. Final close must consume that tail.
    append(group, value, gain=.2)
    with pytest.raises(RuntimeFailure, match='changed music gain'):
        await observer.close()
    assert observer.evidence['passed'] is False and observer.evidence['untouched']['failure']
    assert observer.evidence['untouched']['pcm']['failed_block']['index'] == 1


def fault_fixture(group, tmp_path, monkeypatch, *, block_reconnect=False):
    """Real helper/PCM guards; explicit simulated manager/recovery transport.

    The separate pidfd tests above prove signal admission. This drives the real
    orchestration and recovery/reconnect functions with a deterministic room
    lifecycle, without claiming to run actual systemd or a hardware backend.
    """
    observer, context, b_health, b_player, _outputs, b_capture = observer_fixture(group)
    b_id, a_id = context.untouched, str(uuid4())
    b_state = context.states[b_id]
    units, events, monitor_tasks = {}, [], []
    clock_at = [100.]
    healths = {b_id: b_health}
    monkeypatch.setattr(faults, 'time', SimpleNamespace(monotonic=lambda: clock_at[0], monotonic_ns=lambda: int(clock_at[0]*1e9)))
    def unit(role, iteration):
        identity = {'invocation_id': f'{role}-{iteration}', 'name': role}
        return SimpleNamespace(identity=lambda: deepcopy(identity), alive=True)
    desired = Room(id=a_id, slot=6, name='Target', airplay_name='Target', interface='eth0',
                   enabled=True, volume=100, speakers=[])
    pin = SimpleNamespace(manifest={'card_index': 0, 'device': 1, 'subdevice': 7}, validate=lambda: None)
    class Client:
        async def outputs(self, *_args):
            return [{'id': '0', 'selected': True}]
    a_state = SimpleNamespace(desired=desired, processes={role: unit(role, 0) for role in ('owntone', 'audio', 'shairport')},
        client=Client(), local_pin=pin, selected_ids=['0'], launch_generation='launch-0', status='running',
        directory=tmp_path/'room')
    a_state.directory.mkdir()
    a_state.snapshot = lambda: {'status': a_state.status, 'error': None, 'retry_in_seconds': 0}
    healths[a_id] = {'ready': True, 'source': {'ready': True, 'incarnation': 'incarnation-0',
                       'owner': {'session_id': 'original-a', 'epoch': 1}}}
    class Api:
        async def room(self, identifier):
            assert identifier == a_id
            return desired.model_dump(mode='json') | {'runtime': {}}
    broker = context.broker
    broker.rooms[a_id] = a_state
    broker._worker_socket = lambda state: state.desired.id
    async def actual_monitor_model():
        events.append('production-monitor-started')
        try:
            await asyncio.Event().wait()
        finally:
            events.append('production-monitor-stopped')
    broker._health_monitor = actual_monitor_model
    broker._monitor = asyncio.create_task(actual_monitor_model())
    monitor_tasks.append(broker._monitor)
    async def rpc(path, *_args, **_kwargs):
        if path == b_id:
            b_player['item_progress_ms'] = 100+int((clock_at[0]-100)*1000)
        return deepcopy(healths[path])
    monkeypatch.setattr(faults, 'call_rpc', rpc)
    joined = asyncio.Event()
    async def original_controls():
        await joined.wait()
    original_monitor = asyncio.create_task(original_controls())
    async def handoff():
        assert any(task.get_name() == 'native-zone-fault-untouched-observer' and not task.done()
                   for task in asyncio.all_tasks())
        assert not original_monitor.done()
        events.append('overlapped-handoff')
        joined.set()
        await original_monitor
    old_a_capture = capture(group)
    for _ in range(5):
        append(group, old_a_capture)
    old_a_capture.poll()
    def install_pipeline(value):
        value.closed = False
        value.close = lambda: setattr(value, 'closed', True)
        value.Gst = SimpleNamespace(State=SimpleNamespace(NULL='NULL'))
        value.pipeline = SimpleNamespace(get_state=lambda _timeout: SimpleNamespace(state='NULL' if value.closed else 'PLAYING'))
    install_pipeline(old_a_capture)
    captures = {a_id: old_a_capture, b_id: b_capture}
    guards = {a_id: group.FinalPcmGuard(old_a_capture), b_id: observer.guard}
    producers = {a_id: {'session': 'original-a'}, b_id: object()}
    original_b_producer = producers[b_id]
    def factory(device, clock, base, offset):
        assert (device, clock, base, offset) == ('hw:0,0,7', 'common-clock', 123, 456)
        value = capture(group)
        install_pipeline(value)
        def start():
            for _ in range(5):
                append(group, value)
            events.append('fresh-target-capture-started')
        value.start = start
        return value
    async def kill(_broker, state, role, evidence, *, expected_room_id):
        assert state is a_state and expected_room_id == a_id
        assert broker._monitor is not None and not broker._monitor.done()
        events.append('crash-'+role)
        evidence.update(signal_sent=True, held_cgroup_thawed=True, owned_descriptors_closed=True,
                        reservation_retained=True)
        iteration = len(units)+1
        units[role] = iteration
        a_state.processes = {name: unit(name, iteration) for name in ('owntone', 'audio', 'shairport')}
        a_state.launch_generation = f'launch-{iteration}'
        healths[a_id]['source'] = {'ready': True, 'incarnation': f'incarnation-{iteration}', 'owner': None}
        clock_at[0] += 1.1
        await asyncio.sleep(.07)  # Allow the real B watcher to sample through recovery.
    monkeypatch.setattr(faults, 'kill_owned_main', kill)
    waiting = asyncio.Event()
    async def launch(_broker, state, root, stress, *, duration_seconds):
        assert state is a_state and duration_seconds == 180 and stress == 0
        assert broker._monitor.done()  # Only synthetic replacement, after ready autoheal.
        events.append('fixture-replacement')
        waiting.set()
        if block_reconnect:
            await asyncio.Event().wait()
        receiver = unit('shairport', 'synthetic-'+a_state.launch_generation)
        a_state.processes['shairport'] = receiver
        return {'unit': receiver, 'command': root/'command.json', 'account': object(),
                'session': 'fresh-'+a_state.launch_generation}
    def publish(_path, command, _account):
        assert not broker._monitor.done()  # Restoration BEFORE any media command.
        assert command['action'] == 'run' and command['generation'] == 2
        producer = producers[a_id]
        healths[a_id]['source']['owner'] = {'session_id': producer['session'], 'epoch': 1}
        monitor_tasks.append(broker._monitor)
        events.append('fresh-target-run')
    def status(producer):
        return {'stage': 'granted', 'session_id': producer['session']}
    monkeypatch.setattr(faults, 'root_directory', lambda path: path.mkdir(parents=True))
    context = faults.FaultContext(api=Api(), broker=broker, states={a_id: a_state, b_id: b_state},
        captures=captures, producers=producers, pcm_guards=guards, report={}, temporary=tmp_path,
        clock='common-clock', base_time=123, clock_offset=456, capture_factory=factory,
        guard_factory=group.FinalPcmGuard, launch_producer=launch, publish_command=publish,
        producer_status=status, onset_index=group.music_onset_index,
        retain_capture=group.retain_failed_capture, handoff=handoff, target=a_id, untouched=b_id)
    return context, events, monitor_tasks, waiting, original_b_producer, b_capture


async def close_fixture(context, monitors):
    for monitor in set(monitors+[context.broker._monitor]):
        monitor.cancel()
    await asyncio.gather(*set(monitors+[context.broker._monitor]), return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_fault_orchestration_keeps_original_b_pcm_clock_and_monitor_until_ready_autoheal(group, tmp_path, monkeypatch):
    context, events, monitors, _waiting, b_producer, b_capture = fault_fixture(group, tmp_path, monkeypatch)
    observer = None
    try:
        observer = await faults.exercise(context)
        assert events.count('crash-owntone') == events.count('crash-audio') == 1
        assert events[events.index('overlapped-handoff')+1] == 'crash-owntone'
        assert events.count('fixture-replacement') == events.count('fresh-target-run') == 2
        assert context.producers[context.untouched] is b_producer
        assert context.captures[context.untouched] is b_capture
        assert observer.watcher and not observer.watcher.done()
        evidence = context.report['zone_faults']
        assert evidence['passed'] and [phase['role'] for phase in evidence['faults']] == ['owntone', 'audio']
        for phase in evidence['faults']:
            assert phase['passed'] and phase['original_stream_resumed'] is False
            assert phase['recovered_health']['source']['owner'] is None
            assert phase['fresh_stream']['health']['source']['owner']['session_id'].startswith('fresh-')
            assert await asyncio.to_thread(Path(phase['historical_final_pcm']['path']).read_bytes)
            assert phase['synthetic_reconnect']['continuous_other_room_observer']
        assert observer.samples[-1]['progress_ms'] > observer.samples[0]['progress_ms']+1000
        # The returned observer remains live for subsequent takeover/END proof;
        # an unobserved late bad B buffer must still reject the tail.
        append(group, b_capture, gain=.2)
        with pytest.raises(RuntimeFailure, match='changed music gain'):
            await observer.check()
    finally:
        if observer:
            with pytest.raises(RuntimeFailure):
                await observer.close()
        await close_fixture(context, monitors)


@pytest.mark.asyncio
async def test_cancel_fixture_receiver_replacement_restores_monitor_and_joins_b_guard_preserving_primary_cancel(group, tmp_path, monkeypatch):
    context, _events, monitors, waiting, b_producer, b_capture = fault_fixture(group, tmp_path, monkeypatch, block_reconnect=True)
    task = asyncio.create_task(faults.exercise(context))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert context.broker._monitor.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not context.broker._monitor.done()
        assert context.producers[context.untouched] is b_producer and context.captures[context.untouched] is b_capture
        assert context.report['zone_faults']['untouched']['original_capture_preserved']
        assert not any(task.get_name() == 'native-zone-fault-untouched-observer' and not task.done()
                       for task in asyncio.all_tasks())
        reconnect = context.report['zone_faults']['faults'][0]['synthetic_reconnect']
        assert reconnect['monitor_restored_monotonic_ns'] > 0
        assert context.report['zone_faults']['passed'] is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await close_fixture(context, monitors)


def test_import_and_runner_do_not_start_a_daemon_and_keep_original_supervisor_boundaries():
    import ast
    original = ast.parse((HERE/'run_native_grouping.py').read_text())
    runner = ast.parse((HERE/'run_native_zone_faults.py').read_text())
    first = next(node for node in original.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'stop_child')
    second = next(node for node in runner.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'stop_child')
    assert ast.dump(first, include_attributes=False) == ast.dump(second, include_attributes=False)
    text = (HERE/'run_native_zone_faults.py').read_text()
    assert 'asyncio.wait_for(process.wait(), 280)' in text and "'--zone-faults'" in text
    assert 'manifest[\'processes\'] or manifest[\'networks\']' in text
    assert "'original_stream_resumed') is False" in text
    assert "'original_capture_preserved') is True" in text
    assert faults.INNER_SECONDS == 280 and faults.EXTERNAL_SECONDS == 400

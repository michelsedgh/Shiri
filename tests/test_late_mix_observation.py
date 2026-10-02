"""Actual H1/late-mix observation gates, with no VM or hardware mutations.

Runtime health comes from the real NativeMixer/SpeechOutput. Final signal
checks consume real synthesized PCM through the maintained per-buffer guard;
only external process/API/RPC facts are simulated in observer flow checks.
"""
import ast
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from shiri.domain import Room, SpeakerRef
from shiri.runtime.native import NativeMixer
from shiri.runtime.speech_output import SpeechOutput
from shiri.runtime.system import RuntimeFailure

np = pytest.importorskip('numpy')
pytest.importorskip('aiortc')

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT/'tests/linux'/filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


group = load('late_mix_actual_group', 'check_native_grouping.py')
probe = load('late_mix_actual_latency', 'native_latency_probe.py')
faults = load('late_mix_actual_faults', 'native_zone_faults.py')


def rooms():
    return [Room(id=identifier, slot=slot, name=f'Fixture{slot}', airplay_name=f'Fixture{slot}',
                 interface='fixture0', local_audio_device=f'hw:CARD=Loopback,DEV={device},SUBDEV=7',
                 enabled=True, volume=100, speakers=[SpeakerRef(id='0', name='Virtual', protocol='alsa')])
            for identifier, slot, device in ((group.A, 6, 1), (group.B, 7, 0))]


def runtime_health(*, horizon_ns=1_000_000_000, buffer_ms=500):
    # Neither constructor opens a file/socket or starts a worker.
    output = SpeechOutput(Path('/private/fixture/speech.sock'), group.B, 'a'*32, 1234)
    writer = SimpleNamespace(reader_present=True, written_bytes=0, dropped_bytes=0)
    mixer = NativeMixer(Path('/private/fixture/music.fifo'), writer=writer,
                        speech_output=output, now_ns=lambda: 10_000_000_000,
                        relay_delay_ns=horizon_ns, output_buffer_ms=buffer_ms)
    return {**mixer.health(), 'speech_session_id': None, 'source': {'ready': True, 'owner': None}}


def healths():
    return {identifier: runtime_health() for identifier in (group.A, group.B)}


def constant_pcm(*, gain=1., voice=False):
    positions = np.arange(960)
    mono = 8192*gain*np.sin(2*np.pi*440*positions/48000)
    if voice:
        mono += 600*np.sin(2*np.pi*880*positions/48000)
    return np.repeat(mono.astype('<i2')[:, None], 2, axis=1).tobytes()


def pcm_guard():
    capture = SimpleNamespace(chunks=[constant_pcm() for _ in range(8)], max_packet_gap=.02,
                              last_packet_at=None, poll=lambda: None, captured_at=[], absolute={})
    guard = group.FinalPcmGuard(capture)
    guard.begin(0)
    guard.preserve_reference(group.observation.spectrum(capture.chunks, 48000, minimum_seconds=.1))
    return guard


def candidate_context():
    definitions = rooms()
    frozen = group.freeze_worker_timing(definitions, healths())
    return SimpleNamespace(target=group.A, untouched=group.B,
                           states={room.id: SimpleNamespace(desired=room) for room in definitions},
                           declared_horizon_ns=1_000_000_000, frozen_timing=frozen, group=group,
                           report={'group_alignment': {'relative_offset_ms': 0}})


def test_historical_grouping_policy_reaches_actual_broker_and_config_seams_without_relabeling_health(tmp_path):
    from shiri.runtime.broker import Broker
    from shiri.runtime.latency import latency_plan
    profile=group.load_legacy_profile_module()
    candidate=profile.broker_class(group.IsolatedBroker)
    definition=rooms()[0]
    assert latency_plan(rooms()).common_horizon_ms == 140
    assert candidate.reconcile.__globals__['latency_plan'](rooms()).common_horizon_ms == 1000
    assert candidate.set_outputs.__globals__['room_buffer_ms'](definition) == 500
    assert candidate._material.__globals__['room_buffer_ms'](definition) == 500
    assert candidate.reconcile.__code__ is Broker.reconcile.__code__
    config=candidate._start_room.__globals__['backend_configs']
    _receiver,own=config(definition,tmp_path/"legacy",{"interface":"receiver0"},
        {"api_host_ip":"10.211.0.1","api_ip":"10.211.0.2"},broker_socket=tmp_path/"broker.sock",
        all_receiver_names=[],password="private-test-only",native_timing=True,audio_uid=1234,
        own_username="shiri-output-1",output_buffer_ms=500)
    assert "start_buffer_ms = 500\n" in own.read_text()
    observed=healths()
    observed[group.A]=runtime_health(horizon_ns=140_000_000,buffer_ms=40)
    with pytest.raises(RuntimeFailure,match="Actual worker timing"):
        group.freeze_worker_timing(rooms(),observed)


def test_pre_pcm_calendar_uses_actual_workers_and_is_immutable_independent_of_default(monkeypatch):
    monkeypatch.setattr(group, 'RELAY_DELAY_NS', 9_000_000_000)
    frozen = group.freeze_worker_timing(rooms(), healths())
    assert frozen.horizon_ns == 1_000_000_000
    assert dict(frozen.buffers_ms) == {group.A: 500, group.B: 500}
    with pytest.raises(AttributeError):
        frozen.horizon_ns = 4_000_000_000
    observed = healths()
    for health in observed.values():
        health['source']['owner'] = {'session_id': 'now-streaming'}
        health['native_blocks'] = 12
    group.require_worker_timing(frozen, rooms(), observed)
    observed[group.B]['timing_relay_delay_ms'] = 4000
    with pytest.raises(RuntimeFailure, match='Actual worker timing'):
        group.require_worker_timing(frozen, rooms(), observed)
    assert frozen.horizon_ns == 1_000_000_000


@pytest.mark.parametrize('fault', ['worker_horizon', 'worker_buffer', 'horizon_bool', 'buffer_bool',
                                 'not_ready', 'source_not_ready', 'error', 'error_bool', 'missing_owner', 'owner', 'pcm', 'pcm_bool',
                                 'missing_worker', 'extra_worker', 'offset', 'empty_selection'])
def test_pre_pcm_freeze_refuses_wrong_plan_or_any_started_source(fault):
    definitions, observed = rooms(), healths()
    health = observed[group.A]
    if fault == 'worker_horizon':
        health['timing_relay_delay_ms'] = 4000
    elif fault == 'worker_buffer':
        health['output_buffer_ms'] = 2250
    elif fault == 'horizon_bool':
        health['timing_relay_delay_ms'] = True
    elif fault == 'buffer_bool':
        health['output_buffer_ms'] = True
    elif fault == 'not_ready':
        health['ready'] = False
    elif fault == 'source_not_ready':
        health['source']['ready'] = False
    elif fault == 'error':
        health['error'] = 'actual worker fault'
    elif fault == 'error_bool':
        health['error'] = False
    elif fault == 'missing_owner':
        health['source'].pop('owner')
    elif fault == 'owner':
        health['source']['owner'] = {'session_id': 'already-live'}
    elif fault in {'pcm', 'pcm_bool'}:
        health['native_blocks'] = 1 if fault == 'pcm' else False
    elif fault == 'missing_worker':
        observed.pop(group.B)
    elif fault == 'extra_worker':
        observed['foreign'] = runtime_health()
    elif fault == 'offset':
        definitions[0] = definitions[0].model_copy(update={'speakers': [
            definitions[0].speakers[0].model_copy(update={'offset_ms': -2000})]})
    else:
        definitions[0] = definitions[0].model_copy(update={'speakers': []})
    with pytest.raises(RuntimeFailure):
        group.freeze_worker_timing(definitions, observed)


@pytest.mark.parametrize('fault', ['mix', 'gain', 'target', 'target_bool', 'session', 'drop', 'speech_drop',
                                 'drop_bool', 'output_error', 'output_errno', 'worker_error', 'missing',
                                 'unarmed', 'unreferenced', 'silent', 'ducked', 'voice'])
def test_null_worker_gain_requires_actual_unchanged_final_pcm_and_strict_late_health(fault):
    health, guard = runtime_health(), pcm_guard()
    if fault == 'mix':
        health['speech_mix'] = 'unverified'
    elif fault == 'gain':
        health['music_gain'] = 1
    elif fault == 'target':
        health['music_target_gain'] = .2
    elif fault == 'target_bool':
        health['music_target_gain'] = True
    elif fault == 'session':
        health['speech_session_id'] = 'unexpected-speech'
    elif fault == 'drop':
        health['dropped_bytes'] = 4
    elif fault == 'speech_drop':
        health['speech_dropped_frames'] = 1
    elif fault == 'drop_bool':
        health['dropped_bytes'] = False
    elif fault == 'output_error':
        health['speech_output_error'] = 'send failure'
    elif fault == 'output_errno':
        health['speech_output_errno'] = 11
    elif fault == 'worker_error':
        health['error'] = 'failed'
    elif fault == 'missing':
        health.pop('speech_output_error')
    elif fault == 'unarmed':
        guard.index = None
    elif fault == 'unreferenced':
        guard.reference = None
    else:
        guard.capture.chunks[-1] = constant_pcm(gain=0 if fault == 'silent' else .2 if fault == 'ducked' else 1,
                                              voice=fault == 'voice')
    with pytest.raises(RuntimeFailure):
        group.require_untouched_music_health(health, guard)


def test_real_late_worker_health_passes_only_with_retained_per_buffer_final_gain():
    health, guard = runtime_health(), pcm_guard()
    assert health['music_gain'] is None and health['speech_mix'] == 'owntone_player'
    group.require_untouched_music_health(health, guard)
    assert guard.reference_blocks == len(guard.capture.chunks)
    guard.capture.chunks.append(constant_pcm(gain=.2))
    with pytest.raises(RuntimeFailure, match='individual untouched-zone PCM block changed'):
        group.require_untouched_music_health(health, guard)
    guard.capture.chunks[-1] = constant_pcm()
    with pytest.raises(RuntimeFailure):
        group.require_untouched_music_health(health, guard)


@pytest.mark.asyncio
async def test_default_h1_offset_matrix_refuses_plan_transition_before_any_observer_or_api_change(monkeypatch):
    context = candidate_context()
    monkeypatch.setattr(probe, 'UntouchedObserver', lambda *_a: pytest.fail('untouched baseline was rebased'))
    async def handoff():
        pytest.fail('initial observer was retired')
    context.handoff = handoff
    with pytest.raises(RuntimeFailure, match='separate epoch redesign is pending'):
        await probe.exercise(context)
    evidence = context.report['latency_probe']
    assert evidence['pending_epoch_redesign'] and not evidence['passed']
    assert len(evidence['rows']) == 9 and all(row['status'] == 'pending' for row in evidence['rows'])
    assert all(state.desired.enabled and state.desired.speakers[0].offset_ms == 0
               for state in context.states.values())


def test_unchanged_h1_offset_forecasts_do_not_alter_frozen_actual_plan():
    context = candidate_context()
    probe.require_offset_plan(context, 0)
    probe.require_offset_plan(context, 2000)
    with pytest.raises(RuntimeFailure):
        probe.require_offset_plan(context, -2000)
    assert context.frozen_timing.receipt()['common_horizon_ns'] == 1_000_000_000


@pytest.mark.parametrize('fault', ['missing', 'mutable', 'wrong_horizon', 'wrong_room', 'duplicated_room'])
def test_h1_cannot_use_an_invented_or_changed_context_plan(fault):
    context = candidate_context()
    frozen = context.frozen_timing
    if fault == 'missing':
        context.frozen_timing = None
    elif fault == 'mutable':
        context.frozen_timing = SimpleNamespace(horizon_ns=frozen.horizon_ns, buffers_ms=frozen.buffers_ms)
    elif fault == 'wrong_horizon':
        context.declared_horizon_ns = 4_000_000_000
    elif fault == 'wrong_room':
        context.frozen_timing = group.FrozenWorkerTiming(frozen.horizon_ns, ((group.A, 500), ('foreign', 500)))
    else:
        context.frozen_timing = group.FrozenWorkerTiming(frozen.horizon_ns, frozen.buffers_ms + frozen.buffers_ms)
    with pytest.raises(RuntimeFailure):
        probe.require_candidate_timing(context)


def test_h1_adverse_anchor_uses_actual_buffer_and_keeps_final_output_in_the_future():
    context = candidate_context()
    buffer_ms, lead, scope = probe.adverse_parameters(context, {'cold_health': runtime_health()})
    anchor = context.declared_horizon_ns-buffer_ms*1_000_000
    arrival = -lead
    assert buffer_ms == 500 and lead == -750_000_000
    assert anchor < arrival < context.declared_horizon_ns and 'actual cold worker' in scope
    assert anchor+lead == -250_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['adverse', 'ready', 'matrix'])
async def test_frozen_fresh_context_cannot_be_relabelled_as_a_legacy_experiment(monkeypatch, entry):
    context = candidate_context()
    context.declared_horizon_ns = 4_000_000_000
    async def patch(*_a, **_kw):
        pytest.fail('Changed candidate declaration reached a room mutation')
    context.api = SimpleNamespace(patch=patch)
    monkeypatch.setattr(probe, 'UntouchedObserver', lambda *_a: pytest.fail('Changed context rebased the original observer'))
    with pytest.raises(RuntimeFailure, match='immutable measured H1000/B500 context plan'):
        if entry == 'adverse':
            probe.adverse_parameters(context, {'cold_health': runtime_health()})
        elif entry == 'ready':
            await probe.ready_target(context, None, {'saved_offset_ms': 0})
        else:
            await probe.exercise(context)


@pytest.mark.parametrize('fault', ['missing', 'buffer', 'horizon', 'live_owner', 'not_ready'])
def test_candidate_adverse_row_requires_actual_cold_buffer_health(fault):
    context, health = candidate_context(), runtime_health()
    if fault == 'missing':
        health.pop('output_buffer_ms')
    elif fault == 'buffer':
        health['output_buffer_ms'] = 2250
    elif fault == 'horizon':
        health['timing_relay_delay_ms'] = 4000
    elif fault == 'live_owner':
        health['source']['owner'] = {'session_id': 'live'}
    else:
        health['ready'] = False
    with pytest.raises(RuntimeFailure):
        probe.adverse_parameters(context, {'cold_health': health})


@pytest.mark.parametrize('horizon,slack', [(3_000_000_000, -1_200_000_000), (4_000_000_000, -200_000_000)])
def test_explicit_legacy_adverse_experiment_keeps_original_pinned_policy(horizon, slack):
    context = SimpleNamespace(declared_horizon_ns=horizon)
    buffer_ms, lead, scope = probe.adverse_parameters(context, {})
    assert buffer_ms == 2250 and lead == probe.ADVERSE_LEAD_NS
    assert horizon-buffer_ms*1_000_000+lead == slack and scope.startswith('legacy')


@pytest.mark.asyncio
@pytest.mark.parametrize('qualified', [True, False])
async def test_actual_adverse_row_records_h1_anchor_and_still_requires_scoped_backend_rejection(tmp_path, monkeypatch, qualified):
    context = candidate_context()
    start = 10_500_000_000
    journal_path = tmp_path/'journal.json'
    journal_path.write_text(json.dumps({'units': {f'{group.A}:owntone': {'journal': {
        'invocation_scoped': qualified, 'records': [{'text': 'Native input missed or lost its initial presentation anchor',
                                                   '__MONOTONIC_TIMESTAMP': str(start//1000+1)}]}}}}))
    capture = SimpleNamespace(validation_rows=[], chunks=[], poll=lambda: {'simulated_external_capture': True})
    leads = []
    async def replace(*_args, **kwargs):
        leads.append(kwargs['lead_ns'])
        return {'command': 'private-simulated', 'account': {}}, {'session_id': 'admitted'}
    async def rpc(*_args, **_kwargs):
        return {'native_blocks': 1, 'ready': True, 'source': {'ready': True, 'owner': {'session_id': 'admitted'}}}
    async def wait(predicate, observer, *_args):
        await observer.check()
        return await predicate()
    async def check():
        pass
    async def retained(*_args, **_kwargs):
        return {'path': str(journal_path)}
    context.broker = SimpleNamespace(_worker_socket=lambda _: 'private-simulated', _password='test',
                                     _room_password=lambda _: 'test')
    context.temporary = tmp_path
    context.producers = {}
    context.publish_command = lambda *_a: None
    context.group = SimpleNamespace(FrozenWorkerTiming=group.FrozenWorkerTiming,
        failure_evidence=SimpleNamespace(owned_sources=lambda *_a: ([{'key': f'{group.A}:owntone'}], []), capture=retained))
    clock = iter((100., 200.))
    monkeypatch.setattr(probe, 'time', SimpleNamespace(monotonic_ns=lambda: 10_000_000_000,
                                                     monotonic=lambda: next(clock)))
    monkeypatch.setattr(probe, 'replace_receiver', replace)
    monkeypatch.setattr(probe, 'start_capture', lambda *_a: capture)
    monkeypatch.setattr(probe, 'call_rpc', rpc)
    monkeypatch.setattr(probe, 'observed_wait', wait)
    row = {'passed': False}
    if qualified:
        await probe.adverse_row(context, SimpleNamespace(check=check), {'cold_health': runtime_health(), 'pin': {}}, row)
        assert row['passed'] and row['qualified_outcome'] == 'mapping_admitted_startup_rejected'
    else:
        with pytest.raises(RuntimeFailure, match='current-invocation'):
            await probe.adverse_row(context, SimpleNamespace(check=check), {'cold_health': runtime_health(), 'pin': {}}, row)
        assert not row['passed']
    assert leads == [-750_000_000]
    assert row['own_initial_anchor_ns'] < row['earliest_declared_arrival_ns'] < row['declared_final_origin_ns']
    assert row['output_buffer_ms'] == 500 and row['startup_slack_ns'] == -250_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['fault', 'latency'])
async def test_actual_untouched_observers_keep_sticky_final_pcm_gain_with_null_worker_gain(monkeypatch, kind):
    desired = rooms()[1]
    guard, health = pcm_guard(), runtime_health()
    health.update(native_blocks=50, native_group_id='exact-group', native_generation=1)
    health['source']['owner'] = {'session_id': 'exact-source', 'epoch': 1}
    unit = SimpleNamespace(identity=lambda: {'invocation_id': 'original'}, alive=True)
    player = {'state': 'play', 'item_id': 'original', 'volume': 100, 'item_progress_ms': 100}
    async def outputs(*_args):
        return [{'id': '0', 'selected': True}]
    async def request(*_args):
        return dict(player)
    state = SimpleNamespace(desired=desired, processes={'audio': unit}, status='running', selected_ids=['0'],
                            current_volume=100, client=SimpleNamespace(outputs=outputs, request=request))
    context = SimpleNamespace(untouched=group.B, states={group.B: state}, captures={group.B: guard.capture},
                              pcm_guards={group.B: guard}, group=group,
                              untouched_health_check=group.require_untouched_music_health,
                              broker=SimpleNamespace(rooms={group.B: state}, ready=True, sender_processes={'shared': unit},
                                                     _worker_socket=lambda _: 'private-simulated'))
    module, observer_type = (faults, faults.FaultObserver) if kind == 'fault' else (probe, probe.UntouchedObserver)
    async def rpc(*_args, **_kwargs):
        return deepcopy(health)
    monkeypatch.setattr(module, 'call_rpc', rpc)
    observer = observer_type(context, {})
    observer.health, observer.owner, observer.player = deepcopy(health), deepcopy(health['source']['owner']), dict(player)
    await observer._sample()
    guard.capture.chunks.append(constant_pcm(gain=.2))
    with pytest.raises(RuntimeFailure, match='individual untouched-zone PCM block changed'):
        await observer._sample()
    guard.capture.chunks[-1] = constant_pcm()
    with pytest.raises(RuntimeFailure):
        await observer.check()
    assert observer.error is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['no_lab', 'no_plan', 'wrong_plan'])
async def test_candidate_adverse_launch_refuses_missing_admission_before_receiver_access(monkeypatch, fault):
    context = candidate_context()
    frozen = context.frozen_timing
    monkeypatch.setattr(group, 'NATIVE_LAB', None if fault == 'no_lab' else object())
    if fault == 'no_plan':
        frozen = None
    elif fault == 'wrong_plan':
        frozen = group.FrozenWorkerTiming(4_000_000_000, frozen.buffers_ms)
    # Absence of processes makes any observation of the original receiver a
    # test failure. Exact plan admission must happen first.
    state = SimpleNamespace(desired=SimpleNamespace(id=group.A))
    with pytest.raises(RuntimeFailure, match='measured frozen lab plan'):
        await group.launch_producer(None, state, None, 0, duration_seconds=480,
                                    arrival_lead_ns=-750_000_000, frozen_timing=frozen)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['healthy', 'buffer', 'not_ready', 'source_not_ready', 'error',
                                 'source_missing', 'owner_missing', 'error_missing', 'error_bool', 'ready_integer'])
async def test_real_owned_candidate_launch_publishes_h1_calendar_and_stops_only_after_actual_timing_check(
        tmp_path, monkeypatch, fault):
    spec = importlib.util.spec_from_file_location('late_mix_owned_receiver_fixture', ROOT/'tests/test_native_group_observation.py')
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    fixture = fixtures.receiver_replacement.__wrapped__(tmp_path, monkeypatch)
    observed_group = fixtures.harness
    frozen = observed_group.FrozenWorkerTiming(1_000_000_000,
        tuple((identifier, 500) for identifier in sorted((observed_group.A, observed_group.B))))
    monkeypatch.setattr(observed_group, 'NATIVE_LAB', object())
    async def rpc(*_args, **_kwargs):
        health = runtime_health()
        if fault == 'buffer':
            health['output_buffer_ms'] = 2250
        elif fault == 'not_ready':
            health['ready'] = False
        elif fault == 'source_not_ready':
            health['source']['ready'] = False
        elif fault == 'error':
            health['error'] = 'Native route is faulted'
        elif fault == 'source_missing':
            health['source'] = None
        elif fault == 'owner_missing':
            health['source'].pop('owner')
        elif fault == 'error_missing':
            health.pop('error')
        elif fault == 'error_bool':
            health['error'] = False
        elif fault == 'ready_integer':
            health['ready'] = 1
        return health
    monkeypatch.setattr(observed_group, 'call_rpc', rpc)
    if fault != 'healthy':
        with pytest.raises(RuntimeFailure, match='actual worker timing before its exact stop'):
            await observed_group.launch_producer(fixture.broker, fixture.state, fixture.root, 0,
                duration_seconds=480, arrival_lead_ns=-750_000_000, frozen_timing=frozen)
        assert fixture.events == [] and fixture.state.processes['shairport'] is fixture.original
    else:
        await observed_group.launch_producer(fixture.broker, fixture.state, fixture.root, 0,
            duration_seconds=480, arrival_lead_ns=-750_000_000, frozen_timing=frozen)
        config = json.loads((fixture.root/'config/producer.json').read_text())
        assert config['candidate_timing'] == {'horizon_ms': 1000, 'buffer_ms': 500}
        assert config['arrival_lead_ns'] == -750_000_000 and config['duration_seconds'] == 480
        assert fixture.events == ['exact original stopped', 'canonical replacement reserved']


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['healthy', 'missing_plan', 'wrong_buffer', 'wrong_horizon', 'follower', 'wrong_lead'])
async def test_real_nonroot_producer_prefix_admits_only_exact_candidate_calendar_without_root_profile(
        tmp_path, fault):
    # Execute the maintained credential/calendar admission before any signal,
    # IPC or unit creation. Config bytes are real; OS credentials are explicit
    # test inputs, and no privileged profile is loaded by this path.
    tree = ast.parse((ROOT/'tests/linux/check_native_grouping.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'producer')
    boundary = next(index for index, node in enumerate(function.body) if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'path' for target in node.targets))
    function.body = function.body[:boundary] + ast.parse('return config').body
    namespace = {'json': json, 'Path': Path, 'MAX_DURATION': 90, 'require': group.require,
                 'os': SimpleNamespace(geteuid=lambda: 501, getegid=lambda: 502)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), '<actual producer admission>', 'exec'), namespace)
    config = {'uid': 501, 'gid': 502, 'duration_seconds': 480, 'leader': True, 'arrival_lead_ns': -750_000_000,
              'candidate_timing': {'horizon_ms': 1000, 'buffer_ms': 500}}
    if fault == 'missing_plan':
        config.pop('candidate_timing')
    elif fault == 'wrong_buffer':
        config['candidate_timing']['buffer_ms'] = 2250
    elif fault == 'wrong_horizon':
        config['candidate_timing']['horizon_ms'] = 4000
    elif fault == 'follower':
        config['leader'] = False
    elif fault == 'wrong_lead':
        config['arrival_lead_ns'] = -1_950_000_000
    path = tmp_path/'producer.json'
    path.write_text(json.dumps(config))
    if fault == 'healthy':
        assert await namespace['producer'](path) == config
    else:
        with pytest.raises(RuntimeFailure):
            await namespace['producer'](path)

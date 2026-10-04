"""Original isolated gates at actual minimum policy; no kernel/media actors."""
import ast
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime.system import RuntimeFailure

HERE = Path(__file__).parent/'linux'


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE/file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


timing = load('minimum_coverage_test_helper', 'native_minimum_coverage.py')
faults = load('minimum_coverage_faults_test', 'native_zone_faults.py')


def rooms():
    return [Room(id=identifier, slot=6+n, name=f'Validation {n}', airplay_name=f'Validation {n}',
        interface='eth0', enabled=True, local_audio_device=f'hw:CARD=Loopback,DEV={1-n},SUBDEV=7',
        speakers=[SpeakerRef(id='0', name='Local', protocol='alsa')]) for n, identifier in enumerate((timing.A, timing.B))]


def healths():
    return {identifier: {'ready': True, 'error': None, 'source': {'ready': True, 'owner': None},
        'native_blocks': 0, 'timing_relay_delay_ms': 140, 'output_buffer_ms': 40} for identifier in (timing.A, timing.B)}


def test_freeze_uses_actual_production_plan_and_saved_routes_before_pcm():
    from shiri.runtime.latency import latency_plan
    definitions, actual = rooms(), healths()
    frozen = timing.freeze(definitions, actual)
    assert frozen.plan == latency_plan(definitions)
    assert frozen.horizon_ns == 140_000_000 and timing.valid_receipt(frozen.receipt())
    timing.require_timing(frozen, definitions, actual)
    timing.require_room_timing(frozen, timing.A, definitions[0], actual[timing.A], before_pcm=True)
    actual[timing.A]['source']['owner'] = {'session_id': 'actual-admitted-source'}
    actual[timing.A]['native_blocks'] = 1
    timing.require_timing(frozen, definitions, actual)
    with pytest.raises(RuntimeFailure, match='precede'):
        timing.freeze(definitions, actual)


@pytest.mark.parametrize('key,value', [('output_buffer_ms', 500), ('output_buffer_ms', True),
    ('output_buffer_ms', None), ('timing_relay_delay_ms', 1000), ('timing_relay_delay_ms', True),
    ('timing_relay_delay_ms', None), ('ready', False), ('error', 'failure'), ('source', {}),
    ('source', {'ready': True, 'owner': None, 'unrelated': 'payload'})])
def test_actual_health_refuses_changed_missing_or_unready_timing(key, value):
    definitions, original = rooms(), healths()
    frozen = timing.freeze(definitions, original)
    changed = deepcopy(original)
    changed[timing.A][key] = value
    if key == 'source' and value.get('unrelated'):
        # Optional unrelated diagnostics cannot substitute for required facts.
        timing.require_timing(frozen, definitions, changed)
    else:
        with pytest.raises(RuntimeFailure):
            timing.require_timing(frozen, definitions, changed)


@pytest.mark.parametrize('change', [{'native_blocks': 1}, {'native_blocks': True}, {'native_blocks': None},
    {'source': {'ready': True, 'owner': {'session_id': 'already-live'}}}])
def test_freeze_rejects_existing_owner_or_pcm(change):
    actual = healths()
    actual[timing.B].update(change)
    with pytest.raises(RuntimeFailure, match='precede'):
        timing.freeze(rooms(), actual)


@pytest.mark.parametrize('mutation', ['offset', 'protocol', 'selection', 'device', 'extra', 'missing', 'disabled'])
def test_plan_and_room_rechecks_reject_material_route_mutations(mutation):
    definitions, actual = rooms(), healths()
    frozen = timing.freeze(definitions, actual)
    changed = deepcopy(definitions)
    if mutation == 'offset':
        changed[0] = changed[0].model_copy(update={'speakers': [changed[0].speakers[0].model_copy(update={'offset_ms': 20})]})
    elif mutation == 'protocol':
        changed[0] = changed[0].model_copy(update={'speakers': [changed[0].speakers[0].model_copy(update={'protocol': 'pulseaudio'})]})
    elif mutation == 'selection':
        changed[0].speakers.clear()
    elif mutation == 'device':
        changed[0] = changed[0].model_copy(update={'local_audio_device': 'hw:CARD=Different,DEV=0'})
    elif mutation == 'extra':
        changed.append(changed[0].model_copy(update={'id': '00000000-0000-0000-0000-000000000009', 'slot': 3}))
    elif mutation == 'disabled':
        changed[0] = changed[0].model_copy(update={'enabled': False})
    else:
        changed.pop()
    with pytest.raises((RuntimeFailure, ValueError)):
        timing.require_timing(frozen, changed, actual)
    if mutation in {'offset', 'protocol', 'selection', 'device', 'disabled'}:
        with pytest.raises((RuntimeFailure, ValueError)):
            timing.require_room_timing(frozen, timing.A, changed[0], actual[timing.A])


def test_explicit_bluetooth_volume_revision_keeps_route_and_actual_plan_frozen():
    definitions, actual = rooms(), healths()
    definitions[0] = definitions[0].model_copy(update={'local_audio_device': 'bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp'})
    frozen = timing.freeze(definitions, actual)
    definitions[0] = definitions[0].model_copy(update={'volume': 50, 'revision': definitions[0].revision+1})
    timing.require_timing(frozen, definitions, actual)
    timing.require_room_timing(frozen, timing.A, definitions[0], actual[timing.A])
    assert frozen.receipt()['output_buffers_ms'] == {timing.A: 40, timing.B: 40}


@pytest.mark.parametrize('mutation', ['missing', 'historical', 'bool_h', 'bool_b', 'owner', 'route', 'speaker', 'extra'])
def test_outer_receipt_rejects_absent_historical_and_substituted_minimum_claims(mutation):
    value = timing.freeze(rooms(), healths()).receipt()
    if mutation == 'missing':
        value.pop('saved_routes')
    elif mutation == 'historical':
        value['common_horizon_ns'], value['output_buffers_ms'] = 1_000_000_000, {timing.A: 500, timing.B: 500}
    elif mutation == 'bool_h':
        value['common_horizon_ns'] = True
    elif mutation == 'bool_b':
        value['output_buffers_ms'][timing.A] = True
    elif mutation == 'owner':
        value['frozen_before_owner_or_pcm'] = False
    elif mutation == 'route':
        value['saved_routes'][0]['room_id'] = 'unadmitted'
    elif mutation == 'speaker':
        value['saved_routes'][0]['speakers'][0]['offset_ms'] = False
    else:
        value['extra'] = 'unverified'
    assert not timing.valid_receipt(value)
    assert not timing.valid_receipt(None)


@pytest.mark.parametrize('minimum_policy', [False, True])
@pytest.mark.parametrize('file', ['run_native_grouping.py', 'run_native_speech_stress.py', 'run_native_zone_faults.py'])
async def test_exact_supervisor_child_command_propagates_only_explicit_minimum_flag(file, minimum_policy):
    tree = ast.parse((HERE/file).read_text())
    launch = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and ast.unparse(node.func) == 'asyncio.create_subprocess_exec')
    function = ast.AsyncFunctionDef(name='launch', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
        kw_defaults=[], defaults=[]), body=[ast.Return(value=ast.Await(value=launch))], decorator_list=[])
    call = AsyncMock(return_value=object())
    environment = {'asyncio': SimpleNamespace(create_subprocess_exec=call, subprocess=asyncio.subprocess),
        'sys': SimpleNamespace(executable='/exact/installed/python'), 'HERE': HERE/file,
        'namespace': 'shiri_group_run_12345678', 'descriptor': 10, 'original_descriptor': 11,
        'minimum_policy': minimum_policy, 'environment': {'exact': 'environment'}, 'log': object()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(HERE/file), 'exec'), environment)
    await environment['launch']()
    args, kwargs = call.await_args
    assert args[:5] == ('/usr/bin/nsenter', '--net=/proc/self/fd/10', '--', '/exact/installed/python', str(HERE/'check_native_grouping.py'))
    assert ('--minimum-policy' in args) is minimum_policy
    assert kwargs['pass_fds'] == (10, 11) and kwargs['env'] == environment['environment']


@pytest.mark.parametrize('buffer', [40, 500])
async def test_actual_recovery_body_requires_healed_minimum_worker_before_new_receiver(monkeypatch, buffer):
    definitions, actual = rooms(), healths()
    frozen = timing.freeze(definitions, actual)
    units = {role: SimpleNamespace(alive=True, identity=lambda role=role: {'invocation_id': 'fresh-'+role})
             for role in ('owntone', 'audio', 'shairport')}
    intent = definitions[0].model_dump(mode='json')
    state = SimpleNamespace(desired=definitions[0], status='running', launch_generation='fresh', processes=units,
        selected_ids=['0'], local_pin=SimpleNamespace(validate=lambda: None, manifest={'admitted': 'pin'}),
        client=SimpleNamespace(outputs=AsyncMock(return_value=[{'id': '0', 'selected': True}])),
        snapshot=lambda: {'status': 'running', 'error': None, 'retry_in_seconds': 0})
    context = SimpleNamespace(target=timing.A, states={timing.A: state}, minimum_timing=frozen,
        timing_check=timing.require_room_timing, broker=SimpleNamespace(_worker_socket=lambda _state: '/not-opened'),
        api=SimpleNamespace(room=AsyncMock(return_value=intent)))
    health = {**actual[timing.A], 'output_buffer_ms': buffer, 'source': {'ready': True, 'owner': None, 'incarnation': 'fresh'}}
    monkeypatch.setattr(faults, 'call_rpc', AsyncMock(return_value=health))
    old = {'intent': intent, 'launch_generation': 'old', 'units': {role: {'invocation_id': 'old-'+role} for role in units},
           'pcm_pin': {'admitted': 'pin'}, 'source_incarnation': 'old'}
    observer, evidence = SimpleNamespace(check=AsyncMock()), {}
    if buffer == 40:
        assert await faults.recovered(context, old, observer, evidence) == health
        assert evidence['original_stream_resumed'] is False
    else:
        with pytest.raises(RuntimeFailure, match='actual worker H/B'):
            await faults.recovered(context, old, observer, evidence)
        assert 'recovered_health' not in evidence
    assert evidence['recovery_observations']


def test_original_sticky_fault_context_cannot_drop_minimum_verifier():
    definitions, actual = rooms(), healths()
    context = SimpleNamespace(minimum_timing=timing.freeze(definitions, actual), timing_check=None)
    with pytest.raises(RuntimeFailure, match='verifier'):
        faults.require_minimum_timing(context, timing.B, definitions[1], actual[timing.B])
    context.minimum_timing = None
    faults.require_minimum_timing(context, timing.B, definitions[1], actual[timing.B])


@pytest.mark.parametrize('arguments', [{'minimum_policy': 1}, {'minimum_policy': 'yes'},
    {'minimum_policy': True, 'music_minimum': True},
    {'minimum_policy': True, 'music_soak': True}])
async def test_minimum_flag_cannot_enter_other_prepared_or_historical_experiments(monkeypatch, arguments):
    pytest.importorskip('aiortc')
    group = load('minimum_flag_admission_test', 'check_native_grouping.py')
    monkeypatch.setattr(group, 'NATIVE_LAB', object())
    # These are the exact initial guards, before any root/path/namespace action.
    with pytest.raises(RuntimeFailure, match='Minimum coverage'):
        await group.run_check(**arguments)


async def test_original_bt_healthy_body_refuses_actual_historical_output_buffer():
    pytest.importorskip('aiortc')
    from test_native_bluetooth_route import actual_granted_source, checker_coroutine, checker_health_environment
    native, _writer, _handle, _grant, expected = await actual_granted_source()
    try:
        environment, _report = checker_health_environment(native, expected)
        native.mixer.output_buffer_ms = 500
        healthy = checker_coroutine('healthy', environment)
        with pytest.raises(RuntimeFailure, match='actual worker H/B'):
            await healthy()
    finally:
        await native.close()


def test_runtime_minimum_calendars_and_recovery_checks_are_explicit():
    group = ast.parse((HERE/'check_native_grouping.py').read_text())
    body = next(node for node in group.body if getattr(node, 'name', None) == 'run_check')
    text = ast.unparse(body)
    assert 'broker_class = IsolatedBroker' in text
    assert 'declared_horizon_ns = minimum_timing.horizon_ns' in text
    assert 'common_start = time.monotonic_ns() + 4000000000' in text
    assert 'common_start + declared_horizon_ns + 2000000000' in text
    bt = ast.parse((HERE/'check_native_bluetooth_route.py').read_text())
    base = next(node for node in bt.body if getattr(node, 'name', None) == 'IsolatedBluetoothBroker')
    assert ast.unparse(base.bases[0]) == 'group.IsolatedBroker'
    exercise = next(node for node in bt.body if getattr(node, 'name', None) == 'exercise')
    assert 'frozen_timing.horizon_ns / 1000000000.0' in ast.unparse(exercise)
    healthy = next(node for node in ast.walk(exercise) if getattr(node, 'name', None) == 'healthy')
    assert 'require_room_timing' in ast.unparse(healthy)
    fault_tree = ast.parse((HERE/'native_zone_faults.py').read_text())
    sample = next(node for node in ast.walk(fault_tree) if getattr(node, 'name', None) == '_sample_once')
    assert 'require_minimum_timing(self.context, self.room, state.desired, health)' in ast.unparse(sample)
    recovered = next(node for node in fault_tree.body if getattr(node, 'name', None) == 'recovered')
    assert 'before_pcm=True' in ast.unparse(recovered)


@pytest.mark.parametrize('owned', [True, False])
async def test_actual_minimum_fault_hook_uses_owned_broker_plan_and_refuses_lost_context(owned):
    """The real nested hook needs no free timing variable in a legacy fragment."""
    tree = ast.parse((HERE/'check_native_grouping.py').read_text())
    run = next(node for node in tree.body if getattr(node, 'name', None) == 'run_check')
    hook = next(node for node in ast.walk(run) if isinstance(node, ast.If) and ast.unparse(node.test) == 'faults'
                and any(isinstance(child, ast.Attribute) and child.attr == 'FaultContext' for child in ast.walk(node)))
    frozen = timing.freeze(rooms(), healths())
    broker = SimpleNamespace()
    if owned:
        broker.minimum_coverage_timing = frozen
        broker.minimum_coverage_timing_check = timing.require_room_timing
    called = []
    async def exercise(context):
        called.append(context)
        assert context.minimum_timing is frozen and context.timing_check is timing.require_room_timing
        return 'exact-fault-observer'
    function = ast.AsyncFunctionDef(name='hook', args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[],
        kw_defaults=[], defaults=[]), body=[hook, ast.Return(value=ast.Name(id='fault_observer', ctx=ast.Load()))], decorator_list=[])
    environment = {'faults': SimpleNamespace(FaultContext=lambda **kw: SimpleNamespace(**kw), exercise=exercise),
        'report': {'minimum_policy': True, 'producer_final': {}}, 'deepcopy': deepcopy,
        'api': object(), 'broker': broker, 'room_states': {}, 'captures': {}, 'producers': {}, 'pcm_guards': {},
        'temporary': Path('/not-created'), 'clock': object(), 'base_time': 1, 'offset': 2,
        'capture_class': object(), 'FinalPcmGuard': object(), 'launch_producer': object(),
        'publish_command': object(), 'producer_status': lambda _handle: {}, 'music_onset_index': object(),
        'retain_failed_capture': object(), 'A': timing.A, 'B': timing.B, 'require_untouched_music_health': object(),
        'require': timing.require}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(HERE/'check_native_grouping.py'), 'exec'), environment)
    if owned:
        assert await environment['hook']() == 'exact-fault-observer'
        assert len(called) == 1
    else:
        with pytest.raises(RuntimeFailure, match='exact pre-PCM broker plan'):
            await environment['hook']()
        assert not called

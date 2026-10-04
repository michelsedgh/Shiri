"""Current native late-mix observation gates, with no VM or hardware mutations.

Runtime health comes from the real NativeMixer/SpeechOutput. Final signal
checks consume real synthesized PCM through the maintained per-buffer guard;
only external process/API/RPC facts are simulated in observer flow checks.
"""
from copy import deepcopy
import importlib.util
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


def runtime_health(*, horizon_ns=140_000_000, buffer_ms=40):
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


def test_pre_pcm_calendar_uses_actual_workers_and_is_immutable_independent_of_default(monkeypatch):
    monkeypatch.setattr(group, 'RELAY_DELAY_NS', 9_000_000_000)
    frozen = group.freeze_worker_timing(rooms(), healths())
    assert frozen.horizon_ns == 140_000_000
    assert dict(frozen.buffers_ms) == {group.A: 40, group.B: 40}
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
    assert frozen.horizon_ns == 140_000_000


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

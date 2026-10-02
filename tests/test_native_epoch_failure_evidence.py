"""Exact epoch evidence paths/binds and refused final clock attempts.

Real file descriptors/type/modes/inodes; only UID attribution and OS/service
facts are simulated on this portable host. No extra evidence authority is
admitted for the unchanged legacy default.
"""
# File observations are bounded and occur between awaits.
# ruff: noqa: ASYNC240
from copy import deepcopy
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from shiri.runtime.system import RuntimeFailure
from shiri.runtime.units import Bind, UnitManager, UnitSpec, new_unit

ROOT = Path(__file__).parents[1].resolve()
ROOM = 'b6786543-7eb2-443d-83b1-65b984123a76'
BOOT = '49d00e63-ed5f-45a0-bc99-63cde4f1f15b'


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT/'tests/linux'/file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evidence = load('epoch_failure_actual_evidence', 'group_failure_evidence.py')
group = load('epoch_failure_actual_group', 'check_native_grouping.py')
probe = load('epoch_failure_actual_probe', 'native_latency_probe.py')


@pytest.fixture
def owned(tmp_path, monkeypatch):
    epoch = str(uuid4())
    room = tmp_path.resolve()/'rooms'/ROOM
    root = room/'native-validation-epoch'/epoch
    for part in ('config', 'status', 'logs'):
        (root/part).mkdir(parents=True)
    log = root/'logs/shairport.log'
    log.write_text('Synthetic native clock sampling exhausted its5ms elapsed budget\n')
    log.chmod(0o600)
    account = {'name': 'shiri-receiver-6', 'uid': 1991, 'gid': os.getgid() or 1992}
    status = root/'status/status.json'
    status.write_text(json.dumps({'uid': account['uid'], 'gid': account['gid'], 'pid': 1234,
        'frames': 241920, 'stage': 'streaming', 'finished': True, 'error': 'Synthetic clock failed',
        'clock_sample_attempts': 254, 'clock_sample_retries': 2, 'max_clock_bracket_ns': 900000,
        'max_attempt_clock_bracket_ns': 5100000}))
    status.chmod(0o600)
    original_fstat = os.fstat
    receiver_inodes = {status.stat().st_ino, status.parent.stat().st_ino}
    def fstat(descriptor):
        info = original_fstat(descriptor)
        values = list(info)
        values[4] = account['uid'] if info.st_ino in receiver_inodes else 0
        if info.st_ino in receiver_inodes:
            values[5] = account['gid']
        return os.stat_result(values)
    monkeypatch.setattr(evidence.os, 'fstat', fstat)
    original_lstat = Path.lstat
    def lstat(path):
        values = list(original_lstat(path))
        values[4] = 0
        return os.stat_result(values)
    monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(evidence, 'boot_id', lambda: BOOT)
    # UnitSpec.intent requires the same independently supplied boot identity.
    from shiri.runtime import units
    monkeypatch.setattr(units, 'boot_id', lambda: BOOT)
    spec = UnitSpec(new_unit('ab3fa8b8', ROOM, 'shairport'), 'shairport', account['name'], account['name'],
        (sys.executable, str(ROOT/'tests/linux/check_native_grouping.py'), '--producer',
         '/run/shiri-worker/probe-config/producer.json'), binds=(
            Bind(str(room/'input'), '/run/shiri-worker/input'),
            Bind(str(root/'config'), '/run/shiri-worker/probe-config'),
            Bind(str(root/'status'), '/run/shiri-worker/validation', True)))
    entry = spec.intent()
    entry.update(invocation_id='1'*32, control_group='/system.slice/'+spec.name, cgroup_inode=12345,
                 log_path=str(log))
    UnitManager.validate_saved(entry)
    handle = SimpleNamespace(identity=lambda: deepcopy(entry), manager=SimpleNamespace(
        inspect=AsyncMock(return_value={'InvocationID': '1'*32, 'ActiveState': 'failed', 'SubState': 'failed',
        'Result': 'exit-code', 'MainPID': 0, 'ExecMainPID': 1234, 'ExecMainCode': 1, 'ExecMainStatus': 1})))
    producer = {'unit': handle, 'key': ROOM+':shairport', 'status': status,
                'command': root/'config/command.json', 'account': dict(account)}
    state = SimpleNamespace(directory=room, processes={'shairport': handle}, desired=SimpleNamespace(id=ROOM, slot=6))
    broker = SimpleNamespace(config=SimpleNamespace(runtime_state_dir=room.parents[1]), sender_processes={},
        _account=lambda owner, role, slot: dict(account), network=SimpleNamespace(installation_tag='ab3fa8b8',
        manifest={'processes': {producer['key']: deepcopy(entry)}}))
    return SimpleNamespace(epoch=epoch, root=root, log=log, status=status, account=account,
        producer=producer, state=state, broker=broker, entry=entry, handle=handle)


def admit(value, epoch=None):
    return evidence.owned_sources(value.broker, {ROOM: value.state}, {ROOM: value.producer}, epoch_id=epoch)


async def test_exact_dead_epoch_producer_status_and_logs_survive_room_deletion(owned, tmp_path):
    sources, errors = admit(owned, owned.epoch)
    assert errors == [] and len(sources) == 1
    assert sources[0]['producer']['path'] == owned.status and sources[0]['log_path'] == owned.log
    async def journal(source, sanitizer):
        return {'records': [], 'omitted_nonmatching_records': 0}
    receipt = await evidence.capture(tmp_path.resolve()/'failure-evidence', sources, journal=journal)
    shutil.rmtree(owned.state.directory)
    retained = json.loads(Path(receipt['path']).read_text())['units'][owned.producer['key']]
    assert retained['producer_status']['state']['frames'] == 241920
    assert retained['producer_status']['state']['max_attempt_clock_bracket_ns'] == 5100000
    assert retained['status']['MainPID'] == 0 and retained['status']['ExecMainStatus'] == 1
    assert '5ms elapsed budget' in retained['logs'][0]['text']


def test_default_legacy_evidence_does_not_infer_or_admit_an_epoch_directory(owned):
    sources, errors = admit(owned)
    assert sources == [] and errors


@pytest.mark.parametrize('fault', ['uuid', 'status', 'command', 'log', 'uid', 'gid', 'name', 'slot',
                                  'handle', 'key', 'manifest', 'argv', 'bind_status', 'bind_config', 'bind_input'])
def test_epoch_evidence_refuses_wrong_phase_path_credentials_command_or_bind_authority(owned, fault):
    epoch = owned.epoch
    if fault == 'uuid':
        epoch = str(uuid4())
    elif fault in ('status', 'command'):
        owned.producer[fault] = owned.root.parent/str(uuid4())/owned.producer[fault].relative_to(owned.root)
    elif fault == 'log':
        owned.entry['log_path'] = str(owned.root.parent/str(uuid4())/'logs/shairport.log')
    elif fault in ('uid', 'gid'):
        owned.producer['account'][fault] += 1
    elif fault == 'name':
        owned.producer['account']['name'] = 'shiri-receiver-7'
    elif fault == 'slot':
        owned.state.desired.slot = 7
    elif fault == 'handle':
        owned.producer['unit'] = SimpleNamespace(identity=owned.handle.identity)
    elif fault == 'key':
        owned.producer['key'] = str(uuid4())+':shairport'
    elif fault == 'manifest':
        owned.broker.network.manifest['processes'][owned.producer['key']]['invocation_id'] = '2'*32
    elif fault == 'argv':
        owned.entry['argv'][1] = '/private/wrong/check_native_grouping.py'
    elif fault == 'bind_status':
        owned.entry['properties']['BindPaths'] = owned.entry['properties']['BindPaths'].replace(':norbind', ':norbind '+str(owned.root)+'/extra:/run/shiri-worker/extra:norbind')
    elif fault == 'bind_config':
        owned.entry['properties']['BindReadOnlyPaths'] = owned.entry['properties']['BindReadOnlyPaths'].replace('/config:', '/other-config:')
    else:
        owned.entry['properties']['BindReadOnlyPaths'] = owned.entry['properties']['BindReadOnlyPaths'].replace('/input:', '/wrong-input:')
    if fault in ('log', 'argv', 'bind_status', 'bind_config', 'bind_input'):
        owned.broker.network.manifest['processes'][ROOM+':shairport'] = deepcopy(owned.entry)
    sources, errors = admit(owned, epoch)
    assert sources == [] and errors


@pytest.mark.parametrize('bad', [False, 1, '', 'NOT-A-UUID', '11C2D007-A709-4A95-B7D0-FA0645C22342'])
def test_evidence_epoch_requires_explicit_canonical_uuid(owned, bad):
    with pytest.raises(RuntimeFailure, match='canonical UUID'):
        admit(owned, bad)


def failed_clock(monkeypatch, *, attempts=False):
    if attempts:
        readings = [n for i in range(4) for n in (10_000_000_000+i*1_010_000, 10_001_001_000+i*1_010_000)]
        raw = iter(7_000_000_000+i*1_010_000 for i in range(4))
    else:
        readings, raw = [10_000_000_000, 10_005_100_000], iter([7_000_000_000])
    mono = iter(readings)
    monkeypatch.setattr(group.time, 'monotonic_ns', lambda: next(mono))
    monkeypatch.setattr(group.time, 'CLOCK_MONOTONIC_RAW', 4, raising=False)
    monkeypatch.setattr(group.time, 'clock_gettime_ns', lambda _: next(raw))
    stats = {}
    with pytest.raises(RuntimeFailure, match='5ms|4 coherent'):
        group.coherent_clock_sample(stats=stats)
    return stats


@pytest.mark.parametrize('attempts', [False, True])
def test_exact_final_refused_clock_attempt_is_recorded_before_unchanged_source_guard(owned, monkeypatch, attempts):
    stats = failed_clock(monkeypatch, attempts=attempts)
    final = stats['clock_sample_failure']
    assert final['budget_ns'] == 5_000_000 and final['bracket_limit_ns'] == 1_000_000
    assert final['kind'] == ('attempts' if attempts else 'elapsed_budget')
    assert final['attempt'] == (4 if attempts else 1)
    assert final['bracket_ns'] == (1_001_000 if attempts else 5_100_000)
    status = json.loads(owned.status.read_text())
    status.update(stats)
    owned.status.write_text(json.dumps(status))
    sources, errors = admit(owned, owned.epoch)
    assert not errors
    retained = evidence.producer_status(sources[0], evidence.Sanitizer())['state']
    assert retained['clock_sample_failure'] == final
    assert retained['max_attempt_clock_bracket_ns'] == final['bracket_ns']


@pytest.mark.parametrize('fault', ['elapsed', 'bracket', 'budget', 'attempt', 'type', 'extra'])
def test_clock_refusal_receipt_cannot_relabel_or_expand_actual_failed_guard(owned, monkeypatch, fault):
    status = json.loads(owned.status.read_text())
    status.update(failed_clock(monkeypatch))
    failed = status['clock_sample_failure']
    key, value = {'elapsed': ('elapsed_ns', 1), 'bracket': ('bracket_ns', 1),
        'budget': ('budget_ns', 99_000_000), 'attempt': ('attempt', 5),
        'type': ('after_ns', True), 'extra': ('invented', 1)}[fault]
    failed[key] = value
    owned.status.write_text(json.dumps(status))
    sources, errors = admit(owned, owned.epoch)
    assert not errors
    with pytest.raises(RuntimeFailure, match='relabels'):
        evidence.producer_status(sources[0], evidence.Sanitizer())


@pytest.mark.parametrize('health_failure', [None, 'error', 'timeout'])
async def test_failed_post_silence_player_keeps_actual_health_before_unchanged_warm_assertion(monkeypatch, health_failure):
    class Tone:
        samples, utterance, audible = 0, 0, False
        triggers = []
        def release(self):
            self.utterance += 1
            self.triggers = [{'utterance': self.utterance, 'first_emitted_monotonic_ns': 10_000_000_000}]
        def stop(self):
            pass
    tone = Tone()
    peer = SimpleNamespace(connectionState='connected', localDescription=SimpleNamespace(sdp='simulated-external'),
        createOffer=AsyncMock(return_value=None), setLocalDescription=AsyncMock(), setRemoteDescription=AsyncMock())
    async def close():
        peer.connectionState = 'closed'
    peer.close = close
    monkeypatch.setattr(probe, 'make_peer', lambda: (peer, tone))
    capture = SimpleNamespace(chunks=[], validation_rows=[], poll=lambda: {})
    monkeypatch.setattr(probe, 'start_capture', lambda context, manifest: capture)
    monkeypatch.setattr(probe, 'playback_closed', lambda pin: None)
    async def wait(predicate, observer, seconds, description):
        return await predicate()
    monkeypatch.setattr(probe, 'observed_wait', wait)
    async def gate(context, observer, gate, record, **kwargs):
        record.update(passed=True)
    monkeypatch.setattr(probe, 'marker_gate', gate)
    health = {'ready': True, 'source': {'ready': True, 'owner': None}, 'audio_active': False,
              'speech_input_active': False, 'speech_sent_frames': 960, 'speech_dropped_frames': 0}
    monkeypatch.setattr(probe, 'WARM_DIAGNOSTIC_SECONDS', .01)
    async def health_rpc(*args, **kwargs):
        if health_failure == 'error':
            raise RuntimeFailure('Simulated missing exact worker socket')
        if health_failure == 'timeout':
            await asyncio.sleep(1)
        return health
    monkeypatch.setattr(probe, 'call_rpc', health_rpc)
    calls = []
    async def request(method, path, *, json):
        calls.append(json['action'])
        return {'admitted_room_id': 'target', 'type': 'answer', 'sdp': 'simulated-external'}
    state = SimpleNamespace(processes={}, client=SimpleNamespace(request=AsyncMock(return_value={'state': 'stop', 'item_id': 1})))
    context = SimpleNamespace(target='target', states={'target': state}, api=SimpleNamespace(request=request),
        broker=SimpleNamespace(_worker_socket=lambda state: '/private/exact.sock'),
        pcm_guards={'target': SimpleNamespace(evidence=lambda: {'checked_music_blocks': 0, 'failed_block': None})})
    configuration = {'pin': {}, 'saved_offset_ms': 0, 'units': {}}
    rows = [{'kind': kind, 'offset_ms': 0, 'status': 'pending', 'passed': False} for kind in ('cold_idle', 'warm_idle')]
    with pytest.raises(RuntimeFailure, match='lost the warm OwnTone path'):
        await probe.idle_rows(context, None, configuration, rows)
    assert rows[0]['post_silence_player'] == {'state': 'stop', 'item_id': 1}
    if health_failure is None:
        assert rows[0]['post_silence_worker_health'] == health
    else:
        assert 'post_silence_worker_health' not in rows[0]
        assert rows[0]['post_silence_worker_health_error']['type'] == ('TimeoutError' if health_failure == 'timeout' else 'RuntimeFailure')
    assert rows[0]['post_silence_sender']['peer_connection_state'] == 'connected'
    assert rows[0]['post_silence_sender']['audible'] is False
    assert rows[0]['post_silence_sender']['triggers']
    assert rows[0]['post_silence_outbound_rtp_stats_error']['type'] == 'AttributeError'
    assert rows[0]['post_silence_final_pcm_guard']['failed_block'] is None
    assert rows[1]['status'] == 'pending' and rows[1]['passed'] is False
    assert calls == ['offer', 'close'] and peer.connectionState == 'closed'


@pytest.mark.asyncio
async def test_successful_bounded_outbound_rtp_diagnostics_are_json_facts(monkeypatch):
    spec = importlib.util.spec_from_file_location('epoch_warm_stats_probe', Path(__file__).parent/'linux/native_latency_probe.py')
    probe = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = probe
    spec.loader.exec_module(probe)
    monkeypatch.setattr(probe, 'call_rpc', AsyncMock(return_value={'speech_sent_frames': 1920}))
    item = SimpleNamespace(type='outbound-rtp', id='exact-rtp', packetsSent=12, bytesSent=1440)
    peer = SimpleNamespace(connectionState='connected', getStats=AsyncMock(return_value={'exact-rtp': item}))
    tone = SimpleNamespace(samples=1920, utterance=1, audible=False, triggers=[{'utterance': 1}], readyState='live')
    context = SimpleNamespace(target='target', broker=SimpleNamespace(_worker_socket=lambda state: '/private/exact.sock'),
        pcm_guards={'target': SimpleNamespace(evidence=lambda: {'failed_block': None})})
    row = {}
    await probe.retain_warm_transition(context, object(), peer, tone, row, {'state': 'stop', 'item_id': 3})
    assert row['post_silence_outbound_rtp_stats'] == [{'type': 'outbound-rtp', 'id': 'exact-rtp', 'packetsSent': 12, 'bytesSent': 1440}]
    assert row['post_silence_player']['state'] == 'stop'
    assert row['post_silence_sender']['samples'] == 1920
    assert row['post_silence_worker_health'] == {'speech_sent_frames': 1920}
    assert row['post_silence_observed_monotonic_ns'] <= row['post_silence_diagnostics_finished_monotonic_ns']
    json.dumps(row, allow_nan=False)

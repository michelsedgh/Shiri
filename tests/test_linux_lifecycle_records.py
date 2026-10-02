"""Portable read-only unit retirement assertions; no manager/device/process effects."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shiri.runtime import units
from shiri.runtime.system import RuntimeFailure
from test_daemon_privileges import actual, BOOT, INSTALLATION

spec = importlib.util.spec_from_file_location('portable_lifecycle_records', Path(__file__).with_name('test_linux_lifecycle.py'))
lifecycle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lifecycle
spec.loader.exec_module(lifecycle)
ROOM = 'b6786543-7eb2-443d-83b1-65b984123a76'


@pytest.fixture
def record(monkeypatch):
    monkeypatch.setattr(units, 'boot_id', lambda: BOOT)
    monkeypatch.setattr(lifecycle, 'boot_id', lambda: BOOT)
    service = units.UnitSpec(units.new_unit(INSTALLATION.replace('-', '')[:8], ROOM, 'shairport'), 'shairport',
        'shiri-receiver-7', 'shiri-receiver-7', ('/opt/shiri/sbin/shairport-sync',))
    entry = service.intent() | {'invocation_id': '11'*16, 'control_group': f'/system.slice/{service.name}', 'cgroup_inode': 12345}
    state = actual(service) | {'ActiveState': 'inactive', 'MainPID': 0, 'ControlPID': 0}
    manager = units.UnitManager(SimpleNamespace(), SimpleNamespace())
    manager.inspect = AsyncMock(return_value=state)
    manager.cgroup_empty = Mock(return_value=True)
    saved = {'installation_id': INSTALLATION, 'processes': {ROOM+':shairport': entry}}
    return saved, entry, state, manager


async def verify(record):
    saved, _entry, _state, manager = record
    return await lifecycle.assert_recorded_daemons_retired(saved, manager, installation_id=INSTALLATION, room_id=ROOM)


@pytest.mark.parametrize('state', [None, 'inactive', 'failed'])
async def test_unit_retirement_requires_separate_old_cgroup_proof(record, state):
    _saved, entry, original, manager = record
    manager.inspect.return_value = None if state is None else original | {'ActiveState': state}
    result = await verify(record)
    manager.inspect.assert_awaited_once_with(entry['unit'])
    manager.cgroup_empty.assert_called_once_with(entry)
    assert result[0]['old_cgroup_empty'] is True and result[0]['observed_state'] == state
    assert result[0]['cgroup_inode'] == 12345


@pytest.mark.parametrize('fault', ['record_kind', 'installation', 'room', 'key', 'argv', 'policy', 'boot',
    'invocation_missing', 'cgroup_missing', 'inode_missing', 'active', 'activating', 'main_pid', 'control_pid',
    'pid_bool', 'pid_absent', 'reused', 'foreign_user', 'foreign_unit', 'foreign_group', 'foreign_argv',
    'nonempty_cgroup', 'uncertain_cgroup', 'truthy_cgroup', 'inspect_error'])
async def test_unit_retirement_rejects_malformed_active_reused_foreign_and_uncertain_records(record, fault):
    saved, entry, state, manager = record
    if fault == 'record_kind':
        entry['kind'] = 'unknown-unit'
    elif fault == 'installation':
        saved['installation_id'] = '0'*8+INSTALLATION[8:]
    elif fault == 'room':
        entry['unit'] = units.new_unit(INSTALLATION.replace('-', '')[:8], 'sender', 'shairport')
        entry['description'] = 'Shiri owned daemon '+entry['unit']
        entry['control_group'] = '/system.slice/'+entry['unit']
    elif fault == 'key':
        saved['processes'] = {ROOM+':audio': entry}
    elif fault == 'argv':
        entry['argv'] = []
    elif fault == 'policy':
        entry['properties']['User'] = 'root'
    elif fault == 'boot':
        entry['boot_id'] = 'another-boot'
    elif fault in {'invocation_missing', 'cgroup_missing', 'inode_missing'}:
        entry[{'invocation_missing': 'invocation_id', 'cgroup_missing': 'control_group', 'inode_missing': 'cgroup_inode'}[fault]] = None
    elif fault in {'active', 'activating'}:
        state['ActiveState'] = fault
    elif fault in {'main_pid', 'control_pid'}:
        state['MainPID' if fault == 'main_pid' else 'ControlPID'] = 1234
    elif fault == 'pid_bool':
        state['MainPID'] = False
    elif fault == 'pid_absent':
        state.pop('ControlPID')
    elif fault == 'reused':
        state['InvocationID'] = list(bytes.fromhex('22'*16))
    elif fault == 'foreign_user':
        state['User'] = 'root'
    elif fault == 'foreign_unit':
        state['Id'] = units.new_unit(INSTALLATION.replace('-', '')[:8], 'sender', 'shairport')
    elif fault == 'foreign_group':
        state['ControlGroup'] = '/system.slice/foreign.service'
    elif fault == 'foreign_argv':
        state['ExecStart'][0][1] = ['/foreign/daemon']
    elif fault == 'nonempty_cgroup':
        manager.cgroup_empty.return_value = False
    elif fault == 'truthy_cgroup':
        manager.cgroup_empty.return_value = 1
    elif fault == 'uncertain_cgroup':
        manager.cgroup_empty.side_effect = RuntimeFailure('Cannot prove exact old cgroup')
    else:
        manager.inspect.side_effect = RuntimeFailure('Read-only manager uncertainty')
    with pytest.raises((AssertionError, RuntimeFailure)):
        await verify(record)


@pytest.mark.parametrize('birth', [None, 'reused-other-birth', 'old-birth'])
async def test_direct_process_keeps_exact_original_pid_birth_assertion(monkeypatch, birth):
    monkeypatch.setattr(lifecycle, 'process_birth', lambda pid: birth if pid == 4242 else None)
    saved = {'installation_id': INSTALLATION, 'processes': {'legacy': {'pid': 4242, 'birth': 'old-birth'}}}
    if birth == 'old-birth':
        with pytest.raises(AssertionError, match='survived recovery'):
            await lifecycle.assert_recorded_daemons_retired(saved, None, installation_id=INSTALLATION, room_id=ROOM)
    else:
        result = await lifecycle.assert_recorded_daemons_retired(saved, None, installation_id=INSTALLATION, room_id=ROOM)
        assert result == [{'kind': 'process', 'pid': 4242, 'birth': 'old-birth', 'retired': True}]

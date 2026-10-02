"""Execute the manual harness's real cleanup body without resource startup."""
import ast
import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
import json
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.runtime.system import RuntimeFailure, atomic_json

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['matching', 'fresh-before', 'fresh-during', 'metadata-changed',
                                      'stop-error', 'stop-cancel', 'still-alive'])
async def test_exact_producer_cleanup_cannot_forget_reconnected_receiver(scenario):
    tree = ast.parse((ROOT/'tests/linux/check_native_grouping.py').read_text())
    run = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run_check')
    guarded = next(node for node in run.body if isinstance(node, ast.Try))
    loop = next(node for node in guarded.finalbody if isinstance(node, ast.For)
                and isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Attribute)
                and isinstance(node.iter.func.value, ast.Name) and node.iter.func.value.id == 'producers')
    fragment = ast.AsyncFunctionDef(name='cleanup', args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[loop], decorator_list=[])
    code = ast.fix_missing_locations(ast.Module(body=[fragment], type_ignores=[]))
    key = 'receiver:canonical-room'
    old_identity = {'kind': 'systemd-unit', 'unit': 'exact-old.service', 'invocation_id': 'old',
                    'cgroup_inode': 11, 'log_path': 'exact-old-private-log'}
    fresh_identity = {**old_identity, 'invocation_id': 'fresh', 'cgroup_inode': 12}
    fresh_unit = SimpleNamespace(alive=True)
    manifest = {'processes': {key: deepcopy(old_identity)}}
    state = SimpleNamespace(processes={})
    forgotten, errors = [], []

    class OldUnit:
        alive = True

        def identity(self):
            return old_identity

        async def stop(self):
            if scenario in {'fresh-during', 'stop-error', 'stop-cancel'}:
                manifest['processes'][key] = deepcopy(fresh_identity)
                state.processes['shairport'] = fresh_unit
            if scenario == 'stop-error':
                raise OSError('Exact old stop failed')
            if scenario == 'stop-cancel':
                raise asyncio.CancelledError
            self.alive = scenario == 'still-alive'

    old_unit = OldUnit()
    state.processes['shairport'] = old_unit
    if scenario == 'fresh-before':
        manifest['processes'][key] = deepcopy(fresh_identity)
        state.processes['shairport'] = fresh_unit
    elif scenario == 'metadata-changed':
        # Full exact equality is required, even where primary process fields
        # match. A partial/normalized identity must not erase this reservation.
        manifest['processes'][key]['log_path'] = 'changed-owned-log'
    preserved = deepcopy(manifest['processes'][key])

    def forget_process(value):
        forgotten.append(value)
        manifest['processes'].pop(value)

    def require(value, message):
        if not value:
            raise RuntimeFailure(message)

    report = {'cleanup': {}}
    namespace = {'asyncio': asyncio, 'deepcopy': deepcopy, 'require': require,
                 'epoch_context': None,
                 'producers': {'room': {'unit': old_unit, 'key': key}},
                 'room_states': {'room': state}, 'report': report, 'errors': errors,
                 'broker': SimpleNamespace(network=SimpleNamespace(manifest=manifest, forget_process=forget_process))}
    exec(compile(code, str(ROOT/'tests/linux/check_native_grouping.py'), 'exec'), namespace)
    if scenario == 'stop-cancel':
        with pytest.raises(asyncio.CancelledError):
            await namespace['cleanup']()
    else:
        await namespace['cleanup']()
    if scenario == 'matching':
        assert forgotten == [key] and manifest['processes'] == {} and state.processes == {}
    else:
        assert forgotten == []
        assert manifest['processes'][key] == (fresh_identity if scenario in {
            'fresh-before', 'fresh-during', 'stop-error', 'stop-cancel'} else preserved)
        if scenario in {'fresh-before', 'fresh-during', 'stop-error', 'stop-cancel'}:
            assert state.processes['shairport'] is fresh_unit and fresh_unit.alive
    assert errors == ([f'producer cleanup: {"OSError" if scenario == "stop-error" else "RuntimeFailure"}']
                      if scenario in {'stop-error', 'still-alive'} else [])
    assert bool(report['cleanup']) == (scenario not in {'stop-error', 'stop-cancel', 'still-alive'})


@pytest.mark.asyncio
@pytest.mark.parametrize('inherited_namespace', [False, True])
async def test_api_close_failure_cannot_skip_broker_lan_or_final_ownership_checks(tmp_path, inherited_namespace):
    tree = ast.parse((ROOT / "tests/linux/check_native_grouping.py").read_text())
    run = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_check")
    guarded = next(node for node in run.body if isinstance(node, ast.Try))
    # Compile the exact maintained finally statements, avoiding every startup
    # path. Moving API close back outside its guard makes this test raise before
    # either owned resource or final manifest verification can run.
    setup = ast.parse('''
done = asyncio.Event()
monitor = fault_observer = identity = tone = peer = api_process = None
epoch_context = None
result_path = RESULT
producers, captures, room_states = {}, {}, {}
token = "private-token"
baseline, legacy = "host-baseline", "legacy-baseline"
complete, errors = True, []
''').body
    fragment = ast.AsyncFunctionDef(name="cleanup", args=ast.arguments(
        posonlyargs=[], args=[ast.arg(arg=name) for name in ["report", "api", "broker", "lan", "original_netns_fd"]],
        kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=setup + guarded.finalbody + [ast.Return(ast.Name(id="report", ctx=ast.Load()))],
        decorator_list=[])
    code = ast.fix_missing_locations(ast.Module(body=[fragment], type_ignores=[]))
    operations = []

    class Api:
        async def close(self):
            operations.append("api")
            raise OSError("client close failed private-token")

    class Broker:
        sessions = {}

        async def close(self):
            operations.append("broker")

    class Lan:
        async def close(self):
            operations.append("lan")

    async def host_snapshot():
        operations.append("host-snapshot")
        return "host-baseline"

    def require(value, message):
        if not value:
            raise RuntimeFailure(message)

    def legacy_snapshot():
        operations.append("legacy-snapshot")
        return "legacy-baseline"

    (tmp_path / "ownership.json").write_text(json.dumps({
        "installation_id": "exact-installation", "networks": {}, "processes": {}}))
    namespace = {"asyncio": asyncio, "os": os, "suppress": suppress, "datetime": datetime, "timezone": timezone,
                 "json": json, "uuid4": uuid4, "require": require, "atomic_json": atomic_json,
                 "STATE": tmp_path, "RESULT": tmp_path / "result.json", "legacy_snapshot": legacy_snapshot,
                 "observation": SimpleNamespace(redact_exception=lambda exc, token: str(exc).replace(token, "<redacted>"),
                     base=SimpleNamespace(closed_slot=lambda: operations.append("slot-check"), host_snapshot=host_snapshot))}
    exec(compile(code, str(ROOT / "tests/linux/check_native_grouping.py"), "exec"), namespace)
    descriptor = None
    if inherited_namespace:
        path = tmp_path/'inherited-namespace-fd'
        path.write_text('Only FD lifetime is exercised; no namespace admission occurs')
        descriptor = os.open(path, os.O_RDONLY)
    result = await namespace["cleanup"]({"cleanup": {}, "installation_id": "exact-installation"},
                                         Api(), Broker(), Lan(), descriptor)
    if descriptor is not None:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert operations == ["api", "broker", "lan", "slot-check", "host-snapshot", "legacy-snapshot"]
    assert result["cleanup"] == {"broker_closed": True, "speech_ownership_released": True,
                                  "isolated_lan_closed": True, "empty_manifest": True,
                                  "slot7_closed": True, "legacy_and_host_preserved": True}
    assert result["cleanup_errors"] == ["API client cleanup: client close failed <redacted>"]
    assert result["passed"] is False
    assert json.loads((tmp_path / "result.json").read_text())["passed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('speech_stress,name', [(False, 'baseline.json'),
    (True, 'shiri-v2-native-speech-stress-result.json')])
async def test_actual_complete_early_failure_reports_original_error_and_does_not_acquire_resources(
        tmp_path, monkeypatch, speech_stress, name):
    pytest.importorskip('numpy')
    pytest.importorskip('aiortc')
    source = ROOT/'tests/linux/check_native_grouping.py'
    spec = importlib.util.spec_from_file_location('group_actual_early_cleanup', source)
    group = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(group)
    monkeypatch.setattr(group, 'os', SimpleNamespace(geteuid=lambda: 501))
    monkeypatch.setattr(group, 'RESULT', tmp_path/'baseline.json')
    # Run the maintained whole function, including its pre-try locals. Unlike
    # the finally-only fault fixture above, this would expose a genuinely late
    # result_path/default initialization while acquiring no resource at all.
    assert await group.run_check(speech_stress=speech_stress) == 1
    result = json.loads((tmp_path/name).read_text())
    assert result['passed'] is False and result['cleanup'] == {} and result['cleanup_errors'] == []
    assert result['failure'] == {'type': 'RuntimeFailure', 'message': 'Run explicitly as Linux root', 'causes': []}
    assert sorted(path.name for path in tmp_path.iterdir()) == [name]

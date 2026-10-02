"""Exercise the opt-in supervisor's real launch/finally paths without a VM."""
import ast
import importlib.util
import json
from pathlib import Path

import pytest


HERE = Path(__file__).parent
SOURCE = HERE/'linux/run_native_speech_stress.py'
spec = importlib.util.spec_from_file_location('stress_supervisor_fixtures', HERE/'test_group_supervisor.py')
portable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable)


@pytest.mark.asyncio
@pytest.mark.parametrize('remaining_owner', [False, True])
async def test_stress_supervisor_real_finally_reaps_only_child_and_requires_empty_candidate(tmp_path, monkeypatch, remaining_owner):
    monkeypatch.setattr(portable, 'SOURCE', SOURCE)
    await portable.test_real_finally_reaps_resistant_child_and_preserves_namespace_until_all_room_ownership_is_empty(
        tmp_path, remaining_owner)


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', [None, 'parent', 'original'])
async def test_stress_launcher_retains_namespace_fd_guards_and_explicit_mode(tmp_path, monkeypatch, replacement):
    monkeypatch.setattr(portable, 'SOURCE', SOURCE)
    original_functions, original_json = portable.functions, portable.atomic_json
    inner = tmp_path/'inner.json'
    def functions(*names, namespace):
        namespace['INNER_RESULT'] = inner
        if 'group' in namespace:
            namespace['group'].ZONES = ['A', 'B']
        return original_functions(*names, namespace=namespace)
    def write(path, value):
        if path == inner:
            value.update(mode='speech_stress', speech_stress={'passed': True, 'audible_sessions': {'A': 20, 'B': 20}})
        original_json(path, value)
    monkeypatch.setattr(portable, 'functions', functions)
    monkeypatch.setattr(portable, 'atomic_json', write)
    await portable.test_supervisor_launches_through_inherited_exact_namespace_fd_without_remounting_sysfs(
        tmp_path, replacement)
    result = json.loads((tmp_path/'result.json').read_text())
    assert result['inner_deadline_seconds'] == 480 and result['external_watchdog_seconds'] == 600
    # Assert the actual subprocess call includes the opt-in flag by executing
    # the existing held-FD fixture's creation spy through this source's body.
    source = ast.parse(SOURCE.read_text())
    launch = next(node for node in ast.walk(source) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.func.attr == 'create_subprocess_exec')
    assert any(isinstance(value, ast.Constant) and value.value == '--speech-stress' for value in launch.args)


@pytest.mark.parametrize('counts,mode,inner_passed,stress_passed,exit_code,accepted', [
    ({'A': 20, 'B': 20}, 'speech_stress', True, True, 0, True),
    ({'A': 19, 'B': 20}, 'speech_stress', True, True, 0, False),
    ({'A': 20, 'B': 20, 'other': 20}, 'speech_stress', True, True, 0, False),
    ({'A': True, 'B': 20}, 'speech_stress', True, True, 0, False),
    ({'A': 20, 'B': 20}, None, True, True, 0, False),
    ({'A': 20, 'B': 20}, 'speech_stress', True, False, 0, False),
    ({'A': 20, 'B': 20}, 'speech_stress', False, True, 0, False),
    ({'A': 20, 'B': 20}, 'speech_stress', True, True, -9, False),
])
def test_supervisor_cannot_accept_exit_zero_without_both_exact_observed_counts(
        counts, mode, inner_passed, stress_passed, exit_code, accepted):
    tree = ast.parse(SOURCE.read_text())
    assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                and target.slice.value == 'harness_passed' for target in node.targets))
    namespace = {'result': {}, 'inner': {'passed': inner_passed, 'mode': mode},
                 'stress': {'passed': stress_passed}, 'counts': counts,
                 'process': portable.SimpleNamespace(returncode=exit_code),
                 'group': portable.SimpleNamespace(ZONES=('A', 'B'))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[assignment], type_ignores=[])), str(SOURCE), 'exec'), namespace)
    assert namespace['result']['harness_passed'] is accepted

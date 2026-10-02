"""Cause evidence exercises the maintained harness without audio or hardware."""
import ast
import asyncio
import errno
from pathlib import Path
from types import SimpleNamespace


def causes(exception):
    source = Path(__file__).parent/'linux/check_native_grouping.py'
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'failure_cause')
    namespace = {'asyncio': asyncio}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(source), 'exec'), namespace)
    return namespace['failure_cause'](exception)


def test_partial_rpc_read_reports_lengths_without_payload_or_exception_rendering():
    private = b'v=0\nPRIVATE SDP AND PCM'
    short_read = asyncio.IncompleteReadError(private, 128)
    error = RuntimeError('RPC disconnected')
    error.__cause__ = short_read
    assert causes(error) == [{'type': 'IncompleteReadError', 'timeout': False,
                              'expected': 128, 'received': len(private)}]
    assert private.decode() not in repr(causes(error))


def test_oserror_and_timeout_chain_reports_safe_typed_transport_facts_only():
    timeout = TimeoutError(errno.ETIMEDOUT, 'PRIVATE token/SDP/path')
    refused = ConnectionRefusedError(errno.ECONNREFUSED, 'PRIVATE /run/exact/socket')
    refused.__cause__ = timeout
    error = RuntimeError('RPC failed')
    error.__cause__ = refused
    assert causes(error) == [
        {'type': 'ConnectionRefusedError', 'timeout': False, 'errno': errno.ECONNREFUSED},
        {'type': 'TimeoutError', 'timeout': True, 'errno': errno.ETIMEDOUT}]


def test_suppressed_context_and_explicit_cause_follow_python_exception_ownership():
    error = RuntimeError('safe')
    error.__context__ = FileNotFoundError(errno.ENOENT, 'PRIVATE path')
    assert causes(error) == [{'type': 'FileNotFoundError', 'timeout': False, 'errno': errno.ENOENT}]
    error.__suppress_context__ = True
    assert causes(error) == []
    error.__cause__ = TimeoutError('PRIVATE')
    assert causes(error) == [{'type': 'TimeoutError', 'timeout': True}]


def test_cause_walk_is_bounded_cycle_safe_and_never_calls_string_conversion():
    class Unrenderable(Exception):
        def __str__(self):
            raise AssertionError('Cause rendering can reveal a secret')
    errors = [Unrenderable() for _ in range(6)]
    for first, following in zip(errors, errors[1:], strict=False):
        first.__cause__ = following
    assert causes(errors[0]) == [{'type': 'Unrenderable', 'timeout': False}]*3
    errors[1].__cause__ = errors[0]
    assert causes(errors[0]) == [{'type': 'Unrenderable', 'timeout': False}]


def test_unbounded_errno_and_unknown_expected_length_are_not_serialized_as_counts():
    error = RuntimeError('safe')
    error.__cause__ = OSError(1 << 1024, 'PRIVATE')
    assert causes(error) == [{'type': 'OSError', 'timeout': False}]
    error.__cause__ = asyncio.IncompleteReadError(b'PRIVATE', None)
    assert causes(error) == [{'type': 'IncompleteReadError', 'timeout': False, 'received': 7}]


def test_real_harness_failure_handler_retains_cause_facts_before_finally_cleanup():
    source = Path(__file__).parent/'linux/check_native_grouping.py'
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run_check')
    guarded = next(node for node in function.body if isinstance(node, ast.Try))
    # Execute the real failure handler around a controlled transport exception;
    # no startup, audio or kernel path is in this compiled fragment.
    fragment = ast.Try(body=[ast.Raise(exc=ast.Name(id='fault', ctx=ast.Load()), cause=None)],
                       handlers=guarded.handlers, orelse=[], finalbody=[])
    fault = RuntimeError('safe wrapper')
    fault.__cause__ = asyncio.IncompleteReadError(b'PRIVATE SDP/PCM', 256)
    namespace = {'fault': fault, 'report': {}, 'token': None, 'broker': None,
                 'failure_cause': causes, 'observation': SimpleNamespace(redact_exception=lambda *_args: 'redacted')}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fragment], type_ignores=[])), str(source), 'exec'), namespace)
    assert namespace['report']['failure'] == {'type': 'RuntimeError', 'message': 'redacted',
        'causes': [{'type': 'IncompleteReadError', 'timeout': False, 'expected': 256, 'received': 15}]}

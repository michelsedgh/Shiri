"""Timed3 fixture clocks and owned absolute-deadline PCM sends; no actors."""
import ast
import asyncio
import hashlib
from copy import deepcopy
from pathlib import Path
from types import FunctionType, SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from shiri.runtime.system import RuntimeFailure
from shiri.runtime.timing import Clock, Kind, Packet, RATE, map_native_time

HERE = Path(__file__).parent / 'linux'
# Exact frozen GC361 AST preimages, portable to the admitted Linux source tree.
FUNCTION_PREIMAGES = {'check_native_grouping.py': {'coherent_clock_sample': '340c07da52d97a32cc0fbdd869e4b6a312617882d186b19b1dfb95c3cfa986cb', 'program_pcm': '6dd33a222449a257290fe656965810bff5609873494233d4d1b18c0de5bfd48f', 'envelope': 'a8391715f68fcd561106d3525801211b337fd730bcfa3c18638538f4bb8a7b13', 'coded_880_bound': '3224d60abf554d45644d5362311fd578e904c4b386d5eae791c34c43ad257d89'}, 'native_music_minimum.py': {'require': '3c66172bc2ddef9ec4825c7ab02230d31c6c9ebb59572ad8c8b1d4d6fc8bb4a1', 'load_epoch': 'd947db0106c0b0c6caeeb00dec978e4769fd6c5adf8ece841bce48960b52d7fc', 'bind': '14342336b3ab730ead5b97741fdeca45a3ecc5b003e278cbc5a12581b1fd43cd', 'candidate_plan': 'd78a96cd648ba3fca3ea0378733d004654417bebf5b84b60773fc795fd286250', 'broker_class': 'fb1cc07f725dccf7dc9325ec245ae277fefab07922fd792b7d62a50efd2c9158', 'require_idle_worker': '98cbee94a06e2f93c6362c6bab0b104f3f64da97dca964e0859d8c28ae9e53c0', 'admit_launch': 'e0bba1b08bf6b103bf5ff2ab11dd44bf1a2412f1895ffaf4daa4fbbb484b34a4', 'plan_receipt': '554dd6263beb454fb37cbb276bc438da69cca6c12f54c8985fe3b087a0748ca7', 'prepare': '9586d65718cbb46f26f2e2c823703afd57a886aac8307d0b12ab355db3e8073c', 'calendar': '01e9c627ab531667afcf6b13e38a87f1487119f37a15910e167553d037cf5c1b', 'complete_music_reference': '3cd95bbbfa2e051f5ca4f2758df8cfe6be30827387732beefe7fd9f66a7ee665', 'verify_complete_music': '677dd84abfabe5bbe7c5d62c4d022ad5000cfecb833c91e923c7f3b91356602d', 'freeze_complete_music': '4d534328a7b1721096fb590990ebc293edef61c84652c99dc8072b7273b378f3', 'check_original_actors': '237445e22169d9cee67ff0bfb6d89d806f17a173570603e35200bd3b7378fe32', 'exercise': '1acca4aaf1e2d93fd6e2e7294e89da659f83eef80476a8e2ea1cf5e5ff1d1152'}, 'native_music_soak.py': {'load': '62d4174c34a56121efe68fa4e047de471d783d49759fba723512586e49dc7047', 'require': '3c66172bc2ddef9ec4825c7ab02230d31c6c9ebb59572ad8c8b1d4d6fc8bb4a1', 'configure': '793ecd430803b8bf914d8178af4d110d4152b576dcb2c34d886d1ca7605fb16a', 'policy': '4ae00d702b32415c8f455305b597992a2ccae40fa5de7c2495414ba556cc9143', 'producer_profile': '0f0573bc7d3636848dfd699c3e4b1848f974ed120c18937b76f638efb7618a7a', 'candidate_plan': '56c0bd0dfcefde3e3faae9e47480b164cac9af072001ad8e0642bf96a3d7346f', 'room_buffer_ms': '8ea511c4fbc8f0cf55eb5b4edfe36b4b4f47f48f81d786d8603f117643b835c9', 'broker_class': '2044f54827f6db83b3e206796822354023129ca086456557c2bb55e1323925b6', 'require_idle_worker': '14c1dbb5b63ffa3a77440d555c43b35873b0b34a4bc46350fd8139cd6edffb5e', 'admit_launch': 'bb8768fe252ff451997aca0e5b2b6d16665525881903d3f649e8f1d5c5a258bf', 'plan_receipt': '017b9a46b070ae74f3411a8af8f98b56fae07097d159b62ca2127ec884b08cb8', 'envelope': '1c85c8165fd94664dffb6ac3301a239d6682d749e8f07c34a9af576a7127ac97', 'program_pcm': '553fe898d1c4403dad7f82af91ac043b522a1e13e40871c3309f52d439fd51ea', 'guard_type': '10cc9396fe2bb98bef23364460e76c9751f2cb51a0fbccadf83320ebcc44f7ea', 'join_owned': '2fe770f72bfc80a59d77e2671e061ba99fda8f794020f8a50fbc0fef1dc34004', 'analysis': '71399181489eb1085c8c9826ae417829f21d7c085485613363ec675fd6cdc020', 'prepare': '6cfeb052fc8ccac83a749764788db57b0cd6a8803a6ac1d3e85065adabf4e29d', 'exercise': '54443647a052f7357ce07a650c58852bff80468606e43a2d4b1f1c04a70d74b2', 'require_elapsed': '322f2b40590823f83dc34afca8bc801448ba32095433785c7113fc1c933127ab', 'record_window': '30a9610e21e6af748b4d67ab88e55f5d2d29ab329b32f54d236720e7952ab8aa', 'finalize_capture': '0437e7426486cfb2c2eed49f881895bbb0abe277b969e80c2b51d0ae1fc5da18', 'retain_failed_capture': '38d2769f658250d06fbc2289887cc1379bbe71fc0403d7d435ebd41df0384794'}}
PRODUCER_PREIMAGES = {'check_native_grouping.py': {'sha256': '8c765755fee672dc493e3aa5f6d6e9f8dd5c75ffaaff5d5e8ecea058774e62ea', 'send': 'await asyncio.wait_for(loop.sock_sendall(connection, packet.encode()), 0.3)', 'final': "for item in connections:\n    item.close()\nstate['finished'] = True\natomic_json(path, state)"}, 'native_music_minimum.py': {'sha256': '618d3ec98f1b8142a95300f77248f0abbc0b1adcdeee8b77264dbaa98ea91af9', 'send': 'await asyncio.wait_for(loop.sock_sendall(connection, packet.encode()), 0.3)', 'final': "if connection is not None:\n    connection.close()\nstate['finished'] = True\natomic_json(path, state)"}, 'native_music_soak.py': {'sha256': '325f7b7df8291dcfb6cfe576017a4127d3514332ce86646c6a355c3aa13a7a6a', 'send': 'await bounded(loop.sock_sendall(connection, packet.encode()), 0.3)', 'final': "if connection is not None:\n    connection.close()\nstate['finished'] = True\natomic_json(path, state)"}}
NAMES = {'coherent_clock_sample', 'ClockSamplingTimeout', 'RecoveryClock', 'recovered_clock_sample',
         'check_clock_recovery_deadline', 'send_native_packet', 'retire_producer', 'bracketed_packet',
         'envelope', 'program_pcm'}


class ScriptClock:
    CLOCK_MONOTONIC_RAW = 4
    def __init__(self, monotonic, raw=()):
        self.monotonic = iter(monotonic)
        self.raw = iter(raw)
        self.mono_reads = self.raw_reads = 0
    def monotonic_ns(self):
        self.mono_reads += 1
        return next(self.monotonic)
    def clock_gettime_ns(self, clock):
        assert clock == self.CLOCK_MONOTONIC_RAW
        self.raw_reads += 1
        return next(self.raw)


def module(clock):
    tree = ast.parse((HERE / 'check_native_grouping.py').read_text())
    chosen = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
              and node.name in NAMES]
    def require(value, message):
        if not value:
            raise RuntimeFailure(message)
    namespace = {'time': clock, 'require': require, 'RuntimeFailure': RuntimeFailure,
        'FunctionType': FunctionType, 'asyncio': asyncio, 'np': np, 'Clock': Clock, 'Kind': Kind,
        'Packet': Packet, 'RATE': RATE, 'CODE_SECONDS': 20, 'CODE_CHIP_FRAMES': RATE * 120 // 1000,
        'CLOCK_SAMPLE_ATTEMPTS': 4, 'CLOCK_SAMPLE_BUDGET_NS': 5_000_000,
        'CLOCK_BRACKET_LIMIT_NS': 1_000_000, 'CLOCK_RECOVERY_BATCHES': 4,
        'CLOCK_RECOVERY_BUDGET_NS': 20_000_000, 'SEND_LATE_NS': 150_000_000}
    exec(compile(ast.Module(body=chosen, type_ignores=[]), '<actual fixture recovery functions>', 'exec'), namespace)
    return SimpleNamespace(**namespace)


def recovering_clock(raw_second=706_206_050):
    return ScriptClock([1_000_000_000, 1_000_000_001, 1_000_000_002, 1_006_206_002,
        1_006_206_003, 1_006_206_004, 1_006_206_005, 1_006_206_105, 1_006_206_106],
        [700_000_000, raw_second])


def grant():
    return Packet(Kind.GRANT, uuid4().bytes, incarnation=uuid4().bytes, group=uuid4().bytes,
                  generation=7, epoch=3, flags=1)


def test_actual_recovered_packet_retains_calendar_identity_pcm_and_no_terminal_failure():
    clock = recovering_clock()
    tested = module(clock)
    owner = grant()
    original = owner.encode()
    state = {'frames': 1920}
    packet = tested.bracketed_packet(owner, owner.group, 1920, 21, 1_200_000_000, stats=state)
    decoded = Packet.decode(packet.encode())
    assert map_native_time(decoded, now_ns=decoded.monotonic_after_ns).monotonic_ns == 1_240_000_000
    assert (decoded.frame_index, decoded.sequence, decoded.frames) == (1920, 21, 960)
    assert decoded.pcm == tested.program_pcm(1920)
    assert (decoded.session, decoded.incarnation, decoded.group, decoded.generation, decoded.epoch, decoded.flags) == (
        owner.session, owner.incarnation, owner.group, owner.generation, owner.epoch, owner.flags)
    assert owner.encode() == original and state['frames'] == 1920
    assert state['clock_recovery']['passed'] is True and state['clock_recovery']['batches'] == 2
    assert state['clock_recovery']['refused'] == 1 and state['clock_sample_attempts'] == 2
    assert state['last_clock_recovery_rejection']['elapsed_ns'] == 6_206_000
    assert 'clock_sample_failure' not in state and 'clock_recovery_failure' not in state


@pytest.mark.parametrize('monotonic,raw', [
    ([1_000_000_000, 1_020_000_000], []),
    ([1_000_000_000, 1_000_000_001, 1_000_000_002, 1_020_000_002, 1_020_000_003], [700_000_000]),
])
def test_exact20ms_expiry_never_constructs_or_advances_packet(monotonic, raw):
    tested = module(ScriptClock(monotonic, raw))
    owner = grant()
    constructed, generated = [], []
    tested.bracketed_packet.__globals__.update(Packet=lambda *_a, **_k: constructed.append(True),
        program_pcm=lambda *_a, **_k: generated.append(True))
    state = {'frames': 1920}
    with pytest.raises(tested.ClockSamplingTimeout, match='20ms'):
        tested.bracketed_packet(owner, owner.group, 1920, 21, 1_200_000_000, stats=state)
    assert not constructed and not generated and state['frames'] == 1920


def test_four_refused_batches_are_finite_with_original16_attempts():
    mono, raw, now = [1_000_000_000], [], 1_000_000_000
    for _batch in range(4):
        now += 1
        mono.append(now)
        for _attempt in range(4):
            now += 1
            mono.append(now)
            raw.append(700_000_000 + now - 1_000_000_000)
            now += 1_100_000
            mono.append(now)
        now += 1
        mono.append(now)
    tested = module(ScriptClock(mono, raw))
    state = {'frames': 960}
    with pytest.raises(tested.ClockSamplingTimeout, match='4 fresh batches'):
        tested.recovered_clock_sample(stats=state)
    assert state['clock_sample_attempts'] == 16 and state['clock_sample_retries'] == 12
    assert state['clock_recovery']['batches'] == state['clock_recovery']['refused'] == 4
    assert state['clock_recovery']['elapsed_ns'] < 20_000_000 and state['frames'] == 960


@pytest.mark.parametrize('kind', ['raw', 'monotonic'])
def test_cross_batch_invalid_clocks_fail_without_further_retry(kind):
    clock = recovering_clock(raw_second=699_999_999)
    if kind == 'monotonic':
        clock = ScriptClock([1_000_000_000, 1_000_000_001, 999_999_999], [])
    tested = module(clock)
    state = {}
    with pytest.raises(RuntimeFailure, match='invalid or backwards') as captured:
        tested.recovered_clock_sample(stats=state)
    assert not isinstance(captured.value, tested.ClockSamplingTimeout)
    assert clock.raw_reads == (2 if kind == 'raw' else 0)


def canonical_ast_fingerprint(node):
    """Keep original3.12 hashes when3.10 omits the empty PEP695 field."""
    canonical = deepcopy(node)
    for item in ast.walk(canonical):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parameters = getattr(item, 'type_params', [])
            assert type(parameters) is list and not parameters, 'Unexpected nonempty AST type parameters'
            if 'type_params' not in item._fields:
                item._fields = (*item._fields, 'type_params')
                item.type_params = []
    return hashlib.sha256(ast.dump(canonical).encode()).hexdigest()


def pre312_ast_shape(node):
    """Emulate only the absent3.10 field, without changing any body node."""
    older = deepcopy(node)
    for item in ast.walk(older):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assert not getattr(item, 'type_params', [])
            item._fields = tuple(name for name in item._fields if name != 'type_params')
            if hasattr(item, 'type_params'):
                del item.type_params
    return older


def test_original_inner_sampler_and_waveforms_unchanged():
    for filename, expected in FUNCTION_PREIMAGES.items():
        tree = ast.parse((HERE / filename).read_text())
        functions = {node.name: node for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name, digest in expected.items():
            assert canonical_ast_fingerprint(functions[name]) == digest
            assert canonical_ast_fingerprint(pre312_ast_shape(functions[name])) == digest


SOAK_OBSERVATIONS = {ast.unparse(ast.parse(expression).body[0]) for expression in (
    'observe_delivery(state, "command_read", frame, available + frame * 1000000000 // RATE)',
    'observe_delivery(state, "preclock", frame, target, lateness_ns=lateness)',
    'observe_delivery(state, "clock_sample", frame, target)',
    'observe_delivery(state, "native_send", frame, target)',
    'observe_delivery(state, "progress_publish", packet.frame_index, target)',
)}


def restore_original_producer(changed, filename):
    preimage = PRODUCER_PREIMAGES[filename]
    new = deepcopy(next(node for node in changed.body
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == 'producer'))
    if filename == 'check_native_grouping.py':
        end = [node for node in ast.walk(new) if isinstance(node, ast.If)
               and ast.unparse(node.test) == "action['action'] == 'end'"]
        assert len(end) == 1
        hold = end[0].body[-2]
        assert ast.unparse(hold) == 'await hold_bluetooth_receiver_after_end(config, connection, path, state, stop)'
        assert isinstance(end[0].body[-1], ast.Break)
        assert ast.unparse(end[0].body[-3]) == "state['commands'].append({'generation': seen, 'action': 'end'})"
        assert sum(isinstance(node, ast.Name) and node.id == 'hold_bluetooth_receiver_after_end'
                   for node in ast.walk(new)) == 1
        end[0].body.pop(-2)
    class UndoOwnedSeams(ast.NodeTransformer):
        def __init__(self, expected):
            self.expected, self.observations, self.progress = expected, set(), 0
        def visit_Expr(self, node):
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                if node.value.func.id == 'observe_delivery':
                    expression = ast.unparse(node)
                    assert filename == 'native_music_soak.py' and expression in SOAK_OBSERVATIONS
                    assert expression not in self.observations
                    self.observations.add(expression)
                    return None
                if node.value.func.id == 'progress_json':
                    assert filename == 'native_music_soak.py' and ast.unparse(node) == 'progress_json(path, state)'
                    self.progress += 1
                    return ast.parse('atomic_json(path, state)').body[0]
            return self.generic_visit(node)
        def visit_Assign(self, node):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == 'primary':
                return None
            if 'send_native_packet' in ast.unparse(node):
                assert 'target' in ast.unparse(node) and 'packet.encode()' in ast.unparse(node)
                return ast.parse(self.expected['send']).body[0]
            return self.generic_visit(node)
        def visit_Try(self, node):
            node = self.generic_visit(node)
            if node.finalbody and any('retire_producer' in ast.unparse(item) for item in node.finalbody):
                node.finalbody = ast.parse(self.expected['final']).body
            return node
    inverse = UndoOwnedSeams(preimage)
    restored = inverse.visit(new)
    assert inverse.observations == (SOAK_OBSERVATIONS if filename == 'native_music_soak.py' else set())
    assert inverse.progress == (1 if filename == 'native_music_soak.py' else 0)
    return restored


def test_current_producers_share_absolute_deadline_and_original_calendars():
    for filename, preimage in PRODUCER_PREIMAGES.items():
        restored = restore_original_producer(ast.parse((HERE / filename).read_text()), filename)
        assert canonical_ast_fingerprint(restored) == preimage['sha256']
        assert canonical_ast_fingerprint(pre312_ast_shape(restored)) == preimage['sha256']


def test_soak_progress_inverse_rejects_changed_observation_calendar():
    changed = ast.parse((HERE / 'native_music_soak.py').read_text())
    call = next(node for node in ast.walk(changed) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == 'observe_delivery')
    call.args[2] = ast.BinOp(left=call.args[2], op=ast.Add(), right=ast.Constant(value=960))
    with pytest.raises(AssertionError):
        restore_original_producer(changed, 'native_music_soak.py')


def test_soak_progress_inverse_preserves_rejection_of_deadline_mutation():
    changed = ast.parse((HERE / 'native_music_soak.py').read_text())
    producer = next(node for node in changed.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'producer')
    comparison = next(node for node in ast.walk(producer) if isinstance(node, ast.Compare)
                      and ast.unparse(node) == 'lateness < SEND_LATE_NS')
    comparison.comparators[0] = ast.BinOp(left=comparison.comparators[0], op=ast.Add(), right=ast.Constant(value=1))
    restored = restore_original_producer(changed, 'native_music_soak.py')
    assert canonical_ast_fingerprint(restored) != PRODUCER_PREIMAGES['native_music_soak.py']['sha256']


@pytest.mark.parametrize('source', ['def preserved():\n    return 1\n',
                                   'async def preserved():\n    return 1\n',
                                   'class Preserved:\n    def guarded(self):\n        return 1\n'])
def test_canonical_ast_rejects_nonempty_type_parameters(source):
    original = ast.parse(source).body[0]
    assert canonical_ast_fingerprint(original) == canonical_ast_fingerprint(pre312_ast_shape(original))
    before = ast.dump(original)
    changed = deepcopy(original)
    changed.type_params = [ast.Name(id='ForeignParameter', ctx=ast.Load())]
    with pytest.raises(AssertionError, match='nonempty AST type parameters'):
        canonical_ast_fingerprint(changed)
    assert ast.dump(original) == before


def test_canonical_ast_rejects_original_sampler_body_mutation():
    original = next(node for node in ast.parse((HERE / 'check_native_grouping.py').read_text()).body
                    if isinstance(node, ast.FunctionDef) and node.name == 'coherent_clock_sample')
    expected = FUNCTION_PREIMAGES['check_native_grouping.py']['coherent_clock_sample']
    assert canonical_ast_fingerprint(original) == expected
    changed = deepcopy(original)
    changed.body.append(ast.Pass())
    assert canonical_ast_fingerprint(changed) != expected
    assert canonical_ast_fingerprint(pre312_ast_shape(changed)) != expected


class MutableClock:
    def __init__(self, now=1_000_000_000):
        self.now = now
    def monotonic_ns(self):
        return self.now


def admitted_stats(clock):
    return {'frames': 960, 'clock_recovery': {'passed': True, 'started_ns': clock.now,
            'checked_ns': clock.now, 'elapsed_ns': 0}}


@pytest.mark.asyncio
@pytest.mark.parametrize('lateness', [0, 149_999_999, 150_000_000, 150_000_001])
async def test_send_completion_retains_strict_original150ms(lateness):
    clock = MutableClock()
    tested, calls = module(clock), []
    async def send(_connection, payload):
        calls.append(payload)
        clock.now += lateness
    stats = admitted_stats(clock)
    operation = tested.send_native_packet(SimpleNamespace(sock_sendall=send), object(), b'original', clock.now, stats=stats)
    if lateness < 150_000_000:
        assert await operation == lateness
    else:
        with pytest.raises(RuntimeFailure, match='delivery cadence'):
            await operation
    assert calls == [b'original'] and stats['frames'] == 960


@pytest.mark.asyncio
@pytest.mark.parametrize('elapsed', [20_000_000, 150_000_000])
async def test_post_sampling_or_encoded_expiry_prevents_child_send(elapsed):
    clock = MutableClock()
    tested, calls = module(clock), []
    stats = admitted_stats(clock)
    target = clock.now
    clock.now += elapsed
    async def send(*_args):
        calls.append(True)
    with pytest.raises(RuntimeFailure):
        await tested.send_native_packet(SimpleNamespace(sock_sendall=send), object(), b'original', target, stats=stats)
    assert not calls and stats['frames'] == 960


@pytest.mark.asyncio
async def test_owned_child_rechecks_when_scheduled_after_deadline(monkeypatch):
    clock = MutableClock()
    tested, calls = module(clock), []
    stats, target = admitted_stats(clock), clock.now
    original_wait = asyncio.wait
    async def wait(*args, **kwargs):
        clock.now = target+150_000_000
        return await original_wait(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'wait', wait)
    async def send(*_args):
        calls.append(True)
    with pytest.raises(RuntimeFailure, match='delivery cadence'):
        await tested.send_native_packet(SimpleNamespace(sock_sendall=send), object(), b'original', target, stats=stats)
    assert not calls


@pytest.mark.asyncio
async def test_slow_send_is_cancelled_and_joined_at_remaining_absolute_deadline():
    clock = MutableClock()
    tested = module(clock)
    retired = []
    async def send(*_args):
        try:
            await asyncio.Event().wait()
        finally:
            retired.append(True)
    target = clock.now-149_000_000
    with pytest.raises(RuntimeFailure, match='delivery cadence'):
        await tested.send_native_packet(SimpleNamespace(sock_sendall=send), object(), b'original', target, stats=admitted_stats(clock))
    assert retired == [True]


@pytest.mark.asyncio
async def test_repeated_cancellation_joins_exact_child_before_reraising():
    clock = MutableClock()
    tested = module(clock)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    retired = []
    async def send(*_args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            retired.append(True)
    task = asyncio.create_task(tested.send_native_packet(SimpleNamespace(sock_sendall=send), object(), b'original', clock.now, stats=admitted_stats(clock)))
    await entered.wait()
    task.cancel('original')
    await cleaning.wait()
    task.cancel('during join')
    await asyncio.sleep(0)
    assert not task.done() and not retired
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert retired == [True]


def test_cleanup_and_final_evidence_failures_preserve_original_primary():
    tested = module(MutableClock())
    first, closed = RuntimeFailure('original clock refusal'), []
    def fail_close():
        closed.append('first')
        raise OSError('close failed')
    def publish(*_args):
        raise OSError('receipt failed')
    connections = [SimpleNamespace(close=fail_close), SimpleNamespace(close=lambda: closed.append('second'))]
    state = {'frames': 960}
    with pytest.raises(RuntimeFailure) as captured:
        tested.retire_producer(connections, 'unused', state, first, publish=publish)
    assert captured.value is first and isinstance(first.__cause__, OSError)
    assert closed == ['first', 'second'] and state['finished'] is True and state['frames'] == 960


@pytest.mark.asyncio
async def test_new_cancellation_during_timeout_join_retains_failure_and_cancels_after_retirement():
    clock = MutableClock()
    tested = module(clock)
    cleaning, release = asyncio.Event(), asyncio.Event()
    retired, stats = [], admitted_stats(clock)
    async def send(*_args):
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            retired.append(True)
    task = asyncio.create_task(tested.send_native_packet(SimpleNamespace(sock_sendall=send), object(),
        b'original', clock.now-149_000_000, stats=stats))
    await cleaning.wait()
    task.cancel('new caller cancellation')
    await asyncio.sleep(0)
    assert not task.done() and not retired
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() and retired == [True]
    assert stats['native_send_failure_before_cancellation']['type'] == 'RuntimeFailure'
    assert 'delivery cadence' in stats['native_send_failure_before_cancellation']['message']


@pytest.mark.parametrize('kind', ['pre_spin_monotonic', 'raw'])
def test_wide_invalid_reads_refuse_before_following_spin(kind):
    mono = [1_000_000_000, 1_000_000_001, 1_000_000_002]
    raw = []
    if kind == 'pre_spin_monotonic':
        mono.append(1_000_000_001)
    else:
        mono.append(1_000_075_002)
        raw.append(0)
    clock = ScriptClock(mono, raw)
    tested = module(clock)
    with pytest.raises(RuntimeFailure, match='invalid or backwards'):
        tested.recovered_clock_sample(wide=True, stats={})
    assert clock.mono_reads == len(mono) and clock.raw_reads == len(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize('sending,finished,expected_sends', [(1_000_005_000, 1_000_006_000, 0),
                                                           (1_000_010_001, 1_000_010_000, 1)])
async def test_postdeadline_or_completion_backwards_reads_reject_without_advancing_media(sending, finished, expected_sends):
    clock = ScriptClock([1_000_000_000, 1_000_000_000, 1_000_000_000,
                         1_000_010_000, sending, finished])
    tested, calls = module(clock), []
    stats = {'frames': 960, 'sequence': 1,
             'clock_recovery': {'passed': True, 'started_ns': 1_000_000_000,
                                'checked_ns': 1_000_000_000, 'elapsed_ns': 0}}
    async def send(*_args):
        calls.append(True)
    with pytest.raises(RuntimeFailure, match='delivery cadence'):
        await tested.send_native_packet(SimpleNamespace(sock_sendall=send), object(), b'original',
                                       1_000_000_000, stats=stats)
    assert len(calls) == expected_sends and (stats['frames'], stats['sequence']) == (960, 1)

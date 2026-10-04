"""Independent external room leases share one native connection without speech."""

import asyncio
from types import SimpleNamespace

import pytest

from shiri.rpc import RpcError
from shiri.runtime.audio import AudioWorker


class Clock:
    def __init__(self):
        self.value = 10_000_000_000

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += round(seconds * 1_000_000_000)


class Mixer:
    def __init__(self):
        self.closed = False

    def tick(self, **_arguments):
        raise AssertionError("Connection warming must not drive the mixer")

    def close(self):
        self.closed = True


class Native:
    """Exact aggregate ownership with fake gates, no audio or control sockets."""

    def __init__(self):
        self.warm_calls = []
        self.release_calls = []
        self.observe_calls = []
        self.deadline_calls = []
        self.connections = {}
        self.terminal = set()
        self.warm_entered = asyncio.Event()
        self.warm_gate = None
        self.warm_ack_gate = None
        self.release_entered = asyncio.Event()
        self.release_gate = None
        self.deadline_entered = asyncio.Event()
        self.deadline_gate = None
        self.failure = None
        self.closed = False

    async def warm_connection(self, identity, deadline):
        self.warm_calls.append((identity, deadline))
        self.warm_entered.set()
        if self.warm_gate is not None:
            await self.warm_gate.wait()
        if self.failure is not None:
            raise self.failure
        if identity in self.terminal:
            raise RpcError("session_conflict", "A retired native aggregate cannot be replayed")
        self.connections[identity] = deadline
        if self.warm_ack_gate is not None:
            await self.warm_ack_gate.wait()
        return self.connected()

    @staticmethod
    def connected():
        return {"state": "connected", "connected": True, "output_count": 1, "launch_generation": "a" * 32}

    async def observe_warm_connection(self, identity):
        self.observe_calls.append(identity)
        if identity not in self.connections:
            raise RpcError("session_conflict", "The exact native aggregate has retired")
        return self.connected()

    async def release_warm_connection(self, identity):
        self.release_calls.append(identity)
        self.release_entered.set()
        if self.release_gate is not None:
            await self.release_gate.wait()
        self.connections.pop(identity, None)
        self.terminal.add(identity)

    async def adjust_warm_deadline(self, identity, deadline):
        self.deadline_calls.append((identity, deadline))
        self.deadline_entered.set()
        if self.deadline_gate is not None:
            await self.deadline_gate.wait()
        if identity not in self.connections:
            raise RpcError("session_conflict", "A deadline cannot resurrect a retired aggregate")
        self.connections[identity] = deadline
        return self.connected()

    def begin_speech(self, *_arguments, **_keywords):
        raise AssertionError("Presence cannot admit a voice")

    async def close(self):
        self.closed = True


@pytest.fixture
async def rig(monkeypatch):
    clock, mixer, native = Clock(), Mixer(), Native()
    monkeypatch.setattr("shiri.runtime.audio.time.monotonic_ns", clock)
    worker = AudioWorker(mixer, native=native)
    result = SimpleNamespace(clock=clock, mixer=mixer, native=native, worker=worker)
    yield result
    if native.warm_gate is not None:
        native.warm_gate.set()
    if native.release_gate is not None:
        native.release_gate.set()
    if native.warm_ack_gate is not None:
        native.warm_ack_gate.set()
    if native.deadline_gate is not None:
        native.deadline_gate.set()
    await worker.close()


def request(rig, number=1, *, seconds=60, action="acquire"):
    result = {"action": action, "lease_id": f"{number:032x}"}
    if action == "acquire":
        result["deadline_monotonic_ns"] = rig.clock() + round(seconds * 1_000_000_000)
    return result


async def call(rig, number=1, **options):
    return await rig.worker.dispatch("warm", request(rig, number, **options))


async def test_four_external_leases_share_one_aggregate_and_release_only_last(rig):
    for number in range(1, 5):
        result = await call(rig, number, seconds=number * 10)
        assert result["connected"] is True
    identities = {identity for identity, _deadline in rig.native.warm_calls}
    assert len(identities) == 1 and len(rig.native.connections) == 1
    identity = identities.pop()
    assert rig.native.connections[identity] == rig.clock() + 40_000_000_000
    assert rig.worker.session is None
    with pytest.raises(RpcError) as error:
        await call(rig, 5)
    assert error.value.code == "session_limit" and len(rig.native.warm_calls) == 4
    for number in range(1, 4):
        assert (await call(rig, number, action="release"))["state"] == "released"
        assert not rig.native.release_calls
        assert (await call(rig, 4, action="observe"))["connected"] is True
    await call(rig, 4, action="release")
    assert rig.native.release_calls == [identity] and not rig.native.connections
    await call(rig, 4, action="release")
    assert rig.native.release_calls == [identity]


async def test_later_caller_after_last_release_gets_fresh_aggregate_identity(rig):
    await call(rig)
    old = rig.native.warm_calls[-1][0]
    await call(rig, action="release")
    await call(rig, 2)
    new = rig.native.warm_calls[-1][0]
    assert old != new and new not in rig.native.terminal
    assert set(rig.native.connections) == {new}


async def test_unknown_release_installs_terminal_fence_for_late_acquire(rig):
    await call(rig, 7, action="release")
    with pytest.raises(RpcError) as error:
        await call(rig, 7)
    assert error.value.code == "session_conflict"
    assert rig.native.warm_calls == rig.native.release_calls == []
    await call(rig, 8)
    assert len(rig.native.connections) == 1


async def test_same_external_lease_refresh_never_shortens_aggregate_deadline(rig):
    await call(rig, seconds=60)
    first = rig.native.warm_calls[-1]
    rig.clock.advance(5)
    await call(rig, seconds=5)
    assert rig.native.warm_calls[-1] == first
    await call(rig, seconds=90)
    assert rig.native.warm_calls[-1] == (first[0], rig.clock() + 90_000_000_000)


async def test_releasing_longest_lease_shortens_native_crash_guard_without_reopening_connection(rig):
    await call(rig, seconds=10)
    await call(rig, 2, seconds=300)
    identity = rig.native.warm_calls[0][0]
    assert rig.native.connections[identity] == rig.clock() + 300_000_000_000
    await call(rig, 2, action="release")
    assert rig.native.connections[identity] == rig.clock() + 10_000_000_000
    assert rig.native.deadline_calls == [(identity, rig.clock() + 10_000_000_000)]
    assert len(rig.native.warm_calls) == 2 and rig.native.release_calls == []
    assert (await call(rig, action="observe"))["connected"] is True


async def test_repeated_cancel_of_longest_release_joins_shortening_before_releasing_guard(rig):
    await call(rig, seconds=10)
    await call(rig, 2, seconds=300)
    identity = rig.native.warm_calls[0][0]
    rig.native.deadline_gate = asyncio.Event()
    releasing = asyncio.create_task(call(rig, 2, action="release"))
    await asyncio.wait_for(rig.native.deadline_entered.wait(), 1)
    releasing.cancel()
    await asyncio.sleep(0)
    releasing.cancel()
    observer = asyncio.create_task(call(rig, action="observe"))
    await asyncio.sleep(0)
    assert not releasing.done() and not observer.done()
    assert rig.native.connections[identity] == rig.clock() + 300_000_000_000
    rig.native.deadline_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await releasing
    assert (await asyncio.wait_for(observer, 1))["connected"] is True
    assert rig.native.connections[identity] == rig.clock() + 10_000_000_000
    assert len(rig.native.warm_calls) == 2 and rig.native.release_calls == []


async def test_cancelled_ack_after_longer_backend_extension_joins_exact_deadline_restoration(rig):
    await call(rig, seconds=10)
    identity = rig.native.warm_calls[0][0]
    rig.native.warm_entered.clear()
    rig.native.warm_ack_gate = asyncio.Event()
    rig.native.deadline_gate = asyncio.Event()
    acquiring = asyncio.create_task(call(rig, 2, seconds=300))
    await asyncio.wait_for(rig.native.warm_entered.wait(), 1)
    assert rig.native.connections[identity] == rig.clock() + 300_000_000_000
    acquiring.cancel()
    await asyncio.wait_for(rig.native.deadline_entered.wait(), 1)
    acquiring.cancel()
    await asyncio.sleep(0)
    acquiring.cancel()
    observer = asyncio.create_task(call(rig, action="observe"))
    await asyncio.sleep(0)
    assert not acquiring.done() and not observer.done()
    rig.native.deadline_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert (await asyncio.wait_for(observer, 1))["connected"] is True
    assert rig.native.connections[identity] == rig.clock() + 10_000_000_000
    assert rig.native.deadline_calls == [(identity, rig.clock() + 10_000_000_000)]
    assert rig.native.release_calls == []


async def test_failed_longer_acquire_restores_surviving_native_deadline_without_reopening(rig):
    await call(rig, seconds=10)
    identity = rig.native.warm_calls[0][0]
    rig.native.failure = RpcError("backend_unavailable", "Longer caller rejected")
    with pytest.raises(RpcError):
        await call(rig, 2, seconds=300)
    assert rig.native.deadline_calls == [(identity, rig.clock() + 10_000_000_000)]
    assert rig.native.connections[identity] == rig.clock() + 10_000_000_000
    assert len(rig.native.warm_calls) == 2 and rig.native.release_calls == []
    assert (await call(rig, action="observe"))["connected"] is True


async def test_subset_expiry_updates_native_guard_to_exact_remaining_deadline(rig):
    await call(rig, seconds=5)
    await call(rig, 2, seconds=10)
    identity = rig.native.warm_calls[0][0]
    rig.clock.advance(5)
    assert (await call(rig, 2, action="observe"))["connected"] is True
    assert rig.native.deadline_calls == [(identity, rig.clock() + 5_000_000_000)]
    assert len(rig.native.warm_calls) == 2 and rig.native.release_calls == []


async def test_expiring_one_lease_preserves_other_and_final_expiry_rotates_identity(rig):
    await call(rig, seconds=5)
    await call(rig, 2, seconds=10)
    old = rig.native.warm_calls[0][0]
    rig.clock.advance(5)
    with pytest.raises(RpcError) as error:
        await call(rig, action="observe")
    assert error.value.code == "session_conflict" and not rig.native.release_calls
    assert (await call(rig, 2, action="observe"))["connected"] is True
    rig.clock.advance(5)
    with pytest.raises(RpcError):
        await call(rig, 2, action="observe")
    assert rig.native.release_calls == [old] and not rig.native.connections
    await call(rig, 3)
    assert rig.native.warm_calls[-1][0] != old


async def test_first_setup_expiring_before_reply_is_retired_without_voice(rig):
    rig.native.warm_gate = asyncio.Event()
    pending = asyncio.create_task(call(rig, seconds=5))
    await asyncio.wait_for(rig.native.warm_entered.wait(), 1)
    rig.clock.advance(5)
    rig.native.warm_gate.set()
    with pytest.raises(RpcError) as error:
        await pending
    assert error.value.code == "deadline_exceeded"
    assert not rig.native.connections and len(rig.native.release_calls) == 1
    assert rig.worker.session is None


async def test_first_failed_setup_is_retired_and_next_caller_gets_fresh_identity(rig):
    rig.native.failure = RpcError("backend_unavailable", "Injected connection failure")
    with pytest.raises(RpcError):
        await call(rig)
    old = rig.native.warm_calls[-1][0]
    assert rig.native.release_calls == [old]
    rig.native.failure = None
    await call(rig, 2)
    assert rig.native.warm_calls[-1][0] != old
    with pytest.raises(RpcError) as error:
        await call(rig)
    assert error.value.code == "session_conflict"


async def test_failed_second_caller_preserves_first_connected_aggregate(rig):
    await call(rig)
    original = rig.native.warm_calls[-1][0]
    rig.native.failure = RpcError("backend_unavailable", "Injected second-caller refusal")
    with pytest.raises(RpcError):
        await call(rig, 2)
    assert not rig.native.release_calls and set(rig.native.connections) == {original}
    assert (await call(rig, action="observe"))["connected"] is True
    rig.native.failure = None
    await call(rig, 3)
    assert rig.native.warm_calls[-1][0] == original


async def test_cancelled_second_caller_preserves_first_connected_aggregate(rig):
    await call(rig)
    original = rig.native.warm_calls[-1][0]
    rig.native.warm_entered.clear()
    rig.native.warm_gate = asyncio.Event()
    pending = asyncio.create_task(call(rig, 2))
    await asyncio.wait_for(rig.native.warm_entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not rig.native.release_calls and set(rig.native.connections) == {original}
    assert (await call(rig, action="observe"))["connected"] is True


async def test_repeated_release_cancellation_holds_guard_until_original_cleanup_finishes(rig):
    await call(rig)
    old = rig.native.warm_calls[-1][0]
    rig.native.release_gate = asyncio.Event()
    retiring = asyncio.create_task(call(rig, action="release"))
    await asyncio.wait_for(rig.native.release_entered.wait(), 1)
    retiring.cancel()
    await asyncio.sleep(0)
    retiring.cancel()
    successor = asyncio.create_task(call(rig, 2))
    await asyncio.sleep(0)
    assert len(rig.native.warm_calls) == 1 and not retiring.done() and not successor.done()
    rig.native.release_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await retiring
    await asyncio.wait_for(successor, 1)
    new = rig.native.warm_calls[-1][0]
    assert new != old and set(rig.native.connections) == {new}
    assert rig.native.release_calls == [old]


@pytest.mark.parametrize("deadline", [False, True, None, "11000000000", 10_000_000_000.0,
                                       10_000_000_000, 9_999_999_999, 310_000_000_001])
async def test_invalid_deadlines_do_not_reach_native_backend(rig, deadline):
    message = request(rig)
    message["deadline_monotonic_ns"] = deadline
    with pytest.raises(RpcError) as error:
        await rig.worker.dispatch("warm", message)
    assert error.value.code == "invalid_request"
    assert not rig.native.warm_calls and not rig.native.release_calls


@pytest.mark.parametrize("changes", [{"lease_id": ""}, {"lease_id": "A" * 32}, {"lease_id": "g" * 32},
                                     {"lease_id": None}, {"action": "renew"}, {"action": "speech"},
                                     {"duck_gain": .2}, {"text": "No audio"}, {"lease_id": True}])
async def test_unknown_schema_and_actions_do_not_reach_native_backend(rig, changes):
    message = {**request(rig), **changes}
    with pytest.raises(RpcError) as error:
        await rig.worker.dispatch("warm", message)
    assert error.value.code == "invalid_request" and not rig.native.warm_calls


async def test_missing_deadline_or_extra_release_fields_are_rejected(rig):
    for payload in ({"action": "acquire", "lease_id": "1" * 32},
                    {**request(rig, action="release"), "deadline_monotonic_ns": rig.clock() + 1}):
        with pytest.raises(RpcError) as error:
            await rig.worker.dispatch("warm", payload)
        assert error.value.code == "invalid_request"
    assert not rig.native.warm_calls


async def test_unknown_observation_cannot_acquire_implicitly(rig):
    with pytest.raises(RpcError) as error:
        await call(rig, action="observe")
    assert error.value.code == "not_found"
    assert not rig.native.warm_calls and not rig.native.observe_calls


async def test_retirement_capacity_retains_fences_until_the_documented_window(rig):
    for number in range(1, 65):
        await call(rig, number, action="release")
    with pytest.raises(RpcError) as error:
        await call(rig, 65)
    assert error.value.code == "session_limit" and not rig.native.warm_calls
    with pytest.raises(RpcError) as error:
        await call(rig)
    assert error.value.code == "session_conflict"
    rig.clock.advance(601)
    await call(rig, 65)
    assert len(rig.native.warm_calls) == 1


async def test_worker_close_retires_aggregate_and_does_not_admit_successor(rig):
    await call(rig)
    identity = rig.native.warm_calls[-1][0]
    await rig.worker.close()
    assert rig.native.release_calls == [identity] and not rig.native.connections
    assert rig.native.closed and rig.mixer.closed and rig.worker.session is None
    with pytest.raises(RpcError) as error:
        await call(rig, 2)
    assert error.value.code == "session_conflict"
    assert len(rig.native.warm_calls) == 1

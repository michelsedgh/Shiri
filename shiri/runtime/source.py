"""Live music ownership and PCM fencing for a single zone.

Receiver adapters admit once per native producer incarnation, then retain the
returned complete token. A new grant atomically clears the previous program
at the mixer boundary. Each ingress and final program write checks that same
token under the media lock. Slow transport shutdown therefore cannot leak
old PCM or let a delayed callback change a successor. Speech is an independent
overlay and does not request, end or change music ownership.

The synchronous fence/write callbacks must finish promptly and perform no
blocking transport I/O. They execute on the mixer boundary, not on a speaker
clock. OwnTone still schedules the outputs from their program timestamps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
import inspect
import logging
import math
import threading
from typing import Protocol

from shiri.domain import Conflict
from shiri.source import (
    InputEnded,
    InputProtocol,
    InputRequested,
    InputVolumeChanged,
    SourceState,
    SourceToken,
    reduce_source,
)

log = logging.getLogger(__name__)


class NativeHandle(Protocol):
    """A transport handle bound to one exact native producer incarnation."""

    async def quiesce(self, token: SourceToken) -> None: ...

    def abort(self, token: SourceToken) -> None: ...

    def set_volume(self, token: SourceToken, volume: int) -> None: ...


def _synchronous(callback, *args):
    result = callback(*args)
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        elif callable(getattr(result, "cancel", None)):
            result.cancel()
        raise TypeError("Music fence/write/volume callbacks must be synchronous")
    return result


class SourceActor:
    """Newest admitted music producer wins; stale work is rejected at writes.

    One actor lives inside one audio worker. Its incarnation is always fresh
    on startup; saved intent never restores a live token. A caller retaining
    an old token cannot match this actor even if its native ID is reused.
    Optional persistence runs before a grant is acknowledged, for diagnostic
    journals; it must not restore the actor incarnation after a restart.
    """

    def __init__(self, zone_id: str, fence: Callable[[SourceToken | None], None], *,
                 persist: Callable[[SourceState], None] | None = None,
                 barrier: Callable[[SourceToken | None, SourceToken | None], Awaitable[None]] | None = None,
                 barrier_timeout: float = 5.0,
                 revoke_timeout: float = 2.0, max_pending_revocations: int = 16):
        if (isinstance(revoke_timeout, bool) or not isinstance(revoke_timeout, (int, float))
                or not math.isfinite(revoke_timeout) or revoke_timeout <= 0
                or type(max_pending_revocations) is not int or max_pending_revocations < 1):
            raise ValueError("Source retirement requires finite positive limits")
        if (isinstance(barrier_timeout, bool) or not isinstance(barrier_timeout, (int, float))
                or not math.isfinite(barrier_timeout) or barrier_timeout <= 0):
            raise ValueError("Source transition requires a finite positive deadline")
        self._state = SourceState(zone_id=zone_id)
        self._fence = fence
        self._persist = persist
        self._barrier = barrier
        self._barrier_timeout = barrier_timeout
        self._barrier_task: asyncio.Task | None = None
        self._transitioning = False
        self._handle: NativeHandle | None = None
        self._mutation = asyncio.Lock()
        self._media = threading.RLock()
        self._revocations: dict[asyncio.Task, NativeHandle] = {}
        self._revocation_aborts: dict[asyncio.Task, Callable[[], None]] = {}
        self._watchdogs: set[asyncio.Task] = set()
        self._timeout = revoke_timeout
        self._limit = max_pending_revocations
        self._closed = False
        self._error: str | None = None
        self.accepted_writes = 0
        self.accepted_idle_writes = 0
        self.rejected_writes = 0
        self.retirement_failures = 0

    def snapshot(self):
        with self._media:
            return {**self._state.model_dump(), "ready": not self._closed and self._error is None and not self._transitioning,
                    "transitioning": self._transitioning,
                    "error": self._error, "accepted_writes": self.accepted_writes,
                    "accepted_idle_writes": self.accepted_idle_writes,
                    "rejected_writes": self.rejected_writes,
                    "pending_revocations": len(self._revocations),
                    "retirement_failures": self.retirement_failures}

    def owns(self, token: SourceToken) -> bool:
        """Observation only. Use write() to combine the check and PCM effect."""
        with self._media:
            return (isinstance(token, SourceToken) and not self._closed
                    and self._error is None and not self._transitioning and self._state.owner == token)

    def write(self, token: SourceToken, callback: Callable[[], object]) -> bool:
        """Gate both ingress and final mixed program packets without an await."""
        with self._media:
            if (not isinstance(token, SourceToken) or self._closed or self._error is not None or self._transitioning
                    or self._state.owner != token):
                self.rejected_writes += 1
                return False
            _synchronous(callback)
            self.accepted_writes += 1
            return True

    def write_idle(self, callback: Callable[[], object]) -> bool:
        """Gate idle-bed/speech output atomically without a music token."""
        with self._media:
            if self._closed or self._error is not None or self._transitioning or self._state.owner is not None:
                self.rejected_writes += 1
                return False
            _synchronous(callback)
            self.accepted_idle_writes += 1
            return True

    def _commit(self, state: SourceState, handle: NativeHandle | None, *, persisted=False):
        # Persistence failure leaves the currently audible program untouched.
        if self._persist and not persisted:
            _synchronous(self._persist, state.model_copy(deep=True))
        with self._media:
            try:
                _synchronous(self._fence, state.owner.model_copy(deep=True) if state.owner else None)
            except BaseException:
                # A broken mixer fence must silence/fail closed, never admit
                # another token against an uncleared program queue.
                self._state = state.model_copy(update={"owner": None})
                self._handle = None
                self._error = "The mixer could not fence the previous music program"
                raise
            self._state, self._handle = state, handle

    async def _transition(self, state: SourceState, handle: NativeHandle | None):
        if self._barrier is None:
            self._commit(state, handle)
            return
        if self._persist:
            _synchronous(self._persist, state.model_copy(deep=True))
        previous = self._state.owner
        with self._media:
            self._transitioning = True
        try:
            with self._media:
                _synchronous(self._fence, None)
            task = asyncio.create_task(self._barrier(
                previous.model_copy(deep=True) if previous else None,
                state.owner.model_copy(deep=True) if state.owner else None),
                name=f"music-output-barrier-{state.epoch}")
            self._barrier_task = task
            # A network/backend task must not extend admission indefinitely by
            # suppressing cancellation. On failure this actor cannot grant any
            # successor; backend token/epoch fencing also rejects late effects
            # after a separately restarted actor/backend pair.
            done, _ = await asyncio.wait({task}, timeout=self._barrier_timeout)
            if not done:
                raise TimeoutError("The output backend did not acknowledge the source transition")
            task.result()
            self._commit(state, handle, persisted=True)
        except BaseException:
            task = self._barrier_task
            if task is not None:
                if not task.done():
                    task.cancel()
                def consume(done):
                    if not done.cancelled():
                        done.exception()
                task.add_done_callback(consume)
            with self._media:
                self._state = state.model_copy(update={"owner": None})
                self._handle = None
                self._error = "The output backend could not acknowledge a fenced source transition"
            raise
        finally:
            with self._media:
                self._transitioning = False

    def _retire(self, handle: NativeHandle, token: SourceToken):
        aborted = False

        def abort():
            nonlocal aborted
            callback = getattr(handle, "abort", None)
            if aborted or not callable(callback):
                return
            aborted = True
            try:
                _synchronous(callback, token.model_copy(deep=True))
            except Exception as error:
                self.retirement_failures += 1
                log.warning("An exactly fenced music transport could not be aborted: %s", type(error).__name__)

        async def quiesce():
            await handle.quiesce(token.model_copy(deep=True))

        task = asyncio.create_task(quiesce(), name=f"music-revoke-{token.epoch}")
        self._revocations[task] = handle
        self._revocation_aborts[task] = abort

        def complete(done):
            self._revocations.pop(done, None)
            self._revocation_aborts.pop(done, None)
            if done.cancelled():
                abort()
            else:
                error = done.exception()
                if error:
                    abort()
                    self.retirement_failures += 1
                    log.warning("An exactly fenced music transport failed to close: %s", type(error).__name__)

        task.add_done_callback(complete)

        async def watchdog():
            done, _ = await asyncio.wait({task}, timeout=self._timeout)
            if not done:
                self.retirement_failures += 1
                # Cancellation before the coroutine's first instruction never
                # runs its finally block. Close the exact native resource now;
                # task cancellation alone is not a transport closure proof.
                abort()
                task.cancel()
                # No await of an uncooperative cancellation. Its old token is
                # already fenced; retain the task in the bounded admission
                # limit until it really exits.
                log.warning("An exactly fenced music transport exceeded its close deadline")

        watcher = asyncio.create_task(watchdog(), name=f"music-revoke-watch-{token.epoch}")
        self._watchdogs.add(watcher)
        watcher.add_done_callback(self._watchdogs.discard)

    async def admit_native(self, protocol: InputProtocol, session_id: str, handle: NativeHandle) -> SourceToken:
        async with self._mutation:
            if self._closed or self._error:
                raise Conflict("This zone's music route is unavailable")
            if not callable(getattr(handle, "quiesce", None)) or not callable(getattr(handle, "set_volume", None)):
                raise ValueError("A native music handle must provide exact quiesce and volume hooks")
            change = reduce_source(self._state, InputRequested(
                zone_id=self._state.zone_id, protocol=protocol, session_id=session_id))
            if change.reason == "already_owner":
                if handle is not self._handle:
                    raise Conflict("A native session identity cannot be reused by another connection")
                return self._state.owner.model_copy(deep=True)
            if handle is self._handle or any(handle is item for item in self._revocations.values()):
                raise Conflict("A new music incarnation requires its own exact transport handle")
            if len(self._revocations) >= self._limit:
                raise Conflict("Previous music transports are still closing; retry after they finish")
            previous, old_handle = self._state.owner, self._handle
            try:
                await self._transition(change.state, handle)
            except BaseException:
                if self._error and previous is not None and old_handle is not None:
                    self._retire(old_handle, previous)
                raise
            if previous is not None and old_handle is not None:
                self._retire(old_handle, previous)
            return self._state.owner.model_copy(deep=True)

    async def end_native(self, token: SourceToken) -> bool:
        async with self._mutation:
            if self._closed or self._error:
                return False
            change = reduce_source(self._state, InputEnded(token=token))
            if not change.accepted:
                return False
            handle = self._handle
            try:
                await self._transition(change.state, None)
            except BaseException:
                if self._error and handle is not None:
                    self._retire(handle, token)
                raise
            if handle is not None:
                self._retire(handle, token)
            return True

    async def volume_native(self, token: SourceToken, volume: int) -> bool:
        """Apply only the current producer's bounded, synchronous volume hook.

        Asynchronous persistence/control callbacks must carry the complete
        token too and recheck it where their final mutation occurs. A detached
        RPC using only room_id or session_id is not an ownership fence.
        """
        async with self._mutation:
            if self._closed or self._error:
                return False
            change = reduce_source(self._state, InputVolumeChanged(token=token, volume=volume))
            if not change.accepted:
                return False
            if self._persist:
                _synchronous(self._persist, change.state.model_copy(deep=True))
            with self._media:
                handle = self._handle
                try:
                    _synchronous(handle.set_volume, token.model_copy(deep=True), volume)
                except BaseException:
                    self._state = change.state.model_copy(update={"owner": None})
                    self._handle = None
                    self._error = "The native source volume effect could not be confirmed"
                    with suppress(Exception):
                        _synchronous(self._fence, None)
                    self._retire(handle, token)
                    raise
                else:
                    self._state = change.state
            return True

    async def flush_native(self, token: SourceToken) -> bool:
        """A seek/flush fences the same owner; it is never a new admission.

        The exact native handle validates and advances its transport flush
        generation before invoking this method. The downstream barrier carries
        that generation and a separate monotonically increasing operation ID.
        Speech offer/media/close handlers must never invoke this operation.
        """
        async with self._mutation:
            if not self.owns(token):
                return False
            handle = self._handle
            try:
                await self._transition(self._state, handle)
            except BaseException:
                if self._error and handle is not None:
                    self._retire(handle, token)
                raise
            return True

    async def close(self):
        async with self._mutation:
            if self._closed:
                return
            previous, handle = self._state.owner, self._handle
            with self._media:
                self._closed = True
                self._state = self._state.model_copy(update={"owner": None})
                self._handle = None
                try:
                    _synchronous(self._fence, None)
                finally:
                    if previous is not None and handle is not None:
                        self._retire(handle, previous)
            tasks = set(self._revocations)
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=self._timeout)
            for task in pending:
                abort = self._revocation_aborts.get(task)
                if abort is not None:
                    abort()
                task.cancel()
        for watcher in tuple(self._watchdogs):
            watcher.cancel()
        if self._barrier_task and not self._barrier_task.done():
            self._barrier_task.cancel()

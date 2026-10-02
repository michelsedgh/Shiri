"""Owned asyncio deadlines with cancellation preserved on Python 3.10.

A cancelled caller never accepts a completed child through wait_for's
ready/cancel race. A resource result abandoned by the caller can be retired by
the operation that knows its identity; this helper creates no new authority.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress


async def bounded(awaitable, timeout, *, abandoned=None):
    """Wait once, cancel and join a pending child, and retire an abandoned result."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout if timeout is not None else None
    task = asyncio.ensure_future(awaitable)
    completed = False
    try:
        done, _pending = await asyncio.wait({task}, timeout=max(0, timeout) if timeout is not None else None)
        if not done or (deadline is not None and loop.time() >= deadline):
            raise asyncio.TimeoutError
        result = task.result()
        completed = True
        return result
    finally:
        try:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            # A completed result already belongs to this caller. Do not yield
            # before returning it: Python3.10 gather yields even for done tasks,
            # and a cancellation there would strand the accepted resource.
        finally:
            if not completed and task.done() and not task.cancelled():
                # Consume an error even when a deadline rejects a child that
                # already finished; it still belongs to this operation.
                if task.exception() is None and abandoned is not None:
                    with suppress(Exception):
                        abandoned(task.result())

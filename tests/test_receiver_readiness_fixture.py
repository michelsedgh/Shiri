"""Manual fixture error handling must retain truthful negative-case evidence."""
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from shiri.runtime.system import RuntimeFailure

SOURCE = Path(__file__).parent/'linux/check_receiver_readiness.py'
SPEC = importlib.util.spec_from_file_location('receiver_readiness_fixture', SOURCE)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


async def test_completed_expected_observer_failure_returns_the_rejection_evidence(monkeypatch):
    observer = AsyncMock(side_effect=RuntimeFailure('Receiver did not own its listener before the deadline'))
    monkeypatch.setattr(fixture, 'wait_receiver_ready', observer)
    receipt = await fixture.reject('exact-unit', 'held-namespace', 'mapped-user', .3, ['deadline'])
    assert receipt['error'] == 'Receiver did not own its listener before the deadline'
    assert receipt['elapsed_seconds'] < .3
    observer.assert_awaited_once_with('exact-unit', 'held-namespace', 'mapped-user', timeout=.3)


async def test_before_callback_failure_is_preserved_and_pending_observer_is_cancelled(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def observer(*_, **__):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    error = ValueError('Fixture setup failed before the exit signal')

    async def before():
        await started.wait()
        raise error

    monkeypatch.setattr(fixture, 'wait_receiver_ready', observer)
    with pytest.raises(ValueError) as caught:
        await fixture.reject(None, None, None, .3, ['deadline'], before=before)
    assert caught.value is error
    assert cancelled.is_set()


async def test_an_unexpectedly_accepted_negative_case_always_fails_the_fixture(monkeypatch):
    monkeypatch.setattr(fixture, 'wait_receiver_ready', AsyncMock(return_value={'ready': True}))
    with pytest.raises(RuntimeError, match='was accepted'):
        await fixture.reject(None, None, None, .3, ['deadline'])


async def test_a_failure_at_an_unrelated_gate_does_not_count_as_listener_rejection(monkeypatch):
    monkeypatch.setattr(fixture, 'wait_receiver_ready', AsyncMock(side_effect=RuntimeFailure('Broken invocation')))
    with pytest.raises(RuntimeError, match='intended ownership gate'):
        await fixture.reject(None, None, None, .3, ['deadline'])


def capabilities(monkeypatch, *, extra=0, missing=0):
    production = sum(1 << index for index in (0, 1, 3, 4, 5, 6, 7, 10, 12, 13, 21, 23))
    mask = (production | extra) & ~missing
    text = '\n'.join(f'{key}:\t{mask:x}' for key in ['CapEff', 'CapPrm', 'CapInh', 'CapBnd', 'CapAmb'])
    monkeypatch.setattr(Path, 'read_text', lambda _: text)


def test_manual_fixture_requires_the_actual_production_broker_capability_boundary(monkeypatch):
    capabilities(monkeypatch)
    receipt = fixture.capability_admission()
    assert set(receipt) == {'CapEff', 'CapPrm', 'CapInh', 'CapBnd', 'CapAmb'}
    assert all(not int(value, 16) & (1 << 19) for value in receipt.values())


@pytest.mark.parametrize('extra', [1 << 19, 1 << 2])
def test_ptrace_or_other_additional_capabilities_refuse_the_manual_proof(monkeypatch, extra):
    capabilities(monkeypatch, extra=extra)
    with pytest.raises(RuntimeError, match='CAP_SYS_PTRACE|outside the production set'):
        fixture.capability_admission()


@pytest.mark.parametrize('missing', [1 << 7, 1 << 3, 1 << 4])
def test_missing_credential_drop_or_metadata_capability_refuses_the_manual_proof(monkeypatch, missing):
    capabilities(monkeypatch, missing=missing)
    with pytest.raises(RuntimeError, match='production broker capabilities'):
        fixture.capability_admission()

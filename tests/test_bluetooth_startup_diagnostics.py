"""Test-only authenticated error and pre-teardown log retention; no D-Bus."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

from dbus_next import Message, MessageType
import pytest

from shiri.runtime.system import RuntimeFailure

ROOT = Path(__file__).resolve().parent.parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT/'tests/linux'/file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


diagnostics = load('startup_diagnostics_test', 'bluetooth_startup_diagnostics.py')


class Bus:
    def __init__(self):
        self.serial, self.handler, self.messages = 0, None, []
        self.response = None
        self.sent = asyncio.Queue()

    def add_message_handler(self, handler):
        self.handler = handler

    def next_serial(self):
        self.serial += 1
        return self.serial

    async def send(self, message):
        self.messages.append(message)
        self.sent.put_nowait(message)
        if self.response is not None:
            reply = self.response(message)
            self.handler(reply)


def reply(message, *, sender=':1.23', text='Acquire transport: Input/output error', descriptors=None):
    return Message(message_type=MessageType.ERROR, sender=sender, reply_serial=message.serial,
                   error_name='org.freedesktop.DBus.Error.IOError', signature='s', body=[text],
                   unix_fds=descriptors or [])


async def request(transport, member='OpenRestricted'):
    return await transport.request(':1.23', '/org/bluealsa/pcm', 'org.bluealsa.PCM1', member, timeout=.1)


async def test_actual_production_request_still_rejects_error_once_and_disposes_fds():
    bus, records = Bus(), {'errors': [], 'omitted_errors': 0}
    transport = diagnostics.DiagnosticBus(bus, records)
    read, write = os.pipe()
    sent = os.dup(write)
    bus.response = lambda message: reply(message, descriptors=[sent])
    try:
        with pytest.raises(RuntimeFailure, match='rejected OpenRestricted'):
            await request(transport)
        assert len(bus.messages) == 1 and bus.messages[0].member == 'OpenRestricted'
        error = records['errors'][0]
        assert error['reply_serial'] == bus.messages[0].serial
        assert error['reply_sender'] == error['expected_reply_sender'] == ':1.23'
        assert error['member'] == 'OpenRestricted' and error['interface'] == 'org.bluealsa.PCM1'
        assert error['error_name'] == 'org.freedesktop.DBus.Error.IOError'
        assert error['body_text'] == 'Acquire transport: Input/output error'
        assert error['received_fd_count'] == 1
        assert not transport.pending and not transport.methods
        with pytest.raises(OSError):
            os.fstat(sent)
    finally:
        for descriptor in (read, write, sent):
            with suppress(OSError):
                os.close(descriptor)


async def test_successful_descriptor_reply_is_transferred_without_observer_consumption():
    bus, records = Bus(), {'errors': [], 'omitted_errors': 0}
    transport = diagnostics.DiagnosticBus(bus, records)
    read, write = os.pipe()
    sent = os.dup(write)
    bus.response = lambda message: Message(message_type=MessageType.METHOD_RETURN, sender=':1.23',
        reply_serial=message.serial, signature='h', body=[0], unix_fds=[sent])
    try:
        accepted = await request(transport)
        assert accepted.unix_fds == [sent] and os.fstat(sent).st_ino == os.fstat(write).st_ino
        assert records == {'errors': [], 'omitted_errors': 0}
        assert len(bus.messages) == 1 and not transport.pending and not transport.methods
    finally:
        for descriptor in (read, write, sent):
            with suppress(OSError):
                os.close(descriptor)


@pytest.mark.parametrize('kind', ['foreign_sender', 'unknown_serial', 'already_done', 'closed'])
async def test_unowned_or_retired_replies_are_never_diagnostic_authority(kind):
    bus, records = Bus(), {'errors': [], 'omitted_errors': 0}
    transport = diagnostics.DiagnosticBus(bus, records)
    task = asyncio.create_task(request(transport))
    message = await asyncio.wait_for(bus.sent.get(), .1)
    wrong = reply(message)
    if kind == 'foreign_sender':
        wrong.sender = ':1.99'
    elif kind == 'unknown_serial':
        wrong.reply_serial += 1
    elif kind == 'already_done':
        transport.pending[message.serial][0].cancel()
    else:
        transport.closed = True
    read, write = os.pipe()
    sent = os.dup(write)
    wrong.unix_fds = [sent]
    try:
        assert transport._received(wrong) is True
        with pytest.raises(OSError):
            os.fstat(sent)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert records == {'errors': [], 'omitted_errors': 0}
        assert not transport.pending and not transport.methods
    finally:
        for descriptor in (read, write, sent):
            with suppress(OSError):
                os.close(descriptor)


async def test_concurrent_exact_serials_keep_their_actual_method_not_latest_method():
    bus, records = Bus(), {'errors': [], 'omitted_errors': 0}
    transport = diagnostics.DiagnosticBus(bus, records)
    tasks = [asyncio.create_task(request(transport, member)) for member in ['OpenRestricted', 'GetManagedObjects']]
    await asyncio.wait_for(bus.sent.get(), .1)
    await asyncio.wait_for(bus.sent.get(), .1)
    for message in reversed(bus.messages):
        transport._received(reply(message))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(value, RuntimeFailure) for value in results)
    assert [(error['reply_serial'], error['member']) for error in records['errors']] == [
        (2, 'GetManagedObjects'), (1, 'OpenRestricted')]
    assert not transport.methods


async def test_bounded_error_body_and_record_count_do_not_change_rejections():
    bus, records = Bus(), {'errors': [], 'omitted_errors': 0}
    transport = diagnostics.DiagnosticBus(bus, records)
    bus.response = lambda message: reply(message, text='é' * 10000)
    for _ in range(diagnostics.MAX_ERRORS+3):
        with pytest.raises(RuntimeFailure, match='rejected OpenRestricted'):
            await request(transport)
    assert len(records['errors']) == diagnostics.MAX_ERRORS and records['omitted_errors'] == 3
    assert all(len(error['body_text'].encode()) <= diagnostics.MAX_ERROR_TEXT_BYTES for error in records['errors'])
    assert len(json.dumps(records).encode()) < 128*1024
    assert len(bus.messages) == diagnostics.MAX_ERRORS+3


def daemon(tmp_path):
    directory = tmp_path/'private'
    directory.mkdir(mode=0o710)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    info = os.fstat(descriptor)
    result = SimpleNamespace(directory=directory, directory_fd=descriptor,
        directory_identity=(info.st_dev, info.st_ino, info.st_uid),
        startup_diagnostics={'errors': [], 'omitted_errors': 0},
        daemon=SimpleNamespace(pid=123, returncode=None), mock=SimpleNamespace(calls=[('org.bluez.MediaTransport1', 'Acquire')]))
    destination = tmp_path/'retained'
    destination.mkdir(mode=0o700)
    return result, destination


def log(daemon, payload):
    path = daemon.directory/'bluealsa.log'
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def test_bounded_tail_survives_original_log_and_directory_removal(tmp_path):
    original, destination = daemon(tmp_path)
    payload = b'old' * 50000+b'Acquire transport: Input/output error\n'
    path = log(original, payload)
    try:
        receipt = diagnostics.retain(destination, original)
        path.unlink()
        original.directory.rmdir()
        data = json.loads(Path(receipt['path']).read_bytes())
        retained = Path(data['log']['path']).read_bytes()
        assert retained == payload[-diagnostics.MAX_LOG_TAIL_BYTES:]
        assert data['log']['observed_source_bytes'] == len(payload)
        assert data['log']['sha256'] == hashlib.sha256(retained).hexdigest()
        assert data['log']['tail_text'] == retained.decode('utf-8', errors='replace')
        assert receipt['log']['tail_text'] == data['log']['tail_text']
        assert data['phase'] == 'before_private_daemon_teardown'
        assert data['daemon_pid'] == 123 and data['daemon_returncode'] is None
        assert data['mock_bluez_calls'] == [['org.bluez.MediaTransport1', 'Acquire']]
        assert stat.S_IMODE(Path(data['log']['path']).stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(receipt['path']).stat().st_mode) == 0o600
    finally:
        os.close(original.directory_fd)


@pytest.mark.parametrize('kind', ['symlink', 'file_mode', 'hardlink', 'fifo', 'parent_replaced', 'log_replaced', 'oversize'])
def test_foreign_or_replaced_log_is_refused_without_copy_or_delete(tmp_path, monkeypatch, kind):
    original, destination = daemon(tmp_path)
    path = log(original, b'exact old log')
    if kind == 'symlink':
        saved = tmp_path/'outside'
        path.rename(saved)
        path.symlink_to(saved)
    elif kind == 'file_mode':
        path.chmod(0o644)
    elif kind == 'hardlink':
        os.link(path, tmp_path/'other-link')
    elif kind == 'fifo':
        path.unlink()
        os.mkfifo(path, 0o600)
    elif kind == 'parent_replaced':
        original.directory.rename(tmp_path/'old-private')
        original.directory.mkdir(mode=0o710)
    elif kind == 'oversize':
        with path.open('r+b') as stream:
            stream.truncate(diagnostics.MAX_SOURCE_LOG_BYTES+1)
    else:
        real = diagnostics.os.pread
        def replaced(descriptor, count, offset):
            data = real(descriptor, count, offset)
            path.rename(tmp_path/'old-log')
            log(original, b'foreign successor')
            return data
        monkeypatch.setattr(diagnostics.os, 'pread', replaced)
    try:
        with pytest.raises((RuntimeFailure, OSError)):
            diagnostics.retain(destination, original)
        assert not list(destination.iterdir())
        assert original.directory.exists()
    finally:
        os.close(original.directory_fd)


def test_initial_absent_log_has_an_explicit_receipt_and_no_fabricated_error(tmp_path):
    original, destination = daemon(tmp_path)
    try:
        receipt = diagnostics.retain(destination, original)
        data = json.loads(Path(receipt['path']).read_bytes())
        assert data['log'] == {'status': 'not_created'}
        assert data['dbus'] == {'errors': [], 'omitted_errors': 0}
    finally:
        os.close(original.directory_fd)


@pytest.mark.parametrize('retention_fails', [False, True])
async def test_actual_cleanup_retains_before_stop_and_does_not_skip_stop_on_retention_failure(
        tmp_path, monkeypatch, retention_fails):
    pytest.importorskip('numpy')
    pytest.importorskip('av')
    pytest.importorskip('aiortc')
    fixture = load('bluetooth_diagnostic_fixture_test', 'check_native_bluetooth_route.py')
    original, destination = daemon(tmp_path)
    log(original, b'Actual private diagnostic before stop\n')
    original.manager, original.capture = SimpleNamespace(leases={}), None
    events = []
    async def close(**kwargs):
        events.append('stop')
        assert kwargs == {'leases_released': True}
        assert original.directory.exists()
        if not retention_fails:
            assert (destination/'private-bluetooth-startup-diagnostics.json').is_file()
    def remove():
        events.append('remove')
        (original.directory/'bluealsa.log').unlink()
        original.directory.rmdir()
    original.close, original.remove_directory = close, remove
    if retention_fails:
        def failing_capture(*args):
            events.append('diagnostic_failed')
            raise RuntimeFailure('injected retention failure')
        monkeypatch.setattr(fixture.route.diagnostics, 'retain', failing_capture)
    report = {'failure': {'type': 'RuntimeFailure', 'message': 'Bluetooth service rejected OpenRestricted'},
              'cleanup': {}, 'artifacts': {}}
    errors = []
    try:
        await fixture.finish_private_bluetooth(original, None, destination, report, errors)
        assert events[-2:] == ['stop', 'remove']
        assert report['failure']['message'] == 'Bluetooth service rejected OpenRestricted'
        assert report['cleanup']['private_daemon_closed'] and report['cleanup']['private_directory_removed']
        assert bool(errors) == retention_fails
    finally:
        os.close(original.directory_fd)


def startup_fixture():
    pytest.importorskip('numpy')
    pytest.importorskip('av')
    pytest.importorskip('aiortc')
    return load('bluetooth_first_failure_fixture_test', 'check_native_bluetooth_route.py')


def observed_broker(fixture, monkeypatch):
    # Exercise the maintained override and real production room loop without
    # creating namespaces, buses, units, descriptors or a privileged broker.
    monkeypatch.setattr(fixture.group.IsolatedBroker, '__init__', lambda self, *args, **kwargs: None)
    result = fixture.IsolatedBluetoothBroker(None, None)
    result._password = 'private-master-value'
    result.startup_api_token = 'private-api-value'
    result._room_password = lambda identifier: 'private-room-value'
    result._closing, result.ready, result.error = False, True, None
    return result


async def test_actual_room_loop_retains_original_post_admission_failure_before_cleanup_and_retry(
        tmp_path, monkeypatch):
    fixture = startup_fixture()
    from shiri.domain import Room
    from shiri.runtime.broker import RuntimeRoom
    definition = Room(id='b6786543-7eb2-443d-83b1-65b984123a76', slot=7, name='Private A',
                      airplay_name='Private A', interface='eth0', enabled=True, revision=1)
    room = RuntimeRoom(definition, tmp_path)
    broker = observed_broker(fixture, monkeypatch)
    original = RuntimeFailure('Bridge publication failed private-room-value private-api-value private-master-value')
    original.__cause__ = PermissionError(13, 'secret path intentionally never rendered')
    failures = [original, RuntimeFailure('Bluetooth service rejected OpenRestricted')]
    events = []
    async def start(self, current):
        failure = failures.pop(0)
        if failure is original:
            current.bluetooth_admission = object()
            current.bluetooth_handoff = object()
            current.launch_generation = 'a'*32
            current.processes['bluetooth-output'] = object()
            self._startup_stage(current.desired.id, 'persist_process:bluetooth-output')
        events.append(failure)
        raise failure
    monkeypatch.setattr(fixture.Broker, '_start_room', start)
    async def stop(current):
        # The production catch still owns its cleanup. Inspect the receipt
        # BEFORE erasing resources, rather than reconstructing them afterwards.
        first = broker.startup_diagnostics['first_failure']
        assert first is not None and first['failure']['captured_before_cleanup'] is True
        events.append('cleanup')
        current.bluetooth_admission = current.bluetooth_handoff = None
        current.processes.clear()
        broker._closing = True
    broker._stop_room = stop
    for _ in range(2):
        broker._closing = False
        room.wake.set()
        await asyncio.wait_for(broker._room_loop(room), .5)
    assert events[0] is original and events[1::2] == ['cleanup', 'cleanup']
    receipt = broker.startup_diagnostics
    assert receipt['first_failure']['stage'] == 'persist_process:bluetooth-output'
    first = receipt['first_failure']['failure']
    assert first['bluetooth_admission_present'] and first['bluetooth_handoff_present']
    assert first['started_roles'] == ['bluetooth-output']
    assert first['launch_generation'] == 'a'*32
    assert first['causes'] == [{'type': 'PermissionError', 'timeout': False, 'errno': 13}]
    assert '[redacted]' in first['message'] and 'private-' not in first['message']
    assert len(receipt['attempts']) == 2 and not broker._startup_current
    assert receipt['attempts'][1]['failure']['message'] == 'Bluetooth service rejected OpenRestricted'
    assert receipt['first_failure']['failure']['message'] != receipt['attempts'][1]['failure']['message']
    assert room.status == 'error' and room.failures == 2


async def test_startup_observation_rethrows_exact_exception_without_repeating_operation(monkeypatch):
    fixture = startup_fixture()
    broker = observed_broker(fixture, monkeypatch)
    room = SimpleNamespace(desired=SimpleNamespace(id='room', revision=1), bluetooth_admission=None,
        bluetooth_handoff=None, client=None, processes={}, launch_generation=None)
    expected = RuntimeFailure('first exact failure')
    calls = []
    async def start(self, current):
        calls.append(current)
        raise expected
    monkeypatch.setattr(fixture.Broker, '_start_room', start)
    with pytest.raises(RuntimeFailure) as raised:
        await broker._start_room(room)
    assert raised.value is expected and calls == [room]
    assert broker.startup_diagnostics['first_failure']['failure']['bluetooth_admission_present'] is False


async def test_success_and_cancellation_are_not_promoted_or_retried(monkeypatch):
    fixture = startup_fixture()
    broker = observed_broker(fixture, monkeypatch)
    room = SimpleNamespace(desired=SimpleNamespace(id='room', revision=1), bluetooth_admission=None,
        bluetooth_handoff=None, client=None, processes={}, launch_generation=None)
    returned = object()
    async def start(self, current):
        return returned
    monkeypatch.setattr(fixture.Broker, '_start_room', start)
    assert await broker._start_room(room) is returned
    assert broker.startup_diagnostics['first_failure'] is None
    cancelled = asyncio.CancelledError()
    async def abort(self, current):
        raise cancelled
    monkeypatch.setattr(fixture.Broker, '_start_room', abort)
    with pytest.raises(asyncio.CancelledError) as raised:
        await broker._start_room(room)
    assert raised.value is cancelled and not broker._startup_current
    assert broker.startup_diagnostics['attempts'][0]['outcome'] == 'returned'


async def test_retry_diagnostic_history_is_bounded_and_first_failure_is_independent(monkeypatch):
    fixture = startup_fixture()
    broker = observed_broker(fixture, monkeypatch)
    room = SimpleNamespace(desired=SimpleNamespace(id='room', revision=1), bluetooth_admission=None,
        bluetooth_handoff=None, client=None, processes={}, launch_generation=None)
    async def start(self, current):
        for index in range(100):
            self._startup_stage(current.desired.id, 'known_stage:'+str(index))
        raise RuntimeFailure('é'*10000)
    monkeypatch.setattr(fixture.Broker, '_start_room', start)
    for _ in range(11):
        with pytest.raises(RuntimeFailure):
            await broker._start_room(room)
    history = broker.startup_diagnostics
    assert len(history['attempts']) == 8 and history['omitted_attempts'] == 3
    assert all(len(row['stages']) == 32 and row['omitted_stages'] == 68 for row in history['attempts'])
    assert len(history['first_failure']['failure']['message'].encode()) <= 1024
    before = json.dumps(history['first_failure'])
    history['attempts'][0]['failure']['message'] = 'later mutation'
    assert json.dumps(history['first_failure']) == before
    assert len(json.dumps(history).encode()) < 64*1024


def test_retained_artifact_embeds_first_broker_failure_and_exact_bounded_debug_log(tmp_path):
    original, destination = daemon(tmp_path)
    first = {'first_failure': {'failure': {'type': 'PermissionError', 'message': 'original failure'}},
             'attempts': [], 'omitted_attempts': 0}
    original.broker_startup_diagnostics = first
    payload = b'Acquire succeeded\nBridge launch failed\nRelease observed\n' + b'\xff'
    log(original, payload)
    try:
        receipt = diagnostics.retain(destination, original)
        original.broker_startup_diagnostics['first_failure']['failure']['message'] = 'later retry'
        assert receipt['broker_startup']['first_failure']['failure']['message'] == 'original failure'
        assert receipt['log']['tail_text'] == payload.decode('utf-8', errors='replace')
        saved = json.loads(Path(receipt['path']).read_bytes())
        assert saved['broker_startup'] == receipt['broker_startup']
        assert saved['log']['sha256'] == hashlib.sha256(payload).hexdigest()
    finally:
        os.close(original.directory_fd)

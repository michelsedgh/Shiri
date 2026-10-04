"""Independent retirement failures must not abandon the worker's resources."""

import asyncio
import fcntl
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shiri.runtime import audio, backend, native as native_runtime, speech_output
from shiri.runtime.audio import AudioWorker, SpeechSession
from test_audio import FakePeer, RecordingMixer
from test_room_warm_audio import Native


async def test_failed_warm_retirement_still_closes_peer_native_and_mixer():
    mixer, native, peer = RecordingMixer(), Native(), FakePeer()
    native.release_warm_connection = AsyncMock(side_effect=OSError("backend unavailable"))
    worker = AudioWorker(mixer, native=native)
    worker._warm_deadlines["1" * 32] = 2**63 - 1
    session = SpeechSession("voice", "request", peer, 0.28)
    worker.session = session

    with pytest.raises(OSError, match="backend unavailable"):
        await worker.close()

    assert mixer.closed and native.closed
    assert session.disposed and peer.close_calls == 1
    assert worker.session is None and not worker._warm_deadlines


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_run_unlinks_under_worker_lock_even_after_cleanup_failure(tmp_path, monkeypatch, cleanup_fails):
    socket_path = tmp_path / "audio.sock"
    lock_path = str(socket_path) + ".lock"
    descriptors = []
    original_open = os.open
    original_unlink = Path.unlink
    unlinked_under_lock = []

    def tracked_open(path, *args, **kwargs):
        descriptor = original_open(path, *args, **kwargs)
        if path == lock_path:
            descriptors.append(descriptor)
        return descriptor

    def checked_unlink(path, *args, **kwargs):
        if path == socket_path:
            contender = original_open(lock_path, os.O_RDWR)
            try:
                try:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    unlinked_under_lock.append(True)
                else:
                    unlinked_under_lock.append(False)
            finally:
                os.close(contender)
        return original_unlink(path, *args, **kwargs)

    class Worker:
        def __init__(self, *_args, **_kwargs):
            pass

        async def dispatch(self, *_args):
            pass

        async def tick(self, _elapsed):
            raise RuntimeError("stop fixture")

        async def close(self):
            if cleanup_fails:
                raise OSError("cleanup failure")

    server = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())

    async def serve(*_args, **_kwargs):
        socket_path.touch()
        return server

    monkeypatch.setattr(audio.os, "open", tracked_open)
    monkeypatch.setattr(Path, "unlink", checked_unlink)
    monkeypatch.setattr(backend, "OwnToneClient", lambda *_args, **_kwargs: SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(native_runtime, "NativeMixer", lambda *_args, **_kwargs: RecordingMixer())
    monkeypatch.setattr(native_runtime, "NativeController", lambda *_args, **_kwargs: SimpleNamespace(
        initialize=AsyncMock(), listen=AsyncMock(), close=AsyncMock()))
    monkeypatch.setattr(speech_output, "SpeechOutput", lambda *_args: SimpleNamespace(open=lambda: None, close=lambda: None))
    monkeypatch.setattr(audio, "AudioWorker", Worker)
    monkeypatch.setattr(audio, "serve_rpc", serve)
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)
    credential = tmp_path / "credential.json"
    credential.write_text('{"password":"fixture-password-private"}')
    credential.chmod(0o600)
    args = SimpleNamespace(signal=False, socket=socket_path, room_dir=tmp_path,
                           own_password_file=credential, own_url="http://fixture.invalid",
                           room_id="fixture", native_socket=None, native_uid=os.getuid(),
                           speech_socket=tmp_path / "speech.sock", speech_launch_generation="1"*32,
                           output_uid=os.getuid(), output_buffer_ms=40, relay_delay_ms=140,
                           control_revision=1, control_volume=50, signal_socket=None)
    try:
        error, message = (OSError, "cleanup failure") if cleanup_fails else (RuntimeError, "stop fixture")
        with pytest.raises(error, match=message):
            await audio.run(args)
        assert not socket_path.exists()
        assert unlinked_under_lock == [True]
        # A second worker must be able to acquire the same lock after failure.
        descriptor = original_open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
    finally:
        # Keep the reproduction itself leak-free against the broken version.
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

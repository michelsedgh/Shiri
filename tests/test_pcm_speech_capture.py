"""Capture every room-runtime sample over real private sockets, without audio devices.

The readiness service is controlled here; the RPC server, PCM validation,
NativeMixer, authenticated datagram producer and socket receiver are real.
This proves the Python boundary preserves a prefix and streams before EOF.
It does not claim that OwnTone or a physical speaker rendered these samples.
"""

import asyncio
import base64
import os
from pathlib import Path
import socket
import struct
from tempfile import TemporaryDirectory
import time
from uuid import UUID

import pytest

from shiri.rpc import call_rpc, serve_rpc
from shiri.runtime.audio import AudioWorker
from shiri.runtime.speech_output import HEADER, HEADER_BYTES, PCM, SpeechOutput
from test_pcm_speech import ending, opening
from test_speech_startup import native_controller


@pytest.mark.parametrize("setup_seconds", [0.0, 0.62], ids=["warm", "cold"])
async def test_real_rpc_and_datagrams_preserve_entire_paced_speech_before_eof(setup_seconds):
    native, writer, client, overlay = await native_controller()
    worker = AudioWorker(native.mixer, native=native)
    with TemporaryDirectory(prefix="shiri-prefix-capture-", dir="/tmp") as directory:
        root = Path(directory)
        output_directory = root / "overlay"
        output_directory.mkdir()
        output_uid = os.getuid() or 1001
        os.chown(output_directory, output_uid if os.getuid() == 0 else -1, os.getgid())
        output_directory.chmod(0o2710)
        output_path = output_directory / "speech.sock"
        receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        receiver.bind(str(output_path))
        os.chown(output_path, output_uid if os.getuid() == 0 else -1, os.getgid())
        output_path.chmod(0o660)
        receiver.setblocking(False)
        output = SpeechOutput(output_path, str(UUID(bytes=overlay.room)),
                              UUID(bytes=overlay.launch).hex, output_uid)
        native.mixer.speech_output = output
        rpc_path = root / "audio.sock"
        server = await serve_rpc(rpc_path, worker.dispatch)
        captured = []
        first_pcm = asyncio.Event()
        producer_finished = False
        frames = 230  # The reported 4.6-second utterance, as unique 20 ms PCM frames.
        packet_samples = 960
        original = [struct.pack("<h", index + 1000) * packet_samples for index in range(frames)]
        expected = b"".join(original)

        async def capture():
            loop = asyncio.get_running_loop()
            while len(captured) < frames:
                message = await loop.sock_recv(receiver, HEADER_BYTES + 1920)
                header = HEADER.unpack(message[:HEADER_BYTES])
                assert header[6:8] == (overlay.room, overlay.launch)
                assert header[13] == UUID(hex=native.mixer.speech_preparation.speech_id).bytes
                if header[2] == PCM:
                    assert header[9] == packet_samples
                    captured.append((header, message[HEADER_BYTES:], producer_finished))
                    first_pcm.set()

        capturing = asyncio.create_task(capture(), name="silent-datagram-capture")
        preparing = asyncio.create_task(call_rpc(rpc_path, "speech", opening()))
        try:
            await asyncio.wait_for(client.prepare_entered.wait(), 1)
            if setup_seconds:
                await asyncio.sleep(setup_seconds)
            assert not captured and not output.sent_frames
            client.connect()
            client.first_mix()
            prepared = await asyncio.wait_for(preparing, 1)
            assert not captured and not writer.packets
            started_ns = time.monotonic_ns()
            for index, pcm in enumerate(original):
                deadline_ns = started_ns + index * 20_000_000
                delay = (deadline_ns - time.monotonic_ns()) / 1e9
                if delay > 0:
                    await asyncio.sleep(delay)
                await call_rpc(rpc_path, "speech", {
                    "action": "pcm", "session_id": prepared["session_id"],
                    "request_id": prepared["request_id"], "stream_id": prepared["stream_id"],
                    "sequence": index + 1, "frame_index": index * packet_samples,
                    "pcm_base64": base64.b64encode(pcm).decode("ascii"),
                })
                if index == 0:
                    await asyncio.wait_for(first_pcm.wait(), 1)
                    assert captured[0][1] == original[0]
                    assert len(captured) == 1 and not producer_finished
                await worker.tick(0.02)
            producer_finished = True
            result = await call_rpc(rpc_path, "speech", ending(
                prepared, sequence=frames, frames=frames * packet_samples,
            ))
            await asyncio.wait_for(capturing, 1)
            assert result["admitted_frames"] == frames * packet_samples
            assert b"".join(packet for _, packet, _ in captured) == expected
            assert captured[0][2] is False
            assert output.sent_frames == frames * packet_samples
            assert output.dropped_frames == 0 and not writer.packets
            assert worker.session is None and native.mixer.speech_preparation.retired
        finally:
            server.close()
            await server.wait_closed()
            capturing.cancel()
            preparing.cancel()
            await asyncio.gather(capturing, preparing, return_exceptions=True)
            await worker.close()
            receiver.close()

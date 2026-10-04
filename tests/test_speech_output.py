"""Exercise real datagrams, bounded pressure and private endpoint admission."""
from array import array
import errno
import os
from pathlib import Path
import socket
import tempfile
from uuid import uuid4

import pytest

from shiri.rpc import RpcError
from shiri.runtime.speech_output import CONTROL, HEADER, HEADER_BYTES, MAGIC, PCM, SpeechOutput


@pytest.fixture
def endpoint():
    temporary = tempfile.TemporaryDirectory(prefix='shiri-tts-', dir='/tmp')
    directory = Path(temporary.name)/'overlay'
    directory.mkdir(mode=0o2710)
    # Root-run Linux tests still admit a real non-root output-owned endpoint.
    # The sender's actual primary group remains the checked traverse/send group.
    output_uid = os.getuid() or 1001
    os.chown(directory, output_uid if os.getuid() == 0 else -1, os.getgid())
    directory.chmod(0o2710)
    path = directory/'speech.sock'
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(path))
    os.chown(path, output_uid if os.getuid() == 0 else -1, os.getgid())
    path.chmod(0o660)
    server.settimeout(0.1)
    sender = SpeechOutput(path, str(uuid4()), uuid4().hex, output_uid, now_ns=lambda: 17_000_000_000)
    sender.begin(uuid4().hex)
    try:
        yield sender, server
    finally:
        sender.close()
        server.close()
        temporary.cleanup()


def test_real_speech_socket_carries_exact_launch_order_pcm_and_gain_without_music_commands(endpoint):
    sender, server = endpoint
    sender.control(True, 0.2)
    header = HEADER.unpack(server.recv(4096))
    assert header == (MAGIC, 2, CONTROL, HEADER_BYTES, 0, 1, sender.room, sender.launch,
                      17_000_000_000, 0, round(0.2*65536), 1, 0, sender.owner)
    original = array('h', range(1920)).tobytes()
    assert sender.push(original, 1920)
    messages = [server.recv(4096), server.recv(4096)]
    headers = [HEADER.unpack(message[:HEADER_BYTES]) for message in messages]
    assert [header[5] for header in headers] == [2, 3]
    assert all(header[2] == PCM and header[4] == 1920 and header[9] == 960 for header in headers)
    assert b''.join(message[HEADER_BYTES:] for message in messages) == original
    assert sender.health()['speech_sent_frames'] == 1920


def test_text_envelope_is_identical_on_early_control_pcm_and_release(endpoint):
    sender, server = endpoint
    sender.retire(sender.owner.hex())
    voice = uuid4().hex
    sender.begin(voice, attack_ms=300, release_ms=600)
    sender.control(True, .2)
    sender.push((1000).to_bytes(2, 'little', signed=True) * 960, 960)
    sender.control(False, .2)
    headers = [HEADER.unpack(server.recv(4096)[:HEADER_BYTES]) for _ in range(3)]
    assert [h[2] for h in headers] == [CONTROL, PCM, CONTROL]
    assert [h[11] for h in headers] == [1, 1, 0]
    assert all(h[12] == (300 << 16) | 600 for h in headers)
    assert all(h[13] == sender.owner for h in headers)
    assert sender.first_active_control_ns == 17_000_000_000
    with pytest.raises(RpcError, match='envelope cannot change'):
        sender.begin(voice, attack_ms=200, release_ms=600)
    assert sender.envelope == (300 << 16) | 600


@pytest.mark.parametrize('attack,release', [(True, 600), (300, True), (0, 600), (300, 0),
                                           (39, 600), (2001, 600), (300, 39), (300, 5001)])
def test_invalid_envelope_preserves_existing_owner_and_sequence(endpoint, attack, release):
    sender, _server = endpoint
    before = sender.owner, sender.sequence, sender.envelope
    with pytest.raises(ValueError, match='bounded attack'):
        sender.begin(sender.owner.hex(), attack_ms=attack, release_ms=release)
    assert (sender.owner, sender.sequence, sender.envelope) == before


def test_missing_and_replaced_speech_endpoint_never_builds_a_local_voice_backlog(endpoint):
    sender, server = endpoint
    server.close()
    sender.path.unlink()
    assert not sender.push(bytes(1920), 960)
    assert sender.socket is None and sender.dropped_frames == 960
    sender.path.write_bytes(b'not a socket')
    assert not sender.push(bytes(1920), 960)
    assert sender.error == 'PermissionError' and sender.dropped_frames == 1920
    assert sender.sent_frames == 0


def test_full_real_datagram_queue_is_nonblocking_and_recovery_keeps_strict_sequence(endpoint):
    sender, server = endpoint
    accepted = 0
    for _ in range(1000):
        if not sender.push(bytes(1920), 960):
            break
        accepted += 1
    else:
        pytest.fail('A real undrained datagram queue did not apply backpressure')
    assert sender.error_errno in {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS}
    assert sender.socket is None
    server.setblocking(False)
    sequences = []
    while True:
        try:
            sequences.append(HEADER.unpack(server.recv(4096)[:HEADER_BYTES])[5])
        except BlockingIOError:
            break
    assert len(sequences) == accepted and sequences == list(range(1, accepted+1))
    assert sender.control(False, 0.2)
    header = HEADER.unpack(server.recv(4096))
    assert header[5] == accepted+2 and header[11] == 0


@pytest.mark.parametrize('mode', [0o700, 0o2770, 0o2750, 0o777])
def test_audio_sender_cannot_admit_an_endpoint_in_a_replaceable_or_unbound_directory(endpoint, mode):
    sender, _server = endpoint
    sender.path.parent.chmod(mode)
    with pytest.raises(PermissionError):
        sender.open()
    assert sender.socket is None


@pytest.mark.parametrize('samples,data', [(0, b''), (True, b'00'), (9601, bytes(19202)), (960, b'bad')])
def test_invalid_frame_never_consumes_a_sequence_or_opens_a_channel(endpoint, samples, data):
    sender, _server = endpoint
    with pytest.raises(RpcError):
        sender.push(data, samples)
    assert sender.sequence == 0 and sender.socket is None


@pytest.mark.parametrize('gain', [float('nan'), float('inf'), -0.01, 1.01, True])
def test_invalid_gain_never_mutates_the_backend_lease(endpoint, gain):
    sender, _server = endpoint
    with pytest.raises(ValueError):
        sender.control(True, gain)
    assert sender.sequence == 0 and not sender.active


def test_room_launch_and_path_are_exact_private_identities():
    with pytest.raises(ValueError):
        SpeechOutput(Path('/tmp/voice.sock'), str(uuid4()).upper(), uuid4().hex, 1000)
    with pytest.raises(ValueError):
        SpeechOutput(Path('/tmp/voice.sock'), str(uuid4()), str(uuid4()), 1000)
    with pytest.raises(ValueError):
        SpeechOutput(Path('voice.sock'), str(uuid4()), uuid4().hex, 1000)


@pytest.mark.parametrize('uid', [0, -1, True, 2**32])
def test_output_uid_requires_exact_nonroot_kernel_identity(uid):
    with pytest.raises(ValueError):
        SpeechOutput(Path('/tmp/voice.sock'), str(uuid4()), uuid4().hex, uid)


def test_quiet_packet_does_not_inherit_audibility_from_later_decoded_speech(endpoint):
    sender, server = endpoint
    sender.control(True, 0.1)
    server.recv(4096)
    pcm = bytes(1920)+array('h', [1000]*960).tobytes()
    assert sender.push(pcm, 1920)
    first, second = (HEADER.unpack(server.recv(4096)[:HEADER_BYTES]) for _ in range(2))
    assert first[11] == 0 and second[11] == 1
    assert first[10] == second[10] == round(0.1*65536)


def test_audible_pcm_invalidates_old_inactive_control_so_short_eof_is_delivered(endpoint):
    sender, server = endpoint
    assert sender.control(False, 0.28)
    server.recv(4096)
    assert sender.push(array('h', [1000]*960).tobytes(), 960)
    server.recv(4096)
    assert sender.control(False, 0.28)
    eof = HEADER.unpack(server.recv(4096))
    assert eof[2] == CONTROL and eof[11] == 0 and eof[5] == 3
    assert sender.sent_controls == 2


def test_active_controls_are_bounded_and_quiet_pcm_cannot_extend_the_control_clock(endpoint):
    sender, server = endpoint
    clock = [17_000_000_000]
    sender.now_ns = lambda: clock[0]
    assert sender.control(True, 0.1)
    server.recv(4096)
    clock[0] += 99_000_000
    assert sender.push(bytes(1920), 960)
    quiet = HEADER.unpack(server.recv(4096)[:HEADER_BYTES])
    assert quiet[11] == 0
    assert sender.control(True, 0.1) and sender.sent_controls == 1
    clock[0] += 1_000_000
    assert sender.control(True, 0.1)
    assert HEADER.unpack(server.recv(4096))[2] == CONTROL
    assert sender.sent_controls == 2

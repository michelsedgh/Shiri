from dataclasses import replace
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.runtime.timing import (
    Clock, FLAG_GAP, FLAG_SPEECH_ONLY, FramedFifoWriter, HEADER_BYTES,
    Kind, MAX_FRAMES, Packet, RELAY_DELAY_NS, StreamFence, TimingError, ZERO_UUID,
    map_native_time, output_packet,
)


def native(**changes):
    return replace(Packet(Kind.PCM, uuid4().bytes, frames=480, pcm=bytes(1920),
                          presentation_ns=10_150_000_000, clock_sample_ns=10_000_000_000,
                          monotonic_before_ns=15_000_000_000, monotonic_after_ns=15_000_000_200), **changes)


def test_cross_clock_wire_roundtrip_and_exact_sample_time():
    packet = native(sequence=73, rtp=2**32 - 30, group=uuid4().bytes, epoch=3)
    assert Packet.decode(packet.encode()) == packet
    mapped = map_native_time(packet, now_ns=15_000_000_500)
    assert mapped.monotonic_ns == 15_150_000_100
    assert mapped.uncertainty_ns == 75_100
    token = SimpleNamespace(session_id=packet.session_id, incarnation=str(uuid4()), epoch=7)
    output, _ = output_packet(packet, packet.pcm, token, now_ns=15_000_000_500)
    assert output.presentation_ns == mapped.monotonic_ns + RELAY_DELAY_NS
    assert (output.rtp, output.group, output.frame_index, output.frames) == (packet.rtp, packet.group, 0, 480)
    assert Packet.decode(output.encode()) == output


@pytest.mark.parametrize("field,value", [(0, b"OTHERPCM"), (9,b"\x03"), (12,b"\0\0\0\x9f"),
                                        (16,b"\0\0\0\0"), (20,b"\0\0\xacD"), (148,b"\x01")])
def test_wire_malformed_fields_are_rejected(field, value):
    message = bytearray(native().encode())
    message[field:field+len(value)] = value
    with pytest.raises(TimingError):
        Packet.decode(bytes(message))


@pytest.mark.parametrize("changes", [dict(monotonic_after_ns=15_001_000_001),
                                    dict(monotonic_before_ns=15_000_000_700),
                                    dict(clock_sample_ns=0),dict(presentation_ns=12_000_000_001)])
def test_no_startup_offset_or_unbounded_clock_projection(changes):
    with pytest.raises(TimingError):
        map_native_time(native(**changes), now_ns=15_000_000_500)
    with pytest.raises(TimingError):
        map_native_time(native(), now_ns=15_300_000_500)


def test_two_zone_independent_arrivals_preserve_same_native_deadline():
    group = uuid4().bytes
    a = native(group=group)
    b = native(group=group, clock_sample_ns=10_050_000_000,
               monotonic_before_ns=15_050_000_000, monotonic_after_ns=15_050_000_200)
    # Starts/queue ordering need not match; a common receiver clock deadline does.
    assert map_native_time(a, now_ns=15_000_000_500).monotonic_ns == map_native_time(b, now_ns=15_050_000_800).monotonic_ns


def test_flush_end_and_sequence_do_not_allow_old_pcm_or_reused_rtp():
    p = native(sequence=1)
    fence = StreamFence(p.session)
    fence.accept(p)
    with pytest.raises(TimingError):
        fence.accept(p)
    with pytest.raises(TimingError):
        fence.accept(replace(p,sequence=3,frame_index=960))
    fence.accept(replace(p,sequence=3,frame_index=960,flags=FLAG_GAP,presentation_ns=p.presentation_ns+20_000_000))
    flush = Packet(Kind.FLUSH,p.session,generation=2)
    fence.flush(flush)
    with pytest.raises(TimingError):
        fence.accept(replace(p,sequence=4,frame_index=1440))
    fence.accept(replace(p,generation=2))
    fence.end(Packet(Kind.END,p.session,generation=2))
    with pytest.raises(TimingError):
        fence.accept(replace(p,generation=2,sequence=2,frame_index=480))
    assert fence.gaps == 1


def read_packets(fd):
    data = os.read(fd,65536)
    packets = []
    while data:
        payload = int.from_bytes(data[16:20],"big")
        size = HEADER_BYTES + payload
        packets.append(Packet.decode(data[:size]))
        data = data[size:]
    return packets


def test_fifo_splits_atomic_packets_preserving_subblock_first_sample_time(tmp_path):
    path = tmp_path/'audio.pipe'
    os.mkfifo(path,0o600)
    reader = os.open(path,os.O_RDONLY|os.O_NONBLOCK)
    writer = FramedFifoWriter(path)
    frames = writer.maximum_frames + 1
    if frames > MAX_FRAMES:
        writer.close()
        os.close(reader)
        pytest.skip('Every valid native packet fits this platform atomic FIFO bound')
    p = native(clock=Clock.MONOTONIC,frames=frames,pcm=bytes(frames * 4))
    writer.reset((p.incarnation,p.session,p.epoch,p.generation))
    assert writer.write(p)
    pieces = read_packets(reader)
    assert sum(x.frames for x in pieces) == frames
    assert len(pieces) > 1
    for piece in pieces:
        assert len(piece.encode()) <= os.pathconf(path,'PC_PIPE_BUF')
        assert piece.presentation_ns == p.presentation_ns + piece.frame_index*1_000_000_000//48000
        assert piece.rtp == p.rtp
    writer.close()
    os.close(reader)


def test_no_reader_is_bounded_drop_then_explicit_gap_and_exact_owner(tmp_path):
    path = tmp_path/'audio.pipe'
    os.mkfifo(path,0o600)
    writer = FramedFifoWriter(path)
    p = native(clock=Clock.MONOTONIC)
    owner = (p.incarnation,p.session,p.epoch,p.generation)
    writer.reset(owner)
    assert writer.write(p) is False
    assert writer.dropped_bytes == len(p.pcm)
    reader = os.open(path,os.O_RDONLY|os.O_NONBLOCK)
    assert writer.write(replace(p,frame_index=480))
    assert read_packets(reader)[0].flags & FLAG_GAP
    with pytest.raises(TimingError):
        writer.write(replace(p,generation=2))
    writer.close()
    os.close(reader)


def test_idle_speech_does_not_invent_music_identity():
    idle = Packet(Kind.PCM,ZERO_UUID,incarnation=uuid4().bytes,clock=Clock.MONOTONIC,
                  flags=FLAG_SPEECH_ONLY,frames=480,pcm=bytes(1920),presentation_ns=19_000_000_000)
    assert Packet.decode(idle.encode()).session == ZERO_UUID
    with pytest.raises(TimingError):
        replace(idle,flags=0)


@pytest.mark.parametrize('change',[{'presentation_ns':10_150_000_000}, {'presentation_ns':10_140_000_000}, {'clock':Clock.MONOTONIC}])
def test_presentation_replay_backward_clock_or_clock_kind_switch_requires_flush(change):
    packet = native(sequence=1)
    fence = StreamFence(packet.session)
    fence.accept(packet)
    next_packet = replace(packet,sequence=2,frame_index=480,presentation_ns=10_160_000_000,**({} if 'presentation_ns' in change else change))
    if 'presentation_ns' in change:
        next_packet=replace(next_packet,**change)
    with pytest.raises(TimingError,match='presentation clock'):
        fence.accept(next_packet)
    fence.flush(Packet(Kind.FLUSH,packet.session,generation=2))
    fence.accept(replace(next_packet,generation=2))

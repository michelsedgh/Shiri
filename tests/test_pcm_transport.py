"""Final-output wire and retired-generation guards, without physical audio."""
from dataclasses import replace
import struct
from uuid import uuid4

import pytest

from shiri.runtime.bluealsa import PCMEndpoint
from shiri.runtime.pcm_transport import cadence_bound_ns, Frame, HEADER, HEADER_BYTES, Operation, StreamState
from shiri.runtime.system import RuntimeFailure


def endpoint():
    return PCMEndpoint(":1.22", "/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsrc/sink",
                       "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF", "AA:BB:CC:DD:EE:FF", 12,
                       0x8210, "S16LE", 2, 2, 48000, "SBC", True, True)


def start():
    return Frame(Operation.START, str(uuid4()), uuid4().hex, str(uuid4()), 1, 0, 0, 48000, 0x8210, 2)


def data(initial, *, sequence=1, first_frame=0, frames=960, pts_ns=None, generation=1):
    if pts_ns is None:
        pts_ns = 10_000_000_000 + first_frame * 1_000_000_000 // initial.rate
    return replace(initial, operation=Operation.DATA, sequence=sequence, generation=generation,
                   first_frame=first_frame, frames=frames, pts_ns=pts_ns, payload=b"\x01\x02\x03\x04" * frames)


def test_wire_offsets_match_independent_native_contract():
    initial = replace(start(), room_id="00112233-4455-6677-8899-aabbccddeeff",
                      launch="112233445566778899aabbccddeeff00", stream="22334455-6677-8899-aabb-ccddeeff0011")
    packet = data(initial, sequence=7, first_frame=123, frames=2).encode()
    assert HEADER.size == HEADER_BYTES == 112 and packet[:8] == b"SHRIOUT1"
    assert struct.unpack_from("!HHI", packet, 8) == (1, 2, 112)
    assert packet[16:32].hex() == "00112233445566778899aabbccddeeff"
    assert packet[32:48].hex() == initial.launch
    assert struct.unpack_from("!QQQ", packet, 64) == (1, 7, 10_002_562_500)
    assert struct.unpack_from("!IHHIIQ", packet, 88) == (48000, 0x8210, 2, 2, 8, 123)
    assert Frame.decode(packet).encode() == packet


@pytest.mark.parametrize("operation", [Operation.START, Operation.READY, Operation.FLUSH, Operation.END])
def test_control_round_trip_contains_no_pcm(operation):
    initial = start()
    control = replace(initial, operation=operation,
                      generation=2 if operation in {Operation.FLUSH, Operation.END} else 1,
                      sequence=1 if operation in {Operation.FLUSH, Operation.END} else 0)
    result = Frame.decode(control.encode())
    assert result.operation == operation and not result.payload and not result.frames


@pytest.mark.parametrize("field,value", [("generation", 0), ("generation", True), ("sequence", -1),
                                        ("pts_ns", 0), ("frames", 0), ("frames", 961), ("channels", 0),
                                        ("format_code", 0xFFFF), ("rate", 0), ("first_frame", True),
                                        ("room_id", "00000000-0000-0000-0000-000000000000")])
def test_data_header_rejects_invalid_counts_caps_and_identities(field, value):
    with pytest.raises(RuntimeFailure):
        replace(data(start()), **{field: value}).encode()


@pytest.mark.parametrize("mutation", ["version", "magic", "short", "extra", "length", "operation", "oversize"])
def test_untrusted_wire_bytes_require_one_complete_bounded_packet(mutation):
    packet = bytearray(data(start()).encode())
    if mutation == "version":
        packet[9] = 2
    elif mutation == "magic":
        packet[0] = 0
    elif mutation == "short":
        packet = packet[:111]
    elif mutation == "extra":
        packet.extend(b"\x00")
    elif mutation == "length":
        packet[103] ^= 1
    elif mutation == "operation":
        packet[11] = 7
    else:
        packet.extend(b"x" * 16385)
    with pytest.raises(RuntimeFailure):
        Frame.decode(bytes(packet))


def test_flush_has_no_ack_until_the_selected_pcm_drop_completes():
    initial = start()
    state = StreamState(endpoint(), initial.room_id, initial.launch)
    assert state.accept(initial).operation == Operation.READY
    assert state.accept(data(initial)) is None
    flush = replace(initial, operation=Operation.FLUSH, generation=2, sequence=2)
    assert state.accept(flush) is None
    assert state.generation == 1 and state.pending_control is flush
    ack = state.dropped()
    assert ack.operation == Operation.DROP_ACK and ack.frames == 3
    assert ack.generation == 2 and ack.sequence == 2 and not ack.payload
    assert state.accept(data(initial, sequence=3, generation=2, pts_ns=20_000_000_000)) is None


def test_data_arriving_during_drop_fails_closed_and_cannot_make_late_ack_release_it():
    initial = start()
    state = StreamState(endpoint(), initial.room_id, initial.launch)
    state.accept(initial)
    state.accept(replace(initial, operation=Operation.FLUSH, generation=2, sequence=1))
    with pytest.raises(RuntimeFailure, match="pending a Drop"):
        state.accept(data(initial, sequence=2, generation=2))
    with pytest.raises(RuntimeFailure, match="pending a Drop"):
        state.dropped()


@pytest.mark.parametrize("fault", ["other-room", "old-launch", "other-stream", "lost-sequence",
                                  "repeated-frame", "skipped-frame", "old-generation", "wrong-rate", "missing-time"])
def test_successor_pcm_cannot_repair_a_failed_room_or_generation_boundary(fault):
    initial = start()
    state = StreamState(endpoint(), initial.room_id, initial.launch)
    state.accept(initial)
    state.accept(data(initial))
    correct = data(initial, sequence=2, first_frame=960)
    changes = {"other-room": {"room_id": str(uuid4())}, "old-launch": {"launch": str(uuid4())},
               "other-stream": {"stream": str(uuid4())}, "lost-sequence": {"sequence": 3},
               "repeated-frame": {"first_frame": 0}, "skipped-frame": {"first_frame": 1920},
               "old-generation": {"generation": 2},
               "wrong-rate": {"rate": 44100}, "missing-time": {"pts_ns": correct.pts_ns + 20_000_000}}
    with pytest.raises(RuntimeFailure):
        state.accept(replace(correct, **changes[fault]))
    with pytest.raises(RuntimeFailure):
        state.accept(correct)
    assert state.next_frame == 960


def test_end_ack_names_end_and_new_packets_cannot_revive_that_stream():
    initial = start()
    state = StreamState(endpoint(), initial.room_id, initial.launch)
    state.accept(initial)
    ending = replace(initial, operation=Operation.END, generation=2, sequence=1)
    state.accept(ending)
    ack = state.dropped()
    assert ack.frames == 4 and state.ended
    with pytest.raises(RuntimeFailure, match="ended"):
        state.accept(data(initial, sequence=2, generation=2))


def test_integer_sample_period_rounding_does_not_accumulate_packet_clock_error():
    initial = replace(start(), rate=44100)
    admitted = replace(endpoint(), rate=44100)
    state = StreamState(admitted, initial.room_id, initial.launch)
    state.accept(initial)
    for sequence in range(1, 101):
        state.accept(data(initial, sequence=sequence, first_frame=(sequence - 1) * 352, frames=352))
    assert state.next_frame == 35200


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("rate,frames,limit", [(48000, 960, 3_110_001), (44100, 352, 3_103_991),
                                              (192000, 1, 3_100_003)])
def test_bounded_native_clock_correction_accepts_exact_positive_pts_at_the_wire_limit(sign, rate, frames, limit):
    initial, admitted = replace(start(), rate=rate), replace(endpoint(), rate=rate)
    state = StreamState(admitted, initial.room_id, initial.launch)
    state.accept(initial)
    state.accept(data(initial, frames=frames))
    elapsed = frames * 1_000_000_000 // rate
    assert cadence_bound_ns(elapsed) == limit  # Independently fixed wire contract values.
    correction = sign * limit
    assert state.accept(data(initial, sequence=2, first_frame=frames, frames=frames,
                             pts_ns=10_000_000_000 + elapsed + correction)) is None
    assert state.next_frame == 2 * frames


@pytest.mark.parametrize("sign", [-1, 1])
def test_one_nanosecond_outside_clock_bound_fails_sticky_without_consuming_frames(sign):
    initial = start()
    state = StreamState(endpoint(), initial.room_id, initial.launch)
    state.accept(initial)
    state.accept(data(initial))
    wrong = data(initial, sequence=2, first_frame=960,
                 pts_ns=10_020_000_000 + sign * (cadence_bound_ns(20_000_000) + 1))
    with pytest.raises(RuntimeFailure, match="discontinuous"):
        state.accept(wrong)
    with pytest.raises(RuntimeFailure, match="discontinuous"):
        state.accept(data(initial, sequence=2, first_frame=960))
    assert state.next_frame == 960


@pytest.mark.parametrize("sign", [-1, 1])
def test_per_packet_valid_clock_steps_cannot_accumulate_unbounded_origin_drift(sign):
    initial = start()
    state = StreamState(endpoint(), initial.room_id, initial.launch)
    state.accept(initial)
    state.accept(data(initial))
    state.accept(data(initial, sequence=2, first_frame=960, pts_ns=10_020_000_000 + sign * 3_000_000))
    with pytest.raises(RuntimeFailure, match="cumulative origin"):
        state.accept(data(initial, sequence=3, first_frame=1920, pts_ns=10_040_000_000 + sign * 6_000_000))
    assert state.next_frame == 1920


@pytest.mark.parametrize("sign", [-1, 1])
def test_long_running_clock_drift_uses_global_frame_counts_and_declared_500ppm(sign):
    initial = replace(start(), rate=44100)
    state = StreamState(replace(endpoint(), rate=44100), initial.room_id, initial.launch)
    state.accept(initial)
    for sequence in range(1, 1201):
        first_frame = (sequence - 1) * 352
        elapsed = first_frame * 1_000_000_000 // initial.rate
        state.accept(data(initial, sequence=sequence, first_frame=first_frame, frames=352,
                          pts_ns=10_000_000_000 + elapsed + sign * elapsed // 2000))
    assert state.next_frame == 1200 * 352


def test_completed_drop_resets_clock_origin_only_for_the_acknowledged_successor():
    initial = start()
    state = StreamState(endpoint(), initial.room_id, initial.launch)
    state.accept(initial)
    state.accept(data(initial))
    state.accept(replace(initial, operation=Operation.FLUSH, generation=2, sequence=2))
    assert state.origin_pts_ns == 10_000_000_000
    state.dropped()
    assert state.origin_pts_ns is None and state.last_pts_ns is None and state.last_first_frame == 0
    state.accept(data(initial, sequence=3, generation=2, pts_ns=20_000_000_000))
    assert state.origin_pts_ns == 20_000_000_000 and state.next_frame == 960


def test_start_and_converter_warmup_do_not_create_a_receive_time_clock_origin():
    initial = start()
    state = StreamState(endpoint(), initial.room_id, initial.launch)
    state.accept(initial)
    assert state.origin_pts_ns is None and state.next_frame == 0 and state.last_pts_ns is None
    # No DATA exists during converter warmup. The first actual converted frame
    # carries the producer's original native anchor; START's zero PTS is not it.
    first = data(initial, frames=425, pts_ns=22_750_000_000)
    state.accept(first)
    assert state.origin_pts_ns == 22_750_000_000 and state.next_frame == 425

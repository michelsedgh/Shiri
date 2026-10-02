"""Actual output C and independent Python receiver agree on the bounded wire."""
import importlib.util
from pathlib import Path
import struct

from shiri.runtime.pcm_transport import Frame, Operation

ROOT = Path(__file__).resolve().parents[1]


def test_actual_framed_output_pacing_flush_gain_and_cross_language_packets():
    path = ROOT / "tests/native/check_framed_output.py"
    spec = importlib.util.spec_from_file_location("framed_output_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = Frame(Operation.DATA, "00112233-4455-6677-8899-aabbccddeeff",
                     "112233445566778899aabbccddeeff00", "22334455-6677-8899-aabb-ccddeeff0011",
                     7, 129, 15_000_000_000, 48000, 0x8210, 2, 2, 413, b"\x01\x02\x03\x04" * 2)
    packet = original.encode()
    vectors = [(packet, True)]
    for offset, value in [(0, 0), (9, 2), (11, 7), (15, 111), (95, 0), (99, 0), (103, 0)]:
        bad = bytearray(packet)
        bad[offset] = value
        vectors.append((bytes(bad), False))
    for offset in [16, 32, 48]:
        bad = bytearray(packet)
        bad[offset:offset + 16] = bytes(16)
        vectors.append((bytes(bad), False))
    bad = bytearray(packet)
    struct.pack_into("!Q", bad, 64, 2**63)
    vectors += [(bytes(bad), False), (packet[:-1], False), (packet + b"\x00", False)]
    result = module.run_tests(vectors=vectors)
    assert result["ok"] and result["sanitized"]
    frame = Frame.decode(bytes.fromhex(result["wire_vector"]))
    assert frame.operation == Operation.DATA
    assert (frame.generation, frame.sequence, frame.pts_ns, frame.rate, frame.format_code,
            frame.channels, frame.frames, frame.first_frame, frame.payload) == (
                1, 1, 123_456_789_012, 48000, 0x8210, 2, 1, 3, bytes(4))
    assert frame.room_id == "01000000-0000-0000-0000-000000000000"

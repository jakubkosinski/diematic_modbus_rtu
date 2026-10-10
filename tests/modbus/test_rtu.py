import pytest

from diematic.modbus import rtu
from diematic.modbus.rtu import FrameKind, FrameSplitter


def test_crc16_known_vector():
    assert rtu.crc16(bytes.fromhex("01 03 00 00 00 0A")) == 0xCDC5


def test_build_read_request_layout():
    frame = rtu.build_read_request(0x0A, 600, 3)
    assert frame[:6] == bytes.fromhex("0A 03 02 58 00 03")
    assert rtu.crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")


def test_build_write_request_layout():
    frame = rtu.build_write_request(0x0A, 300, [0x1234])
    assert frame[:9] == bytes.fromhex("0A 10 01 2C 00 01 02 12 34")
    assert len(frame) == 11


@pytest.mark.parametrize("count", [0, 126])
def test_read_request_rejects_bad_count(count):
    with pytest.raises(ValueError):
        rtu.build_read_request(1, 0, count)


def test_write_request_rejects_out_of_range_value():
    with pytest.raises(ValueError):
        rtu.build_write_request(1, 0, [0x10000])


def test_splitter_decodes_all_frame_kinds():
    stream = (
        rtu.build_read_request(0x0A, 10, 2)
        + rtu.build_read_response(0x0A, [1, 0xFFFF])
        + rtu.build_write_request(0x0A, 20, [5, 6])
        + rtu.build_write_response(0x0A, 20, 2)
        + rtu.build_exception_response(0x0A, rtu.FC_WRITE_MULTIPLE, rtu.EXC_WRITE_PENDING)
    )
    frames = FrameSplitter().feed(stream)
    assert [f.kind for f in frames] == [
        FrameKind.READ_REQUEST,
        FrameKind.READ_RESPONSE,
        FrameKind.WRITE_REQUEST,
        FrameKind.WRITE_RESPONSE,
        FrameKind.EXCEPTION,
    ]
    assert (frames[0].start, frames[0].count) == (10, 2)
    assert frames[1].values == (1, 0xFFFF)
    assert (frames[2].start, frames[2].values) == (20, (5, 6))
    assert frames[4].exception_code == rtu.EXC_WRITE_PENDING
    assert frames[4].function == rtu.FC_WRITE_MULTIPLE


def test_splitter_handles_byte_by_byte_delivery():
    stream = rtu.build_read_response(0x0A, [7, 8, 9]) + rtu.build_read_request(0x0A, 1, 1)
    splitter = FrameSplitter()
    frames = []
    for byte in stream:
        frames += splitter.feed(bytes([byte]))
    assert [f.kind for f in frames] == [FrameKind.READ_RESPONSE, FrameKind.READ_REQUEST]
    assert frames[0].values == (7, 8, 9)


def test_splitter_skips_trailing_zero_padding():
    first = rtu.build_read_request(0x0A, 1, 1)
    second = rtu.build_read_request(0x0A, 2, 1)
    stream = first + b"\x00" + second + b"\x00"
    frames = FrameSplitter().feed(stream)
    assert [f.start for f in frames] == [1, 2]


def test_splitter_resyncs_after_garbage():
    splitter = FrameSplitter()
    frames = splitter.feed(b"\xde\xad\xbe\xef" + rtu.build_read_request(0x0A, 5, 1))
    assert len(frames) == 1 and frames[0].start == 5
    assert splitter.discarded_bytes >= 4


def test_splitter_drops_frame_with_bad_crc():
    bad = bytearray(rtu.build_read_request(0x0A, 5, 1))
    bad[-1] ^= 0xFF
    good = rtu.build_read_request(0x0A, 6, 1)
    frames = FrameSplitter().feed(bytes(bad) + good)
    assert [f.start for f in frames] == [6]


def test_splitter_reset_drops_partial_frame():
    splitter = FrameSplitter()
    frame = rtu.build_read_response(0x0A, [1, 2, 3, 4])
    assert splitter.feed(frame[:6]) == []
    splitter.reset()
    assert splitter.feed(frame) and splitter.discarded_bytes == 6


def test_parse_read_response_returns_values():
    frame = FrameSplitter().feed(rtu.build_read_response(0x0A, [1, 2]))[0]
    assert rtu.parse_read_response(frame, 0x0A, 2) == [1, 2]


def test_parse_read_response_raises_exception_with_retryable_flag():
    frame = FrameSplitter().feed(
        rtu.build_exception_response(0x0A, rtu.FC_READ_HOLDING, rtu.EXC_TARGET_NO_RESPONSE)
    )[0]
    with pytest.raises(rtu.ModbusException) as info:
        rtu.parse_read_response(frame, 0x0A, 1)
    assert info.value.retryable


def test_parse_read_response_rejects_wrong_address_and_count():
    frame = FrameSplitter().feed(rtu.build_read_response(0x0A, [1, 2]))[0]
    with pytest.raises(rtu.FrameError):
        rtu.parse_read_response(frame, 0x0B, 2)
    with pytest.raises(rtu.FrameError):
        rtu.parse_read_response(frame, 0x0A, 3)


def test_check_write_response():
    frame = FrameSplitter().feed(rtu.build_write_response(0x0A, 300, 1))[0]
    rtu.check_write_response(frame, 0x0A, 300, 1)
    with pytest.raises(rtu.FrameError):
        rtu.check_write_response(frame, 0x0A, 301, 1)

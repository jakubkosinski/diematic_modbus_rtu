"""Pure Modbus RTU framing: CRC, request builders, response parsing and a stream splitter."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum

FC_READ_HOLDING = 0x03
FC_WRITE_MULTIPLE = 0x10
EXCEPTION_FLAG = 0x80

MAX_READ_REGISTERS = 125
MAX_WRITE_REGISTERS = 123

EXC_ILLEGAL_FUNCTION = 0x01
EXC_ILLEGAL_ADDRESS = 0x02
EXC_ILLEGAL_VALUE = 0x03
EXC_DEVICE_FAILURE = 0x04
EXC_WRITE_PENDING = 0x06
EXC_PATH_UNAVAILABLE = 0x0A
EXC_TARGET_NO_RESPONSE = 0x0B

# Codes the GTW-26 documentation describes as "try again shortly" rather than a hard failure.
RETRYABLE_EXCEPTIONS = frozenset({EXC_DEVICE_FAILURE, EXC_WRITE_PENDING, EXC_TARGET_NO_RESPONSE})


class ModbusError(Exception):
    """Base class for protocol-level failures."""


class FrameError(ModbusError):
    """Frame is malformed, truncated or has a wrong CRC."""


class ModbusException(ModbusError):
    """Slave answered with an exception response."""

    def __init__(self, function: int, code: int) -> None:
        super().__init__(f"Modbus exception 0x{code:02X} for function 0x{function:02X}")
        self.function = function
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_EXCEPTIONS


def crc16(data: bytes | bytearray) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _with_crc(body: bytes) -> bytes:
    return body + struct.pack("<H", crc16(body))


def build_read_request(address: int, start: int, count: int) -> bytes:
    if not 1 <= count <= MAX_READ_REGISTERS:
        raise ValueError(f"register count out of range: {count}")
    return _with_crc(struct.pack(">BBHH", address, FC_READ_HOLDING, start, count))


def build_write_request(address: int, start: int, values: list[int]) -> bytes:
    if not 1 <= len(values) <= MAX_WRITE_REGISTERS:
        raise ValueError(f"register count out of range: {len(values)}")
    if any(not 0 <= v <= 0xFFFF for v in values):
        raise ValueError("register values must fit in 16 bits")
    header = struct.pack(">BBHHB", address, FC_WRITE_MULTIPLE, start, len(values), 2 * len(values))
    return _with_crc(header + struct.pack(f">{len(values)}H", *values))


def build_read_response(address: int, values: list[int]) -> bytes:
    header = struct.pack(">BBB", address, FC_READ_HOLDING, 2 * len(values))
    return _with_crc(header + struct.pack(f">{len(values)}H", *values))


def build_write_response(address: int, start: int, count: int) -> bytes:
    return _with_crc(struct.pack(">BBHH", address, FC_WRITE_MULTIPLE, start, count))


def build_exception_response(address: int, function: int, code: int) -> bytes:
    return _with_crc(struct.pack(">BBB", address, function | EXCEPTION_FLAG, code))


class FrameKind(Enum):
    READ_REQUEST = "read_request"
    READ_RESPONSE = "read_response"
    WRITE_REQUEST = "write_request"
    WRITE_RESPONSE = "write_response"
    EXCEPTION = "exception"


@dataclass(frozen=True, slots=True)
class Frame:
    kind: FrameKind
    address: int
    function: int
    raw: bytes
    start: int = 0
    count: int = 0
    values: tuple[int, ...] = ()
    exception_code: int = 0


def _crc_ok(raw: bytes) -> bool:
    return crc16(raw[:-2]) == int.from_bytes(raw[-2:], "little")


def _registers(payload: bytes) -> tuple[int, ...]:
    return struct.unpack(f">{len(payload) // 2}H", payload)


def _candidates(buf: bytes) -> tuple[list[tuple[FrameKind, int]], int | None]:
    """Possible (kind, total length) readings of the frame at the buffer start.

    The second item is the smallest length still missing bytes for, if any reading needs more data.
    """
    function = buf[1] if len(buf) > 1 else None
    options: list[tuple[FrameKind, int]] = []
    if function is None:
        return options, 2
    if function & EXCEPTION_FLAG:
        options.append((FrameKind.EXCEPTION, 5))
    elif function == FC_READ_HOLDING:
        options.append((FrameKind.READ_REQUEST, 8))
        if len(buf) > 2 and buf[2] % 2 == 0:
            options.append((FrameKind.READ_RESPONSE, 5 + buf[2]))
        elif len(buf) <= 2:
            options.append((FrameKind.READ_RESPONSE, 5))
    elif function == FC_WRITE_MULTIPLE:
        options.append((FrameKind.WRITE_RESPONSE, 8))
        if len(buf) > 6 and buf[6] == 2 * int.from_bytes(buf[4:6], "big"):
            options.append((FrameKind.WRITE_REQUEST, 9 + buf[6]))
        elif len(buf) <= 6:
            options.append((FrameKind.WRITE_REQUEST, 9))
    return options, None


def _decode(kind: FrameKind, raw: bytes) -> Frame:
    address, function = raw[0], raw[1]
    if kind is FrameKind.EXCEPTION:
        return Frame(kind, address, function & ~EXCEPTION_FLAG, raw, exception_code=raw[2])
    if kind is FrameKind.READ_REQUEST:
        start, count = struct.unpack(">HH", raw[2:6])
        return Frame(kind, address, function, raw, start=start, count=count)
    if kind is FrameKind.READ_RESPONSE:
        values = _registers(raw[3:-2])
        return Frame(kind, address, function, raw, count=len(values), values=values)
    if kind is FrameKind.WRITE_REQUEST:
        start, count = struct.unpack(">HH", raw[2:6])
        return Frame(
            kind, address, function, raw, start=start, count=count, values=_registers(raw[7:-2])
        )
    start, count = struct.unpack(">HH", raw[2:6])
    return Frame(kind, address, function, raw, start=start, count=count)


class FrameSplitter:
    """Extracts valid RTU frames from a byte stream that has no reliable inter-frame gaps.

    Both directions of the bus are fed through it, so a frame may be a request or a response.
    Garbage is skipped one byte at a time until a CRC-valid frame lines up. Leading 0x00 bytes
    are discarded: the Diematic bus pads frames with a zero byte and address 0 is never used.
    """

    MAX_BUFFER = 1024

    def __init__(self) -> None:
        self._buf = bytearray()
        self.discarded_bytes = 0

    def reset(self) -> None:
        self.discarded_bytes += len(self._buf)
        self._buf.clear()

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        frames: list[Frame] = []
        while True:
            while self._buf and self._buf[0] == 0:
                del self._buf[0]
            if len(self._buf) < 2:
                break
            buf = bytes(self._buf)
            options, _ = _candidates(buf)
            matched = False
            waiting = False
            for kind, length in options:
                if length > len(buf):
                    waiting = True
                    continue
                raw = buf[:length]
                if _crc_ok(raw):
                    frames.append(_decode(kind, raw))
                    del self._buf[:length]
                    matched = True
                    break
            if matched:
                continue
            if waiting and len(buf) < self.MAX_BUFFER:
                break
            del self._buf[0]
            self.discarded_bytes += 1
        return frames


def parse_read_response(frame: Frame, address: int, count: int) -> list[int]:
    if frame.address != address:
        raise FrameError(f"unexpected slave address 0x{frame.address:02X}")
    if frame.kind is FrameKind.EXCEPTION:
        raise ModbusException(frame.function, frame.exception_code)
    if frame.kind is not FrameKind.READ_RESPONSE or frame.count != count:
        raise FrameError("response does not match the read request")
    return list(frame.values)


def check_write_response(frame: Frame, address: int, start: int, count: int) -> None:
    if frame.address != address:
        raise FrameError(f"unexpected slave address 0x{frame.address:02X}")
    if frame.kind is FrameKind.EXCEPTION:
        raise ModbusException(frame.function, frame.exception_code)
    if frame.kind is not FrameKind.WRITE_RESPONSE or (frame.start, frame.count) != (start, count):
        raise FrameError("response does not match the write request")

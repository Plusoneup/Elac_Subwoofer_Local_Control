"""SUB3030 BLE wire-protocol primitives: framing, byte stuffing and CRC.

This module deliberately handles framing/checksums only. Command semantics and
payload scaling remain separate because several IDs are overloaded by context.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

FRAME_DELIMITER = 0xC0
ESCAPE = 0xDB
ESCAPED_DELIMITER = 0xDC
ESCAPED_ESCAPE = 0xDD
MIN_BODY_LENGTH = 6  # command id (2) + type (1) + status (1) + CRC (2)
DEFAULT_MAX_FRAME_SIZE = 1024
_COMMAND_ID_RE = re.compile(r"^[0-9A-Fa-f]{4}$")


class PacketError(ValueError):
    """Malformed ELAC packet or frame."""


def _make_crc_table() -> tuple[int, ...]:
    table = []
    for value in range(256):
        crc = value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _make_crc_table()


def crc16_xmodem(data: bytes | bytearray | memoryview, initial: int = 0) -> int:
    """CRC-16/XMODEM, polynomial 0x1021, init=0, refin/refout=False, xorout=0.

    Calculated over the command body without its last two CRC bytes; the CRC
    is sent as a big-endian 16-bit integer.
    """
    crc = initial & 0xFFFF
    for value in data:
        crc = (((crc << 8) & 0xFF00) ^ _CRC_TABLE[((crc >> 8) ^ value) & 0xFF]) & 0xFFFF
    return crc


def _stuff(data: bytes) -> bytes:
    out = bytearray()
    for value in data:
        if value == FRAME_DELIMITER:
            out.extend((ESCAPE, ESCAPED_DELIMITER))
        elif value == ESCAPE:
            out.extend((ESCAPE, ESCAPED_ESCAPE))
        else:
            out.append(value)
    return bytes(out)


def _unstuff(data: bytes, *, strict: bool = False) -> bytes:
    """Undo byte stuffing.

    Known escapes are DB DC -> C0 and DB DD -> DB. In compatibility mode,
    DB is dropped and an unknown following byte is kept; strict mode
    rejects unknown/dangling escapes instead.
    """
    out = bytearray()
    i = 0
    while i < len(data):
        value = data[i]
        if value != ESCAPE:
            # A raw C0 inside the encoded body would be a framing delimiter.
            if value == FRAME_DELIMITER and strict:
                raise PacketError("unescaped C0 inside frame body")
            out.append(value)
            i += 1
            continue
        i += 1
        if i >= len(data):
            raise PacketError("dangling DB escape at end of frame")
        escaped = data[i]
        if escaped == ESCAPED_DELIMITER:
            out.append(FRAME_DELIMITER)
        elif escaped == ESCAPED_ESCAPE:
            out.append(ESCAPE)
        elif strict:
            raise PacketError(f"unknown escape sequence DB {escaped:02X}")
        else:
            # Lenient handling of unknown escape codes.
            out.append(escaped)
        i += 1
    return bytes(out)


@dataclass(frozen=True, slots=True)
class Packet:
    command_id: str
    command_type: int
    status: int
    payload: bytes
    received_crc: int
    calculated_crc: int
    crc_ok: bool
    body: bytes


@dataclass(frozen=True, slots=True)
class FilterRecord:
    """Decoded 9-byte 0049 record: index, filter type, fc, Q and gain."""
    prefix: int
    index: int
    filter_type: int
    frequency_cutoff: int
    quality: float
    gain: float
    raw: bytes


def _signed_i16(raw: bytes) -> int:
    if len(raw) != 2:
        raise PacketError("signed int16 requires exactly two bytes")
    return int.from_bytes(raw, "big", signed=True)


def _trunc_div(value: int, divisor: int) -> int:
    """Java integer division, which truncates toward zero (unlike Python //)."""
    return (abs(value) // divisor) * (-1 if value < 0 else 1)


def decode_filter_record(payload: bytes) -> FilterRecord:
    """Decode the filter record used by command 0049.

    The record is the first 9 bytes of the payload:
    prefix, two signed bytes, then signed big-endian int16 fields scaled x10,
    x100 and x10. The protocol does not reveal the human-readable parameter
    names by itself.
    """
    if not isinstance(payload, bytes) or len(payload) < 9:
        raise PacketError("0049 filter record payload must be at least 9 bytes")
    raw = payload[:9]
    return FilterRecord(
        prefix=raw[0],
        index=int.from_bytes(raw[1:2], "big", signed=True),
        filter_type=int.from_bytes(raw[2:3], "big", signed=True),
        frequency_cutoff=_trunc_div(_signed_i16(raw[3:5]), 10),
        quality=_signed_i16(raw[5:7]) / 100.0,
        gain=_signed_i16(raw[7:9]) / 10.0,
        raw=raw,
    )


def encode_filter_record(
    index: int,
    filter_type: int,
    frequency_cutoff: int,
    quality: float,
    gain: float,
    *,
    prefix: int = 0,
) -> bytes:
    """Encode the 9-byte 0049 filter record.

    Signed bytes and 16-bit fields must fit their encoded ranges. Floating
    values are multiplied and truncated toward zero.
    """
    for name, value in (("prefix", prefix), ("index", index), ("filter_type", filter_type)):
        if not isinstance(value, int):
            raise TypeError(f"{name} must be int")
    if not 0 <= prefix <= 255:
        raise ValueError("prefix must fit in one unsigned byte")
    for name, value in (("index", index), ("filter_type", filter_type)):
        if not -128 <= value <= 127:
            raise ValueError(f"{name} must fit in a signed byte")
    if not isinstance(frequency_cutoff, int):
        raise TypeError("frequency_cutoff must be int")
    if not isinstance(quality, (int, float)) or not isinstance(gain, (int, float)):
        raise TypeError("quality and gain must be numeric")

    scaled_values = (frequency_cutoff * 10, int(quality * 100), int(gain * 10))
    if any(not -32768 <= v <= 32767 for v in scaled_values):
        raise ValueError("scaled value does not fit in a signed int16")
    return bytes((prefix, index & 0xFF, filter_type & 0xFF)) + b"".join(
        value.to_bytes(2, "big", signed=True) for value in scaled_values
    )


def encode_frame(
    command_id: str,
    command_type: int = 0,
    status: int = 0,
    payload: bytes = b"",
) -> bytes:
    """Encode one complete C0-delimited ELAC command frame.

    Wire body: command ID (two decoded hex bytes), type, status, payload,
    CRC-16/XMODEM (big-endian). There is no separate payload-length field.
    """
    if not _COMMAND_ID_RE.fullmatch(command_id):
        raise PacketError("command_id must be exactly four hexadecimal characters")
    for name, value in (("command_type", command_type), ("status", status)):
        if not isinstance(value, int) or not 0 <= value <= 0xFF:
            raise PacketError(f"{name} must be an integer from 0 to 255")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")

    header = bytes.fromhex(command_id) + bytes((command_type, status)) + payload
    checksum = crc16_xmodem(header).to_bytes(2, "big")
    return bytes((FRAME_DELIMITER,)) + _stuff(header + checksum) + bytes((FRAME_DELIMITER,))


def decode_frame(frame: bytes, *, strict_escapes: bool = False) -> Packet:
    """Decode one complete frame, returning crc_ok rather than hiding bad CRCs."""
    if len(frame) < 2 or frame[0] != FRAME_DELIMITER or frame[-1] != FRAME_DELIMITER:
        raise PacketError("frame must start and end with C0")
    body = _unstuff(frame[1:-1], strict=strict_escapes)
    if len(body) < MIN_BODY_LENGTH:
        raise PacketError(f"decoded body too short: {len(body)} bytes")

    command_id = body[:2].hex().upper()
    command_type = body[2]
    status = body[3]
    payload = body[4:-2]
    received_crc = int.from_bytes(body[-2:], "big")
    calculated_crc = crc16_xmodem(body[:-2])
    return Packet(
        command_id=command_id,
        command_type=command_type,
        status=status,
        payload=payload,
        received_crc=received_crc,
        calculated_crc=calculated_crc,
        crc_ok=(received_crc == calculated_crc),
        body=body,
    )


class FrameStreamDecoder:
    """Split BLE notification chunks into C0-delimited frames.

    The original app parses each supplied byte array as a single frame. A Linux
    client should handle notifications split or coalesced by the BLE stack, so
    this helper adds stream buffering without changing the underlying format.
    """
    def __init__(self, *, max_frame_size: int = DEFAULT_MAX_FRAME_SIZE, strict_escapes: bool = False):
        if max_frame_size < MIN_BODY_LENGTH + 2:
            raise ValueError("max_frame_size is too small")
        self.max_frame_size = max_frame_size
        self.strict_escapes = strict_escapes
        self._inside = False
        self._buffer = bytearray()

    def reset(self) -> None:
        self._inside = False
        self._buffer.clear()

    def feed(self, chunk: bytes) -> list[Packet]:
        packets: list[Packet] = []
        for value in chunk:
            if value == FRAME_DELIMITER:
                if not self._inside:
                    self._inside = True
                    self._buffer.clear()
                elif self._buffer:
                    complete = bytes((FRAME_DELIMITER,)) + bytes(self._buffer) + bytes((FRAME_DELIMITER,))
                    self._inside = False
                    self._buffer.clear()
                    packets.append(decode_frame(complete, strict_escapes=self.strict_escapes))
                # Consecutive delimiters are harmless; the next non-empty
                # body remains framed by the latest delimiter.
                continue
            if self._inside:
                self._buffer.append(value)
                if len(self._buffer) > self.max_frame_size:
                    self.reset()
                    raise PacketError("frame exceeds configured maximum size")
        return packets

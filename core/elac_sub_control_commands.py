"""Command builders for the SUB3030 BLE protocol.

Names stay deliberately conservative where the device-side meaning is not certain.
Requires elac_sub_control_protocol.py.
"""
from __future__ import annotations

from elac_sub_control_protocol import encode_frame


def request(command_id: str, payload: bytes = b"") -> bytes:
    """Build a type-0 request with status=0."""
    return encode_frame(command_id, command_type=0, status=0, payload=payload)


def set_u8(command_id: str, value: int) -> bytes:
    """Build a type-1, one-byte setter. Use only for commands confirmed to do so."""
    if not isinstance(value, int) or not 0 <= value <= 255:
        raise ValueError("value must be an integer from 0 to 255")
    return encode_frame(command_id, command_type=1, status=0, payload=bytes([value]))


def query_0049(index: int) -> bytes:
    """Build the observed 0049 type-0 query: payload [00, index]."""
    if not isinstance(index, int) or not 0 <= index <= 255:
        raise ValueError("index must be an integer from 0 to 255")
    return request("0049", bytes((0, index)))


def set_0049_filter_record(payload: bytes) -> bytes:
    """Build the observed 0049 type-1 filter record (payload must be 9 bytes).

    Byte layout:
      [00, index, type, signed BE int16 frequency_cutoff*10,
       signed BE int16 quality*100, signed BE int16 gain*10]
    The permissible ranges and supported filter types remain unconfirmed.
    """
    if not isinstance(payload, bytes) or len(payload) != 9:
        raise ValueError("0049 filter record payload must be exactly 9 bytes")
    if payload[0] != 0:
        raise ValueError("observed 0049 filter record starts with 00")
    return encode_frame("0049", command_type=1, status=0, payload=payload)


def set_0024_text(value: str, encoding: str = "utf-8") -> bytes:
    """Build command 0024 with encoded text plus a terminating NUL byte."""
    if not isinstance(value, str):
        raise TypeError("value must be str")
    return encode_frame("0024", command_type=1, status=0,
                        payload=value.encode(encoding) + b"\x00")


def set_004b_scaled(value: int) -> bytes:
    """Reproduce observed 004B setter bytes: payload [00, (value*10) & FF].

    The scale/meaning is not yet confirmed against the SUB 3030. Kept separate
    from any human-facing control until on-device validation is performed.
    """
    if not isinstance(value, int):
        raise TypeError("value must be int")
    return encode_frame("004B", command_type=1, status=0,
                        payload=bytes((0, (value * 10) & 0xFF)))


def startup_requests() -> list[bytes]:
    """Return the 22 request frames used to read the full device state.

    This mirrors observed command construction, not proven on-device timing or
    acknowledgement policy. Use for analysis/testing; hardware must validate it.
    """
    requests = [request("0040"), request("004A")]
    requests.extend(query_0049(i) for i in (0, 1, *range(8, 16)))
    requests.extend(request(command_id) for command_id in (
        "004B", "0022", "0023", "0172", "0170", "0024", "0001", "0002", "0005", "01D0"
    ))
    return requests

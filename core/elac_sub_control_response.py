"""Conservative decoding of the responses a SUB3030 sends over BLE.

Only command IDs whose meaning is known are decoded; everything else is returned
as unknown. See WRITE_ENABLE_NOTES.md for what has been verified on hardware.
"""
from __future__ import annotations

from typing import Any

from elac_sub_control_protocol import Packet, PacketError, decode_filter_record

# How each payload is read: scalar values are a signed byte at a length-dependent
# offset; delay is a signed big-endian short at a length-dependent offset.
_STRING_FIELDS = {
    "0024": "name",
    "0001": "firmware_version",
    "0002": "hardware_version",
    "0004": "manufacturer_name",
    "0005": "model_name",
    "01D0": "ip_address",
}
_SCALAR_FIELDS = {
    "0040": "master_volume",
    "0045": "input",
    "0022": "power_mode",
    "0023": "power_threshold",
    "0152": "led_brightness",
    "004A": "preset",
}


def _java_signed_byte(value: int) -> int:
    return value - 256 if value >= 128 else value


def _scalar(payload: bytes) -> int:
    """Signed byte at index 2/4 for payload lengths 3/5."""
    if len(payload) == 3:
        return _java_signed_byte(payload[2])
    if len(payload) == 5:
        return _java_signed_byte(payload[4])
    if len(payload) == 0:
        return 0
    return _java_signed_byte(payload[0])


def _short(payload: bytes) -> int:
    """Signed int16 BE at offset 2/4 for payload lengths 4/6."""
    if len(payload) == 0:
        return 0
    if len(payload) == 4:
        raw = payload[2:4]
    elif len(payload) == 6:
        raw = payload[4:6]
    else:
        raw = payload[:2]
    if len(raw) != 2:
        raise PacketError(f"cannot read a short from a {len(payload)}-byte payload")
    return int.from_bytes(raw, "big", signed=True)


def decode_response(packet: Packet, *, require_valid_crc: bool = True) -> dict[str, Any]:
    """Decode a known response into a conservative key/value dictionary.

    Returns `known=False` for command IDs whose semantics have not been
    established. If CRC is invalid, the default is to refuse semantic decoding.
    """
    result: dict[str, Any] = {
        "command_id": packet.command_id,
        "command_type": packet.command_type,
        "status": packet.status,
        "payload_hex": packet.payload.hex(" ").upper(),
        "crc_ok": packet.crc_ok,
        "known": True,
    }
    if require_valid_crc and not packet.crc_ok:
        result.update(known=False, error="CRC mismatch; semantic decode skipped")
        return result

    command_id = packet.command_id
    payload = packet.payload
    if command_id in _SCALAR_FIELDS:
        result.update(field=_SCALAR_FIELDS[command_id], value=_scalar(payload))
    elif command_id == "004B":
        result.update(field="delay_raw", value=_short(payload), scale_note="setter multiplies input by 10; user-facing units unverified")
    elif command_id == "0049":
        if len(payload) >= 9:
            record = decode_filter_record(payload)
            result.update(field="filter_record", value={
                "prefix": record.prefix,
                "index": record.index,
                "filter_type": record.filter_type,
                "frequency_cutoff": record.frequency_cutoff,
                "quality": record.quality,
                "gain": record.gain,
                "raw_hex": record.raw.hex(" ").upper(),
            })
        else:
            result.update(field="filter_or_query_response", value=None,
                          note="payload shorter than 9 bytes; preserved as raw data")
    elif command_id in ("0170", "0172"):
        result.update(
            field="autoeq_calibrated" if command_id == "0170" else "autoeq_enabled",
            value=(len(payload) > 0 and payload[0] == 1),
        )
    elif command_id in _STRING_FIELDS:
        # Text payloads end with a NUL byte, which is dropped.
        result.update(field=_STRING_FIELDS[command_id], value=payload[:-1].decode("utf-8", errors="replace"))
    elif command_id == "0021":
        result.update(field="device_event", value=None,
                      note="response triggers app callback; payload meaning unknown")
    else:
        result.update(known=False, value=None, note="command/response semantics not mapped yet")
    return result

"""Canned device state used by DEMO=1 (no Bluetooth hardware needed).

Mirrors the shape of ``ElacBLE.snapshot()``: values are made up, not read from a device.
"""
from __future__ import annotations

import time
from typing import Any


def _scalar(cmd: str, value: Any) -> dict[str, Any]:
    return {"command_id": cmd, "value": value, "payload_hex": "", "status": 1, "crc_ok": True}


def _filter(index: int, freq: float, q: float, gain: float) -> dict[str, Any]:
    result = {"index": index, "type": 0, "q": q, "gain_db": gain, "raw_hex": ""}
    if index == 1:
        result["type"] = 7
        result["phase_degrees"] = freq
    else:
        result["frequency_hz"] = freq
    return result


def demo_snapshot(address: str) -> dict[str, Any]:
    peq = {8: (31.5, 4.0, 3.0), 9: (63.0, 2.5, -2.5), 10: (90.0, 6.0, 1.5)}
    filters = {"0": _filter(0, 80.0, 1.0, 0.0), "1": _filter(1, 30.0, 1.0, 0.0)}
    for i in range(8, 16):
        f, q, g = peq.get(i, (50.0, 5.0, 0.0))
        filters[str(i)] = _filter(i, f, q, g)
    scalars = {
        "0040": _scalar("0040", 62),
        "004A": _scalar("004A", 2),
        "004B": {**_scalar("004B", 10), "raw_value": 10, "delay_ms": 1.0},
        "0022": _scalar("0022", 0),
        "0023": {**_scalar("0023", 6), "threshold_mv": 6},
        "0172": _scalar("0172", False),
        "0170": _scalar("0170", True),
        "0024": _scalar("0024", "ELAC SUB3030 (demo)"),
        "0001": _scalar("0001", "demo-1.0"),
        "0002": _scalar("0002", "rev.A"),
        "0005": _scalar("0005", "ELAC SUB3030"),
    }
    return {"address": address, "scalars": scalars, "filters": filters, "errors": {}, "partial": False,
            "read_only": True, "read_at": time.time(), "demo": True}

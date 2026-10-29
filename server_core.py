"""Server-side logic for the ELAC SUB3030 dashboard.

Deliberately free of web-framework imports so it can be unit-tested without
FastAPI or Bluetooth hardware. ``app.py`` is a thin HTTP wrapper around
:class:`Service`.

Everything that talks to the subwoofer goes through ``ElacBLE.op_*`` methods,
which are serialised by one lock. The API is still read-only: no setter is
exposed anywhere.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import math
import json
import logging
import os
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core import autoeq
from core.demo_snapshot import demo_snapshot
from core.elac_sub_control_protocol import FrameStreamDecoder, decode_filter_record, encode_filter_record, encode_frame

ROOT = Path(__file__).resolve().parent
# Where config.json and elac_app.log live. Defaults to the program folder; the Linux service uses /var/lib/elac-sub3030.
DATA_DIR = Path(os.environ.get("ELAC_DATA_DIR") or ROOT)
TOKEN_COOKIE = "elac_token"
MIN_TOKEN_LEN = 12
DEFAULT_ADDRESS = ""  # no built-in device: pick yours with Scan in the GUI, or set ELAC_ADDRESS
SERVICE = "5f317bff-5772-4385-91a8-a2bc7f5cfd2a"
NOTIFY = "116bd560-2c1b-4769-b07d-85e36fc5e086"
WRITE = "c6a0486f-71b5-4226-9057-bf2baa8334e8"

SCALAR_COMMANDS = ("0040", "004A", "004B", "0022", "0023", "0172", "0170", "0024", "0001", "0002", "0005")
FILTER_INDEXES = (0, 1, *range(8, 16))
MAX_CONSECUTIVE_FAILURES = 3  # give up on a read after this many failed commands in a row
BLE_CHUNK = 20         # the ELAC app splits frames into 20-byte writes (default ATT MTU 23 - 3)
BLE_CHUNK_GAP_S = 0.05  # and waits up to 50 ms between them


# ---------------------------------------------------------------- errors ----
class AppError(Exception):
    """Error with a Polish and an English message (the GUI picks by language)."""

    def __init__(self, code: str, pl: str, en: str | None = None, status: int = 502):
        super().__init__(pl)
        self.code, self.pl, self.en, self.status = code, pl, en or pl, status


class LinkError(AppError):
    """The BLE link is gone; there is no point continuing the current operation."""


def error_payload(exc: BaseException) -> dict[str, str]:
    if isinstance(exc, AppError):
        return {"detail": exc.pl, "detail_en": exc.en, "code": exc.code}
    text = str(exc) or exc.__class__.__name__
    return {"detail": text, "detail_en": text, "code": "error"}


# -------------------------------------------------------------- settings ----
_ADDRESS_RE = re.compile(
    r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$"  # MAC (Linux / Windows)
    r"|^[0-9A-F]{8}-(?:[0-9A-F]{4}-){3}[0-9A-F]{12}$"  # UUID (macOS)
)


def normalize_address(value: Any) -> str:
    text = str(value or "").strip().upper()
    if re.fullmatch(r"(?:[0-9A-F]{2}-){5}[0-9A-F]{2}", text):  # AA-BB-... -> AA:BB:...
        text = text.replace("-", ":")
    if not _ADDRESS_RE.match(text):
        raise AppError("bad_address", "Nieprawidłowy adres Bluetooth.", "Invalid Bluetooth address.", 422)
    return text


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _float(value: str | None, default: float) -> float:
    try:
        return max(0.0, float(value)) if value not in (None, "") else default
    except ValueError:
        return default


class Settings:
    """Configuration: environment variables > config.json > built-in default."""

    def __init__(self, root: Path = DATA_DIR, environ: dict[str, str] | None = None):
        env = os.environ if environ is None else environ
        self.config_path = Path(root) / "config.json"
        # Access from other devices on the home network: a shared token (cookie after login, or X-Elac-Token header).
        self.token = env.get("ELAC_TOKEN", "").strip()
        self.tls = bool(env.get("ELAC_TLS_CERT", "").strip())
        self.demo = _truthy(env.get("DEMO"))
        self.idle_timeout_s = _float(env.get("ELAC_IDLE_DISCONNECT_S"), 300.0)  # 0 disables
        self.allowed_hosts = default_allowed_hosts(env.get("ELAC_ALLOWED_HOSTS", ""))
        config = self._load()
        # AutoEQ generator/upload commands come from the older ELAC app and are unconfirmed on a SUB3030.
        # Off by default; switched in the GUI (saved in config.json) or forced on with ELAC_AUTOEQ_EXPERIMENTAL=1.
        self.autoeq_experimental_locked = _truthy(env.get("ELAC_AUTOEQ_EXPERIMENTAL"))
        self.autoeq_experimental = self.autoeq_experimental_locked or config.get("autoeq_experimental") is True
        env_address = env.get("ELAC_ADDRESS", "").strip()
        self.address_locked = bool(env_address)
        self.address = normalize_address(env_address) if env_address else self._saved_address(config)
        self.devices = self._saved_devices(config)   # {address: name} of every subwoofer used so far
        if self.address and self.address not in self.devices:
            self.devices[self.address] = ""

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            logging.warning("Ignoring %s: not a JSON object", self.config_path)
        except FileNotFoundError:
            pass
        except Exception as exc:  # corrupt file: fall back to defaults
            logging.warning("Ignoring %s: %s", self.config_path, exc)
        return {}

    def _saved_address(self, config: dict[str, Any]) -> str:
        if "address" in config:
            try:
                return normalize_address(config.get("address"))
            except AppError as exc:
                logging.warning("Ignoring %s: %s", self.config_path, exc.en)
        return DEFAULT_ADDRESS

    def _saved_devices(self, config: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in config.get("devices") or []:
            try:
                out[normalize_address(item.get("address"))] = clean_device_name(item.get("name", ""))
            except (AppError, AttributeError):
                logging.warning("Ignoring a saved device entry in %s", self.config_path)
        return out

    def device_list(self) -> list[dict[str, str]]:
        return [{"address": a, "name": n} for a, n in self.devices.items()]

    def _save_devices(self) -> None:
        self._save(devices=self.device_list())

    def rename_device(self, address: str, name: str) -> None:
        if address not in self.devices:
            raise AppError("unknown_device", "Nie znam tego subwoofera.", "Unknown subwoofer.", 404)
        self.devices[address] = clean_device_name(name)
        self._save_devices()

    def forget_device(self, address: str) -> None:
        if address == self.address:
            raise AppError("device_in_use", "Nie można usunąć używanego subwoofera.", "The subwoofer in use cannot be removed.", 409)
        if self.devices.pop(address, None) is not None:
            self._save_devices()

    def _save(self, **changes: Any) -> None:
        data = {**self._load(), **changes}  # keep the other saved settings
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.config_path)

    def save_address(self, address: str) -> None:
        self.devices.setdefault(address, "")
        self._save(address=address, devices=self.device_list())
        self.address = address

    def save_autoeq_experimental(self, enabled: bool) -> None:
        self._save(autoeq_experimental=bool(enabled))
        self.autoeq_experimental = self.autoeq_experimental_locked or bool(enabled)


# -------------------------------------------------------------- security ----
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def hostname_of(host_header: str) -> str:
    h = host_header.strip().lower()
    if h.startswith("["):  # [::1]:8765
        end = h.find("]")
        return h[1:end] if end != -1 else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def default_allowed_hosts(extra: str = "", own_name: str | None = None) -> frozenset[str]:
    """Local names plus this machine's own hostname (with .local/.lan) and anything in ELAC_ALLOWED_HOSTS."""
    hosts = {"127.0.0.1", "localhost", "::1"}
    name = (own_name if own_name is not None else socket.gethostname()).strip().lower().rstrip(".")
    if name:
        short = name.split(".")[0]
        hosts.update({name, short, f"{short}.local", f"{short}.lan", f"{short}.home"})
    hosts.update(h.strip().lower() for h in extra.split(",") if h.strip())
    return frozenset(hosts)


def is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def token_matches(expected: str, *candidates: str | None) -> bool:
    """Constant-time check of any candidate against the configured token. No token configured -> always True."""
    if not expected:
        return True
    want = expected.encode()
    return any(c is not None and hmac.compare_digest(c.encode(), want) for c in candidates)


def check_request(method: str, host: str | None, origin: str | None,
                  allowed_hosts: frozenset[str] | set[str], sec_fetch_site: str | None = None) -> str | None:
    """Return a reason string if the request must be refused, else None.

    * Host must be an IP address or a known local name (blocks DNS-rebinding attacks,
      which always arrive under the attacker's domain name, never as an IP literal).
    * State-changing requests must be same-origin (blocks other web pages from
      driving the local API through the user's browser).
    """
    if not host:
        return "missing Host header"
    name = hostname_of(host)
    if name not in allowed_hosts and not is_ip_literal(name):
        return f"host {host!r} is not allowed (set ELAC_ALLOWED_HOSTS to permit it)"
    if method.upper() in _SAFE_METHODS:
        return None
    if sec_fetch_site and sec_fetch_site.strip().lower() not in {"same-origin", "none"}:
        return "cross-site request blocked"
    if origin is not None:
        if origin.strip().lower() == "null" or urlsplit(origin).netloc.lower() != host.strip().lower():
            return "cross-origin request blocked"
    return None


MAX_DEVICE_NAME = 32


def clean_device_name(name: Any) -> str:
    """A short label like "Living room": printable text only, trimmed, at most 32 characters."""
    text = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()
    if len(text) > MAX_DEVICE_NAME:
        raise AppError("bad_value", f"Nazwa może mieć najwyżej {MAX_DEVICE_NAME} znaki.",
                       f"The name can be at most {MAX_DEVICE_NAME} characters.", 422)
    return text


# ------------------------------------------------------------- scan data ----
def describe_advert(address: str, name: str | None, rssi: int | None, service_uuids: list[str] | None) -> dict[str, Any]:
    uuids = [u.lower() for u in (service_uuids or [])]
    lname = (name or "").lower()
    likely = SERVICE in uuids or "elac" in lname or "sub30" in lname
    return {"name": name or "", "address": address, "rssi": rssi, "likely": likely}


def rank_scan(entries: list[dict[str, Any]], limit: int = 30) -> list[dict[str, Any]]:
    usable = [e for e in entries if e["name"] or e["likely"]]  # drop anonymous noise
    usable.sort(key=lambda e: (not e["likely"], -(e["rssi"] if e["rssi"] is not None else -999)))
    return usable[:limit]


# ------------------------------------------------------------------- BLE ----
class ElacBLE:
    def __init__(self, address: str = DEFAULT_ADDRESS):
        self.address = address
        self.client = None
        self.decoder = FrameStreamDecoder()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.loop = None
        self.disconnected = False
        self.op_lock = asyncio.Lock()  # one BLE operation at a time
        self._read_task: asyncio.Task | None = None
        self.last_activity = time.monotonic()

    # -- state ---------------------------------------------------------
    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected and not self.disconnected)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    # -- low level (callers must hold op_lock) -------------------------
    def on_disconnect(self, client):
        self.disconnected = True
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
        except Exception:
            pass

    def on_notify(self, sender, data):
        try:
            for p in self.decoder.feed(bytes(data)):
                logging.debug("RX %s type=%s status=%s payload=%s crc=%s", p.command_id, p.command_type, p.status, p.payload.hex(), p.crc_ok)
                if p.crc_ok:
                    self.queue.put_nowait(p)
                else:
                    logging.warning("RX frame with bad CRC dropped: %s payload=%s", p.command_id, p.payload.hex(" ").upper())
        except Exception:
            logging.exception("BLE notification decode failed")

    async def connect(self):
        if self.connected:
            return
        if not self.address:
            raise AppError("no_address", "Najpierw wybierz subwoofer: Połączenie → Szukaj.",
                           "Choose your subwoofer first: Connection → Scan.", 409)
        from bleak import BleakClient, BleakScanner  # imported lazily so demo mode / tests need no Bluetooth stack

        await self.close()
        self.loop = asyncio.get_running_loop()
        last: BaseException | None = None
        for attempt in range(1, 5):
            try:
                dev = await BleakScanner.find_device_by_address(self.address, timeout=12)
                if dev is None:
                    raise AppError("not_found",
                                   f"Nie znaleziono {self.address}. Włącz subwoofer i zamknij aplikację ELAC w telefonie.",
                                   f"Device {self.address} not found. Power on the subwoofer and close the ELAC app on your phone.", 503)
                self.client = BleakClient(dev, timeout=20, disconnected_callback=self.on_disconnect)
                await self.client.connect()
                if not self.client.is_connected:
                    raise AppError("no_link", "BLE nie nawiązał połączenia.", "BLE did not establish a connection.", 503)
                chars = [c.uuid.lower() for s in self.client.services for c in s.characteristics]
                if NOTIFY not in chars or WRITE not in chars:
                    raise AppError("bad_device", "Urządzenie nie udostępnia oczekiwanych charakterystyk ELAC.",
                                   "The device does not expose the expected ELAC characteristics.", 503)
                await self.client.start_notify(NOTIFY, self.on_notify)
                self.disconnected = False
                logging.info("Connected to %s", self.address)
                return
            except Exception as e:
                last = e
                logging.warning("connect attempt %s failed: %s", attempt, e)
                await self.close()
                if attempt < 4:
                    await asyncio.sleep((3, 6, 10)[attempt - 1])
        en = last.en if isinstance(last, AppError) else str(last)
        raise AppError("connect_failed", f"Nie udało się połączyć z ELAC po 4 próbach: {last}",
                       f"Could not connect to the ELAC subwoofer after 4 attempts: {en}", 503)

    async def close(self):
        c, self.client = self.client, None
        if c:
            try:
                if c.is_connected:
                    try:
                        await c.stop_notify(NOTIFY)
                    except Exception:
                        pass
                await c.disconnect()
            except Exception:
                pass

    async def write_frame(self, frame: bytes) -> None:
        """Write one frame; longer than 20 bytes is split like the ELAC app does (the sub reassembles the stream).

        A single 40-byte write fails on Windows with "Invalid Attribute Value Length" (seen with the 0048 upload).
        """
        if len(frame) <= BLE_CHUNK:
            await self.client.write_gatt_char(WRITE, frame, response=True)
            return
        for start in range(0, len(frame), BLE_CHUNK):
            await self.client.write_gatt_char(WRITE, frame[start:start + BLE_CHUNK], response=True)
            if start + BLE_CHUNK < len(frame):
                await asyncio.sleep(BLE_CHUNK_GAP_S)

    async def exchange(self, cmd: str, payload: bytes = b"", expected_index: int | None = None, timeout: float = 5, command_type: int = 0):
        if not self.connected:
            raise LinkError("no_link", "Brak aktywnego połączenia BLE. Połącz ponownie.", "No active BLE connection. Reconnect.", 503)
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        await self.write_frame(encode_frame(cmd, command_type=command_type, status=0, payload=payload))
        deadline = asyncio.get_running_loop().time() + timeout
        no_reply = AppError("timeout", f"Brak odpowiedzi na komendę {cmd}.", f"No response to command {cmd}.")
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise no_reply
            try:
                p = await asyncio.wait_for(self.queue.get(), left)
            except asyncio.TimeoutError:
                raise no_reply from None
            if p is None:
                raise LinkError("link_closed", "Subwoofer zamknął połączenie BLE.", "The subwoofer closed the BLE connection.", 503)
            if p.command_id != cmd:
                continue
            if expected_index is not None and (len(p.payload) < 2 or p.payload[1] != expected_index):
                continue
            if not p.crc_ok or p.status != 1:
                raise AppError("bad_response", f"Nieprawidłowa odpowiedź {cmd}: status={p.status}, CRC={p.crc_ok}",
                               f"Invalid response for {cmd}: status={p.status}, CRC={p.crc_ok}")
            return p

    async def scalar(self, cmd: str) -> dict[str, Any]:
        p = await self.exchange(cmd)
        if cmd == "004B":
            raw = p.payload[2:4] if len(p.payload) == 4 else p.payload[4:6] if len(p.payload) == 6 else p.payload[:2]
            value = int.from_bytes(raw, "big", signed=True)
        elif cmd in ("0024", "0001", "0002", "0005"):
            value = p.payload[:-1].decode("utf-8", errors="replace")
        elif cmd in ("0170", "0172"):
            value = bool(p.payload and p.payload[0] == 1)
        else:
            if not p.payload:
                raise AppError("empty", f"Pusta odpowiedź {cmd}.", f"Empty response for {cmd}.")
            value = p.payload[0] if p.payload[0] < 128 else p.payload[0] - 256
        result = {"command_id": cmd, "value": value, "payload_hex": p.payload.hex(" ").upper(), "status": p.status, "crc_ok": p.crc_ok}
        # Live captures confirm command 004B reports delay in tenths of a millisecond.
        if cmd == "004B":
            result["delay_ms"] = value / 10.0
            result["raw_value"] = value
        if cmd == "0023":
            # Confirmed on hardware 2026-10-26: app 6 mV -> raw 6, app 10 mV -> raw 10 (old ELAC app: 1-10 mV, raw = mV).
            result["threshold_mv"] = value
        return result

    async def filt(self, index: int) -> dict[str, Any]:
        p = await self.exchange("0049", bytes((0, index)), expected_index=index)
        r = decode_filter_record(p.payload)
        result = {"index": r.index, "type": r.filter_type, "q": r.quality, "gain_db": r.gain, "raw_hex": r.raw.hex(" ").upper()}
        if index == 1 and r.filter_type == 7:
            # For type 7 the first scaled int16 is phase in degrees, not frequency.
            result["phase_degrees"] = r.frequency_cutoff
        else:
            result["frequency_hz"] = r.frequency_cutoff
        return result

    async def write_scalar(self, cmd: str, value: int) -> dict[str, Any]:
        """Write the confirmed scalar setters: volume, preset and Auto-On threshold (1-10 mV)."""
        if cmd not in ("0040", "004A", "0023"):
            raise AppError("write_not_allowed", "Ta komenda nie jest dostępna do zapisu.", "This command is not writable through this interface.", 422)
        maximum = {"0040": 100, "004A": 3, "0023": 10}[cmd]
        minimum = 1 if cmd == "0023" else 0
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise AppError("bad_value", "Wartość poza dozwolonym zakresem.", "Value is outside the allowed range.", 422)
        await self.exchange(cmd, bytes((value,)), command_type=1)
        result = await self.scalar(cmd)
        if int(result["value"]) != value:
            raise AppError("verify_failed", f"Weryfikacja zapisu {cmd} nie powiodła się.", f"Read-back verification failed for {cmd}.", 502)
        return result

    async def write_filter(self, index: int, frequency_hz: int, q: float, gain_db: float, filter_type: int) -> dict[str, Any]:
        if index == 0 and filter_type != 4 or index == 1 and filter_type != 7 or index >= 8 and filter_type != 9 or index not in (0, 1, *range(8, 16)):
            raise AppError("write_not_allowed", "Typ filtra nie jest dopuszczony do zapisu.", "This filter type is not allowed for writing.", 422)
        payload = encode_filter_record(index, filter_type, frequency_hz, q, gain_db)
        await self.exchange("0049", payload, expected_index=index, command_type=1)
        result = await self.filt(index)
        read_value = result.get("phase_degrees") if index == 1 else result.get("frequency_hz")
        if (read_value is None or int(read_value) != int(frequency_hz)
                or abs(float(result["q"]) - float(q)) > 0.011
                or abs(float(result["gain_db"]) - float(gain_db)) > 0.11):
            raise AppError("verify_failed", f"Weryfikacja zapisu filtra {index} nie powiodła się.", f"Read-back verification failed for filter {index}.", 502)
        return result

    async def write_autoeq_enabled(self, value: int) -> dict[str, Any]:
        """0172 type 1 (0/1). Like the ELAC app, only allowed once the sub reports a calibration (0170)."""
        calibrated = await self.scalar("0170")
        if not calibrated["value"]:
            raise AppError("not_calibrated", "AutoEQ można przełączać dopiero po kalibracji.",
                           "AutoEQ can only be switched after a calibration.", 409)
        await self.exchange("0172", bytes((value,)), command_type=1)
        result = await self.scalar("0172")
        if bool(result["value"]) != bool(value):
            raise AppError("verify_failed", "Weryfikacja zapisu 0172 nie powiodła się.", "Read-back verification failed for 0172.", 502)
        return result

    async def send_command(self, cmd: str, payload: bytes = b"", command_type: int = 1, timeout: float = 2.0,
                           required: bool = False, min_len: int = 0, ignore_payload: bytes | None = None):
        """Send a frame; return the first matching reply, or None when no reply is required and none came."""
        if not self.connected:
            raise LinkError("no_link", "Brak aktywnego połączenia BLE. Połącz ponownie.", "No active BLE connection. Reconnect.", 503)
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        logging.info("AutoEQ TX %s type=%s payload=%s", cmd, command_type, payload.hex(" ").upper() or "-")
        await self.write_frame(encode_frame(cmd, command_type=command_type, status=0, payload=payload))
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_running_loop().time()
            try:
                if left <= 0:
                    raise asyncio.TimeoutError
                p = await asyncio.wait_for(self.queue.get(), left)
            except asyncio.TimeoutError:
                if required:
                    raise AppError("timeout", f"Brak odpowiedzi na komendę {cmd}.", f"No response to command {cmd}.") from None
                logging.info("No reply to %s within %.1fs (not required)", cmd, timeout)
                return None
            if p is None:
                raise LinkError("link_closed", "Subwoofer zamknął połączenie BLE.", "The subwoofer closed the BLE connection.", 503)
            if p.command_id != cmd or len(p.payload) < min_len or (ignore_payload is not None and p.payload == ignore_payload):
                # kept in the log: an unexpected reply here is the best clue when the sub does not answer as expected
                logging.info("AutoEQ RX (waiting for %s, skipped) %s type=%s status=%s payload=%s",
                             cmd, p.command_id, p.command_type, p.status, p.payload.hex(" ").upper() or "-")
                continue
            logging.info("AutoEQ RX %s status=%s payload=%s", cmd, p.status, p.payload.hex(" ").upper())
            return p

    # AutoEQ device steps (see core/autoeq.py for the protocol). DemoBLE overrides these four.
    async def _gen_start(self, step: int) -> bool:
        return await self.send_command("0174", bytes((0, step))) is not None

    async def _gen_state(self, step: int) -> int | None:
        p = await self.exchange("0177", bytes((0, step)), expected_index=step, timeout=3)
        return autoeq.generator_state(p.payload)

    async def _gen_stop(self) -> None:
        await self.send_command("0175")

    async def _upload(self, step: int, payload: bytes) -> list[float] | None:
        if step == autoeq.STEP_NEAR:
            p = await self.send_command("0048", payload, timeout=5)
            self.last_upload_ack = p is not None
            if p is None:
                logging.warning("AutoEQ near-field upload: no reply from the subwoofer within 5 s")
            return None
        # The app expects 16 correction values in the 0048 reply. The SUB3030 (fw v1.1.956.23) answers
        # differently (hardware log 2026-10-27): a short ack [00 01], then ~3 s later it announces
        # 0170 = 1 ("calibrated") and keeps the correction to itself. Both answers count as success.
        need = 2 + 2 * len(autoeq.BANDS_HZ)
        self.last_calibrated = None
        loop = asyncio.get_running_loop()
        p = await self.send_command("0048", payload, timeout=20, required=True, ignore_payload=payload)
        if len(p.payload) >= need:
            return autoeq.decode_correction(p.payload)
        self.last_upload_ack = True
        deadline = loop.time() + 20
        while True:
            left = deadline - loop.time()
            try:
                if left <= 0:
                    raise asyncio.TimeoutError
                q = await asyncio.wait_for(self.queue.get(), left)
            except asyncio.TimeoutError:
                logging.warning("AutoEQ far-field upload acknowledged, but neither a correction nor 0170 followed within 20 s")
                return None
            if q is None:
                raise LinkError("link_closed", "Subwoofer zamknął połączenie BLE.", "The subwoofer closed the BLE connection.", 503)
            logging.info("AutoEQ RX (after far-field upload) %s type=%s status=%s payload=%s",
                         q.command_id, q.command_type, q.status, q.payload.hex(" ").upper() or "-")
            if q.command_id == "0048" and len(q.payload) >= need and q.payload != payload:
                return autoeq.decode_correction(q.payload)
            if q.command_id == "0170":
                self.last_calibrated = bool(q.payload[0]) if q.payload else None
                return None

    async def snapshot(self) -> dict[str, Any]:
        """Read everything; a command that fails is reported in ``errors`` instead of aborting the read."""
        scalars: dict[str, Any] = {}
        filters: dict[str, Any] = {}
        errors: dict[str, str] = {}
        streak = 0

        async def attempt(key: str, coro_factory, store: dict, store_key: str):
            nonlocal streak
            try:
                store[store_key] = await coro_factory()
                streak = 0
            except LinkError:
                raise
            except Exception as e:
                if not self.connected:
                    raise LinkError("link_lost", "Utracono połączenie BLE podczas odczytu.", "The BLE link was lost during the read.", 503) from e
                logging.warning("read %s failed: %s", key, e)
                errors[key] = e.en if isinstance(e, AppError) else (str(e) or e.__class__.__name__)
                streak += 1
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    raise AppError("read_failed", f"Odczyt przerwany po {streak} błędach z rzędu (ostatni: {key}).",
                                   f"Read aborted after {streak} consecutive failures (last: {key}).") from e

        for cmd in SCALAR_COMMANDS:
            await attempt(cmd, lambda c=cmd: self.scalar(c), scalars, cmd)
        for idx in FILTER_INDEXES:
            await attempt(f"filter:{idx}", lambda i=idx: self.filt(i), filters, str(idx))
        if not scalars and not filters:
            raise AppError("read_failed", "Nie udało się odczytać żadnego parametru.", "Could not read any parameter.")
        return {"address": self.address, "scalars": scalars, "filters": filters, "errors": errors,
                "partial": bool(errors), "read_only": False, "write_supported": True, "read_at": time.time()}

    async def scan(self, timeout: float = 8.0) -> list[dict[str, Any]]:
        from bleak import BleakScanner

        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        entries = [describe_advert(addr, dev.name or adv.local_name, adv.rssi, list(adv.service_uuids or []))
                   for addr, (dev, adv) in found.items()]
        return rank_scan(entries)

    # -- public operations (these take the lock) ------------------------
    async def op_connect(self) -> None:
        async with self.op_lock:
            self.touch()
            await self.connect()

    async def op_read(self) -> dict[str, Any]:
        """Concurrent callers share one in-flight read instead of fighting over the link."""
        if self._read_task is None or self._read_task.done():
            self._read_task = asyncio.ensure_future(self._read_locked())
            self._read_task.add_done_callback(lambda t: t.cancelled() or t.exception())  # mark exception as retrieved
        return await asyncio.shield(self._read_task)

    async def op_write_setting(self, setting: dict[str, Any]) -> dict[str, Any]:
        async with self.op_lock:
            self.touch()
            if not self.connected:
                raise LinkError("no_link", "Brak aktywnego połączenia BLE. Najpierw połącz urządzenie i odczytaj ustawienia.",
                                "No active BLE connection. Connect and read the device first.", 503)
            kind = setting.get("kind")
            try:
                if kind == "volume":
                    value = int(setting.get("value"))
                    result = await self.write_scalar("0040", value)
                elif kind == "preset":
                    value = setting.get("value")
                    if isinstance(value, str):
                        value = {"Flat": 0, "Cinema": 1, "Music": 2, "Night": 3}.get(value)
                    if not isinstance(value, int) or isinstance(value, bool):
                        raise AppError("bad_value", "Nieprawidłowy preset.", "Invalid preset.", 422)
                    result = await self.write_scalar("004A", value)
                elif kind == "autoon_threshold":
                    # Confirmed 2026-10-26: raw = mV, app range 1-10 mV (app 6/10 mV read back as 6/10).
                    raw_value = setting.get("value")
                    if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool) or not float(raw_value).is_integer():
                        raise AppError("bad_value", "Próg Auto-On musi być całkowitą wartością mV.", "Auto-On threshold must be a whole number of mV.", 422)
                    result = await self.write_scalar("0023", int(raw_value))
                elif kind == "delay":
                    raw_value = setting.get("value")
                    if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool) or not float(raw_value).is_integer() or not 0 <= raw_value <= 20:
                        raise AppError("bad_value", "Opóźnienie musi wynosić 0–20 ms.", "Delay must be between 0 and 20 ms.", 422)
                    delay_ms = int(raw_value)
                    await self.exchange("004B", bytes((0, delay_ms * 10)), command_type=1)
                    result = await self.scalar("004B")
                    if int(result["value"]) != delay_ms * 10:
                        raise AppError("verify_failed", "Weryfikacja zapisu opóźnienia nie powiodła się.", "Delay read-back verification failed.", 502)
                elif kind in ("lpf", "phase", "peq"):
                    if kind == "lpf":
                        raw_value = setting.get("frequency_hz")
                        if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool) or not float(raw_value).is_integer():
                            raise AppError("bad_value", "LPF musi być całkowitą liczbą Hz.", "LPF must be a whole number of Hz.", 422)
                        value = int(raw_value)
                        if not 40 <= value <= 150:
                            raise AppError("bad_value", "LPF musi mieścić się w zakresie 40–150 Hz.", "LPF must be between 40 and 150 Hz.", 422)
                        result = await self.write_filter(0, value, 0.71, 0.0, 4)
                    elif kind == "phase":
                        raw_value = setting.get("value")
                        if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool) or not float(raw_value).is_integer():
                            raise AppError("bad_value", "Faza musi być całkowitą wartością stopni.", "Phase must be a whole number of degrees.", 422)
                        value = int(raw_value)
                        if not 0 <= value <= 360:
                            raise AppError("bad_value", "Faza musi wynosić od 0 do 360°.", "Phase must be between 0 and 360°.", 422)
                        result = await self.write_filter(1, value, 0.0, 0.0, 7)
                    else:
                        index = int(setting.get("index"))
                        freq, q, gain = setting.get("frequency_hz"), setting.get("q"), setting.get("gain_db")
                        if index not in range(8, 16):
                            raise AppError("bad_value", "Indeks PEQ musi wynosić 8–15.", "PEQ index must be 8–15.", 422)
                        if not isinstance(freq, (int, float)) or isinstance(freq, bool) or not float(freq).is_integer() or not 20 <= freq <= 200:
                            raise AppError("bad_value", "Częstotliwość PEQ musi być całkowita i wynosić 20–200 Hz.", "PEQ frequency must be a whole number between 20 and 200 Hz.", 422)
                        if not isinstance(q, (int, float)) or isinstance(q, bool) or not 0.1 <= q <= 10:
                            raise AppError("bad_value", "Q musi wynosić 0,1–10.", "Q must be 0.1–10.", 422)
                        if not isinstance(gain, (int, float)) or isinstance(gain, bool) or not -12 <= gain <= 12:
                            raise AppError("bad_value", "Wzmocnienie musi wynosić od -12 do +12 dB.", "Gain must be between -12 and +12 dB.", 422)
                        result = await self.write_filter(index, int(freq), float(q), float(gain), 9)
                elif kind == "autoeq_enabled":
                    value = setting.get("value")
                    if value not in (0, 1) or isinstance(value, float):
                        raise AppError("bad_value", "AutoEQ: wartość musi wynosić 0 lub 1.", "AutoEQ value must be 0 or 1.", 422)
                    result = await self.write_autoeq_enabled(int(value))
                else:
                    raise AppError("write_not_allowed", "Nieobsługiwany typ zapisu.", "Unsupported write type.", 422)
                self.touch()
                logging.info("Device write verified: %s", {k: v for k, v in setting.items() if v is not None and k != "enabled"})
                return {"kind": kind, "verified": True, "result": result}
            except Exception as exc:
                self.touch()
                logging.warning("Device write FAILED: %s (%s)", {k: v for k, v in setting.items() if v is not None and k != "enabled"}, exc)
                raise

    async def _autoeq_op(self, label: str, coro_factory):
        async with self.op_lock:
            self.touch()
            if not self.connected:
                raise LinkError("no_link", "Brak aktywnego połączenia BLE. Najpierw połącz urządzenie.", "No active BLE connection. Connect first.", 503)
            try:
                return await coro_factory()
            finally:
                self.touch()

    async def op_autoeq_start(self, step: int) -> dict[str, Any]:
        ack = await self._autoeq_op("start", lambda: self._gen_start(step))
        logging.info("AutoEQ generator start step=%s ack=%s", step, ack)
        return {"step": step, "ack": ack}

    async def op_autoeq_state(self, step: int) -> dict[str, Any]:
        return {"step": step, "state": await self._autoeq_op("state", lambda: self._gen_state(step))}

    async def op_autoeq_stop(self) -> dict[str, Any]:
        await self._autoeq_op("stop", self._gen_stop)
        logging.info("AutoEQ generator stop")
        return {"stopped": True}

    async def op_autoeq_upload(self, step: int, levels: list[float]) -> dict[str, Any]:
        payload = autoeq.encode_measurement(step, levels)
        logging.info("AutoEQ upload step=%s payload=%s", step, payload.hex(" ").upper())
        self.last_upload_ack = None
        self.last_calibrated = None
        correction = await self._autoeq_op("upload", lambda: self._upload(step, payload))
        return {"step": step, "correction_db": correction, "ack": self.last_upload_ack if correction is None else True,
                "calibrated": self.last_calibrated}

    async def _read_locked(self) -> dict[str, Any]:
        async with self.op_lock:
            self.touch()
            try:
                if not self.connected:
                    await self.connect()
                return await self.snapshot()
            finally:
                self.touch()

    async def op_disconnect(self) -> None:
        async with self.op_lock:
            await self.close()

    async def op_scan(self) -> list[dict[str, Any]]:
        async with self.op_lock:
            self.touch()
            try:
                return await self.scan()
            finally:
                self.touch()

    async def op_set_address(self, address: str) -> None:
        async with self.op_lock:
            await self.close()
            self.address = address
            self.touch()


class DemoBLE(ElacBLE):
    """Same interface, canned data, no Bluetooth. Enabled with DEMO=1."""

    def __init__(self, address: str = "AA:BB:CC:DD:EE:FF"):
        super().__init__(address)
        self._demo_connected = False

    @property
    def connected(self) -> bool:
        return self._demo_connected

    async def connect(self):
        await asyncio.sleep(0.4)
        self._demo_connected = True

    async def close(self):
        self._demo_connected = False

    async def snapshot(self):
        await asyncio.sleep(0.3)
        return demo_snapshot(self.address)

    # --- simulated AutoEQ: the generator "plays" for a few seconds, the far-field upload returns a correction
    DEMO_GENERATOR_S = 6.0

    async def _gen_start(self, step: int) -> bool:
        self._gen = {"step": step, "t0": time.monotonic()}
        return True

    async def _gen_state(self, step: int) -> int | None:
        g = getattr(self, "_gen", None)
        if not g or g["step"] != step:
            return autoeq.GEN_OFF
        return autoeq.GEN_PLAYING if time.monotonic() - g["t0"] < self.DEMO_GENERATOR_S else autoeq.GEN_DONE

    async def _gen_stop(self) -> None:
        self._gen = None

    async def _upload(self, step: int, payload: bytes) -> list[float] | None:
        await asyncio.sleep(0.3)
        n = len(autoeq.BANDS_HZ)
        levels = [int.from_bytes(payload[2 + 2 * i: 4 + 2 * i], "big", signed=True) / 10 for i in range(n)]
        if step == autoeq.STEP_NEAR:
            self._near = levels
            return None
        near = getattr(self, "_near", None) or levels
        diff = [a - b for a, b in zip(near, levels)]
        mid = sorted(diff)[n // 2]  # align levels on the median, like matching overall loudness
        return [round(max(-10.0, min(6.0, d - mid)), 1) for d in diff]

    async def scan(self, timeout: float = 8.0):
        await asyncio.sleep(0.8)
        return rank_scan([
            describe_advert(self.address, "ELAC SUB3030 (demo)", -52, [SERVICE]),
            describe_advert("AA:BB:CC:00:11:22", "Living room speaker", -71, []),
        ])


# --------------------------------------------------------------- service ----
class Service:
    """What the HTTP layer calls. All methods return plain dicts or raise AppError."""

    def __init__(self, settings: Settings | None = None, ble: ElacBLE | None = None):
        self.settings = settings or Settings()
        self.ble = ble or (DemoBLE if self.settings.demo else ElacBLE)(self.settings.address)

    def status(self) -> dict[str, Any]:
        return {"connected": self.ble.connected, "address": self.ble.address, "address_locked": self.settings.address_locked,
                "devices": self.settings.device_list(),
                "read_only": False, "write_supported": not self.settings.demo, "demo": self.settings.demo, "idle_timeout_s": self.settings.idle_timeout_s,
                "autoeq_device": self.autoeq_device_enabled, "autoeq_experimental": self.settings.autoeq_experimental,
                "autoeq_experimental_locked": self.settings.autoeq_experimental_locked,
                "auth": bool(self.settings.token)}

    # ---- AutoEQ (generator + upload). Real hardware only with ELAC_AUTOEQ_EXPERIMENTAL=1; demo is simulated.
    @property
    def autoeq_device_enabled(self) -> bool:
        return self.settings.demo or self.settings.autoeq_experimental

    def _autoeq_guard(self, body: dict[str, Any], needs_enable: bool = True) -> int:
        if not self.autoeq_device_enabled:
            raise AppError("autoeq_disabled", "Komendy AutoEQ są wyłączone. Włącz je w karcie AutoEQ.",
                           "AutoEQ device commands are disabled. Allow them in the AutoEQ card.", 403)
        if needs_enable and not body.get("enabled"):
            raise AppError("writes_disabled", "Najpierw włącz tryb zapisu.", "Enable device writes first.", 403)
        step = body.get("step", 0)
        if step not in (autoeq.STEP_NEAR, autoeq.STEP_FAR) or isinstance(step, bool):
            raise AppError("bad_value", "Krok musi wynosić 0 (blisko) lub 1 (daleko).", "Step must be 0 (near) or 1 (far).", 422)
        return step

    async def autoeq_start(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.ble.op_autoeq_start(self._autoeq_guard(body))

    async def autoeq_state(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.ble.op_autoeq_state(self._autoeq_guard(body, needs_enable=False))

    async def autoeq_stop(self, body: dict[str, Any]) -> dict[str, Any]:
        # No guard: stopping the generator must work even if the flag was switched off while it played.
        return await self.ble.op_autoeq_stop()

    def set_autoeq_experimental(self, enabled: Any) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise AppError("bad_value", "Wartość musi być true lub false.", "The value must be true or false.", 422)
        if self.settings.autoeq_experimental_locked and not enabled:
            raise AppError("locked", "Komendy AutoEQ są wymuszone zmienną ELAC_AUTOEQ_EXPERIMENTAL.",
                           "AutoEQ device commands are forced on by the ELAC_AUTOEQ_EXPERIMENTAL variable.", 409)
        self.settings.save_autoeq_experimental(enabled)
        logging.info("AutoEQ device commands %s from the GUI", "enabled" if enabled else "disabled")
        return {"autoeq_experimental": self.settings.autoeq_experimental, "autoeq_device": self.autoeq_device_enabled}

    async def autoeq_upload(self, body: dict[str, Any]) -> dict[str, Any]:
        step = self._autoeq_guard(body)
        levels = body.get("levels")
        n = len(autoeq.BANDS_HZ)
        if (not isinstance(levels, list) or len(levels) != n
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and autoeq.FLOOR_DB <= v <= 60 for v in levels)):
            raise AppError("bad_value", f"Pomiar musi zawierać {n} wartości dB.", f"The measurement must contain {n} dB values.", 422)
        return await self.ble.op_autoeq_upload(step, [float(v) for v in levels])

    async def connect(self) -> dict[str, Any]:
        await self.ble.op_connect()
        return {"connected": True, "address": self.ble.address}

    async def read(self) -> dict[str, Any]:
        return await self.ble.op_read()

    async def disconnect(self) -> dict[str, Any]:
        await self.ble.op_disconnect()
        return {"connected": False}

    async def write_setting(self, setting: dict[str, Any]) -> dict[str, Any]:
        if not setting.get("enabled"):
            raise AppError("writes_disabled", "Najpierw włącz tryb zapisu.", "Enable device writes first.", 403)
        if self.settings.demo:
            raise AppError("demo_write_blocked", "Tryb demo nie zapisuje do urządzenia.", "Demo mode cannot write to a device.", 403)
        return await self.ble.op_write_setting(setting)

    async def scan(self) -> dict[str, Any]:
        return {"devices": await self.ble.op_scan()}

    async def set_address(self, address: str) -> dict[str, Any]:
        if self.settings.address_locked:
            raise AppError("address_locked", "Adres jest ustawiony przez zmienną ELAC_ADDRESS.",
                           "The address is fixed by the ELAC_ADDRESS environment variable.", 409)
        normalized = normalize_address(address)
        await self.ble.op_set_address(normalized)
        self.settings.save_address(normalized)
        return {"address": normalized, "devices": self.settings.device_list()}

    def rename_device(self, address: str, name: str) -> dict[str, Any]:
        self.settings.rename_device(normalize_address(address), name)
        return {"devices": self.settings.device_list()}

    def forget_device(self, address: str) -> dict[str, Any]:
        self.settings.forget_device(normalize_address(address))
        return {"devices": self.settings.device_list()}

    async def idle_check(self) -> bool:
        """Drop the BLE link if nobody used it for ``idle_timeout_s`` (frees it for the phone app)."""
        limit = self.settings.idle_timeout_s
        if limit <= 0 or not self.ble.connected or self.ble.op_lock.locked():
            return False
        if time.monotonic() - self.ble.last_activity <= limit:
            return False
        logging.info("Idle for more than %.0fs: disconnecting", limit)
        await self.ble.op_disconnect()
        return True

    async def idle_loop(self, interval: float = 10.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.idle_check()
            except Exception:
                logging.exception("idle check failed")

    async def shutdown(self) -> None:
        await self.ble.close()

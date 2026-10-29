"""HTTP wrapper around server_core.Service with guarded, verified setting writes.

Environment variables (all optional):
  DEMO=1                    canned data, no Bluetooth needed
  ELAC_ADDRESS=<address>    BLE address; overrides config.json and locks the address in the GUI
  ELAC_IDLE_DISCONNECT_S    drop the BLE link after this many idle seconds (default 300, 0 = never)
  ELAC_ALLOWED_HOSTS        extra comma-separated Host names to accept (IP addresses and this machine's own
                            name are always accepted)
  ELAC_TOKEN                access token; required before the server may listen on the home network (see run.py)
  ELAC_DATA_DIR             folder for config.json and elac_app.log (default: the program folder)
  ELAC_AUTOEQ_EXPERIMENTAL=1  force-allow the AutoEQ generator/upload commands (normally switched in the GUI)
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from urllib.parse import parse_qs

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server_core import DATA_DIR, ROOT, TOKEN_COOKIE, Service, check_request, error_payload, token_matches

DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=DATA_DIR / "elac_app.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")

service = Service()


@asynccontextmanager
async def lifespan(app: FastAPI):
    idle = asyncio.create_task(service.idle_loop())
    try:
        yield
    finally:
        idle.cancel()
        try:
            await idle
        except asyncio.CancelledError:
            pass
        await service.shutdown()


app = FastAPI(title="ELAC SUB3030 Controller", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def guard(request: Request, call_next):
    reason = check_request(request.method, request.headers.get("host"), request.headers.get("origin"),
                           service.settings.allowed_hosts, request.headers.get("sec-fetch-site"))
    if reason:
        logging.warning("Blocked %s %s: %s", request.method, request.url.path, reason)
        return JSONResponse({"detail": reason, "detail_en": reason, "code": "forbidden"}, status_code=403)
    if request.url.path not in ("/", "/login") and not authorized(request):
        return JSONResponse({"detail": "Wymagane logowanie.", "detail_en": "Login required.", "code": "unauthorized"}, status_code=401)
    return await call_next(request)


def authorized(request: Request) -> bool:
    return token_matches(service.settings.token, request.cookies.get(TOKEN_COOKIE), request.headers.get("x-elac-token"))


def login_page(error: bool = False, status: int = 200) -> HTMLResponse:
    html = (ROOT / "static" / "login.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("<!--ERROR-->", '<p class="err">Nieprawidłowy token · Wrong token</p>' if error else ""),
                        status_code=status)


def failure(exc: Exception, default_status: int) -> JSONResponse:
    return JSONResponse(error_payload(exc), status_code=getattr(exc, "status", default_status))


class AddressBody(BaseModel):
    address: str


class WriteBody(BaseModel):
    enabled: bool = False
    kind: str
    value: int | None = None
    frequency_hz: float | None = None
    q: float | None = None
    gain_db: float | None = None
    index: int | None = None


@app.get("/")
async def index(request: Request):
    if not authorized(request):
        return login_page()
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/login")
async def login(request: Request):
    """Form post from login.html (urlencoded, parsed by hand so no extra dependency is needed)."""
    fields = parse_qs((await request.body()).decode("utf-8", "replace"))
    given = (fields.get("token") or [""])[0].strip()
    if not service.settings.token or not token_matches(service.settings.token, given):
        logging.warning("Failed login from %s", request.client.host if request.client else "?")
        return login_page(error=True, status=401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(TOKEN_COOKIE, given, max_age=365 * 24 * 3600, httponly=True, samesite="strict",
                        secure=service.settings.tls)
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(TOKEN_COOKIE)
    return response


@app.get("/api/status")
async def status():
    return service.status()


@app.post("/api/connect")
async def connect():
    try:
        return await service.connect()
    except Exception as e:
        logging.exception("Connect failed")
        return failure(e, 503)


@app.post("/api/read")
async def read_state():
    try:
        return await service.read()
    except Exception as e:
        logging.exception("Read state failed")
        return failure(e, 502)


@app.post("/api/write")
async def write_setting(body: WriteBody):
    try:
        return await service.write_setting(body.dict())
    except Exception as e:
        logging.exception("Write setting failed")
        return failure(e, 502)


@app.post("/api/disconnect")
async def disconnect():
    try:
        return await service.disconnect()
    except Exception as e:
        logging.exception("Disconnect failed")
        return failure(e, 500)


@app.post("/api/scan")
async def scan():
    try:
        return await service.scan()
    except Exception as e:
        logging.exception("Scan failed")
        return failure(e, 502)


@app.post("/api/address")
async def set_address(body: AddressBody):
    try:
        return await service.set_address(body.address)
    except Exception as e:
        logging.exception("Set address failed")
        return failure(e, 500)


class DeviceNameBody(BaseModel):
    address: str
    name: str = ""


@app.post("/api/devices/rename")
async def rename_device(body: DeviceNameBody):
    try:
        return service.rename_device(body.address, body.name)
    except Exception as e:
        return failure(e, 500)


@app.post("/api/devices/forget")
async def forget_device(body: AddressBody):
    try:
        return service.forget_device(body.address)
    except Exception as e:
        return failure(e, 500)


class AutoEqBody(BaseModel):
    enabled: bool = False
    step: int = 0
    levels: list[float] | None = None


async def _autoeq(call, body: AutoEqBody, label: str):
    try:
        return await call(body.dict())
    except Exception as e:
        logging.exception("AutoEQ %s failed", label)
        return failure(e, 502)


@app.post("/api/autoeq/start")
async def autoeq_start(body: AutoEqBody):
    return await _autoeq(service.autoeq_start, body, "start")


@app.post("/api/autoeq/state")
async def autoeq_state(body: AutoEqBody):
    return await _autoeq(service.autoeq_state, body, "state")


@app.post("/api/autoeq/stop")
async def autoeq_stop(body: AutoEqBody):
    return await _autoeq(service.autoeq_stop, body, "stop")


@app.post("/api/autoeq/upload")
async def autoeq_upload(body: AutoEqBody):
    return await _autoeq(service.autoeq_upload, body, "upload")


class FlagBody(BaseModel):
    enabled: bool


@app.post("/api/autoeq/experimental")
async def autoeq_experimental(body: FlagBody):
    try:
        return service.set_autoeq_experimental(body.enabled)
    except Exception as e:
        logging.exception("AutoEQ flag change failed")
        return failure(e, 500)

"""Start the ELAC SUB3030 web controller.

Settings come from environment variables (on Linux the service reads /etc/elac-sub3030/elac.env):

  ELAC_HOST      address to listen on; 127.0.0.1 (default) = this computer only, 0.0.0.0 = whole home network
  ELAC_PORT      port, default 8765
  ELAC_TOKEN     access token (at least 12 characters); required when ELAC_HOST is not a loopback address
  ELAC_TLS_CERT  + ELAC_TLS_KEY: serve HTTPS (needed for the AutoEQ microphone on a phone)

Listening on the network without a token is refused, because anyone on the Wi-Fi could
otherwise switch on device writes. ELAC_ALLOW_NO_TOKEN=1 overrides that check.
"""
from __future__ import annotations

import ipaddress
import os
import sys

from server_core import MIN_TOKEN_LEN


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def options(env: dict[str, str]) -> dict:
    """Validate the environment and return uvicorn.run() keyword arguments. Raises SystemExit with a message."""
    host = env.get("ELAC_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(env.get("ELAC_PORT", "8765"))
    except ValueError:
        raise SystemExit("ELAC_PORT must be a number") from None
    if not 1 <= port <= 65535:
        raise SystemExit("ELAC_PORT must be between 1 and 65535")
    token = env.get("ELAC_TOKEN", "").strip()
    if token and len(token) < MIN_TOKEN_LEN:
        raise SystemExit(f"ELAC_TOKEN must be at least {MIN_TOKEN_LEN} characters")
    if not is_loopback(host) and not token and env.get("ELAC_ALLOW_NO_TOKEN", "").strip() != "1":
        raise SystemExit(f"Refusing to listen on {host} without ELAC_TOKEN: anyone on the network could control the subwoofer.")
    cert, key = env.get("ELAC_TLS_CERT", "").strip(), env.get("ELAC_TLS_KEY", "").strip()
    if bool(cert) != bool(key):
        raise SystemExit("Set both ELAC_TLS_CERT and ELAC_TLS_KEY, or neither")
    for path in (cert, key):
        if path and not os.path.isfile(path):
            raise SystemExit(f"TLS file not found: {path}")
    kwargs = {"host": host, "port": port, "log_level": "info"}
    if cert:
        kwargs.update(ssl_certfile=cert, ssl_keyfile=key)
    return kwargs


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    kwargs = options(dict(os.environ))
    import uvicorn

    scheme = "https" if "ssl_certfile" in kwargs else "http"
    print(f"ELAC SUB3030 controller on {scheme}://{kwargs['host']}:{kwargs['port']}", file=sys.stderr)
    uvicorn.run("app:app", **kwargs)


if __name__ == "__main__":
    main()

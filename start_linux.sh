#!/usr/bin/env bash
# Run from a checkout (development). For a permanent install on Debian/Proxmox see deploy/README.md.
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -r requirements.txt; }
exec .venv/bin/python run.py

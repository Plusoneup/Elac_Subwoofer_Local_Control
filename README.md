# Elac Subwoofer Local Control

[![tests](https://github.com/Plusoneup/Elac_Subwoofer_Local_Control/actions/workflows/tests.yml/badge.svg)](https://github.com/Plusoneup/Elac_Subwoofer_Local_Control/actions/workflows/tests.yml)

A local web panel for the ELAC SUB3030 subwoofer over Bluetooth LE: volume, preset, LPF, phase,
delay, Auto-On, PEQ and AutoEQ, without the phone app and without any cloud service.

Unofficial project, not affiliated with or endorsed by ELAC. The protocol was worked out for
interoperability. Writing settings to the subwoofer is at your own risk.

Tested on one SUB3030 with firmware v1.1.956.23 (details in `WRITE_ENABLE_NOTES.md`). Other models
or a firmware update may behave differently; writes stay off until you switch them on.

## Run on Windows (this computer only)

1. Install **Python 3.12** (from python.org, with the `py` launcher).
2. Download this repository (Code → Download ZIP) and extract it to a normal folder.
3. Run `install_windows.bat` once.
4. Power on the subwoofer, close the ELAC app on your phone, then run `start_windows.bat`.
   The panel opens at <http://127.0.0.1:8765>.
5. In **Connection**, press **Scan** and choose your subwoofer. The choice is remembered.

## Run on Proxmox (whole home network)

Needs a Bluetooth adapter on the Proxmox host (built-in or USB). In the Proxmox web UI open the
node → **Shell** and run:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Plusoneup/Elac_Subwoofer_Local_Control/main/deploy/proxmox-helper.sh)"
```

The script creates a small Debian VM, passes the Bluetooth adapter to it, installs the panel and
prints the address and access token. Open that address on your phone (same Wi-Fi), log in with the
token, then **Connection → Scan** and choose your subwoofer. Run the same command again to update.

The panel is meant for the home network only. Do not expose it to the internet: no port
forwarding and no public reverse proxy. For access away from home, use a VPN into your network.

Licence: MIT (see `LICENSE`).

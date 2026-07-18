# MXW01 local print server

Reverse-engineered BLE client and REST API for the **MXW01** thermal printer
(Fun Print / Miaoxuewang family). No mobile app required.

Everything in `docs/` and `logs/` was produced by live experiments against
unit `48:0F:57:49:DB:3B`. Conclusions are not assumed — see the step logs.

## Quick start

```bash
cd printer_re
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# one-shot CLI-style print via the library
python - <<'PY'
import asyncio
from printer import MXW01

async def main():
    p = MXW01("48:0F:57:49:DB:3B")
    await p.connect()
    print(await p.status())
    await p.print_text("Hello from printer.py")
    await p.disconnect()

asyncio.run(main())
PY

# REST API
uvicorn api:app --host 0.0.0.0 --port 8080
```

## Docker

```bash
docker compose up -d --build
curl localhost:8080/health
```

Open the upload UI at http://localhost:8080/

The container talks to the printer through the **host's** bluetoothd, so it
needs three things (already in `docker-compose.yml`):

- `network_mode: host` — API reachable without NAT
- `/var/run/dbus` mounted read-only — Bleak/bluetoothctl reach host BlueZ
- `security_opt: apparmor=unconfined` — Ubuntu's default Docker AppArmor
  profile blocks the D-Bus `AddMatch` calls Bleak requires
- `render_output/` bind mount — persistent previews and JSON quality reports
- `render-cache` volume — cached high-DPI PDF pages

No Bluetooth daemon runs inside the container; `bluez` is installed there
only for the `bluetoothctl` client. Configure via `MXW01_ADDRESS`,
`MXW01_INTENSITY`, `RENDER_CONFIG`, and `PORT` environment variables.

```bash
curl localhost:8080/health
curl localhost:8080/status
curl -X POST localhost:8080/print/text \
  -H 'content-type: application/json' \
  -d '{"text":"Hello World","queue":false}'

# Highest-quality automatic PDF rendering (queued by default)
curl -X POST localhost:8080/print/pdf \
  -F file=@document.pdf -F queue=true
```

## PDF quality pipeline

`pipeline.py` renders PDFs at high DPI, removes content-safe margins, evaluates
all four orientations, enhances for 203-DPI thermal output, compares five
threshold and six dither algorithms, trims trailing paper, and produces
byte-aligned packed pages. Configuration is in `rendering.yaml`.

```bash
python pipeline.py document.pdf --config rendering.yaml
```

See `docs/rendering.md` for the scoring model, report fields, and nine preview
stages.

## What was proven

| Step | Result |
|---|---|
| 1 Bluetooth inventory | BLE advertiser, service 0xAF30, no Classic |
| 2 Transport class | **BLE GATT + vendor protocol (98%)** |
| 3 Open-source match | MXW01 `22 21` framing (not GB01 `51 78`) |
| 4 Connect | Bleak OK after PreferredBearer=le |
| 5 GATT table | AE01/AE02/AE03 are the print path |
| 6 Safe writes | empty/0x00 benign; A1/AB/B1/B0 answered |
| 7 Sniff | btmon session + Android HCI recipe documented |
| 8 Image protocol | 384 px, 1 bpp LSB-left, CRC8 on control frames |
| 9 Test prints | hello / rect / checker / QR / PNG — all got AA |
| 10–11 | `printer.py` + FastAPI queue server |
| 12 | this `docs/` tree |

## Layout

```
printer_re/
  printer.py          reusable client
  pipeline.py         PDF rendering orchestrator and CLI
  rendering.yaml      quality and printer configuration
  api.py              FastAPI server
  requirements.txt
  docs/               bluetooth_report, protocol, packet_analysis, …
  logs/               raw command outputs from every experiment
```

## BlueZ tip

If `bluetoothctl connect` fails with `br-connection-page-timeout`:

```
bluetoothctl scan le
bluetoothctl bearer 48:0F:57:49:DB:3B le
```

Setting the bearer alone usually brings the LE link up.

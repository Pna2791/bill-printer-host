# Architecture

```
                 HTTP clients
                      │
                      ▼
              ┌───────────────┐
              │  FastAPI app  │  api.py
              │  /print/*     │
              │  /queue       │
              └───────┬───────┘
                      │ enqueue / direct call
                      ▼
              ┌───────────────┐
              │  PrintQueue   │  background asyncio worker
              │  retry ≤ 2    │  FIFO job map
              └───────┬───────┘
                      │
                      ▼
              ┌───────────────┐
              │    MXW01      │  printer.py
              │  asyncio.Lock │  connect / status / print_*
              └───────┬───────┘
                      │ Bleak GATT
                      ▼
              ┌───────────────┐
              │ BlueZ 5.85    │  PreferredBearer=le pin
              │ hci0 (BT 4.2) │
              └───────┬───────┘
                      │ LE ACL
                      ▼
                   MXW01 printer
              AE01 ctrl / AE02 notify / AE03 data
```

## Modules

| File | Role |
|---|---|
| `printer.py` | Protocol + image encoding + BlueZ connection quirks |
| `api.py` | REST surface, queue, lifespan connect/disconnect |
| `print_test.py` | Step 9 experiment harness (kept for regression) |
| `safe_writes.py` | Step 6 experiment harness |
| `gatt_discover.py` | Step 5 enumerator |
| `docs/` | Reports produced by Steps 1–12 |
| `logs/` | Raw command and experiment outputs |

## Connection strategy (load-bearing)

Bleak 3.x refuses to open a client unless the target was seen in a recent
scan. The MXW01 stops advertising once connected, and BlueZ 5.85 will try
BR/EDR paging (which always fails) unless `PreferredBearer=le` is set.

`MXW01.connect()` therefore:

1. Runs `bluetoothctl scan le` if needed.
2. Sets `bearer <MAC> le` (this alone often auto-establishes the LE link).
3. Builds a `BLEDevice` from the live BlueZ D-Bus object (path + props) so
   Bleak can attach without a fresh advertisement.
4. Subscribes to AE02 notifications.

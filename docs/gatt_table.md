# GATT Table — MXW01 (48:0F:57:49:DB:3B)

Enumerated experimentally with Bleak 3.0.2 over BlueZ 5.85.
Raw log: `printer_re/logs/07_gatt_discovery.log`.

## Service 0x1800 — Generic Access Profile (handle 1)

| Char UUID | Handle | Properties | Value read |
|---|---|---|---|
| 0x2A00 Device Name | 2 | READ, WRITE | `4d 58 57 30 31` = "MXW01" |

## Service 0x1801 — Generic Attribute Profile (handle 4)

| Char UUID | Handle | Properties | Descriptors |
|---|---|---|---|
| 0x2A05 Service Changed | 5 | INDICATE | CCCD (handle 7) = `02 00` (indications pre-armed) |

## Service 0xAE30 — Vendor specific, main printer service (handle 8)

| Char UUID | Handle | Properties | Descriptors | Role (verified Steps 6 & 9) |
|---|---|---|---|---|
| 0xAE01 | 9 | WRITE_NO_RESPONSE | — | Control commands (`22 21 … FF`) |
| 0xAE02 | 11 | NOTIFY | CCCD (13) | Status / A9 ack / AA complete |
| 0xAE03 | 14 | WRITE_NO_RESPONSE | — | Raw 1bpp image rows (no framing) |
| 0xAE04 | 16 | NOTIFY | CCCD (18) | Unknown (never fired in tests) |
| 0xAE05 | 19 | INDICATE | CCCD (21) | Unknown (never fired in tests) |
| 0xAE10 | 22 | READ, WRITE | — | Reads `00 00 00 00`; purpose unknown |

## Service 0xAE3A — Vendor specific, presumed OTA/firmware (handle 64)

| Char UUID | Handle | Properties | Descriptors |
|---|---|---|---|
| 0xAE3B | 65 | WRITE_NO_RESPONSE | — |
| 0xAE3C | 67 | NOTIFY | CCCD (69) |

**Deliberately not touched** — on this device family the second vendor service
is associated with firmware update; writing to it risks bricking the printer.

## Connection parameters

- Address type: public, LE only (BR/EDR ruled out — see bluetooth_report.md)
- Pairing/bonding: not required, GATT fully open
- ATT MTU: see `logs/13_safe_writes.log` (negotiated at data-transfer time)

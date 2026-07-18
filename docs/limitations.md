# Limitations

## Hardware / radio

- Host adapter is Bluetooth 4.2 (Broadcom UART). LE works; throughput is
  limited by the default ATT MTU of 23 (20-byte chunks). Prints succeed but
  take a few seconds for larger images.
- The printer advertises the "Simultaneous LE and BR/EDR" flag but has **no
  Classic radio**. BlueZ must be pinned to `PreferredBearer=le` or connects
  fail with page-timeout and the device may disappear from the cache.
- Connection is flaky if something else holds the LE link (phone, previous
  zombie BlueZ session). Always disconnect before retrying.

## Protocol

- Only 1 bpp mode (`A9` mode byte `0x00`) was verified. The Fun Print "HD"
  (likely 4 bpp) path was not exercised.
- AE04 / AE05 / AE10 / service 0xAE3A were not reverse-engineered; AE3A is
  presumed OTA and is intentionally avoided.
- No-paper / overheat / low-battery error paths were not reproduced on this
  unit; the A1 payload layout for those flags may differ from older docs.
- Minimum job size of 4320 bytes (90 rows) is an observed practical floor,
  not a documented constant — shorter jobs may work but were not proven.

## Software

- `printer.py` shells out to `bluetoothctl` for bearer pinning. Requires
  BlueZ tools on PATH and permission to use the system D-Bus / HCI device.
- No multi-printer support; one `MXW01` instance per process.
- The REST queue is in-memory; jobs vanish on process restart.
- Text rendering depends on DejaVu Sans being installed; falls back to
  Pillow's tiny default font otherwise.
- Image rotation defaults to 180° so output reads right-side-up as the
  printer feeds top-first; pass `rotate_180=False` to override.

## Out of scope

- Pairing / bonding (unnecessary on this firmware).
- Firmware updates.
- ESC/POS compatibility (not applicable — wrong transport and framing).
- Fun Print cloud / account features.

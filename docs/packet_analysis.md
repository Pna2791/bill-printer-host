# Packet Analysis — MXW01

All packets below were captured live on this unit (`48:0F:57:49:DB:3B`)
using Bleak + a concurrent `btmon` session. Raw logs live in `printer_re/logs/`.

## HCI / transport notes

- Link type is **LE ACL only**. A plain BlueZ `connect` on BlueZ 5.85 tries
  BR/EDR first (printer advertises the dual-mode flag but has no Classic radio),
  pages out, and sometimes deletes the device. Working sequence:

      scan le → bearer <MAC> le   # PreferredBearer=le auto-establishes LE

- Pairing/bonding never required.
- Default ATT MTU reported by Bleak: 23 → 20-byte write chunks. Transfers still
  succeed; larger MTU is optional.

## Framing (AE01 write / AE02 notify)

```
22 21 | CMD | FLAGS | LEN_LO LEN_HI | PAYLOAD[LEN] | CRC8(PAYLOAD) | FF
```

CRC-8: poly `0x07`, init `0x00`, no reflect, no xorout. Scope = payload only.
Notifications omit the CRC byte (footer is just the last payload byte / `00`).

## Step 6 — Safe writes (log `13_safe_writes.log`)

| Write | Hex | Result |
|---|---|---|
| empty | _(0 bytes)_ | accepted, no notify, no disconnect |
| single `00` | `00` | accepted, no notify, no disconnect |
| A1 status | `22 21 a1 00 01 00 00 00 ff` | notify `22 21 a1 03 0a 00 00 00 00 5d 19 00 00 00 a2 00 00` |
| AB battery | `22 21 ab 00 01 00 00 00 ff` | notify `22 21 ab 00 01 00 5d 00` → 0x5D = 93% |
| B1 version | `22 21 b1 00 01 00 00 00 ff` | notify `… 31 2e 39 2e 33 2e 31 2e 31` → `"1.9.3.1.1"` |
| B0 type | `22 21 b0 00 01 00 00 00 ff` | notify `22 21 b0 00 01 00 00 00` |

AE04 / AE05 never produced notifications during these tests.

### A1 status payload layout (10 bytes, this firmware)

```
[0] 00
[1] 00
[2] 00
[3] battery   (0x5D ≈ 93%)
[4] temperature (0x19–0x1D ≈ 25–29 °C observed while printing)
[5..7] 00 00 00
[8] unknown (0xA2 / 0xA5 observed)
[9] 00
```

Overall "error flag" described by some docs as payload[12] of a longer
response was not present on this firmware; no-paper / overheat were not
reproduced.

## Step 9 — Print sequence (log `21_print_hello.log`)

```
→ AE01  A2 intensity     22 21 a2 00 01 00 5d 94 ff
→ AE01  A1 status        22 21 a1 00 01 00 00 00 ff
← AE02  A1 response      22 21 a1 03 0a 00 …
→ AE01  A9 print request 22 21 a9 00 04 00 78 00 30 00 7b ff
                           lines=0x0078 (120), fixed=0x30, mode=0x00 (1bpp)
← AE02  A9 ack           22 21 a9 00 01 00 00 00   (status 0x00 = OK)
→ AE03  raw bitmap       5760 bytes in 20-byte chunks, ~15 ms spacing
→ AE01  AD flush         22 21 ad 00 01 00 00 00 ff
← AE02  AA complete      22 21 aa 00 03 00 00 bd bd 00
```

### Jobs that completed with AA

| Job | Lines | Bytes | AA payload | Log |
|---|---|---|---|---|
| hello | 120 | 5760 | `00 bd bd` | `21_print_hello.log` |
| rect | 90 | 4320 | `00 98 98` | `21_print_rect.log` |
| checker | 96 | 4608 | `00 7d 7d` | `21_print_checker.log` |
| qr | 194 | 9312 | `00 4b 4b` | `21_print_qr.log` |
| png | 160 | 7680 | `00 ee ee` | `21_print_png.log` |

AA payload pattern `00 XX XX`: first byte 0x00 (OK), then a duplicated
unknown byte (possibly a rolling counter / checksum of the job).

## Image encoding (experimentally confirmed)

- Width **384 px** fixed → **48 bytes/row**
- 1 bit/pixel, **LSB = leftmost pixel**, black = 1, white = 0
- No RLE, no per-row framing, no CRC on AE03
- Rows concatenated top→bottom
- Jobs shorter than 90 rows padded with `0x00` to 4320 bytes (observed
  minimum that printed cleanly)
- Dithering: Pillow `Image.convert("1")` (Floyd–Steinberg) used for the
  gradient PNG test; solid patterns used pure threshold

## Not ESC/POS

No `ESC @`, `GS v 0`, or any ASCII control sequence appears on the wire.
The entire application protocol is the proprietary `22 21 … FF` framing
above, carried over BLE GATT.

## Android HCI capture (optional cross-check)

If you want to compare a Fun Print print job against these packets:

1. On the phone: *Developer options → Enable Bluetooth HCI snoop log*.
2. Toggle Bluetooth off/on.
3. Print one page from Fun Print.
4. Pull the log:
   - Android 4.4–10: `adb pull /sdcard/btsnoop_hci.log`
   - Android 11+: `adb bugreport bugreport.zip` then extract
     `FS/data/misc/bluetooth/logs/btsnoop_hci.log`
5. Open in Wireshark (`btmon`/btsnoop format) and filter
   `btatt.opcode == 0x52` (Write Command) on handles for AE01/AE03.
6. Expect the same `22 21 a2 / a1 / a9 / ad` sequence and raw AE03 bitmap.

We did not need this step: the open-source MXW01 framing matched our unit
exactly under live experiment.

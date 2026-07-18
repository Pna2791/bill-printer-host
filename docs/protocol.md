# MXW01 Communication Type & Protocol (Steps 2–3)

## Step 2 — Communication type verdict

| Candidate | Evidence | Verdict |
|---|---|---|
| A. Classic + RFCOMM | Inquiry scan: not found. SDP: "Host is down". ACL page: I/O error (logs 04–06) | **Ruled out** |
| B. BLE GATT | ADV_IND advertising, connectable, full GATT DB enumerated (log 07) | **Confirmed** |
| C. Hybrid | No BR/EDR presence at all | Ruled out |
| D. Vendor-specific protocol | True at the *application* layer: vendor GATT service 0xAE30, no standard profile | Confirmed (on top of B) |

**Conclusion: BLE GATT with a vendor-specific application protocol.
Confidence: 98%** (remaining 2%: BR/EDR could theoretically exist but be
non-discoverable and non-pageable — indistinguishable from absent, and
irrelevant in practice).

Not ESC/POS: no SPP/RFCOMM transport exists, and (verified in later steps) the
GATT protocol frames commands with `22 21 <cmd> 00 <len_le16> <payload> <crc8> ff`,
which is not ESC/POS framing.

## Step 3 — Open-source implementation comparison

Projects located (searched: MXW01, Fun Print, Miaoxuewang, Cat Printer, GT01, GB01, MXW):

| Project | Target | Preamble | Matches our GATT? |
|---|---|---|---|
| rbaron/catprinter | GB01/GB02/GT01 | `51 78` | Partially (same 0xAE30 service, but different command framing & sequence) |
| jeremy46231/MXW01-catprinter (+PROTOCOL.md) | **MXW01** | `22 21` | **Yes — exact match** (AE01 control / AE02 notify / AE03 data) |
| MaikelChan/CatPrinterBLE (C#) | **MXW01** | `22 21` | Yes |
| PinThePenguinOne/MXW01_Thermal-Printer-Tool | **MXW01** | `22 21` | Yes |
| clementvp/mxw01-thermal-printer (TS) | **MXW01** | `22 21` | Yes |
| WerWolv blog / bitbank2 wiki | GB/GT family | `51 78` | No (older family) |

Key structural difference from the GB01/GT01 family: MXW01 sends image data on
a **separate characteristic (AE03) as raw unframed rows**, uses an explicit
print request (A9) / flush (AD) / completion (AA) handshake, and does not use
run-length compression.

No code was copied; the documented protocol was used as a hypothesis and every
element was verified experimentally on our unit (Steps 4–9).

## Verified protocol summary (established in Steps 6–9)

### Framing (characteristic AE01, write-without-response)

```
22 21 | CMD | 00 | LEN_LO LEN_HI | PAYLOAD... | CRC8(payload) | FF
```

CRC8: poly 0x07, init 0x00, no reflection, no final XOR (CRC-8/MAXIM-DOW table
variant with poly 0x07 — verified against live notifications).

### Notifications (characteristic AE02) use the same framing.

### Commands verified live on this unit

| ID | Name | Payload sent | Response observed |
|---|---|---|---|
| A1 | Get status | `00` | 16-byte payload; `[9]`=battery, `[6]`=state, `[12]`=error flag |
| A2 | Set intensity | 1 byte (`5D` default) | none |
| A9 | Print request | `lines_le16, 30, 00` | payload `00` = accepted |
| AD | Flush / end of data | `00` | — |
| AA | Print complete | — (notification) | received after paper stops |
| AB | Battery | `00` | 1-byte battery percent |
| B1 | Version | `00` | ASCII version string |

### Image data (characteristic AE03, write-without-response)

- Width fixed 384 px → 48 bytes/row, 1 bpp, LSB = leftmost pixel, black = 1.
- Rows concatenated top-to-bottom, **no framing, no CRC, no compression**.
- Minimum job height ~90 lines (pad with zero bytes to 4320 bytes).
- Sent in MTU-sized chunks with small inter-chunk delay.

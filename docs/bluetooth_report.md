# Bluetooth Report — MXW01 Thermal Printer

Date: 2026-07-18
Host: Linux, BlueZ 5.85, adapter hci0 (Broadcom, BT 4.2, `88:E9:FE:53:63:16`)
Target: `48:0F:57:49:DB:3B` ("MXW01")

All raw command outputs are in `printer_re/logs/`. A full HCI capture of the whole
session is in `logs/btmon_session.snoop` / `logs/btmon_session.txt`.

## 1. Discovery results (experimental)

### 1.1 Dual-mode scan (`bluetoothctl scan on`, log `02_scan_dual.log`)

The device was found **only as a BLE advertiser**:

```
[NEW] Device 48:0F:57:49:DB:3B MXW01
```

### 1.2 Advertisement contents (btmon, log `btmon_session.txt`)

```
Event type: Connectable undirected - ADV_IND
Address type: Public
Address: 48:0F:57:49:DB:3B
Flags: 0x0a (LE General Discoverable, Simultaneous LE+BR/EDR controller)
16-bit Service UUIDs (complete): 0xAF30 (unknown/vendor)
Name (complete): MXW01
Company ID: 0x0F48 (not an assigned Bluetooth SIG company ID)
Manufacturer data: 57 49 db 3b
RSSI: -38 to -43 dBm
Scan response: empty (data length 0)
```

Observations:

- The "manufacturer data" is actually the device's own MAC address: company key
  `0x0F48` = first two MAC bytes (48:0F byte-swapped), value `57 49 db 3b` = the
  remaining four bytes. The vendor abuses the manufacturer-data AD field to
  broadcast the MAC.
- No appearance field, no TX-power field, no service data.
- Address type is **Public**, advertising type **ADV_IND** (connectable).

### 1.3 Bluetooth Classic probes — all negative

| Experiment | Log | Result |
|---|---|---|
| `hcitool scan` (BR/EDR inquiry, 10 s) | `04_classic_inquiry.log` | Printer NOT found |
| `sdptool browse 48:0F:57:49:DB:3B` | `05_classic_probe.log` | `Host is down` (page timeout — no BR/EDR radio listening) |
| `hcitool info` / `hcitool cc` (as root) | `06_classic_probe_root.log` | `Input/output error` (ACL page failed) |

Conclusion: the printer does **not** respond on BR/EDR at all. No RFCOMM/SPP.
(`sdptool` RFCOMM channel discovery is therefore not applicable.)

### 1.4 Device properties after LE connect (`bluetoothctl info`, log `11_bluetoothctl_connect.log`)

```
Paired: no        Bonded: no       Trusted: no
LegacyPairing: no Connected: yes (works WITHOUT pairing)
UUIDs resolved: 0x1800 GAP, 0x1801 GATT, 0xAE30 (vendor), 0xAE3A (vendor), 0xAF30 (advertised)
```

Pairing/bonding is **not required** — a plain LE connection succeeds and GATT is
fully accessible.

## 2. GATT database (Bleak enumeration, log `07_gatt_discovery.log`)

MTU after connect: 23 (default; ATT payload 20 bytes — BlueZ acquires the real
MTU lazily; see gatt_table.md for the negotiated value used during data transfer).

| Service | Characteristic | Handle | Properties | Read value |
|---|---|---|---|---|
| 0x1800 GAP | 0x2A00 Device Name | 2 | read, write | `MXW01` |
| 0x1801 GATT | 0x2A05 Service Changed | 5 | indicate | — |
| **0xAE30 (vendor)** | 0xAE01 | 9 | write-without-response | — |
| | 0xAE02 | 11 | notify | — |
| | 0xAE03 | 14 | write-without-response | — |
| | 0xAE04 | 16 | notify | — |
| | 0xAE05 | 19 | indicate | — |
| | 0xAE10 | 22 | read, write | `00 00 00 00` |
| **0xAE3A (vendor)** | 0xAE3B | 65 | write-without-response | — |
| | 0xAE3C | 67 | notify | — |

Notes:

- There is **no Device Information Service (0x180A)**, no battery service, no
  standard printing profile of any kind.
- The AE01–AE10 layout is characteristic of the "cat printer" family
  (GB01/GB02/GT01/MX-series ODM boards).
- Service 0xAE3A (AE3B write / AE3C notify) is typically the OTA/firmware-update
  channel on these boards — to be avoided.

## 3. Summary of facts established experimentally

- Device type: **BLE-only peripheral** (GATT server), BT 4.x, public address.
- RSSI at test distance: −38 … −43 dBm.
- Advertised service: 0xAF30; GATT services: 0x1800, 0x1801, 0xAE30, 0xAE3A.
- Manufacturer data: own MAC embedded (no real company ID).
- Appearance: not advertised.
- Supported profiles: none standard — vendor-specific GATT only.
- Pairing status: unpaired, unbonded; connection works without pairing.
- Bluetooth Classic: ruled out experimentally (inquiry, SDP, paging all fail).

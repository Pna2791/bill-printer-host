"""Step 6 — Safe write experiments against MXW01.

Strategy: leave the LE link already established by bluetoothctl (or establish
it), then attach with Bleak via the BlueZ D-Bus path so we don't need a fresh
advertisement. Falls back to scan+connect if the device is idle.
"""
import asyncio
import subprocess
import sys
import time
from bleak import BleakClient, BleakScanner
from bleak.backends.bluezdbus.manager import get_global_bluez_manager

ADDR = "48:0F:57:49:DB:3B"
AE01 = "0000ae01-0000-1000-8000-00805f9b34fb"
AE02 = "0000ae02-0000-1000-8000-00805f9b34fb"
AE04 = "0000ae04-0000-1000-8000-00805f9b34fb"
AE05 = "0000ae05-0000-1000-8000-00805f9b34fb"

T0 = time.monotonic()


def log(msg):
    print(f"[{time.monotonic()-T0:7.2f}] {msg}", flush=True)


def crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def frame(cmd: int, payload: bytes) -> bytes:
    return (
        bytes([0x22, 0x21, cmd, 0x00, len(payload) & 0xFF, len(payload) >> 8])
        + payload
        + bytes([crc8(payload), 0xFF])
    )


def ensure_connected_via_bluetoothctl():
    info = subprocess.run(
        ["bluetoothctl", "info", ADDR], capture_output=True, text=True
    ).stdout
    if "Connected: yes" in info and "LE.Connected: yes" in info:
        log("already LE-connected via BlueZ")
        return
    log("scanning + connecting via bluetoothctl")
    subprocess.run(["bluetoothctl", "bearer", ADDR, "le"], capture_output=True)
    subprocess.run(
        ["bluetoothctl", "--timeout", "12", "scan", "le"],
        capture_output=True,
    )
    r = subprocess.run(
        ["bluetoothctl", "connect", ADDR], capture_output=True, text=True
    )
    log(f"bluetoothctl connect: {r.stdout.strip()} {r.stderr.strip()}")
    time.sleep(2)


async def find_bluez_path() -> str | None:
    manager = await get_global_bluez_manager()
    for path, props in manager._properties.items():
        if "org.bluez.Device1" not in props:
            continue
        if props["org.bluez.Device1"].get("Address") == ADDR:
            log(f"found BlueZ path {path} Connected={props['org.bluez.Device1'].get('Connected')}")
            return path
    return None


async def main():
    ensure_connected_via_bluetoothctl()
    path = await find_bluez_path()

    def on_disconnect(_):
        log("!!! DISCONNECTED callback")

    client = None
    if path:
        try:
            c = BleakClient(path, disconnected_callback=on_disconnect, timeout=20.0)
            await c.connect()
            client = c
            log("connected via BlueZ path")
        except Exception as e:
            log(f"path connect failed: {type(e).__name__}: {e}")

    if client is None:
        for attempt in range(1, 4):
            try:
                device = await BleakScanner.find_device_by_address(ADDR, timeout=12.0)
                if device is None:
                    log(f"scan miss attempt {attempt}")
                    continue
                c = BleakClient(device, disconnected_callback=on_disconnect, timeout=20.0)
                await c.connect()
                client = c
                log(f"connected via scan attempt {attempt}")
                break
            except Exception as e:
                log(f"scan connect attempt {attempt} FAILED: {type(e).__name__}: {e}")
                await asyncio.sleep(2)

    if client is None:
        log("giving up")
        sys.exit(2)

    try:
        log(f"connected={client.is_connected} mtu={client.mtu_size}")

        def notif(name):
            def cb(char, data: bytearray):
                log(f"NOTIFY {name}: {bytes(data).hex(' ')}")
            return cb

        for uuid, name in [(AE02, "AE02"), (AE04, "AE04"), (AE05, "AE05")]:
            try:
                await client.start_notify(uuid, notif(name))
                log(f"subscribed {name}")
            except Exception as e:
                log(f"subscribe {name} FAILED: {e}")

        async def wr(desc, data: bytes):
            try:
                await client.write_gatt_char(AE01, data, response=False)
                log(f"WRITE ok  ({desc}): {data.hex(' ') if data else '(empty)'}")
            except Exception as e:
                log(f"WRITE FAIL ({desc}): {e}")
            await asyncio.sleep(2.0)

        await wr("empty", b"")
        await wr("single 0x00", b"\x00")
        await wr("A1 get status", frame(0xA1, b"\x00"))
        await wr("AB battery", frame(0xAB, b"\x00"))
        await wr("B1 version", frame(0xB1, b"\x00"))
        await wr("B0 print type", frame(0xB0, b"\x00"))

        log("waiting 5s for late notifications...")
        await asyncio.sleep(5)
        log(f"still connected: {client.is_connected}")
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log(f"disconnect error: {e}")
        subprocess.run(["bluetoothctl", "disconnect", ADDR], capture_output=True)

    log("clean exit")


asyncio.run(main())

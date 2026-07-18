"""Step 8/9 — Print test pages to MXW01.

Sequence (from experimentally-confirmed framing + open-source MXW01 docs):
  subscribe AE02 → A2 intensity → A1 status → A9 print request →
  AE03 image rows → AD flush → wait for AA complete.
"""
import asyncio
import argparse
import subprocess
import sys
import time
from pathlib import Path

from bleak import BLEDevice, BleakClient, BleakScanner
from bleak.backends.bluezdbus.manager import get_global_bluez_manager
from PIL import Image, ImageDraw, ImageFont
import qrcode

ADDR = "48:0F:57:49:DB:3B"
AE01 = "0000ae01-0000-1000-8000-00805f9b34fb"
AE02 = "0000ae02-0000-1000-8000-00805f9b34fb"
AE03 = "0000ae03-0000-1000-8000-00805f9b34fb"
WIDTH = 384
BYTES_PER_ROW = WIDTH // 8  # 48
MIN_BYTES = 4320  # ~90 rows

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


def image_to_1bpp(img: Image.Image) -> bytes:
    """Convert PIL image to MXW01 1bpp: 384px wide, LSB=left, black=1."""
    img = img.convert("L")
    w, h = img.size
    if w != WIDTH:
        new_h = max(1, int(h * WIDTH / w))
        img = img.resize((WIDTH, new_h), Image.Resampling.LANCZOS)
        h = new_h
    # simple threshold + Floyd-Steinberg-ish: use convert("1") dither
    bw = img.convert("1")  # Pillow dither to 1-bit (black=0 in PIL)
    out = bytearray()
    px = bw.load()
    for y in range(h):
        for x_byte in range(BYTES_PER_ROW):
            b = 0
            for bit in range(8):
                x = x_byte * 8 + bit
                # PIL "1" mode: 0=black, 255=white. We want black=1.
                if px[x, y] == 0:
                    b |= 1 << bit  # LSB = leftmost
            out.append(b)
    if len(out) < MIN_BYTES:
        out.extend(b"\x00" * (MIN_BYTES - len(out)))
    return bytes(out)


def make_hello() -> Image.Image:
    img = Image.new("L", (WIDTH, 120), 255)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
    except OSError:
        font = ImageFont.load_default()
    d.text((20, 30), "Hello World", fill=0, font=font)
    return img


def make_black_rect() -> Image.Image:
    img = Image.new("L", (WIDTH, 80), 255)
    d = ImageDraw.Draw(img)
    d.rectangle((20, 10, WIDTH - 20, 70), fill=0)
    return img


def make_checkerboard() -> Image.Image:
    img = Image.new("L", (WIDTH, 96), 255)
    d = ImageDraw.Draw(img)
    size = 16
    for y in range(0, 96, size):
        for x in range(0, WIDTH, size):
            if ((x // size) + (y // size)) % 2 == 0:
                d.rectangle((x, y, x + size - 1, y + size - 1), fill=0)
    return img


def make_qr(text: str = "https://mxw01.local/hello") -> Image.Image:
    qr = qrcode.QRCode(border=2, box_size=6)
    qr.add_data(text)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("L")
    # center on 384-wide canvas
    canvas = Image.new("L", (WIDTH, qr_img.height + 20), 255)
    canvas.paste(qr_img, ((WIDTH - qr_img.width) // 2, 10))
    return canvas


def make_png_sample() -> Image.Image:
    # tiny PNG-like drawing (gradient + circle) rendered then dithered
    img = Image.new("L", (WIDTH, 160), 255)
    d = ImageDraw.Draw(img)
    for y in range(160):
        shade = int(255 * y / 159)
        d.line([(0, y), (WIDTH - 1, y)], fill=shade)
    d.ellipse((142, 30, 242, 130), outline=0, width=4)
    d.text((150, 70), "MXW01", fill=0)
    return img


def _bluez_le_connected() -> bool:
    info = subprocess.run(
        ["bluetoothctl", "info", ADDR], capture_output=True, text=True
    ).stdout
    return "LE.Connected: yes" in info


def ensure_bluez_le_link():
    """BlueZ 5.85 quirk: this printer advertises the Dual Mode flag, so a plain
    `connect` tries BR/EDR first, pages out, and deletes the device. Working
    sequence discovered experimentally (log 24):

        scan le → bearer <addr> le   # setting bearer auto-establishes LE link
    """
    if _bluez_le_connected():
        log("BlueZ already has LE link")
        return
    log("discovering + pinning PreferredBearer=le")
    subprocess.run(
        ["bluetoothctl", "--timeout", "12", "scan", "le"], capture_output=True
    )
    r = subprocess.run(
        ["bluetoothctl", "bearer", ADDR, "le"], capture_output=True, text=True
    )
    log(f"bearer le -> {r.stdout.strip()} {r.stderr.strip()}")
    # Wait for auto-connect that follows PreferredBearer change
    for _ in range(20):
        if _bluez_le_connected():
            log("LE link up after bearer pin")
            return
        time.sleep(0.5)
    # Last resort: explicit connect (may still try BR/EDR — logged if so)
    r = subprocess.run(
        ["timeout", "20", "bluetoothctl", "connect", ADDR],
        capture_output=True,
        text=True,
    )
    log(f"explicit connect -> {r.stdout.strip()} {r.stderr.strip()}")
    time.sleep(1)


async def bleak_device_from_bluez() -> BLEDevice | None:
    """Build a Bleak BLEDevice from the live BlueZ D-Bus object.

    Required because Bleak 3.x otherwise insists on a fresh advertisement, but
    this printer stops advertising once the LE link is up.
    """
    mgr = await get_global_bluez_manager()
    for path, ifaces in mgr._properties.items():
        props = ifaces.get("org.bluez.Device1")
        if not props or props.get("Address") != ADDR:
            continue
        return BLEDevice(ADDR, props.get("Name"), {"path": path, "props": dict(props)})
    return None


async def connect_client():
    ensure_bluez_le_link()
    for attempt in range(1, 6):
        device = await bleak_device_from_bluez()
        if device is None:
            # Fall back to advertisement scan
            device = await BleakScanner.find_device_by_address(ADDR, timeout=10.0)
        if device is None:
            log(f"no device handle attempt {attempt}")
            ensure_bluez_le_link()
            continue
        try:
            client = BleakClient(device, timeout=20.0)
            await client.connect()
            log(f"bleak connected attempt {attempt}, mtu={client.mtu_size}")
            return client
        except Exception as e:
            log(f"bleak connect fail {attempt}: {type(e).__name__}: {e}")
            await asyncio.sleep(2)
            if not _bluez_le_connected():
                ensure_bluez_le_link()
    raise RuntimeError("could not connect")


async def print_bitmap(client: BleakClient, data: bytes, intensity: int = 0x5D):
    lines = len(data) // BYTES_PER_ROW
    log(f"job: {lines} lines, {len(data)} bytes, intensity=0x{intensity:02x}")

    notifications = []
    done = asyncio.Event()

    def on_notify(_char, payload: bytearray):
        b = bytes(payload)
        log(f"NOTIFY AE02: {b.hex(' ')}")
        notifications.append(b)
        if len(b) >= 3 and b[2] == 0xAA:
            done.set()
        if len(b) >= 3 and b[2] == 0xA9:
            # print-request ack also wakes waiters
            pass

    await client.start_notify(AE02, on_notify)

    async def cmd(name, c, payload, wait_cmd=None, timeout=8.0):
        pkt = frame(c, payload)
        before = len(notifications)
        await client.write_gatt_char(AE01, pkt, response=False)
        log(f"CMD {name}: {pkt.hex(' ')}")
        if wait_cmd is None:
            await asyncio.sleep(0.3)
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for n in notifications[before:]:
                if len(n) >= 3 and n[2] == wait_cmd:
                    return n
            await asyncio.sleep(0.05)
        log(f"TIMEOUT waiting for 0x{wait_cmd:02X}")
        return None

    await cmd("A2 intensity", 0xA2, bytes([intensity]))
    status = await cmd("A1 status", 0xA1, b"\x00", wait_cmd=0xA1)
    if status and len(status) >= 16:
        # payload starts at offset 6
        payload = status[6 : 6 + status[4]]
        log(f"status payload: {payload.hex(' ')}")

    # A9: lines_le16, 0x30, mode 0x00 (1bpp)
    a9_payload = bytes([lines & 0xFF, (lines >> 8) & 0xFF, 0x30, 0x00])
    ack = await cmd("A9 print req", 0xA9, a9_payload, wait_cmd=0xA9, timeout=10.0)
    if ack is None:
        raise RuntimeError("A9 not acknowledged")
    if len(ack) >= 7 and ack[6] != 0x00:
        raise RuntimeError(f"A9 rejected: {ack.hex(' ')}")

    # Transfer image on AE03 in chunks. Prefer negotiated MTU-3, fall back 20.
    try:
        chunk = max(20, client.mtu_size - 3)
    except Exception:
        chunk = 20
    # Cap chunk: many of these boards prefer ~100–180
    chunk = min(chunk, 180)
    log(f"transferring in chunks of {chunk}")
    for i in range(0, len(data), chunk):
        await client.write_gatt_char(AE03, data[i : i + chunk], response=False)
        await asyncio.sleep(0.015)
        if (i // chunk) % 50 == 0:
            log(f"  sent {i}/{len(data)}")
    log(f"  sent {len(data)}/{len(data)}")

    await cmd("AD flush", 0xAD, b"\x00")
    log("waiting for AA print-complete...")
    try:
        await asyncio.wait_for(done.wait(), timeout=60.0)
        log("AA received — print complete")
    except asyncio.TimeoutError:
        log("AA TIMEOUT — printer may still be finishing; check paper")

    await client.stop_notify(AE02)
    return notifications


JOBS = {
    "hello": make_hello,
    "rect": make_black_rect,
    "checker": make_checkerboard,
    "qr": make_qr,
    "png": make_png_sample,
}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=list(JOBS) + ["all"], help="which test page")
    ap.add_argument("--intensity", default="0x5D")
    ap.add_argument("--save-preview", action="store_true")
    args = ap.parse_args()
    intensity = int(args.intensity, 0)

    jobs = list(JOBS) if args.job == "all" else [args.job]
    client = await connect_client()
    try:
        for name in jobs:
            log(f"===== JOB {name} =====")
            img = JOBS[name]()
            if args.save_preview:
                p = Path("logs") / f"preview_{name}.png"
                img.save(p)
                log(f"saved preview {p}")
            data = image_to_1bpp(img)
            Path("logs").mkdir(exist_ok=True)
            (Path("logs") / f"bitmap_{name}.bin").write_bytes(data)
            await print_bitmap(client, data, intensity=intensity)
            await asyncio.sleep(2)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        subprocess.run(["bluetoothctl", "disconnect", ADDR], capture_output=True)
    log("done")


if __name__ == "__main__":
    asyncio.run(main())

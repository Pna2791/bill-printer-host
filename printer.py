"""MXW01 BLE thermal printer client.

Protocol verified experimentally on unit 48:0F:57:49:DB:3B (see docs/).
Transport: BLE GATT only. Application framing: 22 21 … CRC8 FF on AE01/AE02;
raw 1bpp rows on AE03.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

from bleak import BLEDevice, BleakClient, BleakScanner
from bleak.backends.bluezdbus.manager import get_global_bluez_manager
from PIL import Image, ImageDraw, ImageFont
import qrcode

log = logging.getLogger("mxw01")

AE01 = "0000ae01-0000-1000-8000-00805f9b34fb"
AE02 = "0000ae02-0000-1000-8000-00805f9b34fb"
AE03 = "0000ae03-0000-1000-8000-00805f9b34fb"

DEFAULT_WIDTH = 384
DEFAULT_MINIMUM_ROWS = 90
DEFAULT_INTENSITY = 0x5D


def crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def frame(cmd: int, payload: bytes = b"") -> bytes:
    return (
        bytes([0x22, 0x21, cmd & 0xFF, 0x00, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF])
        + payload
        + bytes([crc8(payload), 0xFF])
    )


@dataclass
class PrinterStatus:
    raw: bytes
    battery: Optional[int] = None
    temperature: Optional[int] = None
    version: Optional[str] = None
    connected: bool = False

    def to_dict(self) -> dict:
        return {
            "connected": self.connected,
            "battery": self.battery,
            "temperature": self.temperature,
            "version": self.version,
            "raw": self.raw.hex(" ") if self.raw else None,
        }


class MXW01:
    """Async client for a single MXW01 printer."""

    def __init__(
        self,
        address: str,
        intensity: int = DEFAULT_INTENSITY,
        width: int = DEFAULT_WIDTH,
        minimum_rows: int = DEFAULT_MINIMUM_ROWS,
    ):
        if width <= 0 or width % 8:
            raise ValueError("printer width must be a positive multiple of 8")
        self.address = address.upper()
        self.intensity = intensity & 0xFF
        self.width = width
        self.minimum_rows = max(1, minimum_rows)
        self._client: Optional[BleakClient] = None
        self._lock = asyncio.Lock()
        self._version: Optional[str] = None
        self._notify_q: asyncio.Queue[bytes] = asyncio.Queue()

    # ----- connection helpers (BlueZ 5.85 quirks) -------------------------

    def _le_connected(self) -> bool:
        info = subprocess.run(
            ["bluetoothctl", "info", self.address],
            capture_output=True,
            text=True,
        ).stdout
        return "LE.Connected: yes" in info

    def _ensure_bluez_le(self) -> None:
        """Pin PreferredBearer=le so BlueZ does not page BR/EDR."""
        if self._le_connected():
            return
        subprocess.run(
            ["bluetoothctl", "--timeout", "12", "scan", "le"],
            capture_output=True,
        )
        subprocess.run(
            ["bluetoothctl", "bearer", self.address, "le"],
            capture_output=True,
        )
        for _ in range(20):
            if self._le_connected():
                return
            time.sleep(0.5)
        subprocess.run(
            ["timeout", "20", "bluetoothctl", "connect", self.address],
            capture_output=True,
        )
        time.sleep(1)

    async def _ble_device(self) -> BLEDevice:
        mgr = await get_global_bluez_manager()
        for path, ifaces in mgr._properties.items():
            props = ifaces.get("org.bluez.Device1")
            if props and props.get("Address", "").upper() == self.address:
                return BLEDevice(
                    self.address,
                    props.get("Name"),
                    {"path": path, "props": dict(props)},
                )
        device = await BleakScanner.find_device_by_address(self.address, timeout=12.0)
        if device is None:
            raise ConnectionError(f"printer {self.address} not found")
        return device

    async def connect(self, retries: int = 5) -> None:
        async with self._lock:
            if self._client and self._client.is_connected:
                return
            last_err: Exception | None = None
            for attempt in range(1, retries + 1):
                try:
                    await asyncio.to_thread(self._ensure_bluez_le)
                    device = await self._ble_device()
                    client = BleakClient(device, timeout=20.0)
                    await client.connect()
                    await client.start_notify(AE02, self._on_notify)
                    self._client = client
                    log.info("connected to %s (attempt %d)", self.address, attempt)
                    return
                except Exception as e:
                    last_err = e
                    log.warning("connect attempt %d failed: %s", attempt, e)
                    await asyncio.sleep(2)
            raise ConnectionError(f"failed to connect after {retries} attempts: {last_err}")

    async def disconnect(self) -> None:
        async with self._lock:
            if self._client:
                try:
                    if self._client.is_connected:
                        await self._client.stop_notify(AE02)
                        await self._client.disconnect()
                except Exception as e:
                    log.debug("disconnect: %s", e)
                self._client = None
            subprocess.run(
                ["bluetoothctl", "disconnect", self.address], capture_output=True
            )

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    def _on_notify(self, _char, data: bytearray) -> None:
        self._notify_q.put_nowait(bytes(data))

    async def _drain_notifies(self) -> None:
        while not self._notify_q.empty():
            try:
                self._notify_q.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _has_pending(self, cmd: int) -> bool:
        """Consume the queue; return True if a matching notify is already here.

        Non-matching packets are put back so later waits still see them.
        """
        found = False
        buffered: list[bytes] = []
        while not self._notify_q.empty():
            try:
                pkt = self._notify_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            if len(pkt) >= 3 and pkt[2] == cmd:
                found = True
            else:
                buffered.append(pkt)
        for pkt in buffered:
            self._notify_q.put_nowait(pkt)
        return found

    async def _wait_notify(self, cmd: int, timeout: float = 8.0) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pkt = await asyncio.wait_for(
                    self._notify_q.get(), timeout=max(0.05, deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                break
            if len(pkt) >= 3 and pkt[2] == cmd:
                return pkt
        raise TimeoutError(f"no notify for cmd 0x{cmd:02X}")

    async def _cmd(
        self, cmd: int, payload: bytes = b"", wait: Optional[int] = None, timeout: float = 8.0
    ) -> Optional[bytes]:
        if not self.is_connected:
            raise ConnectionError("not connected")
        assert self._client is not None
        if wait is not None:
            await self._drain_notifies()
        await self._client.write_gatt_char(AE01, frame(cmd, payload), response=False)
        if wait is None:
            await asyncio.sleep(0.15)
            return None
        return await self._wait_notify(wait, timeout=timeout)

    # ----- public API -----------------------------------------------------

    async def status(self) -> PrinterStatus:
        async with self._lock:
            if not self.is_connected:
                await self._connect_unlocked()
            assert self._client is not None
            pkt = await self._cmd(0xA1, b"\x00", wait=0xA1)
            assert pkt is not None
            length = pkt[4] | (pkt[5] << 8)
            payload = pkt[6 : 6 + length]
            battery = payload[3] if len(payload) > 3 else None
            temperature = payload[4] if len(payload) > 4 else None
            if self._version is None:
                try:
                    vpkt = await self._cmd(0xB1, b"\x00", wait=0xB1, timeout=3.0)
                    if vpkt:
                        vlen = vpkt[4] | (vpkt[5] << 8)
                        self._version = vpkt[6 : 6 + vlen].split(b"\x00")[0].decode(
                            "ascii", "replace"
                        )
                except Exception:
                    pass
            return PrinterStatus(
                raw=payload,
                battery=battery,
                temperature=temperature,
                version=self._version,
                connected=True,
            )

    async def _connect_unlocked(self) -> None:
        # connect() takes the lock; this path is only used when already holding it
        # so we inline a single attempt here.
        await asyncio.to_thread(self._ensure_bluez_le)
        device = await self._ble_device()
        client = BleakClient(device, timeout=20.0)
        await client.connect()
        await client.start_notify(AE02, self._on_notify)
        self._client = client

    async def feed(self, lines: int = 40) -> None:
        """Feed blank paper by printing white rows."""
        rows = max(1, lines)
        bytes_per_row = self.width // 8
        rows = max(rows, self.minimum_rows)
        data = b"\x00" * (rows * bytes_per_row)
        await self._print_bitmap(data, bytes_per_row=bytes_per_row)

    async def print_text(
        self,
        text: str,
        font_size: int = 32,
        align: str = "left",
        intensity: Optional[int] = None,
    ) -> None:
        img = render_text(
            text, width=self.width, minimum_rows=self.minimum_rows,
            font_size=font_size, align=align
        )
        await self.print_image(img, intensity=intensity)

    async def print_qrcode(self, data: str, intensity: Optional[int] = None) -> None:
        qr = qrcode.QRCode(border=2, box_size=6)
        qr.add_data(data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("L")
        canvas = Image.new("L", (self.width, qr_img.height + 20), 255)
        canvas.paste(qr_img, ((self.width - qr_img.width) // 2, 10))
        await self.print_image(canvas, intensity=intensity)

    async def print_image(
        self,
        image: Image.Image | bytes | str,
        intensity: Optional[int] = None,
        rotate_180: bool = True,
    ) -> None:
        if isinstance(image, (bytes, bytearray)):
            img = Image.open(BytesIO(image))
        elif isinstance(image, str):
            img = Image.open(image)
        else:
            img = image
        if rotate_180:
            # Printer feeds top-first; rotate so output reads right-side-up.
            img = img.rotate(180)
        data = image_to_1bpp(
            img,
            width=self.width,
            minimum_rows=self.minimum_rows,
        )
        await self._print_bitmap(
            data, intensity=intensity, bytes_per_row=self.width // 8
        )

    async def print_packed(
        self,
        data: bytes,
        width: int,
        height: int,
        intensity: Optional[int] = None,
    ) -> None:
        """Print already-rendered LSB-left 1bpp rows without reprocessing."""
        if width != self.width:
            raise ValueError(
                f"bitmap width {width} does not match configured printer width {self.width}"
            )
        bytes_per_row = width // 8
        if len(data) != height * bytes_per_row:
            raise ValueError("packed bitmap length does not match width and height")
        await self._print_bitmap(
            data, intensity=intensity, bytes_per_row=bytes_per_row
        )

    async def _print_bitmap(
        self,
        data: bytes,
        intensity: Optional[int] = None,
        bytes_per_row: Optional[int] = None,
    ) -> None:
        async with self._lock:
            if not self.is_connected:
                await self._connect_unlocked()
            assert self._client is not None
            energy = self.intensity if intensity is None else intensity & 0xFF
            bytes_per_row = bytes_per_row or self.width // 8
            if len(data) % bytes_per_row:
                raise ValueError("bitmap data is not row-aligned")
            lines = len(data) // bytes_per_row
            log.info("printing %d lines (%d bytes)", lines, len(data))

            # Discard stale notifications from previous jobs NOW, so anything
            # arriving from here on (including an early 0xAA completion while
            # data is still streaming) belongs to this job and is kept.
            await self._drain_notifies()

            await self._cmd(0xA2, bytes([energy]))
            await self._cmd(0xA1, b"\x00", wait=0xA1)
            a9 = bytes([lines & 0xFF, (lines >> 8) & 0xFF, 0x30, 0x00])
            ack = await self._cmd(0xA9, a9, wait=0xA9, timeout=10.0)
            if ack is None or (len(ack) >= 7 and ack[6] != 0x00):
                raise RuntimeError(f"print request rejected: {ack!r}")

            # bleak's BlueZ backend reports mtu_size=23 unless acquired, which
            # previously forced 20-byte chunks (~16 rows/s). The print head is
            # much faster, so long jobs starved the buffer and the printer
            # aborted halfway. The characteristic reports the real negotiated
            # write-without-response size.
            try:
                char = self._client.services.get_characteristic(AE03)
                chunk = max(20, min(244, char.max_write_without_response_size))
            except Exception:
                chunk = 20
            log.info("streaming with %d-byte chunks", chunk)
            for i in range(0, len(data), chunk):
                await self._client.write_gatt_char(
                    AE03, data[i : i + chunk], response=False
                )
                await asyncio.sleep(0.01)

            await self._cmd(0xAD, b"\x00")
            # Long receipts print for minutes; scale the completion timeout
            # with the job size (~90 rows/s worst case, generous floor).
            timeout = max(60.0, lines / 15.0)
            await self._wait_notify(0xAA, timeout=timeout)
            log.info("print complete")


# ----- image helpers ------------------------------------------------------

def image_to_1bpp(
    img: Image.Image,
    width: int = DEFAULT_WIDTH,
    minimum_rows: int = DEFAULT_MINIMUM_ROWS,
) -> bytes:
    img = img.convert("L")
    w, h = img.size
    if w != width:
        img = img.resize((width, max(1, int(h * width / w))), Image.Resampling.LANCZOS)
        h = img.height
    bw = img.convert("1")
    px = bw.load()
    out = bytearray()
    for y in range(h):
        for xb in range(width // 8):
            b = 0
            for bit in range(8):
                if px[xb * 8 + bit, y] == 0:
                    b |= 1 << bit
            out.append(b)
    minimum_bytes = minimum_rows * (width // 8)
    if len(out) < minimum_bytes:
        out.extend(b"\x00" * (minimum_bytes - len(out)))
    return bytes(out)


def render_text(
    text: str,
    width: int = DEFAULT_WIDTH,
    minimum_rows: int = DEFAULT_MINIMUM_ROWS,
    font_size: int = 32,
    align: str = "left",
) -> Image.Image:
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size
        )
    except OSError:
        font = ImageFont.load_default()

    # Word-wrap
    draw_probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split(" ")
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip()
            bbox = draw_probe.textbbox((0, 0), trial, font=font)
            if bbox[2] - bbox[0] > width - 16 and cur:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        lines.append(cur)

    line_h = font_size + 6
    img = Image.new(
        "L", (width, max(minimum_rows, line_h * len(lines) + 20)), 255
    )
    draw = ImageDraw.Draw(img)
    y = 10
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        if align == "center":
            x = (width - tw) // 2
        elif align == "right":
            x = width - tw - 8
        else:
            x = 8
        draw.text((x, y), line, fill=0, font=font)
        y += line_h
    return img

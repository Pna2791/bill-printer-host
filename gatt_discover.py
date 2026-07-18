"""GATT discovery for MXW01 printer. Enumerates services/characteristics/descriptors,
reads all readable characteristics, logs MTU. No writes performed."""
import asyncio
import sys
from bleak import BleakClient, BleakScanner

ADDR = "48:0F:57:49:DB:3B"

PROPS_OF_INTEREST = ["read", "write", "write-without-response", "notify", "indicate"]


def hexdump(b: bytes) -> str:
    return b.hex(" ") if b else "(empty)"


async def main():
    print(f"[*] Scanning for {ADDR} ...")
    device = await BleakScanner.find_device_by_address(ADDR, timeout=15.0)
    if device is None:
        print("[!] Not seen advertising (may already be connected); using address directly")
        device = ADDR
    else:
        print(f"[+] Found: {device!r}")

    disconnect_reason = {"fired": False}

    def on_disconnect(client):
        disconnect_reason["fired"] = True
        print("[!] Disconnected callback fired")

    async with BleakClient(device, disconnected_callback=on_disconnect, timeout=30.0) as client:
        print(f"[+] Connected: {client.is_connected}")
        try:
            print(f"[+] MTU: {client.mtu_size}")
        except Exception as e:
            print(f"[!] MTU read failed: {e}")

        for service in client.services:
            print(f"\nSERVICE {service.uuid} (handle {service.handle}): {service.description}")
            for char in service.characteristics:
                props = ",".join(char.properties)
                print(f"  CHAR {char.uuid} (handle {char.handle}) props=[{props}]")
                if "read" in char.properties:
                    try:
                        val = await client.read_gatt_char(char)
                        print(f"    VALUE: {hexdump(val)}  ascii={val.decode('ascii', 'replace')!r}")
                    except Exception as e:
                        print(f"    READ FAILED: {e}")
                for desc in char.descriptors:
                    print(f"    DESC {desc.uuid} (handle {desc.handle}): {desc.description}")
                    try:
                        dval = await client.read_gatt_descriptor(desc.handle)
                        print(f"      DESC VALUE: {hexdump(dval)}")
                    except Exception as e:
                        print(f"      DESC READ FAILED: {e}")

    print(f"\n[+] Clean exit. disconnect callback fired: {disconnect_reason['fired']}")


asyncio.run(main())

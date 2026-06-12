#!/usr/bin/env python3
"""
Raspberry Pi BLE client using Bleak.
It scans for a device named 'ESP32-Date' and subscribes to notifications
on the date characteristic. Install with: `python3 -m pip install --user bleak`
"""
import asyncio
from bleak import BleakClient, BleakScanner

SERVICE_UUID = "12345678-1234-1234-1234-1234567890ab"
CHAR_UUID = "abcdefab-1234-1234-1234-abcdefabcdef"

async def run():
    print("Scanning for BLE devices (10s)...")
    devices = await BleakScanner.discover(timeout=10.0)
    target = None
    for d in devices:
        name = d.name or d.metadata.get('local_name') if d.metadata else None
        if name == 'ESP32-Date' or SERVICE_UUID.lower() in [u.lower() for u in (d.metadata.get('uuids') or [])]:
            target = d
            break

    if not target:
        print("ESP32-Date not found; try again when the ESP is advertising.")
        return

    print(f"Found device: {target.address} / {target.name}")
    async with BleakClient(target.address) as client:
        if not client.is_connected:
            print("Failed to connect")
            return
        print("Connected, starting notifications on characteristic")

        def callback(sender, data):
            try:
                s = data.decode('utf-8')
            except Exception:
                s = repr(data)
            print("Received:", s)
            with open('last_date_ble.txt', 'w') as f:
                f.write(s + "\n")

        await client.start_notify(CHAR_UUID, callback)
        print("Listening for notifications (press Ctrl+C to stop)...")
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("Stopping")
        await client.stop_notify(CHAR_UUID)

if __name__ == '__main__':
    asyncio.run(run())

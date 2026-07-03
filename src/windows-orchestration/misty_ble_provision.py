"""
Misty BLE WiFi Provisioning — Configure Misty's WiFi over Bluetooth Low Energy.

This tool connects to Misty II via BLE (triggered by holding chin capacitive sensor)
and writes WiFi credentials to the provisioning characteristic.

Usage:
    python misty_ble_provision.py scan                           Scan for Misty BLE devices
    python misty_ble_provision.py provision "SSID" "password"    Provision WiFi
    python misty_ble_provision.py read                           Read status from Misty BLE
    python misty_ble_provision.py test-formats "SSID" "pass"     Try all known payload formats

Prerequisites:
    - pip install bleak
    - Hold Misty's chin sensor to enable BLE advertising

BLE Protocol (reverse-engineered from companion app v1.2.59):
    Service: 1562c132-3a0d-4f39-9e67-a9a632e8d6aa
    Write:   3ee51024-7fdd-4d37-95aa-0a4b0e2d4f34 (write-without-response)
    Read:    418f52ab-10c6-42a6-9590-58cccb818f64

The exact payload format is being reverse-engineered. This tool supports
multiple format hypotheses tested sequentially with read-back confirmation.
"""

import argparse
import asyncio
import json
import struct
import sys
import time
from typing import Optional

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.characteristic import BleakGATTCharacteristic
except ImportError:
    print("ERROR: bleak not installed. Run: pip install bleak")
    sys.exit(1)

# Known Misty BLE UUIDs (discovered from actual Misty II hardware)
MISTY_SERVICE_UUID = "1562c132-3a0d-4f39-9e67-a9a632e8d6aa"
MISTY_WRITE_UUID = "3ee51024-7fdd-4d37-95aa-0a4b0e2d4f34"
MISTY_READ_UUID = "418f52ab-10c6-42a6-9590-58cccb818f64"

# Misty's BLE device name is the last 5 digits of serial number
MISTY_SERIAL_SUFFIX = "03627"  # From serial 20194603627


def build_payload_formats(ssid: str, password: str) -> list[tuple[str, bytes]]:
    """Generate all candidate payload formats for WiFi provisioning.
    
    Returns list of (description, payload_bytes) tuples.
    Based on analysis of Misty companion app (Xamarin/.NET, Plugin.BluetoothLE)
    and common ESP32 BLE provisioning patterns.
    """
    formats = []
    
    # Format 1: JSON with lowercase keys (most commonly referenced)
    formats.append((
        "JSON lowercase keys",
        json.dumps({"ssid": ssid, "password": password}).encode("utf-8")
    ))
    
    # Format 2: JSON with PascalCase keys (matches Misty REST API convention)
    formats.append((
        "JSON PascalCase keys",
        json.dumps({"Ssid": ssid, "Password": password}).encode("utf-8")
    ))
    
    # Format 3: JSON with security type field
    formats.append((
        "JSON with securityType WPA2",
        json.dumps({"ssid": ssid, "password": password, "securityType": "wpa2"}).encode("utf-8")
    ))
    
    # Format 4: JSON PascalCase with security type (matching REST API style)
    formats.append((
        "JSON PascalCase + SecurityType",
        json.dumps({"Ssid": ssid, "Password": password, "SecurityType": "WPA2"}).encode("utf-8")
    ))
    
    # Format 5: Length-prefixed binary (common ESP32 custom protocol)
    # [ssid_len:1][ssid][pass_len:1][password]
    ssid_bytes = ssid.encode("utf-8")
    pass_bytes = password.encode("utf-8")
    formats.append((
        "Length-prefixed binary (1-byte lengths)",
        struct.pack("B", len(ssid_bytes)) + ssid_bytes +
        struct.pack("B", len(pass_bytes)) + pass_bytes
    ))
    
    # Format 6: Length-prefixed with 2-byte LE lengths
    formats.append((
        "Length-prefixed binary (2-byte LE lengths)",
        struct.pack("<H", len(ssid_bytes)) + ssid_bytes +
        struct.pack("<H", len(pass_bytes)) + pass_bytes
    ))
    
    # Format 7: Null-separated (SSID\0PASSWORD\0)
    formats.append((
        "Null-separated (SSID\\0PASS\\0)",
        ssid_bytes + b"\x00" + pass_bytes + b"\x00"
    ))
    
    # Format 8: Newline-separated
    formats.append((
        "Newline-separated (SSID\\nPASS)",
        ssid_bytes + b"\n" + pass_bytes
    ))
    
    # Format 9: Pipe-separated
    formats.append((
        "Pipe-separated (SSID|PASS)",
        ssid_bytes + b"|" + pass_bytes
    ))
    
    # Format 10: Comma-separated with security type
    formats.append((
        "Comma-separated (SSID,PASS,WPA2)",
        ssid_bytes + b"," + pass_bytes + b",WPA2"
    ))
    
    # Format 11: Command-prefixed (0x01 = set wifi)
    formats.append((
        "Command byte 0x01 + null-separated",
        b"\x01" + ssid_bytes + b"\x00" + pass_bytes + b"\x00"
    ))
    
    # Format 12: Command byte + length-prefixed
    formats.append((
        "Command 0x01 + length-prefixed",
        b"\x01" + struct.pack("B", len(ssid_bytes)) + ssid_bytes +
        struct.pack("B", len(pass_bytes)) + pass_bytes
    ))
    
    # Format 13: Just SSID first (two-step: write SSID, then password separately)
    formats.append((
        "SSID only (step 1 of 2-write protocol)",
        ssid_bytes
    ))
    
    # Format 14: JSON with networkId field
    formats.append((
        "JSON with NetworkId",
        json.dumps({"NetworkId": 0, "Ssid": ssid, "Password": password}).encode("utf-8")
    ))
    
    # Format 15: Protobuf-like TLV (Tag-Length-Value)
    # Tag 1 = SSID, Tag 2 = Password, Tag 3 = Security
    formats.append((
        "TLV format (tag:1=SSID, tag:2=PASS)",
        b"\x01" + struct.pack("B", len(ssid_bytes)) + ssid_bytes +
        b"\x02" + struct.pack("B", len(pass_bytes)) + pass_bytes +
        b"\x03\x01\x03"  # security type = 3 (WPA2)
    ))
    
    return formats


async def scan_for_misty(timeout: float = 10.0) -> list[dict]:
    """Scan for Misty BLE devices.
    
    Misty advertises with device name = last 5 digits of serial number
    and exposes service UUID 1562c132-3a0d-4f39-9e67-a9a632e8d6aa.
    BLE must be activated by holding chin capacitive sensor.
    """
    print(f"Scanning for BLE devices ({timeout}s)...")
    print("  (Hold Misty's chin sensor to activate BLE advertising)")
    print()
    
    devices = await BleakScanner.discover(timeout=timeout)
    misty_devices = []
    
    for device in devices:
        is_misty = False
        reason = ""
        
        # Check by name (last 5 digits of serial)
        if device.name and MISTY_SERIAL_SUFFIX in str(device.name):
            is_misty = True
            reason = "name matches serial suffix"
        
        # Check by advertised service UUID
        if hasattr(device, 'metadata') and device.metadata:
            uuids = device.metadata.get('uuids', [])
            if MISTY_SERVICE_UUID in [u.lower() for u in uuids]:
                is_misty = True
                reason = "service UUID matches"
        
        if is_misty:
            misty_devices.append({
                "address": device.address,
                "name": device.name,
                "rssi": getattr(device, 'rssi', None),
                "reason": reason,
            })
            print(f"  *** MISTY FOUND: {device.address}  {device.name}  RSSI={getattr(device, 'rssi', '?')}  ({reason})")
        else:
            # Show all devices for debugging
            if device.name:
                print(f"      {device.address}  {device.name}  RSSI={getattr(device, 'rssi', '?')}")
    
    if not misty_devices:
        print("\n  No Misty BLE devices found.")
        print("  Ensure Misty's chin sensor is held to activate BLE advertising.")
    
    return misty_devices


async def connect_and_discover(address: str) -> Optional[dict]:
    """Connect to Misty BLE and discover all services/characteristics."""
    print(f"\nConnecting to {address}...")
    
    async with BleakClient(address) as client:
        if not client.is_connected:
            print("  ERROR: Failed to connect")
            return None
        
        print(f"  Connected! MTU={client.mtu_size if hasattr(client, 'mtu_size') else 'unknown'}")
        print("\n  Services and Characteristics:")
        
        services_info = {}
        for service in client.services:
            print(f"\n  Service: {service.uuid}")
            chars = []
            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"    Char: {char.uuid} [{props}]")
                
                # Try to read if readable
                if "read" in char.properties:
                    try:
                        value = await client.read_gatt_char(char.uuid)
                        print(f"      Value: {value.hex()} ({value})")
                    except Exception as e:
                        print(f"      Read error: {e}")
                
                chars.append({
                    "uuid": char.uuid,
                    "properties": char.properties,
                })
            
            services_info[service.uuid] = chars
        
        return services_info


async def read_misty_status(address: str) -> Optional[bytes]:
    """Read the status characteristic from Misty BLE."""
    print(f"\nConnecting to {address} to read status...")
    
    async with BleakClient(address) as client:
        if not client.is_connected:
            print("  ERROR: Failed to connect")
            return None
        
        try:
            value = await client.read_gatt_char(MISTY_READ_UUID)
            print(f"  Read characteristic value: {value.hex()}")
            print(f"  As bytes: {list(value)}")
            print(f"  As ASCII: {value.decode('ascii', errors='replace')}")
            return value
        except Exception as e:
            print(f"  Read error: {e}")
            return None


async def provision_wifi(address: str, ssid: str, password: str,
                         format_index: Optional[int] = None,
                         wait_between_writes: float = 1.0) -> bool:
    """Attempt WiFi provisioning over BLE.
    
    If format_index is specified, uses only that format.
    Otherwise tries format 1 (JSON lowercase) by default.
    """
    formats = build_payload_formats(ssid, password)
    
    if format_index is not None:
        if format_index >= len(formats):
            print(f"ERROR: Format index {format_index} out of range (0-{len(formats)-1})")
            return False
        formats = [formats[format_index]]
    else:
        # Default to format 0 (JSON lowercase)
        formats = [formats[0]]
    
    print(f"\nConnecting to {address}...")
    
    async with BleakClient(address) as client:
        if not client.is_connected:
            print("  ERROR: Failed to connect")
            return False
        
        print(f"  Connected! MTU={client.mtu_size if hasattr(client, 'mtu_size') else 'unknown'}")
        
        # Read initial status
        try:
            initial = await client.read_gatt_char(MISTY_READ_UUID)
            print(f"  Initial read value: {initial.hex()}")
        except Exception as e:
            print(f"  Initial read error: {e}")
        
        for desc, payload in formats:
            print(f"\n  Trying format: {desc}")
            print(f"    Payload length: {len(payload)} bytes (contents redacted)")
            
            try:
                # Write payload (without response, matching characteristic properties)
                await client.write_gatt_char(
                    MISTY_WRITE_UUID, payload, response=False
                )
                print(f"    Write successful!")
            except Exception as e:
                print(f"    Write error: {e}")
                # Try with response
                try:
                    await client.write_gatt_char(
                        MISTY_WRITE_UUID, payload, response=True
                    )
                    print(f"    Write (with response) successful!")
                except Exception as e2:
                    print(f"    Write (with response) also failed: {e2}")
                    continue
            
            # Wait and read back status
            await asyncio.sleep(wait_between_writes)
            
            try:
                status = await client.read_gatt_char(MISTY_READ_UUID)
                print(f"    Status after write: {status.hex()} ({list(status)})")
                
                if status != initial:
                    print(f"    *** STATUS CHANGED! This format may have worked.")
                    return True
            except Exception as e:
                print(f"    Status read error: {e}")
        
        # For 2-write protocol: if format was "SSID only", send password next
        if format_index == 12:  # SSID-only format
            print(f"\n  Sending password as second write (redacted)...")
            pass_bytes = password.encode("utf-8")
            try:
                await client.write_gatt_char(MISTY_WRITE_UUID, pass_bytes, response=False)
                print(f"    Password write successful!")
                await asyncio.sleep(wait_between_writes)
                status = await client.read_gatt_char(MISTY_READ_UUID)
                print(f"    Status after password: {status.hex()} ({list(status)})")
            except Exception as e:
                print(f"    Password write error: {e}")
    
    return False


async def test_all_formats(address: str, ssid: str, password: str,
                           delay: float = 3.0) -> None:
    """Test all payload formats sequentially, reading status after each.
    
    WARNING: This sends real WiFi credentials. Use a test network if possible.
    The robot may connect to WiFi mid-test if a format works.
    """
    formats = build_payload_formats(ssid, password)
    
    print(f"\nConnecting to {address} for format testing...")
    print(f"  Testing {len(formats)} payload formats with {delay}s delay between each")
    print(f"  SSID: {ssid}")
    print("  Password: [redacted]")
    print()
    
    async with BleakClient(address) as client:
        if not client.is_connected:
            print("  ERROR: Failed to connect")
            return
        
        print(f"  Connected! MTU={client.mtu_size if hasattr(client, 'mtu_size') else 'unknown'}")
        
        # Read baseline status
        try:
            baseline = await client.read_gatt_char(MISTY_READ_UUID)
            print(f"  Baseline status: {baseline.hex()} ({list(baseline)})")
        except Exception as e:
            print(f"  Baseline read error: {e}")
            baseline = None
        
        print()
        
        for i, (desc, payload) in enumerate(formats):
            print(f"  [{i:2d}/{len(formats)}] {desc}")
            print(f"       Payload length: {len(payload)} bytes")
            
            try:
                await client.write_gatt_char(MISTY_WRITE_UUID, payload, response=False)
                print(f"       Write: OK")
            except Exception as e:
                print(f"       Write: FAILED ({e})")
                continue
            
            await asyncio.sleep(delay)
            
            try:
                status = await client.read_gatt_char(MISTY_READ_UUID)
                changed = status != baseline if baseline else "?"
                print(f"       Status: {status.hex()} changed={changed}")
                
                if changed and changed != "?":
                    print(f"\n  *** FORMAT {i} TRIGGERED A RESPONSE! ***")
                    print(f"  *** Format: {desc}")
                    print("  *** Payload: [redacted]")
                    print(f"  *** New status: {status.hex()}")
                    
                    # Wait longer and check again
                    await asyncio.sleep(5)
                    final = await client.read_gatt_char(MISTY_READ_UUID)
                    print(f"  *** Final status (after 5s): {final.hex()}")
                    return
            except Exception as e:
                print(f"       Status: READ FAILED ({e})")
            
            print()
        
        print("\n  All formats tested. No status change detected.")
        print("  The protocol may require:")
        print("  - A different framing/encoding not yet tested")
        print("  - A handshake or session setup before credential write")
        print("  - Multiple writes in specific sequence")
        print("  - Notification subscription on read characteristic")


async def provision_with_notifications(address: str, ssid: str, password: str,
                                        format_index: int = 0) -> None:
    """Provision with notification subscription on read characteristic.
    
    Some BLE protocols require subscribing to notifications to receive
    status updates rather than polling via read.
    """
    formats = build_payload_formats(ssid, password)
    desc, payload = formats[format_index]
    
    notifications = []
    
    def notification_handler(sender: BleakGATTCharacteristic, data: bytearray):
        print(f"    NOTIFICATION from {sender.uuid}: {data.hex()} ({list(data)})")
        notifications.append(data)
    
    print(f"\nConnecting to {address} with notification subscription...")
    
    async with BleakClient(address) as client:
        if not client.is_connected:
            print("  ERROR: Failed to connect")
            return
        
        print(f"  Connected!")
        
        # Subscribe to notifications on read characteristic
        try:
            await client.start_notify(MISTY_READ_UUID, notification_handler)
            print(f"  Subscribed to notifications on {MISTY_READ_UUID}")
        except Exception as e:
            print(f"  Notification subscribe failed: {e}")
            print(f"  (Characteristic may not support notify/indicate)")
        
        # Read initial value
        try:
            initial = await client.read_gatt_char(MISTY_READ_UUID)
            print(f"  Initial value: {initial.hex()}")
        except Exception as e:
            print(f"  Initial read error: {e}")
        
        # Write credentials
        print(f"\n  Writing format [{format_index}]: {desc}")
        print(f"    Payload length: {len(payload)} bytes (contents redacted)")
        
        try:
            await client.write_gatt_char(MISTY_WRITE_UUID, payload, response=False)
            print(f"    Write successful!")
        except Exception as e:
            print(f"    Write error: {e}")
        
        # Wait for notifications
        print(f"\n  Waiting 10s for notifications...")
        await asyncio.sleep(10)
        
        if notifications:
            print(f"\n  Received {len(notifications)} notification(s)!")
            for i, n in enumerate(notifications):
                print(f"    [{i}] {n.hex()} ({list(n)})")
        else:
            print(f"  No notifications received.")
        
        # Final read
        try:
            final = await client.read_gatt_char(MISTY_READ_UUID)
            print(f"  Final read value: {final.hex()}")
        except Exception as e:
            print(f"  Final read error: {e}")
        
        # Cleanup
        try:
            await client.stop_notify(MISTY_READ_UUID)
        except:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Misty BLE WiFi Provisioning Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s scan
    %(prog)s provision "MyWiFi" "MyPassword"
    %(prog)s provision "MyWiFi" "MyPassword" --format 4
    %(prog)s test-formats "MyWiFi" "MyPassword"
    %(prog)s read --address 57:CA:00:43:1D:D4
    %(prog)s discover --address 57:CA:00:43:1D:D4
        """
    )
    
    parser.add_argument("--address", "-a", help="BLE device address (auto-detected if omitted)")
    
    subparsers = parser.add_subparsers(dest="command", help="Command")
    
    # Scan command
    scan_parser = subparsers.add_parser("scan", help="Scan for Misty BLE devices")
    scan_parser.add_argument("--timeout", type=float, default=10.0)
    
    # Provision command
    prov_parser = subparsers.add_parser("provision", help="Provision WiFi credentials")
    prov_parser.add_argument("ssid", help="WiFi SSID")
    prov_parser.add_argument("password", help="WiFi password")
    prov_parser.add_argument("--format", "-f", type=int, default=None,
                            help="Payload format index (see test-formats)")
    
    # Test formats command
    test_parser = subparsers.add_parser("test-formats", help="Test all payload formats")
    test_parser.add_argument("ssid", help="WiFi SSID")
    test_parser.add_argument("password", help="WiFi password")
    test_parser.add_argument("--delay", type=float, default=3.0,
                            help="Delay between format tests (seconds)")
    
    # Read command
    read_parser = subparsers.add_parser("read", help="Read BLE status characteristic")
    
    # Discover command
    disc_parser = subparsers.add_parser("discover", help="Discover all BLE services/characteristics")
    
    # Notify command
    notify_parser = subparsers.add_parser("notify", help="Provision with notification subscription")
    notify_parser.add_argument("ssid", help="WiFi SSID")
    notify_parser.add_argument("password", help="WiFi password")
    notify_parser.add_argument("--format", "-f", type=int, default=0)
    
    # List formats command
    list_parser = subparsers.add_parser("list-formats", help="List all payload formats")
    list_parser.add_argument("ssid", nargs="?", default="TestSSID")
    list_parser.add_argument("password", nargs="?", default="TestPass")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    if args.command == "list-formats":
        formats = build_payload_formats(args.ssid, "REDACTED")
        print(f"Available payload formats ({len(formats)}):")
        print(f"  (Example with SSID='{args.ssid}', password='REDACTED')")
        print()
        for i, (desc, payload) in enumerate(formats):
            print(f"  [{i:2d}] {desc}")
            print(f"       Hex: {payload.hex()}")
            print("       Txt: [REDACTED - may contain sensitive data]")
            print()
        return
    
    if args.command == "scan":
        asyncio.run(scan_for_misty(args.timeout))
        return
    
    # For commands that need an address, auto-detect if not provided
    address = args.address
    if not address:
        print("No address specified, scanning for Misty...")
        devices = asyncio.run(scan_for_misty(timeout=8.0))
        if devices:
            address = devices[0]["address"]
            print(f"\nUsing first Misty found: {address}")
        else:
            print("\nERROR: No Misty BLE device found. Use --address to specify manually.")
            sys.exit(1)
    
    if args.command == "discover":
        asyncio.run(connect_and_discover(address))
    elif args.command == "read":
        asyncio.run(read_misty_status(address))
    elif args.command == "provision":
        asyncio.run(provision_wifi(address, args.ssid, args.password, args.format))
    elif args.command == "test-formats":
        asyncio.run(test_all_formats(address, args.ssid, args.password, args.delay))
    elif args.command == "notify":
        asyncio.run(provision_with_notifications(address, args.ssid, args.password, args.format))


if __name__ == "__main__":
    main()

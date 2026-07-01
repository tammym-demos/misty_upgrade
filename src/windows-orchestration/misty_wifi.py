"""
Misty WiFi Manager — Add, list, and switch WiFi networks on Misty II.

Usage:
    python misty_wifi.py add "SSID" "password"      Add a network
    python misty_wifi.py list                        List known networks
    python misty_wifi.py connect "SSID"             Connect to a known network
    python misty_wifi.py scan                        Scan for available networks
    python misty_wifi.py forget <networkId>          Remove a saved network

Credentials are read from a local encrypted store (misty_wifi_networks.json.enc)
or passed as arguments. The file is encrypted with a machine-specific key.
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import requests

# Resolve Misty IP: env var → discovery cache → default
sys.path.insert(0, str(Path(__file__).parent))
try:
    from misty_discovery import discover_misty, get_misty_ip
except ImportError:
    discover_misty = None
    get_misty_ip = None


def _get_misty_ip():
    """Resolve Misty IP from env, cache, or discovery."""
    ip = os.getenv("MISTY_IP")
    if ip:
        return ip
    if get_misty_ip:
        cached = get_misty_ip()
        if cached:
            return cached
    if discover_misty:
        found = discover_misty()
        if found:
            return found
    # Fallback
    return "10.0.0.23"


# --- Credential store (XOR-obfuscated, not plaintext) ---
_STORE_FILE = Path(__file__).parent / "misty_wifi_networks.json.enc"


def _get_machine_key() -> bytes:
    """Derive a machine-specific key for obfuscating stored credentials."""
    # Use machine UUID + username as entropy
    machine_id = str(uuid.getnode()) + os.getenv("USERNAME", os.getenv("USER", "misty"))
    return hashlib.sha256(machine_id.encode()).digest()


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    """XOR data with repeating key."""
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _load_store() -> dict:
    """Load the credential store."""
    if not _STORE_FILE.exists():
        return {"networks": []}
    try:
        encrypted = base64.b64decode(_STORE_FILE.read_bytes())
        decrypted = _xor_bytes(encrypted, _get_machine_key())
        return json.loads(decrypted.decode("utf-8"))
    except Exception:
        return {"networks": []}


def _save_store(store: dict):
    """Save the credential store."""
    data = json.dumps(store, indent=2).encode("utf-8")
    encrypted = _xor_bytes(data, _get_machine_key())
    _STORE_FILE.write_bytes(base64.b64encode(encrypted))


def _api(method, endpoint, misty_ip, body=None, timeout=15):
    """Make a REST API call to Misty."""
    url = f"http://{misty_ip}{endpoint}"
    try:
        if method == "GET":
            r = requests.get(url, timeout=timeout)
        elif method == "POST":
            r = requests.post(url, json=body, timeout=timeout)
        elif method == "DELETE":
            r = requests.delete(url, json=body, timeout=timeout)
        else:
            raise ValueError(f"Unknown method: {method}")
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectTimeout:
        print(f"  ERROR: Connection to Misty at {misty_ip} timed out")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        print(f"  ERROR: Cannot reach Misty at {misty_ip}")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)


def cmd_add(args):
    """Add a WiFi network to Misty and save locally."""
    misty_ip = _get_misty_ip()
    ssid = args.ssid
    password = args.password

    print(f"Adding network '{ssid}' to Misty at {misty_ip}...")

    # Push to Misty
    result = _api("POST", "/api/networks", misty_ip, {"Ssid": ssid, "Password": password})
    print(f"  Misty response: {result.get('status', 'unknown')}")

    # Save locally (obfuscated)
    store = _load_store()
    # Update if exists, else append
    existing = next((n for n in store["networks"] if n["ssid"] == ssid), None)
    if existing:
        existing["password"] = password
    else:
        store["networks"].append({"ssid": ssid, "password": password})
    _save_store(store)
    print(f"  Saved to local credential store ({len(store['networks'])} networks)")


def cmd_list(args):
    """List networks known to Misty and locally saved."""
    misty_ip = _get_misty_ip()

    print(f"Querying Misty at {misty_ip}...")
    result = _api("GET", "/api/networks", misty_ip)

    if "result" in result:
        networks = result["result"]
        print(f"\n  Networks on Misty ({len(networks)}):")
        for n in networks:
            marker = " *" if n.get("isConnected") else ""
            print(f"    [{n.get('id', '?')}] {n.get('ssid', 'unknown')}{marker}")
    else:
        print(f"  Response: {result}")

    # Show local store
    store = _load_store()
    if store["networks"]:
        print(f"\n  Locally saved ({len(store['networks'])}):")
        for n in store["networks"]:
            print(f"    - {n['ssid']}")


def cmd_connect(args):
    """Connect Misty to a specific network."""
    misty_ip = _get_misty_ip()
    ssid = args.ssid

    # Check local store for password
    store = _load_store()
    saved = next((n for n in store["networks"] if n["ssid"] == ssid), None)

    if saved:
        print(f"Connecting Misty to '{ssid}'...")
        result = _api("POST", "/api/networks", misty_ip, {"Ssid": ssid, "Password": saved["password"]})
    else:
        print(f"Network '{ssid}' not in local store. Use 'add' first or provide password.")
        sys.exit(1)

    print(f"  Result: {result.get('status', 'unknown')}")
    print("  NOTE: Misty will disconnect from current network. Allow 10-15s to reconnect.")


def cmd_scan(args):
    """Scan for available WiFi networks from Misty."""
    misty_ip = _get_misty_ip()
    print(f"Scanning WiFi from Misty at {misty_ip}...")
    result = _api("GET", "/api/networks/scan", misty_ip, timeout=30)

    if "result" in result:
        networks = result["result"]
        print(f"\n  Available networks ({len(networks)}):")
        for n in sorted(networks, key=lambda x: x.get("signalStrength", 0), reverse=True):
            print(f"    {n.get('ssid', '(hidden)'):<30} Signal: {n.get('signalStrength', '?')}%")
    else:
        print(f"  Response: {result}")


def cmd_forget(args):
    """Remove a network from Misty."""
    misty_ip = _get_misty_ip()
    network_id = args.network_id

    print(f"Removing network {network_id} from Misty...")
    result = _api("DELETE", "/api/networks", misty_ip, {"NetworkId": int(network_id)})
    print(f"  Result: {result.get('status', 'unknown')}")


def cmd_push_all(args):
    """Push all locally saved networks to Misty."""
    misty_ip = _get_misty_ip()
    store = _load_store()

    if not store["networks"]:
        print("No networks in local store.")
        return

    print(f"Pushing {len(store['networks'])} networks to Misty at {misty_ip}...")
    for n in store["networks"]:
        print(f"  Adding '{n['ssid']}'...", end=" ")
        result = _api("POST", "/api/networks", misty_ip, {"Ssid": n["ssid"], "Password": n["password"]})
        print(result.get("status", "unknown"))


def main():
    parser = argparse.ArgumentParser(description="Misty WiFi Manager")
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="Add a WiFi network")
    p_add.add_argument("ssid", help="WiFi SSID")
    p_add.add_argument("password", help="WiFi password")

    sub.add_parser("list", help="List known networks")

    p_conn = sub.add_parser("connect", help="Connect to a saved network")
    p_conn.add_argument("ssid", help="SSID to connect to")

    sub.add_parser("scan", help="Scan available networks")

    p_forget = sub.add_parser("forget", help="Forget a network by ID")
    p_forget.add_argument("network_id", help="Network ID to remove")

    sub.add_parser("push-all", help="Push all saved networks to Misty")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "add": cmd_add,
        "list": cmd_list,
        "connect": cmd_connect,
        "scan": cmd_scan,
        "forget": cmd_forget,
        "push-all": cmd_push_all,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()

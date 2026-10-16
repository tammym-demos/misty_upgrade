"""
Misty WiFi Manager — Add, list, and switch WiFi networks on Misty II.

Usage:
    python misty_wifi.py add "SSID" "password"      Add a network
    python misty_wifi.py list                        List known networks
    python misty_wifi.py connect "SSID"             Connect to a known network
    python misty_wifi.py scan                        Scan for available networks
    python misty_wifi.py forget <networkId>          Remove a saved network

Credentials are read from a local obfuscated store (misty_wifi_networks.json.enc)
or passed as arguments. The file uses machine-specific XOR obfuscation, not strong encryption.
"""

import argparse
import base64
import getpass
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import uuid
import warnings
from pathlib import Path

import requests

# Resolve Misty IP: env var → discovery cache → default
sys.path.insert(0, str(Path(__file__).parent))
try:
    from misty_discovery import discover_misty, get_misty_ip
except ImportError:
    discover_misty = None
    get_misty_ip = None


def _get_misty_ip(explicit_ip=None):
    """Resolve Misty IP from env, cache, or discovery."""
    if explicit_ip:
        return explicit_ip
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


# --- Credential store (XOR-obfuscated, not plaintext or strong encryption) ---
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


def _request(method, endpoint, misty_ip, body=None, timeout=15, params=None):
    with requests.Session() as session:
        session.trust_env = False
        response = session.request(
            method, f"http://{misty_ip}{endpoint}", json=body, params=params,
            timeout=timeout, allow_redirects=False,
        )
        response.raise_for_status()
        return response.json()


def _api(method, endpoint, misty_ip, body=None, timeout=15, params=None):
    """Make a REST API call to Misty."""
    try:
        return _request(method, endpoint, misty_ip, body, timeout, params)
    except requests.exceptions.ConnectTimeout:
        print(f"  ERROR: Connection to Misty at {misty_ip} timed out")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        print(f"  ERROR: Cannot reach Misty at {misty_ip}")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)


def _create_network(misty_ip, ssid, password):
    """Add a network on Misty and attempt to connect to it."""
    return _api(
        "POST",
        "/api/networks/create",
        misty_ip,
        {"networkname": ssid, "password": password},
    )


def cmd_add(args):
    """Add a WiFi network to Misty and save locally."""
    misty_ip = _get_misty_ip(args.misty_ip)
    ssid = args.ssid
    password = args.password

    print(f"Adding network '{ssid}' to Misty at {misty_ip}...")

    # Push to Misty
    result = _create_network(misty_ip, ssid, password)
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
    misty_ip = _get_misty_ip(args.misty_ip)

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
    misty_ip = _get_misty_ip(args.misty_ip)
    ssid = args.ssid

    result = _api("GET", "/api/networks", misty_ip)
    networks = result.get("result", []) if isinstance(result, dict) else []
    existing = next((n for n in networks if n.get("ssid") == ssid), None)
    if existing and existing.get("id") is not None:
        print(f"Connecting Misty to saved network '{ssid}'...")
        result = _api("POST", "/api/networks", misty_ip, {"NetworkId": existing["id"]})
        print(f"  Result: {result.get('status', 'unknown')}")
        print("  NOTE: Misty will disconnect from current network. Allow 10-15s to reconnect.")
        return

    # Fall back to local store if the network has not been saved on Misty yet.
    store = _load_store()
    saved = next((n for n in store["networks"] if n["ssid"] == ssid), None)

    if saved:
        print(f"Network '{ssid}' not found on Misty. Adding and connecting...")
        result = _create_network(misty_ip, ssid, saved["password"])
    else:
        print(f"Network '{ssid}' not in local store. Use 'add' first or provide password.")
        sys.exit(1)

    print(f"  Result: {result.get('status', 'unknown')}")
    print("  NOTE: Misty will disconnect from current network. Allow 10-15s to reconnect.")


def cmd_scan(args):
    """Scan for available WiFi networks from Misty."""
    misty_ip = _get_misty_ip(args.misty_ip)
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
    misty_ip = _get_misty_ip(args.misty_ip)
    network_id = args.network_id

    print(f"Removing network {network_id} from Misty...")
    result = _api("DELETE", "/api/networks", misty_ip, params={"NetworkId": int(network_id)})
    print(f"  Result: {result.get('status', 'unknown')}")


def cmd_push_all(args):
    """Push all locally saved networks to Misty."""
    misty_ip = _get_misty_ip(args.misty_ip)
    store = _load_store()

    if not store["networks"]:
        print("No networks in local store.")
        return

    print(f"Pushing {len(store['networks'])} networks to Misty at {misty_ip}...")
    for n in store["networks"]:
        print(f"  Adding '{n['ssid']}'...", end=" ")
        result = _create_network(misty_ip, n["ssid"], n["password"])
        print(result.get("status", "unknown"))


class WifiSetupError(RuntimeError):
    """An actionable setup failure whose message never includes a network secret."""


class WifiLinkLost(WifiSetupError):
    """The outcome of a request cannot be inferred from a lost connection."""


def _checked_api(method, endpoint, ip, body=None):
    try:
        payload = _request(method, endpoint, ip, body)
    except requests.HTTPError:
        raise WifiSetupError("Misty returned an HTTP error; no verified handoff.") from None
    except (requests.ConnectionError, requests.Timeout):
        raise WifiLinkLost("The connection was lost; the request outcome is unknown.") from None
    except (requests.RequestException, ValueError):
        raise WifiSetupError("Misty's response could not be read; check the local connection.") from None
    if (not isinstance(payload, dict) or payload.get("status") != "Success"
            or payload.get("result") is False):
        raise WifiSetupError("Misty rejected the network request. Check the SSID and credentials.") from None
    return payload.get("result")


def _confirm(message):
    if input(f"{message} [y/N] ").strip().lower() not in ("y", "yes"):
        raise WifiSetupError("Cancelled. No further network changes were made.")


def _hidden_password():
    # getpass can otherwise fall back to echoing input when no usable terminal exists.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            password = getpass.getpass("Network password (hidden; not saved on the laptop): ")
        except getpass.GetPassWarning:
            raise WifiSetupError("Secure password input is unavailable. Use an interactive terminal.") from None
    if not password:
        raise WifiSetupError("This flow requires a shared-password network; open networks are not supported.")
    return password


def _network_name(profile, saved=False):
    value = profile.get("ssid", profile.get("name", profile.get("Name")))
    if saved and isinstance(value, str) and value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, str):
                return decoded
        except ValueError:
            return value
    return value


def _network_id(profile):
    return profile.get("networkId", profile.get("id"))


def _local_interfaces():
    if sys.platform != "win32":
        raise WifiSetupError("Guided Wi-Fi setup requires Windows; existing standalone commands remain available.")
    script = (
        "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | "
        "Select-Object InterfaceAlias,InterfaceIndex,IPAddress,PrefixLength | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15, check=True,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise WifiSetupError("Cannot inspect Windows IPv4 interfaces. Check permissions in Windows Settings.") from None
    rows = data if isinstance(data, list) else [data]
    interfaces = []
    from misty_discovery import private_ipv4
    for row in rows:
        try:
            ip = private_ipv4(row["IPAddress"])
            network = ipaddress.IPv4Network(f"{ip}/{int(row['PrefixLength'])}", strict=False)
            interfaces.append((str(row["InterfaceIndex"]), row["InterfaceAlias"], ip, str(network)))
        except (KeyError, TypeError, ValueError):
            continue
    if not interfaces:
        raise WifiSetupError("No private IPv4 interface is available. Enable the intended network in Windows Settings.")
    return interfaces


def _discover_hotspot():
    """Read only hotspot SSID/state and ICS private-interface identity, never its key."""
    script = r"""
$ErrorActionPreference = 'Stop'
$null = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
$null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
$profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if ($null -eq $profile) { throw 'No upstream connection profile' }
$manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
$ssid = [string]$manager.GetCurrentAccessPointConfiguration().Ssid
$private = @()
$adapterDiscovery = 'available'
try {
    $sharing = New-Object -ComObject HNetCfg.HNetShare
    foreach ($connection in $sharing.EnumEveryConnection) {
        $config = $sharing.INetSharingConfigurationForINetConnection($connection)
        if ($config.SharingEnabled -and $config.SharingConnectionType -eq 1) {
            $properties = $sharing.NetConnectionProps($connection)
            $private += @(Get-NetAdapter -IncludeHidden -ErrorAction Stop |
                Where-Object { [guid]$_.InterfaceGuid -eq [guid]$properties.Guid } |
                Select-Object -ExpandProperty ifIndex)
        }
    }
} catch { $adapterDiscovery = 'unavailable' }
[pscustomobject]@{
    state = [string]$manager.TetheringOperationalState
    ssid = $ssid
    interfaces = @($private | Select-Object -Unique)
    adapterDiscovery = $adapterDiscovery
} | ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20, check=True,
        )
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("state"), str):
            raise ValueError("Invalid hotspot metadata")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {
            "state": "Unknown", "ssid": None, "interface": None,
            "warning": "Windows hotspot discovery is unavailable. Confirm its Settings manually; SSID/interface selection is required.",
        }
    indexes = data.get("interfaces")
    interface = (str(indexes[0]) if isinstance(indexes, list) and len(indexes) == 1
                 and str(indexes[0]).isdigit() else None)
    ssid = data.get("ssid")
    return {
        "state": data["state"],
        "ssid": ssid if isinstance(ssid, str) and ssid else None,
        "interface": interface,
        "warning": (None if interface else
                    "Windows did not identify one ICS private interface. Select the Mobile hotspot adapter manually."),
    }


def _assert_runtime_stopped(controller_port=5001):
    """Fail closed if controller/stage-runner process inspection is unavailable."""
    if sys.platform != "win32":
        raise WifiSetupError("Runtime safety inspection requires Windows.")
    script = (
        "Get-CimInstance Win32_Process -ErrorAction Stop | "
        "Where-Object { $_.Name -match '^(python|pythonw|py)([0-9.]*)?\\.exe$' } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15, check=True,
        )
        data = json.loads(result.stdout) if result.stdout.strip() else []
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            command = row.get("CommandLine")
            if not isinstance(command, str):
                raise WifiSetupError("Cannot inspect a Python process. Confirm permissions before changing networks.")
            controller = re.search(r'(?:^|[\\/\s"])misty_controller\.py(?:["\s]|$)', command, re.I)
            stage = (re.search(r'(?:^|[\\/\s"])conference_mode\.py(?:["\s]|$)', command, re.I)
                     and re.search(r'(?:^|\s)run(?:\s|$)', command))
            if controller or stage:
                raise WifiSetupError("Robot runtime is active. Stop the stage runner, then run npx . stop before setup.")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        raise WifiSetupError("Cannot verify that robot processes are stopped; setup is blocked.") from None
    try:
        response = requests.get(f"http://127.0.0.1:{controller_port}/api/status", timeout=2)
    except requests.ConnectionError:
        return
    except requests.RequestException:
        raise WifiSetupError("Controller status is indeterminate; stop the runtime before setup.") from None
    # Even an unexpected listener on this port needs operator investigation.
    raise WifiSetupError(f"Controller port {controller_port} is responding (HTTP {response.status_code}); stop it before setup.")


def _verify_websocket(ip):
    try:
        import websocket
    except ImportError:
        raise WifiSetupError("Missing websocket-client. Install the existing project requirements before verification.") from None
    event = f"WifiVerify_{uuid.uuid4().hex}"
    connection = None
    try:
        connection = websocket.create_connection(
            f"ws://{ip}/pubsub", timeout=3, http_no_proxy=[ip],
        )
        connection.send(json.dumps({
            "Operation": "subscribe", "Type": "BatteryCharge", "DebounceMs": 100,
            "EventName": event, "ReturnProperty": None, "EventConditions": [],
        }))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            connection.settimeout(max(0.1, deadline - time.monotonic()))
            payload = json.loads(connection.recv())
            if not isinstance(payload, dict):
                continue
            message = payload.get("message")
            if (payload.get("eventName") == event and isinstance(message, dict)
                    and ("chargePercent" in message or "voltage" in message)):
                return
            # Registration acknowledgements are not proof of battery telemetry.
        raise WifiSetupError("REST works but no matching WebSocket telemetry arrived.")
    except (websocket.WebSocketException, OSError, ValueError):
        raise WifiSetupError("REST works but WebSocket verification failed. Check network policy/routing; do not disable the firewall.") from None
    finally:
        if connection is not None:
            try:
                connection.send(json.dumps({"Operation": "unsubscribe", "EventName": event}))
            except (websocket.WebSocketException, OSError):
                print("WARNING: Diagnostic unsubscribe failed; closing its socket.", file=sys.stderr)
            finally:
                connection.close()


def _choose_scope(args, source_ip=None, suggested_interface=None, confirm=True):
    from misty_discovery import approved_subnet, private_ipv4
    interfaces = _local_interfaces()
    print("\nPrivate Windows interfaces (availability/sharing permission is NOT automatically proven):")
    for index, name, ip, network in interfaces:
        upstream = source_ip and ipaddress.IPv4Address(source_ip) in ipaddress.IPv4Network(network)
        marker = " [current robot network; not the private hotspot]" if upstream else ""
        print(f"  {index}: {name} - {ip} ({network}){marker}")
    if source_ip:
        print("Choose the laptop's private Mobile hotspot interface, not the upstream Wi-Fi or a Hyper-V/VPN adapter.")
    while True:
        selected = args.interface or suggested_interface or input("Target private interface index: ").strip()
        matching = [row for row in interfaces if row[0] == selected]
        error = None
        if len(matching) != 1:
            error = "Select one listed interface with a single private IPv4 address."
        elif source_ip and ipaddress.IPv4Address(source_ip) in ipaddress.IPv4Network(matching[0][3]):
            error = "That is Misty's current network. Choose the private Mobile hotspot interface, not the upstream Wi-Fi."
        if error is None:
            break
        if args.interface:
            raise WifiSetupError(error)
        print(f"ERROR: {error}")
        suggested_interface = None
    _, name, local_ip, actual_subnet = matching[0]
    actual_network = ipaddress.IPv4Network(actual_subnet)
    default_subnet = str(ipaddress.IPv4Network(
        f"{local_ip}/{max(24, actual_network.prefixlen)}", strict=False,
    ))
    while True:
        if args.subnet is not None:
            value = args.subnet
        elif not confirm:
            value = default_subnet
        else:
            value = input(
                f"Approved discovery subnet [{default_subnet}] (Enter=shown subnet, /24=prefix shorthand): "
            ).strip()
        try:
            subnet = value or default_subnet
            if subnet.startswith("/"):
                subnet = str(ipaddress.IPv4Network(f"{local_ip}{subnet}", strict=False))
            network = approved_subnet(subnet)
            if not network.subnet_of(actual_network):
                raise ValueError("The subnet must be within the selected interface's network.")
            break
        except ValueError:
            error = f"Enter a private subnet within {actual_subnet}, /24 or smaller (for example {default_subnet} or /24)."
            if args.subnet is not None:
                raise WifiSetupError(error) from None
            print(f"ERROR: {error}")
    _validate_scope(interfaces, network, selected)
    if args.target_ip:
        target = private_ipv4(args.target_ip)
        if ipaddress.IPv4Address(target) not in network or target == local_ip:
            raise WifiSetupError("The target must be a robot address inside the approved subnet.")
    if confirm:
        _confirm(f"Do you have permission to probe {subnet} on {name}?")
    return subnet, selected


def _validate_scope(rows, network, interface):
    if not any(row[0] == interface and network.subnet_of(ipaddress.IPv4Network(row[3]))
               for row in rows):
        raise WifiSetupError("The approved target interface/subnet changed. Rerun verification with the correct scope.")
    if any(row[0] != interface and network.overlaps(ipaddress.IPv4Network(row[3]))
           for row in rows):
        raise WifiSetupError("Another interface overlaps the approved subnet. Resolve routing in Windows Settings before probing.")


def _verify_target(args, expected, subnet, interface, target_ssid=None):
    from misty_discovery import discover_misty_scoped
    # Ensure the selected interface still exists after the manual handoff.
    rows = _local_interfaces()
    network = ipaddress.IPv4Network(subnet)
    _validate_scope(rows, network, interface)
    for attempt in range(6):
        print(f"Verifying expected robot on {subnet} (attempt {attempt + 1}/6)...")
        identity = discover_misty_scoped(
            subnet, expected["serialNumber"], expected["robotId"], args.target_ip,
        )
        if identity:
            if target_ssid is not None and identity.get("networkProfileName") != target_ssid:
                if attempt < 5:
                    time.sleep(5)
                continue
            _verify_websocket(identity["ipAddress"])
            return identity
        if attempt < 5:
            time.sleep(5)
    raise WifiSetupError(
        "The expected robot was not verified. Credentials, routing, isolation, or its address may be wrong. "
        "No rollback is guaranteed after losing the link. Keep saved networks; restore the rehearsed "
        "known-good network or temporary phone hotspot, then rerun wifi verify."
    )


def _source_identity(args, confirm=True):
    from misty_discovery import private_ipv4, probe_misty_identity
    ip = args.misty_ip or os.getenv("MISTY_IP") or (get_misty_ip() if get_misty_ip else None)
    if not ip:
        raise WifiSetupError("An existing robot connection is required. Supply --ip <current-private-ip>.")
    identity = probe_misty_identity(private_ipv4(ip))
    if not identity:
        raise WifiSetupError("The current robot IP did not return a valid Misty identity. Supply --ip; no automatic broad scan is performed.")
    if args.serial and identity["serialNumber"] != args.serial:
        raise WifiSetupError("The current address belongs to a different robot.")
    if args.robot_id and identity["robotId"] != args.robot_id:
        raise WifiSetupError("The current address belongs to a different robot.")
    print(f"Current robot: serial={identity['serialNumber']}, firmware={identity['robotVersion']}, IP={ip}")
    if confirm:
        _confirm("Is this your Misty?")
    return identity


def _switch_network(ip, ssid, password, controller_port=5001,
                    auto_profile=False, confirmation_message=None):
    profiles = _checked_api("GET", "/api/networks", ip)
    if not isinstance(profiles, list):
        raise WifiSetupError("Misty returned an invalid saved-network list.")
    if not all(isinstance(profile, dict) for profile in profiles):
        raise WifiSetupError("Misty returned an invalid saved-network profile.")
    matches = [profile for profile in profiles if _network_name(profile, saved=True) == ssid]
    body = {"networkname": ssid, "password": password}
    endpoint = "/api/networks/create"
    if matches:
        if auto_profile and len(matches) == 1 and _network_id(matches[0]) is not None:
            choice = str(_network_id(matches[0]))
            print(f"Using saved target profile ID: {choice}; the entered password will not replace its saved credentials.")
        else:
            for profile in matches:
                print(f"Saved target profile ID: {_network_id(profile)}")
            choice = input("Saved profile ID to connect, or 'new' to use the entered password: ").strip()
        if choice != "new":
            match = next((profile for profile in matches if str(_network_id(profile)) == choice), None)
            if not match or _network_id(match) is None:
                raise WifiSetupError("Choose an existing target profile ID or 'new'. No switch sent.")
            endpoint, body = "/api/networks", {"NetworkId": _network_id(match)}
    _confirm(confirmation_message or "Switch Misty now? This can immediately disconnect the current link")
    _assert_runtime_stopped(controller_port)
    try:
        _checked_api("POST", endpoint, ip, body)
    except WifiLinkLost:
        print("The link was lost during the switch. Outcome is unknown; verifying without resending credentials.")


def cmd_guided(args):
    from misty_discovery import private_ipv4
    if not sys.stdin.isatty():
        raise WifiSetupError("Guided setup/verification requires an interactive terminal.")
    if not 1 <= args.controller_port <= 65535:
        raise WifiSetupError("--controller-port must be between 1 and 65535.")
    target_ssid = None
    if args.command == "setup":
        _assert_runtime_stopped(args.controller_port)
        mode = args.mode or "hotspot"
        if mode not in ("hotspot", "direct"):
            raise WifiSetupError("Choose hotspot or direct.")
        print("Windows Settings > Network & internet. Networking changes remain manual.")
        if mode == "hotspot":
            print("Join the upstream Wi-Fi and complete its browser sign-in. In Mobile hotspot, "
                  "share Wi-Fi (not cellular) over Wi-Fi; use a stable SSID and try 2.4 GHz. "
                  "Check hotspot power saving. Adapter support and venue permission require rehearsal.")
            hotspot = _discover_hotspot()
            if hotspot["state"] == "Off":
                raise WifiSetupError("Windows Mobile hotspot is OFF. Enable it manually in Settings, sharing Wi-Fi, then rerun setup.")
            if hotspot["state"] not in ("On", "Unknown"):
                raise WifiSetupError("Windows hotspot is changing state. Wait for it to be ON, then rerun setup.")
            if hotspot["warning"]:
                print(f"WARNING: {hotspot['warning']}")
        else:
            kind = input("Target network type: shared-password, portal, or enterprise: ").strip().lower()
            if kind != "shared-password":
                raise WifiSetupError("Direct portal/enterprise/open setup is unsupported. Use an approved laptop hotspot instead.")
            _confirm("Is sharing/device access permitted, and is the intended target network ready?")
        expected = _source_identity(args, confirm=mode != "hotspot")
        ssid_prompt = ("Target laptop hotspot SSID (from Mobile hotspot Settings, not the upstream Wi-Fi): "
                       if mode == "hotspot" else "Target SSID (exact spelling): ")
        ssid = hotspot["ssid"] if mode == "hotspot" and hotspot["ssid"] else input(ssid_prompt).strip()
        if not ssid:
            raise WifiSetupError("An SSID is required.")
        visible = _checked_api("GET", "/api/networks/scan", expected["ipAddress"])
        if not isinstance(visible, list) or not any(isinstance(row, dict) and _network_name(row) == ssid for row in visible):
            raise WifiSetupError("The target SSID is not visible to Misty. Check hotspot/band; no credentials were sent.")
        if any(_network_name(row) == ssid and row.get("isSecure", row.get("IsSecure")) is False
               for row in visible if isinstance(row, dict)):
            raise WifiSetupError("The target is an open network. Use a secured laptop hotspot instead.")
        target_ssid = ssid
        # The target interface may not exist until the operator joins the destination.
        if mode == "hotspot":
            subnet, interface = _choose_scope(
                args, source_ip=expected["ipAddress"],
                suggested_interface=hotspot["interface"], confirm=False,
            )
            print(f"\nReview: Misty serial={expected['serialNumber']} at {expected['ipAddress']}"
                  f"\n  Laptop hotspot SSID: {ssid}"
                  f"\n  Private interface: {interface}; approved discovery subnet: {subnet}"
                  "\n  Windows settings will not be changed; existing robot networks will be kept.")
        password = _hidden_password()
        print("The laptop will not save this password. Misty will retain the network profile.")
        _assert_runtime_stopped(args.controller_port)
        try:
            message = (f"Confirm this is your Misty and you have permission to share/probe {subnet} and switch her now"
                       if mode == "hotspot" else None)
            _switch_network(
                expected["ipAddress"], ssid, password, args.controller_port,
                auto_profile=mode == "hotspot", confirmation_message=message,
            )
        finally:
            password = None
        if mode == "direct":
            input("Join the destination network in Windows Settings. Press Enter when ready to verify: ")
            subnet, interface = _choose_scope(args)
    else:
        if not args.serial or not args.robot_id:
            raise WifiSetupError("Verification requires --serial and --robot-id from the previously confirmed robot.")
        expected = {"serialNumber": args.serial, "robotId": args.robot_id}
        subnet, interface = _choose_scope(args)
    identity = _verify_target(args, expected, subnet, interface, target_ssid)
    report = {**identity, "status": "verified", "discoveredAt": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if args.report:
        Path(args.report).write_text(json.dumps(report), encoding="utf-8")
    else:
        # Direct Python use still persists only a successfully verified identity.
        from misty_discovery import _DISCOVERY_FILE
        _DISCOVERY_FILE.write_text(json.dumps({
            "ip": private_ipv4(identity["ipAddress"]), "timestamp": time.time(),
            "discovered_at": report["discoveredAt"],
        }), encoding="utf-8")
    print(f"Verified local REST + WebSocket connection: {identity['ipAddress']}")
    print("Upstream Internet/portal access was not tested. Rehearse with the phone hotspot OFF.")
    print(f"Start explicitly: npx . start --misty-ip {identity['ipAddress']}")


def main():
    parser = argparse.ArgumentParser(description="Misty WiFi Manager")
    sub = parser.add_subparsers(dest="command")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ip", "--misty-ip", dest="misty_ip", help="Override Misty IP for this command")

    p_add = sub.add_parser("add", help="Add a WiFi network", parents=[common])
    p_add.add_argument("ssid", help="WiFi SSID")
    p_add.add_argument("password", help="WiFi password")

    sub.add_parser("list", help="List known networks", parents=[common])

    p_conn = sub.add_parser("connect", help="Connect to a saved network", parents=[common])
    p_conn.add_argument("ssid", help="SSID to connect to")

    sub.add_parser("scan", help="Scan available networks", parents=[common])

    p_forget = sub.add_parser("forget", help="Forget a network by ID", parents=[common])
    p_forget.add_argument("network_id", help="Network ID to remove")

    sub.add_parser("push-all", help="Push all saved networks to Misty", parents=[common])

    for command in ("setup", "verify"):
        guided = sub.add_parser(command, help=f"Guided travel Wi-Fi {command}", parents=[common])
        guided.add_argument("--mode", choices=("hotspot", "direct"),
                            help="Setup mode (default: hotspot)")
        guided.add_argument("--interface", help="Approved Windows private-interface index")
        guided.add_argument("--subnet", help="Approved private CIDR, /24 or smaller")
        guided.add_argument("--target-ip", help="Probe only this destination IP, without a subnet scan")
        guided.add_argument("--serial", help="Expected robot serial number")
        guided.add_argument("--robot-id", help="Expected robot ID")
        guided.add_argument("--controller-port", type=int, default=5001)
        guided.add_argument("--report", help=argparse.SUPPRESS)

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
        "setup": cmd_guided,
        "verify": cmd_guided,
    }
    try:
        commands[args.command](args)
    except (WifiSetupError, ValueError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. If a switch was sent, its outcome remains unverified; restore the known-good network.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

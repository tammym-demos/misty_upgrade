"""
Misty II Network Discovery

Scans local subnets for a Misty II robot by probing /api/device on port 80.
Persists the discovered IP to a shared JSON file so other sessions/services
can access it without re-scanning.

Usage:
    from misty_discovery import discover_misty

    ip = discover_misty()  # Returns IP string or None
"""

import json
import ipaddress
import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import requests

logger = logging.getLogger(__name__)

# Shared file where the last-known Misty IP is persisted.
# Stored alongside the service code so other sessions can read it.
_DISCOVERY_FILE = Path(__file__).parent / "misty_ip.json"

# How long a cached IP is considered valid before re-probing (seconds)
_CACHE_TTL_S = 300  # 5 minutes


def private_ipv4(value: str) -> str:
    """Validate an explicit RFC1918 host address, not a hostname or public target."""
    address = ipaddress.IPv4Address(value)
    if not any(address in ipaddress.IPv4Network(block)
               for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ValueError("Use a private IPv4 address on an approved local network.")
    return str(address)


def approved_subnet(value: str) -> ipaddress.IPv4Network:
    """Limit opt-in discovery to at most one explicitly approved /24."""
    network = ipaddress.IPv4Network(value, strict=True)
    private_ipv4(str(network.network_address))
    private_ipv4(str(network.broadcast_address))
    if network.prefixlen < 24:
        raise ValueError("Discovery is limited to one /24 or smaller subnet.")
    return network


def probe_misty_identity(ip: str, timeout: float = 2.0) -> Optional[dict]:
    """Read a robot identity without trusting proxies, response text, or cached data."""
    ip = private_ipv4(ip)
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(f"http://{ip}/api/device", timeout=timeout,
                                   allow_redirects=False)
            if response.status_code != 200:
                return None
            payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get("status") != "Success" or not isinstance(result, dict)
            or not all(isinstance(result.get(key), str) and result[key]
                       for key in ("serialNumber", "robotId", "robotVersion"))):
        return None
    # Use the address that actually responded; device metadata can contain a stale IP.
    return {
        "ipAddress": ip,
        "serialNumber": result["serialNumber"],
        "robotId": result["robotId"],
        "robotVersion": result["robotVersion"],
        "networkProfileName": result.get("currentProfileName"),
        "macAddress": result.get("macAddress"),
    }


def discover_misty_scoped(subnet: str, serial_number: str,
                          robot_id: str, candidate_ip: Optional[str] = None) -> Optional[dict]:
    """Find one expected robot only inside an operator-approved private subnet."""
    network = approved_subnet(subnet)
    if candidate_ip:
        candidate = private_ipv4(candidate_ip)
        if ipaddress.IPv4Address(candidate) not in network:
            raise ValueError("The target IP must be inside the approved subnet.")
        found = probe_misty_identity(candidate)
        if found and found["serialNumber"] == serial_number and found["robotId"] == robot_id:
            return found
        # An explicit target never authorizes scanning other addresses.
        return None
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(probe_misty_identity, str(ip), 1.0)
                   for ip in network.hosts()]
        for future in as_completed(futures):
            found = future.result()
            if found and found["serialNumber"] == serial_number and found["robotId"] == robot_id:
                for pending in futures:
                    pending.cancel()
                return found
    return None


def _get_local_subnets() -> list[str]:
    """Return /24 subnet prefixes to scan.
    
    Priority: MISTY_DISCOVERY_SUBNETS env var → auto-detected interfaces → common defaults.
    """
    # Check env var first (comma-separated prefixes)
    env_subnets = os.getenv("MISTY_DISCOVERY_SUBNETS", "").strip()
    if env_subnets:
        return [s.strip() for s in env_subnets.split(",") if s.strip()]

    subnets = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                parts = ip.split(".")
                subnets.add(f"{parts[0]}.{parts[1]}.{parts[2]}")
    except Exception:
        pass

    # Fallback: parse ipconfig output for Windows
    if not subnets:
        try:
            import subprocess
            result = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if "IPv4 Address" in line or "IPv4" in line:
                    parts_raw = line.split(":")
                    if len(parts_raw) >= 2:
                        ip = parts_raw[-1].strip()
                        if not ip.startswith("127.") and not ip.startswith("169.254."):
                            parts = ip.split(".")
                            if len(parts) == 4:
                                subnets.add(f"{parts[0]}.{parts[1]}.{parts[2]}")
        except Exception:
            pass

    # Always include common home subnets as fallback
    for prefix in ["10.0.0", "192.168.1", "192.168.0", "192.168.12"]:
        subnets.add(prefix)

    return list(subnets)


def _probe_ip(ip: str, timeout: float = 1.0) -> Optional[str]:
    """Probe a single IP for Misty's /api/device endpoint. Returns IP if Misty found."""
    try:
        resp = requests.get(f"http://{ip}/api/device", timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            # Misty's API returns a result with robot info
            if "result" in data or "serialNumber" in str(data):
                return ip
    except Exception:
        pass
    return None


def _read_cache() -> Optional[dict]:
    """Read the cached discovery file. Returns dict with 'ip' and 'timestamp' or None."""
    try:
        if _DISCOVERY_FILE.exists():
            data = json.loads(_DISCOVERY_FILE.read_text(encoding="utf-8"))
            if "ip" in data and "timestamp" in data:
                return data
    except Exception:
        pass
    return None


def _write_cache(ip: str) -> None:
    """Persist the discovered IP to the shared JSON file."""
    data = {"ip": ip, "timestamp": time.time(), "discovered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        _DISCOVERY_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info(f"Persisted Misty IP to {_DISCOVERY_FILE}")
    except Exception as e:
        logger.warning(f"Failed to write discovery cache: {e}")


def _verify_ip(ip: str, timeout: float = 2.0) -> bool:
    """Verify that a known IP still responds as Misty."""
    return _probe_ip(ip, timeout=timeout) is not None


def discover_misty(force_scan: bool = False, timeout_per_host: float = 1.0) -> Optional[str]:
    """
    Discover Misty's IP on the local network.

    Strategy:
    1. Check cached IP (if fresh and reachable, return immediately)
    2. Scan all local /24 subnets in parallel for Misty's API

    Args:
        force_scan: Skip cache and always scan the network.
        timeout_per_host: Timeout in seconds for each probe.

    Returns:
        Misty's IP address string, or None if not found.
    """
    # Step 1: Try cached IP
    if not force_scan:
        cache = _read_cache()
        if cache:
            cached_ip = cache["ip"]
            age = time.time() - cache["timestamp"]
            if age < _CACHE_TTL_S:
                logger.debug(f"Using cached Misty IP: {cached_ip} (age: {age:.0f}s)")
                if _verify_ip(cached_ip, timeout=timeout_per_host):
                    return cached_ip
                logger.info(f"Cached IP {cached_ip} is stale/unreachable, scanning...")
            else:
                # Cache expired but try it first (fast path)
                if _verify_ip(cached_ip, timeout=timeout_per_host):
                    _write_cache(cached_ip)  # refresh timestamp
                    return cached_ip
                logger.info(f"Expired cached IP {cached_ip} unreachable, scanning...")

    # Step 2: Full subnet scan
    subnets = _get_local_subnets()
    logger.info(f"Scanning subnets for Misty: {subnets}")

    # Build list of all IPs to probe
    all_ips = []
    for subnet in subnets:
        all_ips.extend(f"{subnet}.{i}" for i in range(1, 255))

    # Parallel scan
    start = time.time()
    with ThreadPoolExecutor(max_workers=64) as executor:
        futures = {executor.submit(_probe_ip, ip, timeout_per_host): ip for ip in all_ips}
        for future in as_completed(futures):
            result = future.result()
            if result:
                elapsed = time.time() - start
                logger.info(f"Misty discovered at {result} in {elapsed:.1f}s")
                _write_cache(result)
                # Cancel remaining futures
                for f in futures:
                    f.cancel()
                return result

    elapsed = time.time() - start
    logger.warning(f"Misty not found on any subnet after {elapsed:.1f}s scan")
    return None


def get_misty_ip() -> Optional[str]:
    """
    Get Misty's IP from the shared cache file without scanning.
    Use this from other sessions/services that just need to read the last known IP.

    Returns:
        Cached IP string, or None if no cache exists.
    """
    cache = _read_cache()
    if cache:
        return cache["ip"]
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("Discovering Misty on the network...")
    ip = discover_misty(force_scan=True)
    if ip:
        print(f"\n✓ Misty found at: {ip}")
        print(f"  Saved to: {_DISCOVERY_FILE}")
    else:
        print("\n✗ Misty not found. Is she powered on and connected to the network?")

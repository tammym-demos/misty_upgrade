"""Isolated travel discovery tests: no real network is accessed."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "windows-orchestration"))
import misty_discovery as discovery


IDENTITY = {
    "ipAddress": "192.168.137.2", "serialNumber": "test-serial",
    "robotId": "test-robot", "robotVersion": "test-firmware",
}


@pytest.mark.parametrize("value", ["8.8.8.8", "127.0.0.1", "169.254.1.1", "example.com", "::1"])
def test_reject_non_private_targets(value):
    with pytest.raises(ValueError):
        discovery.private_ipv4(value)


@pytest.mark.parametrize("value", ["10.0.0.0/16", "192.168.137.5/24", "8.8.8.0/24", "::/128"])
def test_reject_unsafe_or_ambiguous_subnets(value):
    with pytest.raises(ValueError):
        discovery.approved_subnet(value)


def test_explicit_target_never_scans(monkeypatch):
    calls = []
    monkeypatch.setattr(discovery, "probe_misty_identity", lambda ip, *args: calls.append(ip))
    assert discovery.discover_misty_scoped(
        "192.168.137.0/24", "test-serial", "test-robot", "192.168.137.2",
    ) is None
    assert calls == ["192.168.137.2"]


def test_target_must_be_in_approved_scope():
    with pytest.raises(ValueError):
        discovery.discover_misty_scoped("192.168.137.0/24", "s", "r", "10.0.0.5")


def test_scan_is_bounded_and_matches_both_identity_fields(monkeypatch):
    calls = []

    def probe(ip, *args):
        calls.append(ip)
        return {**IDENTITY, "serialNumber": "wrong"} if ip.endswith(".1") else dict(IDENTITY)

    monkeypatch.setattr(discovery, "probe_misty_identity", probe)
    result = discovery.discover_misty_scoped("192.168.137.0/30", "test-serial", "test-robot")
    assert result == IDENTITY
    assert set(calls) <= {"192.168.137.1", "192.168.137.2"}


def test_wrong_robot_id_is_not_accepted(monkeypatch):
    monkeypatch.setattr(discovery, "probe_misty_identity", lambda *args: {**IDENTITY, "robotId": "other"})
    assert discovery.discover_misty_scoped("192.168.137.0/30", "test-serial", "test-robot") is None


@pytest.mark.parametrize("payload", [
    [], {}, {"status": "Success", "result": {}},
    {"status": "Error", "result": IDENTITY},
])
def test_identity_rejects_success_shaped_non_robot_json(monkeypatch, payload):
    session = MagicMock()
    session.get.return_value.status_code = 200
    session.get.return_value.json.return_value = payload
    monkeypatch.setattr(discovery.requests, "Session", lambda: MagicMock(
        __enter__=lambda *args: session, __exit__=lambda *args: None,
    ))
    assert discovery.probe_misty_identity("192.168.137.2") is None


def test_identity_uses_probed_address_and_never_returns_psk(monkeypatch):
    session = MagicMock()
    session.get.return_value.status_code = 200
    session.get.return_value.json.return_value = {
        "status": "Success", "result": {**IDENTITY, "ipAddress": "10.0.0.9", "currentPreSharedKey": "secret"},
    }
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(discovery.requests, "Session", lambda: context)
    result = discovery.probe_misty_identity("192.168.137.2")
    assert result["ipAddress"] == "192.168.137.2"
    assert "secret" not in str(result)
    assert session.trust_env is False
    assert session.get.call_args.kwargs["allow_redirects"] is False


def test_probe_timeout_does_not_use_fallback_subnets(monkeypatch):
    session = MagicMock()
    session.get.side_effect = requests.Timeout()
    context = MagicMock()
    context.__enter__.return_value = session
    monkeypatch.setattr(discovery.requests, "Session", lambda: context)
    assert discovery.probe_misty_identity("192.168.137.2") is None

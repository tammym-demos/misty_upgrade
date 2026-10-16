"""Guided Wi-Fi tests using simulated APIs, prompts, interfaces, and telemetry."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "windows-orchestration"))
import misty_wifi as wifi
import misty_discovery as discovery

DISCOVER_HOTSPOT = wifi._discover_hotspot
SOURCE_IDENTITY = wifi._source_identity


IDENTITY = {
    "ipAddress": "192.168.137.2", "serialNumber": "test-serial",
    "robotId": "test-robot", "robotVersion": "test-firmware", "networkProfileName": "Travel",
}
INTERFACES = [("12", "Private hotspot", "192.168.137.1", "192.168.137.0/24")]


@pytest.fixture
def args(tmp_path):
    return SimpleNamespace(
        command="setup", mode="hotspot", misty_ip="10.0.0.2",
        interface="12", subnet="192.168.137.0/24", target_ip="192.168.137.2",
        serial=None, robot_id=None, report=str(tmp_path / "report.json"), controller_port=5001,
        new_profile=False,
    )


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    monkeypatch.setattr(requests.sessions.Session, "request", MagicMock(side_effect=AssertionError("Real HTTP forbidden")))
    monkeypatch.setattr(wifi.subprocess, "run", MagicMock(side_effect=AssertionError("Real OS inventory forbidden")))
    monkeypatch.setattr(discovery, "_get_local_subnets", MagicMock(side_effect=AssertionError("Broad scan forbidden")))
    monkeypatch.setattr(wifi.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(wifi, "_discover_hotspot", lambda: {
        "state": "On", "ssid": None, "interface": None, "warning": None,
    })


def stub_guided(monkeypatch, inputs):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    monkeypatch.setattr(wifi, "_assert_runtime_stopped", MagicMock())
    monkeypatch.setattr(wifi, "_source_identity", lambda args, **kwargs: {**IDENTITY, "ipAddress": "10.0.0.2"})
    monkeypatch.setattr(wifi, "_local_interfaces", lambda: INTERFACES)
    monkeypatch.setattr(wifi, "_hidden_password", lambda: "never-print-this")
    monkeypatch.setattr(wifi, "_verify_websocket", MagicMock())
    monkeypatch.setattr(discovery, "discover_misty_scoped", lambda *args: dict(IDENTITY))
    api = MagicMock(side_effect=[[{"ssid": "Travel"}], [], True])
    monkeypatch.setattr(wifi, "_checked_api", api)
    return api


@pytest.mark.parametrize("mode,answers", [
    ("hotspot", ["Travel", "yes"]),
    ("direct", ["shared-password", "yes", "Travel", "yes", "", "yes"]),
])
def test_both_modes_verify_before_reporting(monkeypatch, args, capsys, mode, answers):
    args.mode = mode
    api = stub_guided(monkeypatch, iter(answers))
    store = MagicMock(side_effect=AssertionError("No laptop credential storage"))
    monkeypatch.setattr(wifi, "_save_store", store)
    wifi.cmd_guided(args)
    report = json.loads(Path(args.report).read_text())
    assert report["status"] == "verified"
    assert report["ipAddress"] == IDENTITY["ipAddress"]
    assert "never-print-this" not in Path(args.report).read_text() + capsys.readouterr().out
    assert api.call_args_list[-1].args[:3] == ("POST", "/api/networks/create", "10.0.0.2")
    assert not store.called
    assert wifi._verify_websocket.call_count == 1


def test_cancel_before_switch_never_posts(monkeypatch, args):
    api = stub_guided(monkeypatch, iter(["Travel", "no"]))
    with pytest.raises(wifi.WifiSetupError, match="Cancelled"):
        wifi.cmd_guided(args)
    assert all(call.args[0] == "GET" for call in api.call_args_list)
    assert not Path(args.report).exists()


@pytest.mark.parametrize("kind", ["portal", "enterprise", "open"])
def test_unsupported_direct_network_never_sends_credentials(monkeypatch, args, kind):
    args.mode = "direct"
    api = stub_guided(monkeypatch, iter([kind]))
    with pytest.raises(wifi.WifiSetupError, match="unsupported"):
        wifi.cmd_guided(args)
    assert not api.called


def test_invisible_ssid_does_not_read_password_or_post(monkeypatch, args):
    api = stub_guided(monkeypatch, iter(["Other"]))
    monkeypatch.setattr(wifi, "_hidden_password", MagicMock(side_effect=AssertionError("No password needed")))
    with pytest.raises(wifi.WifiSetupError, match="not visible"):
        wifi.cmd_guided(args)
    assert api.call_count == 1


def test_lost_post_is_not_replayed_and_only_verification_can_succeed(monkeypatch, args):
    api = stub_guided(monkeypatch, iter(["Travel", "yes"]))
    api.side_effect = [[{"ssid": "Travel"}], [], wifi.WifiLinkLost("unknown")]
    wifi.cmd_guided(args)
    assert sum(call.args[0] == "POST" for call in api.call_args_list) == 1
    assert Path(args.report).exists()


def test_lost_get_is_not_treated_as_sent_handoff(monkeypatch):
    monkeypatch.setattr(wifi, "_checked_api", MagicMock(side_effect=wifi.WifiLinkLost("unknown")))
    with pytest.raises(wifi.WifiLinkLost):
        wifi._switch_network("10.0.0.2", "Travel", "secret")


def test_rejected_password_never_reports_success(monkeypatch, args):
    api = stub_guided(monkeypatch, iter(["Travel", "yes"]))
    api.side_effect = [[{"ssid": "Travel"}], [], wifi.WifiSetupError("Misty rejected")]
    with pytest.raises(wifi.WifiSetupError, match="rejected"):
        wifi.cmd_guided(args)
    assert not Path(args.report).exists()


def test_duplicate_profiles_require_explicit_id(monkeypatch):
    api = MagicMock(side_effect=[[{"ssid": "Travel", "id": 2}, {"ssid": "Travel", "id": 4}], True])
    monkeypatch.setattr(wifi, "_checked_api", api)
    monkeypatch.setattr(wifi, "_assert_runtime_stopped", MagicMock())
    answers = iter(["4", "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    wifi._switch_network("10.0.0.2", "Travel", "secret")
    assert api.call_args.args == ("POST", "/api/networks", "10.0.0.2", {"NetworkId": 4})


def test_failed_verification_is_bounded_and_does_not_write(monkeypatch, args):
    stub_guided(monkeypatch, iter(["Travel", "yes"]))
    scan = MagicMock(return_value=None)
    monkeypatch.setattr(discovery, "discover_misty_scoped", scan)
    with pytest.raises(wifi.WifiSetupError, match="No rollback is guaranteed"):
        wifi.cmd_guided(args)
    assert scan.call_count == 6
    assert not Path(args.report).exists()


def test_handoff_failure_displays_full_identity_for_recovery(monkeypatch, args, capsys):
    stub_guided(monkeypatch, iter(["Travel", "yes"]))
    monkeypatch.setattr(wifi, "_source_identity", SOURCE_IDENTITY)
    monkeypatch.setattr(discovery, "probe_misty_identity", lambda ip: {**IDENTITY, "ipAddress": ip})
    monkeypatch.setattr(discovery, "discover_misty_scoped", lambda *args: None)
    with pytest.raises(wifi.WifiSetupError, match="No rollback is guaranteed"):
        wifi.cmd_guided(args)
    output = capsys.readouterr().out
    assert f"serial={IDENTITY['serialNumber']}" in output
    assert f"robot ID={IDENTITY['robotId']}" in output
    assert not Path(args.report).exists()


def test_slow_handoff_can_join_after_the_original_three_attempts(monkeypatch, args):
    stub_guided(monkeypatch, iter(["Travel", "yes"]))
    scan = MagicMock(side_effect=[None, None, None, dict(IDENTITY)])
    monkeypatch.setattr(discovery, "discover_misty_scoped", scan)
    wifi.cmd_guided(args)
    assert scan.call_count == 4
    assert json.loads(Path(args.report).read_text())["status"] == "verified"


def test_verify_requires_identity_and_never_mutates_network(monkeypatch, args):
    args.command = "verify"
    args.serial, args.robot_id = IDENTITY["serialNumber"], IDENTITY["robotId"]
    api = stub_guided(monkeypatch, iter(["yes"]))
    wifi.cmd_guided(args)
    assert not api.called
    assert Path(args.report).exists()


def test_scope_overlap_refuses_ambiguous_routing(monkeypatch, args):
    monkeypatch.setattr(wifi, "_local_interfaces", lambda: INTERFACES + [
        ("50", "VPN", "192.168.137.9", "192.168.137.0/24"),
    ])
    with pytest.raises(wifi.WifiSetupError, match="overlaps"):
        wifi._choose_scope(args)


def test_changed_interface_is_not_replaced_by_broad_scan(monkeypatch, args):
    monkeypatch.setattr(wifi, "_local_interfaces", lambda: [])
    with pytest.raises(wifi.WifiSetupError, match="changed"):
        wifi._verify_target(args, IDENTITY, args.subnet, args.interface)


@pytest.mark.parametrize("payload", [{"status": "Error", "result": True}, {"status": "Success", "result": False}, []])
def test_api_rejects_error_and_false_results(monkeypatch, payload):
    monkeypatch.setattr(wifi, "_request", lambda *args: payload)
    with pytest.raises(wifi.WifiSetupError, match="rejected"):
        wifi._checked_api("POST", "/api/networks/create", "10.0.0.2", {"password": "secret"})


@pytest.mark.parametrize("error", [requests.HTTPError("secret"), requests.Timeout("secret"), ValueError("secret")])
def test_api_does_not_expose_echoed_password(monkeypatch, error):
    monkeypatch.setattr(wifi, "_request", MagicMock(side_effect=error))
    with pytest.raises(wifi.WifiSetupError) as exc:
        wifi._checked_api("POST", "/api/networks/create", "10.0.0.2", {"password": "secret"})
    assert "secret" not in str(exc.value)


def test_secure_prompt_refuses_echo_fallback(monkeypatch):
    def fallback(prompt):
        import warnings
        warnings.warn("echo", wifi.getpass.GetPassWarning)
        return "secret"
    monkeypatch.setattr(wifi.getpass, "getpass", fallback)
    with pytest.raises(wifi.WifiSetupError, match="Secure password"):
        wifi._hidden_password()


def test_websocket_requires_matching_telemetry_and_closes(monkeypatch):
    import websocket
    connection = MagicMock()
    connection.recv.side_effect = lambda: json.dumps({
        "eventName": json.loads(connection.send.call_args.args[0])["EventName"],
        "message": {"chargePercent": 80},
    })
    monkeypatch.setattr(websocket, "create_connection", lambda *args, **kwargs: connection)
    wifi._verify_websocket("192.168.137.2")
    connection.close.assert_called_once()


def test_websocket_failure_never_reports_success(monkeypatch):
    import websocket
    connection = MagicMock()
    connection.recv.side_effect = websocket.WebSocketTimeoutException()
    monkeypatch.setattr(websocket, "create_connection", lambda *args, **kwargs: connection)
    with pytest.raises(wifi.WifiSetupError, match="WebSocket"):
        wifi._verify_websocket("192.168.137.2")
    connection.close.assert_called_once()


@pytest.mark.parametrize("command", [
    '"C:\\Python\\python.exe" C:\\repo\\misty_controller.py',
    'python.exe "C:\\repo\\conference_mode.py" run --no-auto',
])
def test_active_runtime_blocks_setup(monkeypatch, command):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(wifi.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout=json.dumps({"ProcessId": 123, "CommandLine": command}),
    ))
    with pytest.raises(wifi.WifiSetupError, match="runtime is active"):
        wifi._assert_runtime_stopped()


def test_unavailable_process_inventory_fails_closed(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(wifi.subprocess, "run", MagicMock(side_effect=OSError("denied")))
    with pytest.raises(wifi.WifiSetupError, match="blocked"):
        wifi._assert_runtime_stopped()


def test_noninteractive_guided_flow_does_not_touch_network(monkeypatch, args):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(wifi.WifiSetupError, match="interactive"):
        wifi.cmd_guided(args)


@pytest.mark.parametrize("scan", [
    {"name": "Travel", "isSecure": True},
    {"Name": "Travel", "IsSecure": True},
])
def test_documented_scan_field_names_are_supported(monkeypatch, args, scan):
    api = stub_guided(monkeypatch, iter(["Travel", "yes"]))
    api.side_effect = [[scan], [], True]
    wifi.cmd_guided(args)
    assert Path(args.report).exists()


def test_documented_saved_profile_fields_are_supported(monkeypatch):
    api = MagicMock(side_effect=[[{"ssid": '"Travel"', "networkId": 7}], True])
    monkeypatch.setattr(wifi, "_checked_api", api)
    monkeypatch.setattr(wifi, "_assert_runtime_stopped", MagicMock())
    answers = iter(["7", "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    wifi._switch_network("10.0.0.2", "Travel", "secret")
    assert api.call_args.args[-1] == {"NetworkId": 7}


def test_robot_still_on_old_ssid_is_not_handoff_success(monkeypatch, args):
    stub_guided(monkeypatch, iter(["Travel", "yes"]))
    monkeypatch.setattr(discovery, "discover_misty_scoped", lambda *args: {
        **IDENTITY, "networkProfileName": "Old network",
    })
    with pytest.raises(wifi.WifiSetupError, match="not verified"):
        wifi.cmd_guided(args)
    assert not Path(args.report).exists()


def test_report_write_failure_is_not_success(monkeypatch, args):
    stub_guided(monkeypatch, iter(["Travel", "yes"]))
    args.report = str(Path(args.report) / "missing-parent")
    with pytest.raises(OSError):
        wifi.cmd_guided(args)


@pytest.mark.parametrize("value,expected", [
    ("", "192.168.137.0/24"),
    ("/24", "192.168.137.0/24"),
    ("/25", "192.168.137.0/25"),
    ("192.168.137.0/24", "192.168.137.0/24"),
])
def test_interactive_subnet_default_and_prefix_shorthand(monkeypatch, args, value, expected):
    args.subnet = None
    answers = iter([value, "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(wifi, "_local_interfaces", lambda: INTERFACES)
    assert wifi._choose_scope(args) == (expected, "12")


def test_bad_subnet_input_retries_without_network_access(monkeypatch, args, capsys):
    args.subnet = None
    answers = iter(["bad", "/16", "10.0.0.0/24", "/24", "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(wifi, "_local_interfaces", lambda: INTERFACES)
    assert wifi._choose_scope(args) == ("192.168.137.0/24", "12")
    assert capsys.readouterr().out.count("ERROR: Enter a private subnet") == 3


def test_invalid_explicit_subnet_has_actionable_error(monkeypatch, args):
    args.subnet = "/16"
    monkeypatch.setattr(wifi, "_local_interfaces", lambda: INTERFACES)
    with pytest.raises(wifi.WifiSetupError, match="for example 192.168.137.0/24 or /24"):
        wifi._choose_scope(args)


def test_hotspot_interface_choice_retries_upstream_selection(monkeypatch, args, capsys):
    args.interface = None
    args.subnet = None
    args.target_ip = None
    rows = [("4", "Wi-Fi", "10.0.0.18", "10.0.0.0/24"),
            ("16", "Wi-Fi 4", "192.168.137.1", "192.168.137.0/24")]
    monkeypatch.setattr(wifi, "_local_interfaces", lambda: rows)
    answers = iter(["4", "16", "/24", "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert wifi._choose_scope(args, source_ip="10.0.0.52") == ("192.168.137.0/24", "16")
    output = capsys.readouterr().out
    assert "current robot network; not the private hotspot" in output
    assert "That is Misty's current network" in output


def test_bad_hotspot_scope_is_rejected_before_password_entry(monkeypatch, args):
    api = stub_guided(monkeypatch, iter(["Travel"]))
    args.interface = "4"
    monkeypatch.setattr(wifi, "_local_interfaces", lambda: [
        ("4", "Wi-Fi", "10.0.0.18", "10.0.0.0/24"),
    ])
    password = MagicMock(side_effect=AssertionError("Do not collect password before valid scope"))
    monkeypatch.setattr(wifi, "_hidden_password", password)
    with pytest.raises(wifi.WifiSetupError, match="current network"):
        wifi.cmd_guided(args)
    assert not password.called
    assert all(call.args[0] == "GET" for call in api.call_args_list)


def test_discovered_hotspot_needs_only_one_final_confirmation(monkeypatch, args, capsys):
    api = stub_guided(monkeypatch, iter([]))
    args.mode = None
    args.interface = None
    args.subnet = None
    monkeypatch.setattr(wifi, "_discover_hotspot", lambda: {
        "state": "On", "ssid": "Travel", "interface": "12", "warning": None,
    })
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return "yes"

    monkeypatch.setattr("builtins.input", answer)
    wifi.cmd_guided(args)
    assert len(prompts) == 1
    assert "permission to share/probe 192.168.137.0/24" in prompts[0]
    assert "Review:" in capsys.readouterr().out
    assert api.call_args.args[0] == "POST"
    assert Path(args.report).exists()


def test_discovered_hotspot_cancellation_does_not_switch_or_scan(monkeypatch, args):
    api = stub_guided(monkeypatch, iter(["no"]))
    monkeypatch.setattr(wifi, "_discover_hotspot", lambda: {
        "state": "On", "ssid": "Travel", "interface": "12", "warning": None,
    })
    scan = MagicMock(side_effect=AssertionError("No target probing before consent"))
    monkeypatch.setattr(discovery, "discover_misty_scoped", scan)
    with pytest.raises(wifi.WifiSetupError, match="Cancelled"):
        wifi.cmd_guided(args)
    assert all(call.args[0] == "GET" for call in api.call_args_list)
    assert not scan.called


def test_hotspot_off_fails_before_robot_or_password_requests(monkeypatch, args):
    api = stub_guided(monkeypatch, iter([]))
    monkeypatch.setattr(wifi, "_discover_hotspot", lambda: {
        "state": "Off", "ssid": "Travel", "interface": None, "warning": None,
    })
    with pytest.raises(wifi.WifiSetupError, match="hotspot is OFF"):
        wifi.cmd_guided(args)
    assert not api.called
    assert not Path(args.report).exists()


def test_unknown_hotspot_falls_back_to_ssid_and_interface_only(monkeypatch, args, capsys):
    api = stub_guided(monkeypatch, iter(["Travel", "12", "yes"]))
    args.interface = args.subnet = None
    monkeypatch.setattr(wifi, "_discover_hotspot", lambda: {
        "state": "Unknown", "ssid": None, "interface": None, "warning": "Manual selection required.",
    })
    wifi.cmd_guided(args)
    assert "WARNING: Manual selection required." in capsys.readouterr().out
    assert api.call_args.args[0] == "POST"


def test_unique_saved_profile_is_reused_without_id_prompt(monkeypatch):
    api = MagicMock(side_effect=[[{"ssid": '"Travel"', "networkId": 7}], True])
    monkeypatch.setattr(wifi, "_checked_api", api)
    monkeypatch.setattr(wifi, "_assert_runtime_stopped", MagicMock())
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "yes")
    wifi._switch_network("10.0.0.2", "Travel", "secret", auto_profile=True)
    assert len(prompts) == 1
    assert api.call_args.args[-1] == {"NetworkId": 7}


def test_hotspot_new_profile_uses_entered_password(monkeypatch, args):
    api = stub_guided(monkeypatch, iter(["Travel", "yes"]))
    args.new_profile = True
    api.side_effect = [[{"ssid": "Travel"}], [{"ssid": "Travel", "networkId": 7}], True]
    wifi.cmd_guided(args)
    assert api.call_args.args == (
        "POST", "/api/networks/create", "10.0.0.2",
        {"networkname": "Travel", "password": "never-print-this"},
    )
    assert Path(args.report).exists()


@pytest.mark.parametrize("indexes,expected", [([16], "16"), ([], None), ([16, 22], None)])
def test_windows_discovery_only_returns_non_secret_unambiguous_metadata(monkeypatch, indexes, expected):
    run = MagicMock(return_value=SimpleNamespace(stdout=json.dumps({
        "state": "On", "ssid": "Travel", "interfaces": indexes, "Passphrase": "never-return",
    })))
    monkeypatch.setattr(wifi.subprocess, "run", run)
    result = DISCOVER_HOTSPOT()
    assert result["ssid"] == "Travel"
    assert result["interface"] == expected
    assert "never-return" not in str(result)
    assert ".Passphrase" not in run.call_args.args[0][-1]


def test_windows_discovery_failure_is_explicit_manual_fallback(monkeypatch):
    monkeypatch.setattr(wifi.subprocess, "run", MagicMock(side_effect=OSError("denied")))
    result = DISCOVER_HOTSPOT()
    assert result["state"] == "Unknown"
    assert result["interface"] is None
    assert "unavailable" in result["warning"]

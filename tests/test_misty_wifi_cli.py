"""CLI delegation tests run only against a temporary simulated repository."""

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPORT = {
    "status": "verified", "ipAddress": "192.168.137.2",
    "serialNumber": "test-serial", "robotId": "test-robot",
    "robotVersion": "test-firmware", "discoveredAt": "test",
}


@pytest.fixture
def repo(tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node is required for CLI smoke tests")
    (tmp_path / "tools").mkdir()
    orch = tmp_path / "src" / "windows-orchestration"
    orch.mkdir(parents=True)
    shutil.copyfile(ROOT / "tools" / "misty-services.mjs", tmp_path / "tools" / "misty-services.mjs")
    (orch / ".env").write_text("MISTY_IP=10.0.0.99\n")
    (orch / "misty_wifi.py").write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path('forwarded.json').write_text(json.dumps(sys.argv[1:]))\n"
        "if '--help' in sys.argv: sys.exit(0)\n"
        "if os.getenv('HELPER_FAIL'): sys.exit(2)\n"
        "if not os.getenv('NO_REPORT'):\n"
        "    report = os.getenv('FAKE_REPORT')\n"
        "    pathlib.Path(sys.argv[sys.argv.index('--report') + 1]).write_text(report)\n",
    )
    return tmp_path


def run_cli(repo, *args, **env):
    return subprocess.run(
        ["node", str(repo / "tools" / "misty-services.mjs"), *args],
        cwd=repo, capture_output=True, text=True, timeout=20,
        env={**os.environ, "FAKE_REPORT": json.dumps(REPORT), **env},
    )


def test_help_does_not_invoke_python_or_start_ai(repo):
    result = run_cli(repo, "wifi", "--help")
    assert result.returncode == 0
    assert "--target-ip" in result.stdout
    assert not (repo / "forwarded.json").exists()
    assert not (repo / ".misty-services.json").exists()


def test_success_preserves_services_and_env_and_synchronizes_caches(repo):
    state = {"foundry": {"managed": False}, "orchestration": {"pid": 123}, "misty": {"ipAddress": "10.0.0.9"}}
    (repo / ".misty-services.json").write_text(json.dumps(state))
    env_before = (repo / "src" / "windows-orchestration" / ".env").read_bytes()
    result = run_cli(repo, "wifi", "verify", "--python", sys.executable,
                     "--interface", "12", "--subnet", "192.168.137.0/24", "--ip", "10.0.0.8")
    assert result.returncode == 0, result.stderr
    saved = json.loads((repo / ".misty-services.json").read_text())
    assert saved["foundry"] == state["foundry"]
    assert saved["orchestration"] == state["orchestration"]
    assert saved["misty"]["ipAddress"] == "192.168.137.2"
    assert "status" not in saved["misty"]
    assert (repo / "src" / "windows-orchestration" / ".env").read_bytes() == env_before
    cache = json.loads((repo / "src" / "windows-orchestration" / "misty_ip.json").read_text())
    assert cache["ip"] == "192.168.137.2"
    forwarded = json.loads((repo / "forwarded.json").read_text())
    assert forwarded[forwarded.index("--ip") + 1] == "10.0.0.8"
    assert "--python" not in forwarded
    report_path = Path(forwarded[forwarded.index("--report") + 1])
    assert not report_path.parent.exists()


def test_verify_defaults_to_saved_identity(repo):
    (repo / ".misty-services.json").write_text(json.dumps({"misty": REPORT}))
    result = run_cli(repo, "wifi", "verify", "--python", sys.executable)
    assert result.returncode == 0, result.stderr
    forwarded = json.loads((repo / "forwarded.json").read_text())
    assert forwarded[forwarded.index("--serial") + 1] == "test-serial"
    assert forwarded[forwarded.index("--robot-id") + 1] == "test-robot"


def test_setup_stop_verify_preserves_robot_identity(repo):
    result = run_cli(repo, "wifi", "setup", "--new-profile", "--python", sys.executable)
    assert result.returncode == 0, result.stderr
    forwarded = json.loads((repo / "forwarded.json").read_text())
    assert "--new-profile" in forwarded
    cleanup_calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            cleanup_calls.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"Success"}')

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = run_cli(
                repo, "stop", "--skip-foundry",
                "--misty-ip", f"127.0.0.1:{server.server_port}",
            )
        finally:
            server.shutdown()
            worker.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert cleanup_calls == [
        "/api/audio/keyphrase/stop", "/api/audio/record/stop",
        "/api/skills/cancel", "/api/halt", "/api/images/display", "/api/led",
    ]
    saved = json.loads((repo / ".misty-services.json").read_text())
    assert saved["misty"]["serialNumber"] == REPORT["serialNumber"]
    assert saved["misty"]["robotId"] == REPORT["robotId"]
    result = run_cli(repo, "wifi", "verify", "--python", sys.executable)
    assert result.returncode == 0, result.stderr
    forwarded = json.loads((repo / "forwarded.json").read_text())
    assert forwarded[forwarded.index("--serial") + 1] == REPORT["serialNumber"]
    assert forwarded[forwarded.index("--robot-id") + 1] == REPORT["robotId"]


@pytest.mark.parametrize("env", [{"HELPER_FAIL": "1"}, {"NO_REPORT": "1"}, {"FAKE_REPORT": "{}"},
                                  {"FAKE_REPORT": json.dumps({**REPORT, "ipAddress": "8.8.8.8"})}])
def test_failure_never_creates_verified_caches(repo, env):
    result = run_cli(repo, "wifi", "verify", "--python", sys.executable, **env)
    assert result.returncode != 0
    assert not (repo / ".misty-services.json").exists()
    assert not (repo / "src" / "windows-orchestration" / "misty_ip.json").exists()


def test_different_identity_never_replaces_state(repo):
    original = json.dumps({"misty": {**REPORT, "serialNumber": "expected-serial"}})
    (repo / ".misty-services.json").write_text(original)
    result = run_cli(repo, "wifi", "verify", "--python", sys.executable)
    assert result.returncode != 0
    assert "different robot" in result.stderr
    assert (repo / ".misty-services.json").read_text() == original


def test_active_controller_blocks_before_delegation(repo):
    original = json.dumps({"controller": {"pid": os.getpid()}})
    (repo / ".misty-services.json").write_text(original)
    result = run_cli(repo, "wifi", "setup", "--python", sys.executable)
    assert result.returncode != 0
    assert "controller is active" in result.stderr
    assert not (repo / "forwarded.json").exists()


def test_subcommand_help_works_even_with_active_controller(repo):
    (repo / ".misty-services.json").write_text(json.dumps({"controller": {"pid": os.getpid()}}))
    result = run_cli(repo, "wifi", "setup", "--help", "--python", sys.executable)
    assert result.returncode == 0, result.stderr


def test_persistence_failure_is_reported_not_hidden(repo):
    (repo / "src" / "windows-orchestration" / "misty_ip.json").mkdir()
    result = run_cli(repo, "wifi", "verify", "--python", sys.executable)
    assert result.returncode != 0
    assert "saving addresses failed" in result.stderr


def test_corrupt_service_state_is_not_silently_overwritten(repo):
    original = "invalid state"
    (repo / ".misty-services.json").write_text(original)
    result = run_cli(repo, "wifi", "verify", "--python", sys.executable)
    assert result.returncode != 0
    assert "state is invalid" in result.stderr
    assert (repo / ".misty-services.json").read_text() == original


@pytest.mark.parametrize("flag", ["--report", "--report=somewhere"])
def test_internal_report_path_cannot_be_overridden(repo, flag):
    result = run_cli(repo, "wifi", "verify", flag)
    assert result.returncode != 0
    assert "reserved" in result.stderr

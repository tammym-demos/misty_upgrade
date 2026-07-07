"""Cloud-safe unit tests for tools/train_face.py (#112).

These tests mock all Misty REST calls, so they run without hardware.
"""

import importlib.util
import os


def load_train_face_module():
    module_path = os.path.join(
        os.path.dirname(__file__), "..", "tools", "train_face.py"
    )
    spec = importlib.util.spec_from_file_location("train_face", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_list_faces_parses_success_payload(monkeypatch):
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")
    monkeypatch.setattr(
        trainer, "_get", lambda ep: {"status": "Success", "result": ["Tammy", "Alex"]}
    )
    assert trainer.list_faces() == ["Tammy", "Alex"]


def test_list_faces_returns_empty_on_error(monkeypatch):
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")

    def boom(ep):
        raise RuntimeError("network down")

    monkeypatch.setattr(trainer, "_get", boom)
    assert trainer.list_faces() == []


def test_start_training_rejects_empty_name():
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")
    assert trainer.start_training("") is False


def test_start_training_rejects_overlong_name():
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")
    too_long = "x" * (mod.MAX_FACE_ID_LEN + 1)
    assert trainer.start_training(too_long) is False


def test_start_training_success(monkeypatch):
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")
    calls = {}

    def fake_post(ep, body=None):
        calls["ep"] = ep
        calls["body"] = body
        return {"status": "Success"}

    monkeypatch.setattr(trainer, "_post", fake_post)
    assert trainer.start_training("Tammy") is True
    assert calls["ep"] == "/faces/training/start"
    assert calls["body"] == {"FaceId": "Tammy"}


def test_start_training_reports_rejection(monkeypatch):
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")
    monkeypatch.setattr(trainer, "_post", lambda ep, body=None: {"status": "Failed"})
    assert trainer.start_training("Tammy") is False


def test_run_training_returns_zero_when_face_present(monkeypatch):
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    # empty before, trained after
    face_lists = iter([[], ["Tammy"]])
    monkeypatch.setattr(trainer, "list_faces", lambda: next(face_lists))
    monkeypatch.setattr(trainer, "start_training", lambda name: True)
    assert mod.run_training(trainer, "Tammy", wait_s=0.0, verify=False) == 0


def test_run_training_returns_one_when_face_missing(monkeypatch):
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(trainer, "list_faces", lambda: [])
    monkeypatch.setattr(trainer, "start_training", lambda name: True)
    assert mod.run_training(trainer, "Tammy", wait_s=0.0, verify=False) == 1


def test_run_training_returns_one_when_start_fails(monkeypatch):
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")
    monkeypatch.setattr(trainer, "list_faces", lambda: [])
    monkeypatch.setattr(trainer, "start_training", lambda name: False)
    assert mod.run_training(trainer, "Tammy", wait_s=0.0, verify=False) == 1


def test_main_requires_name_or_list():
    mod = load_train_face_module()
    try:
        mod.main(["--misty-ip", "127.0.0.1"])
        assert False, "expected SystemExit from argparse error"
    except SystemExit as exc:
        assert exc.code != 0


def test_main_list_uses_env_default_ip(monkeypatch):
    mod = load_train_face_module()
    # No --misty-ip passed: the CLI must fall back to the MISTY_IP env var.
    monkeypatch.setenv("MISTY_IP", "10.9.9.9")
    captured = {}

    def fake_init(self, misty_ip, timeout=10.0):
        captured["ip"] = misty_ip
        self.misty_ip = misty_ip
        self.base_url = f"http://{misty_ip}/api"
        self.timeout = timeout

    monkeypatch.setattr(mod.MistyFaceTrainer, "__init__", fake_init)
    monkeypatch.setattr(mod.MistyFaceTrainer, "check_connectivity", lambda self: True)
    monkeypatch.setattr(mod.MistyFaceTrainer, "list_faces", lambda self: ["Tammy"])
    assert mod.main(["--list"]) == 0
    assert captured["ip"] == "10.9.9.9"


def test_main_errors_when_no_ip_available(monkeypatch):
    mod = load_train_face_module()
    monkeypatch.delenv("MISTY_IP", raising=False)
    try:
        mod.main(["--list"])
        assert False, "expected SystemExit when no IP is available"
    except SystemExit as exc:
        assert exc.code != 0


def test_wait_type_rejects_negative():
    mod = load_train_face_module()
    parser = mod.build_parser()
    try:
        parser.parse_args(["--name", "Tammy", "--misty-ip", "127.0.0.1", "--wait", "-1"])
        assert False, "expected SystemExit for negative --wait"
    except SystemExit as exc:
        assert exc.code != 0


def test_recognize_once_extracts_label(monkeypatch):
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(trainer, "_post", lambda ep, body=None: {"status": "Success"})
    monkeypatch.setattr(
        trainer, "_get", lambda ep: {"result": {"label": "Tammy"}}
    )
    assert trainer.recognize_once(timeout_s=5.0) == "Tammy"


def test_recognize_once_unsupported_get_returns_empty(monkeypatch, capsys):
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(trainer, "_post", lambda ep, body=None: {"status": "Success"})

    def unsupported(ep):
        raise RuntimeError("404 Not Found")

    monkeypatch.setattr(trainer, "_get", unsupported)
    assert trainer.recognize_once(timeout_s=5.0) == ""
    out = capsys.readouterr().out
    assert "not available" in out


def test_recognize_once_ignores_unknown_person(monkeypatch):
    mod = load_train_face_module()
    trainer = mod.MistyFaceTrainer("127.0.0.1")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    # Guarantee exactly one polling iteration: deadline uses the first value (0),
    # first while-check (1.0) is < deadline (5.0) so the body runs once, second
    # check (100.0) exits the loop.
    monotonic_values = iter([0.0, 1.0, 100.0])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(trainer, "_post", lambda ep, body=None: {"status": "Success"})
    seen = {"gets": 0}

    def only_unknown(ep):
        seen["gets"] += 1
        return {"result": {"label": mod.UNKNOWN_LABEL}}

    monkeypatch.setattr(trainer, "_get", only_unknown)
    assert trainer.recognize_once(timeout_s=5.0) == ""
    assert seen["gets"] == 1  # the ignore-logic branch actually executed


def test_wait_type_rejects_nonfinite():
    mod = load_train_face_module()
    parser = mod.build_parser()
    for bad in ("nan", "inf", "-inf"):
        try:
            parser.parse_args(
                ["--name", "Tammy", "--misty-ip", "127.0.0.1", "--wait", bad]
            )
            assert False, f"expected SystemExit for --wait {bad}"
        except SystemExit as exc:
            assert exc.code != 0


def test_main_rejects_list_and_name_together(monkeypatch):
    mod = load_train_face_module()
    try:
        mod.main(["--list", "--name", "Tammy", "--misty-ip", "127.0.0.1"])
        assert False, "expected SystemExit for mutually exclusive actions"
    except SystemExit as exc:
        assert exc.code != 0


def test_main_rejects_verify_without_name(monkeypatch):
    mod = load_train_face_module()
    try:
        mod.main(["--list", "--verify", "--misty-ip", "127.0.0.1"])
        assert False, "expected SystemExit for --verify without --name"
    except SystemExit as exc:
        assert exc.code != 0


def test_main_returns_one_when_unreachable(monkeypatch):
    mod = load_train_face_module()
    monkeypatch.setattr(mod.MistyFaceTrainer, "check_connectivity", lambda self: False)
    assert mod.main(["--list", "--misty-ip", "127.0.0.1"]) == 1

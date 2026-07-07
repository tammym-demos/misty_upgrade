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
    monkeypatch.setattr(mod.MistyFaceTrainer, "check_connectivity", lambda self: True)
    monkeypatch.setattr(mod.MistyFaceTrainer, "list_faces", lambda self: ["Tammy"])
    assert mod.main(["--list", "--misty-ip", "127.0.0.1"]) == 0


def test_main_returns_one_when_unreachable(monkeypatch):
    mod = load_train_face_module()
    monkeypatch.setattr(mod.MistyFaceTrainer, "check_connectivity", lambda self: False)
    assert mod.main(["--list", "--misty-ip", "127.0.0.1"]) == 1

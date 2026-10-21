"""Local-only photo identity and memory boundaries, without hardware or model files."""

import io
import json
import os
import sys
import threading
import time
import types
from unittest import mock

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration"))
import face_identity_service as identity
import face_recognition_service as frs
import misty_controller as mc
import object_detection_service as vision
import orchestration_service as svc


class Embedder(frs.FaceEmbedder):
    def extract_embeddings(self, frame):
        if frame[0, 0, 0] == 255:
            return []
        return [np.array([1, 0, 0, 0] if frame[0, 0, 0] < 100 else [0, 1, 0, 0], dtype=np.float32)]


def recognizer(tmp_path):
    return frs.FaceRecognizer(frs.FaceProfileStore(str(tmp_path)), Embedder(), min_samples=3)


def frames():
    return [np.full((32, 40, 3), 60, dtype=np.uint8) for _ in range(5)]


def test_actual_reference_jpeg_roundtrip_and_forget(tmp_path):
    cv2 = pytest.importorskip("cv2")
    rec = recognizer(tmp_path)
    profile = rec.enroll("Morgan", frames(), save_reference_photo=True, replace=False)
    loaded = rec.store.load("Morgan")
    assert loaded.reference_jpeg.tobytes().startswith(b"\xff\xd8")
    decoded = cv2.imdecode(loaded.reference_jpeg, cv2.IMREAD_COLOR)
    assert decoded.shape == (32, 40, 3)
    assert decoded.mean() == pytest.approx(60, abs=2)
    assert loaded.reference_jpeg.tobytes() == profile.reference_jpeg.tobytes()
    assert rec.recognize_consistent(frames()).name == "Morgan"
    assert rec.store.delete("Morgan")
    assert list(tmp_path.iterdir()) == []


def test_photo_enrollment_never_overwrites_or_leaves_temporary_files(tmp_path):
    pytest.importorskip("cv2")
    rec = recognizer(tmp_path)
    rec.enroll("Morgan", frames(), save_reference_photo=True, replace=False)
    path = tmp_path / "Morgan.npz"
    before = path.read_bytes()
    with pytest.raises(frs.ProfileExistsError):
        rec.enroll("Morgan", frames(), save_reference_photo=True, replace=False)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Morgan.npz"]


def test_legacy_embedding_archive_still_loads(tmp_path):
    np.savez(
        tmp_path / "Morgan.npz", name="Morgan", embeddings=np.array([[1, 0, 0]]),
        model_name=frs.MODEL_NAME, model_version=frs.MODEL_VERSION, created_at="earlier",
    )
    assert frs.FaceProfileStore(str(tmp_path)).load("Morgan").reference_jpeg is None


@pytest.mark.parametrize("issue", ["no_face", "multiple", "different_person"])
def test_photo_enrollment_rejects_ambiguous_samples_without_saving(tmp_path, issue):
    rec = recognizer(tmp_path)
    samples = frames()
    if issue == "no_face":
        samples[-1][:] = 255
    elif issue == "different_person":
        samples[-1][:] = 120
    else:
        rec.embedder.extract_embeddings = lambda _: [np.ones(4), np.ones(4)]
    with pytest.raises(frs.FaceRecognitionError):
        rec.enroll("Morgan", samples, save_reference_photo=True, replace=False)
    assert list(tmp_path.iterdir()) == []


def test_ambiguous_gallery_is_unknown(tmp_path):
    rec = recognizer(tmp_path)
    rec.enroll("Morgan", frames())
    rec.enroll("Casey", frames())
    assert rec.recognize_consistent(frames()) is None


@pytest.mark.parametrize("invalid", [np.zeros(4), np.full(4, np.nan), np.full(4, np.inf)])
def test_invalid_face_features_cannot_match(tmp_path, invalid):
    rec = recognizer(tmp_path)
    rec.embedder.extract_embeddings = lambda _: [invalid]
    with pytest.raises(frs.FaceRecognitionError):
        rec.enroll("Morgan", frames())
    assert rec.store.list_names() == []


def controller(rec):
    ctrl = mc.MistyController.__new__(mc.MistyController)
    ctrl.running = True
    ctrl._muted = False
    ctrl._recognized_face = None
    ctrl._laptop_face_recognizer = rec
    ctrl.state = mc.State.PROCESSING
    ctrl.state_lock = threading.Lock()
    return ctrl


def camera(monkeypatch):
    source = mock.MagicMock()
    source.__enter__.return_value.capture.side_effect = frames()
    factory = mock.Mock(return_value=source)
    monkeypatch.setattr(frs, "MistyCameraFrameSource", factory)
    return factory


def test_controller_saves_photo_only_after_explicit_enrollment(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    rec = recognizer(tmp_path)
    ctrl = controller(rec)
    factory = camera(monkeypatch)
    monkeypatch.setattr(mc, "MISTY_IP", "192.0.2.12")
    result = ctrl.enroll_reference_photo("Morgan")
    assert result == {"status": "ok", "name": "Morgan", "reference_photo_saved": True}
    factory.assert_called_once_with("192.0.2.12")
    assert rec.store.load("Morgan").reference_jpeg is not None
    with pytest.raises(frs.ProfileExistsError):
        ctrl.enroll_reference_photo("morgan")
    assert factory.call_count == 1
    monkeypatch.setattr(mc, "FACE_PROFILE_DIR", str(tmp_path))
    assert ctrl.forget_reference_photo("morgan")["deleted"] is True
    assert rec.store.list_names() == []


@pytest.mark.parametrize("cancel", ["muted", "not_processing", "shutdown", "during_inference"])
def test_cancelled_enrollment_does_not_save(tmp_path, monkeypatch, cancel):
    pytest.importorskip("cv2")
    rec = recognizer(tmp_path)
    ctrl = controller(rec)
    factory = camera(monkeypatch)
    if cancel == "muted":
        ctrl._muted = True
    elif cancel == "not_processing":
        ctrl.state = mc.State.IDLE
    elif cancel == "shutdown":
        ctrl.running = False
    else:
        prepare = rec.prepare_enrollment

        def cancelled(*args, **kwargs):
            profile = prepare(*args, **kwargs)
            ctrl._muted = True
            return profile

        rec.prepare_enrollment = cancelled
    with pytest.raises(frs.FaceRecognitionError):
        ctrl.enroll_reference_photo("Morgan")
    assert rec.store.list_names() == []
    if cancel != "during_inference":
        factory.assert_not_called()


def test_followup_face_check_clears_stale_identity(monkeypatch):
    ctrl = mc.MistyController.__new__(mc.MistyController)
    ctrl._recognized_face = "Morgan"
    ctrl._greeter_enabled = True
    ctrl.recognize_face_laptop = lambda: None
    monkeypatch.setattr(mc, "USE_LAPTOP_FACE_RECOGNITION", True)
    result, thread = ctrl._start_face_check()
    assert ctrl._finish_face_check(result, thread) is None
    assert ctrl._recognized_face is None


def test_ordinary_conversation_only_reuses_explicit_identity_without_camera(monkeypatch):
    ctrl = mc.MistyController.__new__(mc.MistyController)
    ctrl._recognized_face = "Morgan"
    ctrl._greeter_enabled = False
    ctrl.recognize_face_laptop = mock.Mock()
    monkeypatch.setattr(mc, "USE_LAPTOP_FACE_RECOGNITION", True)
    result, thread = ctrl._start_face_check()
    assert thread is None
    assert ctrl._finish_face_check(result, thread) == "Morgan"
    ctrl.recognize_face_laptop.assert_not_called()


@pytest.mark.parametrize("known", [None, "Morgan"])
def test_explicit_recognition_updates_or_clears_conversation_identity(tmp_path, monkeypatch, known):
    ctrl = controller(recognizer(tmp_path))
    ctrl._recognized_face = "Casey"
    factory = camera(monkeypatch)
    ctrl._recognize_camera_person = mock.Mock(return_value=known)
    assert ctrl.recognize_reference_photo() == {"status": "ok", "name": known}
    assert ctrl._recognized_face == known
    factory.assert_called_once()


@pytest.mark.parametrize("utterance", [
    "Take a picture of this person and remember them. Their name is Morgan.",
    "Take a photo of me and remember me, my name is Morgan!",
    "Please take a picture of her and remember her. There name is Morgan.",
])
def test_explicit_voice_enrollment_calls_only_local_controller(monkeypatch, utterance):
    monkeypatch.setenv("USE_LAPTOP_FACE_RECOGNITION", "true")
    response = mock.Mock(status_code=200)
    response.json.return_value = {"status": "ok", "name": "Morgan", "reference_photo_saved": True}
    post = mock.Mock(return_value=response)
    monkeypatch.setattr(identity.requests, "post", post)
    assert identity.face_response_for_query(utterance) == "I saved a local reference photo for Morgan."
    assert post.call_args.args[0].startswith("http://127.0.0.1:")
    assert post.call_args.kwargs["json"] == {"name": "Morgan"}
    assert post.call_args.args[0].endswith("/api/faces/enroll")


@pytest.mark.parametrize("utterance", [
    "Take a picture of me and remember me.",
    "Take a picture of me and remember me. My name is ../../outside.",
])
def test_invalid_or_missing_name_never_captures(monkeypatch, utterance):
    post = mock.Mock()
    monkeypatch.setattr(identity.requests, "post", post)
    assert "short name" in identity.face_response_for_query(utterance)
    post.assert_not_called()


def test_ordinary_name_and_memory_chat_cannot_enroll(monkeypatch):
    post = mock.Mock()
    monkeypatch.setattr(identity.requests, "post", post)
    assert identity.face_response_for_query("My name is Morgan") is None
    assert identity.face_response_for_query("Remember that I like music") is None
    post.assert_not_called()


def test_unknown_and_unavailable_are_not_successful_identity(monkeypatch):
    monkeypatch.setenv("USE_LAPTOP_FACE_RECOGNITION", "true")
    response = mock.Mock(status_code=200)
    response.json.return_value = {"status": "ok", "name": None}
    monkeypatch.setattr(identity.requests, "post", mock.Mock(return_value=response))
    assert "don't confidently recognize" in identity.face_response_for_query("Do you recognize me?")
    response.json.return_value = {"status": "ok", "name": "Morgan", "reference_photo_saved": False}
    assert "couldn't complete" in identity.face_response_for_query(
        "Take a picture of me and remember me. My name is Morgan."
    )


def test_face_api_is_local_opt_in_and_validates_body(monkeypatch):
    handler = mc.ControllerAPIHandler.__new__(mc.ControllerAPIHandler)
    handler.path = "/api/faces/enroll"
    handler.client_address = ("192.0.2.12", 100)
    handler._send_json = mock.Mock()
    handler.controller = mock.Mock(is_muted=False)
    handler.do_POST()
    assert handler._send_json.call_args.args[0] == 403
    handler.client_address = ("127.0.0.1", 100)
    monkeypatch.setattr(mc, "USE_LAPTOP_FACE_RECOGNITION", False)
    handler.do_POST()
    assert handler._send_json.call_args.args[0] == 503
    monkeypatch.setattr(mc, "USE_LAPTOP_FACE_RECOGNITION", True)
    handler.rfile = io.BytesIO(b'{"name": "../outside"}')
    handler.headers = {"Content-Length": len(handler.rfile.getvalue())}
    handler.do_POST()
    assert handler._send_json.call_args.args[0] == 400
    handler.controller.enroll_reference_photo.assert_not_called()


@pytest.mark.parametrize("error, status", [
    (frs.ProfileExistsError("duplicate"), 409),
    (frs.MultipleFacesDetected("two faces"), 422),
    (frs.FaceModelUnavailable("missing"), 503),
])
def test_face_api_does_not_acknowledge_failed_enrollment(monkeypatch, error, status):
    monkeypatch.setattr(mc, "USE_LAPTOP_FACE_RECOGNITION", True)
    handler = mc.ControllerAPIHandler.__new__(mc.ControllerAPIHandler)
    handler.path = "/api/faces/enroll"
    handler.client_address = ("127.0.0.1", 100)
    handler._send_json = mock.Mock()
    handler.controller = mock.Mock(is_muted=False)
    handler.controller.enroll_reference_photo.side_effect = error
    handler.rfile = io.BytesIO(b'{"name": "Morgan"}')
    handler.headers = {"Content-Length": len(handler.rfile.getvalue())}
    handler.do_POST()
    assert handler._send_json.call_args.args[0] == status


@pytest.mark.parametrize("speaker", [None, "Morgan"])
def test_shared_robot_backstory_does_not_require_speaker_recognition(tmp_path, monkeypatch, speaker):
    monkeypatch.setenv("PERSONAL_MEMORY_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(svc, "retrieve_local_context", lambda _: [])
    messages, _ = svc._prepare_chat_messages(
        svc.ConversationState(), "Do you know where you live?", speaker,
        svc.RESPONSE_MODE_CONFIG["short"],
    )
    prompt = messages[0]["content"]
    assert "You live on a farm" in prompt
    assert "two dogs" in prompt and "hackathons" in prompt
    if speaker is None:
        assert "current speaker has no confirmed identity" in prompt
        assert "You are currently talking to" not in prompt


def test_personal_memories_require_current_identity_and_do_not_cross_speakers(tmp_path, monkeypatch):
    path = tmp_path / "memories.json"
    path.write_text(json.dumps({"Morgan": ["Morgan's dog is Pip."]}), encoding="utf-8")
    monkeypatch.setenv("PERSONAL_MEMORY_PATH", str(path))
    monkeypatch.setattr(svc, "retrieve_local_context", lambda _: [])
    state = svc.ConversationState(speaker_name="Morgan")
    messages, _ = svc._prepare_chat_messages(state, "Tell me about my dog", "Morgan", svc.RESPONSE_MODE_CONFIG["short"])
    assert "Morgan's dog is Pip" in messages[0]["content"]
    state.history = [{"role": "assistant", "content": "Your dog is Pip."}]
    state.summary = "Morgan's dog is Pip."
    svc._bind_conversation_speaker(state, None)
    messages, _ = svc._prepare_chat_messages(state, "Tell me about my dog", None, svc.RESPONSE_MODE_CONFIG["short"])
    assert "Pip" not in str(messages)
    assert not state.history and not state.summary
    assert svc._personal_context_for_speaker("Casey") == []


def test_voice_photo_response_bypasses_chat_model_and_updates_history(monkeypatch):
    monkeypatch.setattr(svc, "face_response_for_query", lambda _: "Saved a local reference photo.")
    post = mock.Mock(side_effect=AssertionError("Chat model must not handle enrollment"))
    monkeypatch.setattr(svc.requests, "post", post)
    state = svc._conversation_state("test-photo-direct")
    state.history.clear()
    result = svc.language_model_inference("photo request", time.time(), conversation_id="test-photo-direct")
    assert result["status"] == "ok" and result["responseSource"] == "face"
    assert state.history[-1]["content"] == "Saved a local reference photo."
    post.assert_not_called()


def test_visual_reply_names_only_current_validated_matches(monkeypatch):
    monkeypatch.setenv("USE_OBJECT_DETECTION", "true")
    response = mock.Mock()
    response.json.return_value = {
        "status": "ok", "objects": [{"label": "person", "count": 1, "confidence": 0.9}],
        "recognized_people": ["Morgan"],
    }
    monkeypatch.setattr(vision.requests, "post", mock.Mock(return_value=response))
    assert "I recognize Morgan." in vision.visual_response_for_query("What do you see?")
    response.json.return_value["recognized_people"] = []
    assert "Morgan" not in vision.visual_response_for_query("What do you see?")

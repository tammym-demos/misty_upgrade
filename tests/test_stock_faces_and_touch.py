"""Stock face selection and safe, companion-side chin-touch reactions."""

import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "windows-orchestration"))

import config_defaults
import misty_controller as mc
from face_animator import AnimationSpec, FaceAnimator


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setattr(mc, "USE_FACE_TRACKING", False)
    monkeypatch.setattr(mc, "USE_FACE_ANIMATION", False)
    monkeypatch.setattr(mc, "USE_CUSTOM_FACES", False)
    monkeypatch.setattr(mc, "USE_CHIN_TOUCH_REACTION", True)
    monkeypatch.setattr(mc, "CHIN_TOUCH_REACTION_S", 0)
    with patch("face_animator.requests.post") as post:
        post.return_value = MagicMock(status_code=200)
        ctrl = mc.MistyController()
        ctrl.state = mc.State.IDLE
        ctrl.misty_post = MagicMock(return_value={"status": "Success"})
        ctrl._wake_word_listener = MagicMock()
        yield ctrl
        ctrl._chin_reaction_stop.set()
        if ctrl._chin_reaction_thread:
            ctrl._chin_reaction_thread.join(timeout=2)
        ctrl._face_animator.stop()


def test_stock_faces_are_application_default():
    assert config_defaults.USE_CUSTOM_FACES is False
    assert config_defaults.USE_CHIN_TOUCH_REACTION is True


@pytest.mark.parametrize("enabled", [False, True])
def test_stock_mode_resolves_state_emotion_and_transient_faces(enabled):
    with patch("face_animator.requests.post") as post:
        post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://misty", enabled=enabled, use_custom_faces=False)
        animator.set_custom_faces_available(True)
        assert animator.resolve_asset("face_idle.gif") == "e_DefaultContent.jpg"
        assert animator.resolve_asset("face_listening.png") == "e_Surprise.jpg"
        assert animator.resolve_asset("e_Love.jpg") == "e_Love.jpg"
        assert animator._resolve_single_frame("IDLE") == "e_DefaultContent.jpg"
        animator.set_emotion("happy")
        assert animator._resolve_single_frame("PLAYING") == "e_Joy.jpg"
        assert animator._resolve_single_frame("REACTING") == "e_Love.jpg"


def test_stock_mode_never_uploads_custom_assets(controller):
    with patch.object(controller, "_get_misty_image_names") as inventory, \
         patch.object(controller, "_upload_face_image") as upload:
        assert controller.ensure_face_assets(overwrite=True) is True
        inventory.assert_not_called()
        upload.assert_not_called()


def test_raw_display_also_respects_stock_mode(controller):
    controller.display_image("face_talking_happy.gif")
    controller.misty_post.assert_called_once_with(
        "/api/images/display", {"FileName": "e_Joy.jpg", "Alpha": 1}
    )


def test_chin_reaction_plays_love_then_restores_idle(controller):
    with patch.object(controller._face_animator, "_push_frame", return_value=True) as push:
        controller._run_chin_reaction()
    assert [call.args[0] for call in push.call_args_list] == [
        "e_Love.jpg", "e_DefaultContent.jpg",
    ]
    controller.misty_post.assert_any_call(
        "/api/audio/play", {"FileName": "s_Love.wav", "Volume": 60}, timeout=2.0,
    )
    assert controller.get_state() == mc.State.IDLE
    controller._wake_word_listener.pause.assert_called_once()
    controller._wake_word_listener.resume.assert_called_once()
    assert not any(
        call.args[0] in ("/api/head", "/api/arms", "/api/drive")
        for call in controller.misty_post.call_args_list
    )


@pytest.mark.parametrize("state", [s for s in mc.State if s != mc.State.IDLE])
def test_chin_reaction_does_not_interrupt_busy_states(controller, state):
    controller.state = state
    controller._run_chin_reaction()
    assert controller.get_state() == state
    controller.misty_post.assert_not_called()
    controller._wake_word_listener.pause.assert_not_called()


@pytest.mark.parametrize("field", ["_muted", "running"])
def test_chin_reaction_respects_mute_and_shutdown(controller, field):
    setattr(controller, field, field == "_muted")
    controller._run_chin_reaction()
    controller.misty_post.assert_not_called()


def test_touch_only_triggers_on_chin_contact_edge(controller):
    with patch("misty_controller.threading.Thread") as thread:
        worker = thread.return_value
        worker.is_alive.return_value = False
        controller._handle_touch_event({"sensorPosition": "HeadLeft", "isContacted": True})
        controller._handle_touch_event({"sensorPosition": "Chin", "isContacted": "true"})
        thread.assert_not_called()
        contact = {"sensorPosition": "Chin", "isContacted": True}
        controller._handle_touch_event(contact)
        controller._handle_touch_event(contact)
        assert thread.call_count == 1
        controller._handle_touch_event({"sensorPosition": "Chin", "isContacted": False})
        controller._handle_touch_event({"SensorName": "CapTouch_Chin", "IsContacted": True})
        assert thread.call_count == 2
    controller._chin_reaction_thread = None


def test_active_reaction_does_not_spawn_another_worker(controller):
    controller._chin_reaction_thread = MagicMock()
    controller._chin_reaction_thread.is_alive.return_value = True
    with patch("misty_controller.threading.Thread") as thread:
        controller._handle_touch_event({"sensorPosition": "Chin", "isContacted": True})
        thread.assert_not_called()
    controller._chin_reaction_thread = None


def test_touch_can_be_disabled(controller, monkeypatch):
    monkeypatch.setattr(mc, "USE_CHIN_TOUCH_REACTION", False)
    controller.ws = MagicMock()
    with patch("misty_controller.threading.Thread") as thread:
        controller._handle_touch_event({"sensorPosition": "Chin", "isContacted": True})
        controller._ws_subscribe_touch()
        thread.assert_not_called()
        controller.ws.send.assert_not_called()


def test_subscription_receives_both_contact_and_release_and_reconnects(controller):
    controller.ws = MagicMock()
    controller._touch_event_name = "previous"
    controller._chin_contacted = True
    controller._ws_subscribe_touch()
    messages = [json.loads(call.args[0]) for call in controller.ws.send.call_args_list]
    assert messages[0] == {"Operation": "unsubscribe", "EventName": "previous"}
    assert messages[1]["Type"] == "TouchSensor"
    assert messages[1]["EventConditions"] == []
    assert messages[1]["ReturnProperty"] is None
    assert messages[1]["DebounceMs"] == 0
    assert not controller._chin_contacted


def test_websocket_routes_touch_without_blocking_on_reaction(controller):
    controller._touch_event_name = "chin-test"
    message = {"sensorPosition": "Chin", "isContacted": True}
    with patch.object(controller, "_handle_touch_event") as handle:
        controller._on_ws_message(None, json.dumps({
            "eventName": "chin-test", "message": message,
        }))
        handle.assert_called_once_with(message)


def test_audio_failure_restores_idle_and_reports_error(controller, caplog):
    controller.misty_post.return_value = {"status": "Failed"}
    controller._run_chin_reaction()
    assert controller.get_state() == mc.State.IDLE
    assert "Love sound failed" in caplog.text
    controller._wake_word_listener.resume.assert_called_once()


@pytest.mark.parametrize("interruption", ["charging", "mute", "shutdown"])
def test_reaction_cancels_without_overwriting_safety_state(controller, interruption):
    entered = threading.Event()
    released = threading.Event()

    def wait_for_cancel(timeout):
        entered.set()
        return released.wait(2)

    with patch.object(controller._chin_reaction_stop, "wait", side_effect=wait_for_cancel):
        worker = threading.Thread(target=controller._run_chin_reaction)
        worker.start()
        try:
            assert entered.wait(2)
            if interruption == "charging":
                controller.set_state(mc.State.CHARGING)
            elif interruption == "mute":
                controller.mute()
            else:
                controller.running = False
                controller._cancel_chin_reaction()
            assert controller._chin_reaction_stop.is_set()
        finally:
            released.set()
            worker.join(2)
        assert not worker.is_alive()
    expected = mc.State.CHARGING if interruption == "charging" else (
        mc.State.IDLE if interruption == "mute" else mc.State.REACTING
    )
    assert controller.get_state() == expected
    controller._wake_word_listener.resume.assert_not_called()
    assert controller._chin_audio_playing is False


def test_stock_mode_resolves_custom_multiframe_specs():
    shown = threading.Event()
    custom_map = {
        "IDLE": AnimationSpec(
            frames=("face_idle.gif", "face_processing.gif"), fps=10,
            static_fallback="face_idle.gif",
        ),
    }
    with patch("face_animator.requests.post") as post:
        def respond(*args, **kwargs):
            shown.set()
            return MagicMock(status_code=200)

        post.side_effect = respond
        animator = FaceAnimator(
            "http://misty", enabled=True, use_custom_faces=False,
            animation_map=custom_map,
        )
        animator.start()
        try:
            animator.set_state("IDLE")
            assert shown.wait(2)
        finally:
            animator.stop()
        assert all(
            not call.kwargs["json"]["FileName"].startswith("face_")
            for call in post.call_args_list
        )

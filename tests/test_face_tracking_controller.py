"""Controller integration tests for proactive face greetings."""

import os
import sys
import io
import base64
import json
import threading
import time
from unittest import mock

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration"),
)

import misty_controller as mc
import face_identity_service as identity
import orchestration_service as svc


@pytest.fixture
def ctrl(monkeypatch):
    ctrl = mc.MistyController.__new__(mc.MistyController)
    ctrl.running = True
    ctrl._muted = False
    ctrl.state = mc.State.IDLE
    ctrl.state_lock = threading.Lock()
    ctrl.battery_lock = threading.Lock()
    ctrl.hazard_lock = threading.Lock()
    ctrl.battery = mc.BatteryState(
        charge_percent=0.8, voltage=8, temperature=25, last_updated=time.time(),
    )
    ctrl.hazard = mc.HazardState()
    ctrl._recognized_face = None
    ctrl._greeter_enabled = True
    ctrl._greeter_pending = threading.Event()
    ctrl._greeter_stop = threading.Event()
    ctrl._greeter_playing = False
    ctrl.turn_id = 0
    ctrl._face_tracker = mock.Mock()
    ctrl._face_tracker.status.return_value = {"face_present": True, "error": None}
    ctrl._expression_coordinator = None
    ctrl._talking_head = None
    ctrl._face_animator = None
    ctrl._wake_word_listener = mock.MagicMock()
    ctrl._preloaded_audio_durations = {mc.FACE_GREETING_AUDIO_FILENAME: 0.0}
    ctrl.set_led = mock.Mock()
    ctrl.show_face = mock.Mock()
    ctrl.play_preloaded_audio = mock.Mock(return_value=True)
    ctrl.move_arms = mock.Mock(return_value={"status": "Success"})
    ctrl.misty_post = mock.Mock(return_value={"status": "Success"})
    monkeypatch.setattr(mc, "USE_LAPTOP_FACE_RECOGNITION", False)
    return ctrl


def test_face_tracking_safe_only_when_greeter_running_idle_and_telemetry_safe(ctrl):
    assert ctrl._face_tracking_safe() is True
    ctrl.state = mc.State.PLAYING
    assert ctrl._face_tracking_safe() is False
    assert ctrl._greeter_camera_allowed() is True
    ctrl.state = mc.State.IDLE
    ctrl.battery.last_updated = time.time() - 61
    assert ctrl._face_tracking_safe() is False
    ctrl.battery.last_updated = time.time()
    ctrl.hazard.any_bump_active = True
    assert ctrl._face_tracking_safe() is False
    ctrl.hazard.any_bump_active = False
    ctrl._greeter_enabled = False
    assert ctrl._greeter_camera_allowed() is False
    ctrl._greeter_enabled = True
    ctrl.running = False
    assert ctrl._face_tracking_safe() is False


@pytest.mark.parametrize("state", [
    mc.State.RECORDING, mc.State.PROCESSING, mc.State.PLAYING, mc.State.LISTENING,
])
def test_camera_can_detect_while_busy_but_head_must_not_move(ctrl, state):
    ctrl.state = state
    assert ctrl._greeter_camera_allowed() is True
    assert ctrl._face_tracking_safe() is False
    ctrl._muted = True
    assert ctrl._greeter_camera_allowed() is False


def test_face_greeting_pauses_wake_word_waves_and_restores_idle(ctrl):
    ctrl._greeter_stop.set()
    ctrl._greet_detected_face()
    ctrl._wake_word_listener.pause.assert_called_once()
    ctrl.play_preloaded_audio.assert_called_once_with(mc.FACE_GREETING_AUDIO_FILENAME)
    assert ctrl.move_arms.call_args_list == [
        mock.call(left=-25, right=-25, velocity=45),
        mock.call(left=70, right=70, velocity=45),
    ]
    ctrl._wake_word_listener.resume.assert_called_once()
    assert ctrl.state == mc.State.IDLE
    assert not ctrl._greeter_playing


def test_face_greeting_hands_reply_to_conversation_without_idle_gap(ctrl):
    def listen(*, listen_after_greeting):
        assert listen_after_greeting is True
        assert ctrl.state == mc.State.PLAYING
        assert ctrl.turn_id == 1
        assert not ctrl._greeter_playing
        ctrl._wake_word_listener.resume.assert_not_called()
        assert ctrl.move_arms.call_args == mock.call(left=70, right=70, velocity=45)

    ctrl._handle_conversation_turn = mock.Mock(side_effect=listen)
    ctrl._greet_detected_face()
    ctrl._handle_conversation_turn.assert_called_once_with(listen_after_greeting=True)


@pytest.mark.parametrize("cancel", ["muted", "disabled", "stopped", "playback_failed"])
def test_face_greeting_does_not_listen_after_cancel_or_failed_playback(ctrl, cancel):
    def play(*args):
        if cancel == "muted":
            ctrl._muted = True
        elif cancel == "disabled":
            ctrl._greeter_enabled = False
        elif cancel == "stopped":
            ctrl._greeter_stop.set()
        return cancel != "playback_failed"

    ctrl.play_preloaded_audio.side_effect = play
    ctrl._handle_conversation_turn = mock.Mock()
    ctrl._greet_detected_face()
    ctrl._handle_conversation_turn.assert_not_called()


@pytest.mark.parametrize("reply", [False, True, {"had_speech": True, "movement": {"command": "stop"}}])
def test_greeter_conversation_listens_without_wake_greeting_and_rearms(ctrl, monkeypatch, reply):
    monkeypatch.setattr(mc, "FOLLOWUP_ENABLED", False)
    ctrl.get_battery_snapshot = mock.Mock(return_value=ctrl.battery)
    ctrl._conversation_cycles = 0
    ctrl._do_conversation_exchange = mock.Mock()
    ctrl._listen_for_followup = mock.Mock(return_value=reply)
    ctrl._execute_voice_movement = mock.Mock()
    ctrl._rearm = mock.Mock()
    ctrl._handle_conversation_turn(listen_after_greeting=True)
    ctrl._listen_for_followup.assert_called_once_with(0)
    ctrl._do_conversation_exchange.assert_not_called()
    ctrl._rearm.assert_called_once()
    if isinstance(reply, dict):
        ctrl._execute_voice_movement.assert_called_once_with(0, reply["movement"])


def test_active_expression_defers_greeting(ctrl):
    ctrl._expression_coordinator = mock.Mock(is_active=True)
    ctrl._greet_detected_face()
    ctrl.play_preloaded_audio.assert_not_called()


@pytest.mark.parametrize("muted", [False, True])
def test_rest_recovery_restores_idle_and_respects_operator_mute(ctrl, muted):
    ctrl.state = mc.State.DISCONNECTED
    ctrl._muted = muted
    ctrl._is_dimmed = True
    ctrl.check_misty_health = mock.Mock(return_value=True)
    ctrl._ws_is_connected = mock.Mock(return_value=True)
    ctrl._check_connection_health()
    assert ctrl.state == mc.State.IDLE
    assert ctrl.ready_time > time.time()
    assert not ctrl._is_dimmed
    ctrl.set_led.assert_called_once_with(0, 0 if muted else 255, 0)
    if muted:
        ctrl._wake_word_listener.pause.assert_called_once()
        ctrl._wake_word_listener.resume.assert_not_called()
    else:
        ctrl._wake_word_listener.resume.assert_called_once()


@pytest.mark.parametrize("rest_ok, ws_ok", [(False, True), (True, False), (False, False)])
def test_connection_recovery_requires_both_rest_and_websocket(ctrl, rest_ok, ws_ok):
    ctrl.state = mc.State.DISCONNECTED
    ctrl.check_misty_health = mock.Mock(return_value=rest_ok)
    ctrl._ws_is_connected = mock.Mock(return_value=ws_ok)
    ctrl._check_connection_health()
    assert ctrl.state == mc.State.DISCONNECTED
    ctrl._wake_word_listener.resume.assert_not_called()
    ctrl.set_led.assert_not_called()


def test_failed_idle_health_check_pauses_wake_until_recovery(ctrl):
    ctrl.check_misty_health = mock.Mock(side_effect=[False, True])
    ctrl._ws_is_connected = mock.Mock(return_value=True)
    ctrl._check_connection_health()
    assert ctrl.state == mc.State.DISCONNECTED
    ctrl._wake_word_listener.pause.assert_called_once()
    assert not ctrl._greeter_camera_allowed()
    ctrl._check_connection_health()
    assert ctrl.state == mc.State.IDLE
    assert ctrl._greeter_camera_allowed()
    ctrl._wake_word_listener.resume.assert_called_once()


@pytest.mark.parametrize("state", [
    mc.State.PLAYING, mc.State.PROCESSING, mc.State.LISTENING,
    mc.State.RECORDING, mc.State.REACTING, mc.State.CHARGING,
    mc.State.MOVING, mc.State.REBOOTING, mc.State.REARMING,
])
def test_health_recovery_never_interrupts_owned_or_safety_states(ctrl, state):
    ctrl.state = state
    ctrl.check_misty_health = mock.Mock()
    ctrl._check_connection_health()
    assert ctrl.state == state
    ctrl.check_misty_health.assert_not_called()


def test_health_check_does_not_overwrite_concurrent_conversation(ctrl):
    def busy():
        ctrl.state = mc.State.PLAYING
        return False

    ctrl.check_misty_health = mock.Mock(side_effect=busy)
    ctrl._check_connection_health()
    assert ctrl.state == mc.State.PLAYING
    ctrl._wake_word_listener.pause.assert_not_called()


@pytest.mark.parametrize("blocked", ["busy", "reacting", "muted", "disabled", "hazard", "low_battery"])
def test_face_greeting_suppresses_audio_and_wave_when_unsafe(ctrl, blocked):
    if blocked == "busy":
        ctrl.state = mc.State.PLAYING
    elif blocked == "reacting":
        ctrl.state = mc.State.REACTING
    elif blocked == "muted":
        ctrl._muted = True
    elif blocked == "disabled":
        ctrl._greeter_enabled = False
    elif blocked == "hazard":
        ctrl.hazard.any_bump_active = True
    else:
        ctrl.battery.charge_percent = 0.1
    ctrl._greet_detected_face()
    ctrl._wake_word_listener.pause.assert_not_called()
    ctrl.play_preloaded_audio.assert_not_called()
    ctrl.move_arms.assert_not_called()


def test_detection_queues_during_playback_and_greets_afterward(ctrl):
    ctrl.state = mc.State.PLAYING
    ctrl._queue_face_greeting()
    assert ctrl._greeter_pending.is_set()
    greeted = threading.Event()
    ctrl._greet_detected_face = mock.Mock(side_effect=greeted.set)
    worker = threading.Thread(target=ctrl._run_greeter)
    worker.start()
    try:
        assert not greeted.wait(0.15)
        ctrl.state = mc.State.IDLE
        assert greeted.wait(1)
        ctrl._greet_detected_face.assert_called_once()
    finally:
        ctrl._greeter_stop.set()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_disabling_mode_cancels_pending_and_resets_presence(ctrl):
    ctrl._greeter_pending.set()
    ctrl._recognized_face = "Morgan"
    assert ctrl.set_greeter_mode(False) == {"status": "ok", "greeter_mode": False}
    assert not ctrl._greeter_pending.is_set()
    assert ctrl._recognized_face is None
    assert not ctrl._greeter_camera_allowed()
    ctrl._face_tracker.reset_presence.assert_called_once()
    assert ctrl.set_greeter_mode(True)["greeter_mode"] is True


def test_mode_requires_detector_capability_and_strict_boolean(ctrl):
    ctrl._face_tracker = None
    with pytest.raises(ValueError, match="USE_FACE_TRACKING"):
        ctrl.set_greeter_mode(True)
    with pytest.raises(ValueError, match="boolean"):
        ctrl.set_greeter_mode("true")


@pytest.mark.parametrize("known", [None, "Morgan"])
def test_greeter_uses_fresh_name_or_anonymous_fallback(ctrl, monkeypatch, known):
    monkeypatch.setattr(mc, "USE_LAPTOP_FACE_RECOGNITION", True)
    import face_recognition_service as frs
    monkeypatch.setattr(frs, "MistyCameraFrameSource", mock.MagicMock())
    ctrl._recognize_camera_person = mock.Mock(return_value=known)
    response = mock.Mock(content=b"x" * 200)
    post = mock.Mock(return_value=response)
    monkeypatch.setattr(mc.requests, "post", post)
    ctrl.upload_and_play_audio = mock.Mock(return_value=0)
    ctrl._greeter_stop.set()
    ctrl._greet_detected_face()
    assert ctrl._recognized_face is None
    if known:
        assert post.call_args.kwargs["json"] == {
            "text": mc.FACE_NAMED_GREETING_TEXT.format(name="Morgan")
        }
        ctrl.play_preloaded_audio.assert_not_called()
        ctrl.upload_and_play_audio.assert_called_once()
    else:
        post.assert_not_called()
        ctrl.play_preloaded_audio.assert_called_once()


@pytest.mark.parametrize("reply", [
    "Yes.", "Yes, please!", "Yeah", "Sure", "Absolutely", "Go ahead",
    "I'd love to.", "Let's hear it.", "Tell me more.",
])
def test_greeter_affirmative_selects_fixed_story_only_with_invitation(reply):
    text, source = svc._direct_response_for_query(reply, greeter_reply=True)
    assert source == "greeter_story"
    assert text == svc.GREETER_STORY_TEXT
    assert text.endswith("I was given a new brain.")
    assert svc._direct_response_for_query(reply)[0] is None


@pytest.mark.parametrize("reply", [
    "No", "No thanks.", "No, thank you.", "Not now.", "Maybe later.",
    "Yes, but not now.", "I don't want to.",
])
def test_greeter_decline_never_starts_story(reply):
    assert svc._direct_response_for_query(reply, greeter_reply=True) == (
        svc.GREETER_DECLINE_TEXT, "greeter_decline"
    )


@pytest.mark.parametrize("reply", [
    "Yesterday was sunny.", "The television said yes.", "What is your name?", "Maybe",
])
def test_unrelated_or_ambiguous_speech_does_not_grant_consent(reply):
    assert svc._direct_response_for_query(reply, greeter_reply=True)[0] is None


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("reply, source", [
    ("Yes please.", "greeter_story"), ("No thanks.", "greeter_decline"),
])
def test_greeter_reply_endpoints_bypass_llm_and_end_at_fixed_text(
    monkeypatch, streaming, reply, source
):
    monkeypatch.setattr(svc, "speech_to_text", lambda *_: {"status": "ok", "text": reply})
    tts = mock.Mock(return_value={
        "status": "ok", "audio_file": "mock.wav", "audio_uri": "/api/audio/mock.wav",
        "tts_cached": True,
    })
    monkeypatch.setattr(svc, "text_to_speech", tts)
    monkeypatch.setattr(svc, "_read_audio_bytes_b64", lambda _: "d2F2")
    post = mock.Mock(side_effect=AssertionError("Greeter consent must bypass the LLM"))
    monkeypatch.setattr(svc.requests, "post", post)
    conversation_id = f"greeter-consent-{streaming}-{source}"
    svc.reset_conversation(conversation_id)
    endpoint = "/api/orchestrate/stream" if streaming else "/api/orchestrate"
    response = svc.app.test_client().post(endpoint, data={
        "file": (io.BytesIO(b"wav"), "input.wav"),
        "conversation_id": conversation_id,
        "greeter_reply": "true",
    })
    assert response.status_code == 200
    expected = svc.GREETER_STORY_TEXT if source == "greeter_story" else svc.GREETER_DECLINE_TEXT
    if streaming:
        events = [
            json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
            if line.startswith("data: ")
        ]
        result = events[-1]
        assert result["type"] == "complete"
        assert result["firstTokenMs"] is None
        assert " ".join(e["text"] for e in events if e["type"] == "audio") == expected
    else:
        result = response.get_json()
        assert result["ttsCached"] is True
    assert result["inferenceResponse"] == expected
    assert result["responseSource"] == source
    assert result["endConversation"] is True
    post.assert_not_called()
    assert " ".join(call.args[0] for call in tts.call_args_list) == expected
    svc.reset_conversation(conversation_id)


@pytest.mark.parametrize("streaming", [False, True])
def test_silent_greeter_reply_does_not_speak_or_call_llm(monkeypatch, streaming):
    monkeypatch.setattr(svc, "speech_to_text", lambda *_: {"status": "ok", "text": ""})
    tts = mock.Mock()
    post = mock.Mock()
    monkeypatch.setattr(svc, "text_to_speech", tts)
    monkeypatch.setattr(svc.requests, "post", post)
    endpoint = "/api/orchestrate/stream" if streaming else "/api/orchestrate"
    response = svc.app.test_client().post(endpoint, data={
        "file": (io.BytesIO(b"wav"), "input.wav"), "greeter_reply": "true",
    })
    assert response.status_code == 400
    assert response.get_json()["error"] == "empty_stt"
    tts.assert_not_called()
    post.assert_not_called()


def test_greeter_consent_flag_is_scoped_to_initial_reply_and_fixed_story_ends(ctrl, monkeypatch):
    monkeypatch.setattr(mc, "FOLLOWUP_ENABLED", True)
    ctrl.get_battery_snapshot = mock.Mock(return_value=ctrl.battery)
    ctrl._conversation_cycles = 0
    ctrl._rearm = mock.Mock()

    def reply(turn):
        assert ctrl._greeter_reply_pending is True
        return {"had_speech": True, "end_conversation": True}

    ctrl._listen_for_followup = mock.Mock(side_effect=reply)
    ctrl._handle_conversation_turn(listen_after_greeting=True)
    ctrl._listen_for_followup.assert_called_once_with(0)
    assert ctrl._greeter_reply_pending is False
    ctrl._rearm.assert_called_once()


@pytest.mark.parametrize("streaming", [False, True])
def test_controller_forwards_consent_and_honors_end_of_story(ctrl, monkeypatch, streaming):
    monkeypatch.setattr(mc, "STREAMING_ENABLED", streaming)
    monkeypatch.setattr(mc.time, "sleep", lambda _: None)
    ctrl._greeter_reply_pending = True
    ctrl._conversation_id = "controller-consent"
    ctrl.state = mc.State.PROCESSING
    ctrl._talking_head = mock.Mock()
    ctrl._face_animator = mock.Mock()
    ctrl.move_head = mock.Mock()
    ctrl._express_for_response_emotion = mock.Mock()
    ctrl.upload_and_play_audio = mock.Mock(return_value=0)
    encoded = base64.b64encode(b"wav").decode("ascii")
    response = mock.Mock(status_code=200)
    response.json.return_value = {
        "status": "ok", "audioBytes": encoded, "endConversation": True,
    }
    response.iter_lines.return_value = [
        "data: " + json.dumps({"type": "audio", "sequence": 0, "audioBytes": encoded}),
        "data: " + json.dumps({"type": "complete", "endConversation": True}),
    ]
    post = mock.Mock(return_value=response)
    monkeypatch.setattr(mc.requests, "post", post)
    result = ctrl._do_orchestrate_and_respond(1, b"wav", time.time())
    assert result == {"had_speech": True, "end_conversation": True}
    assert post.call_args.kwargs["data"]["greeter_reply"] == "true"
    ctrl.upload_and_play_audio.assert_called_once()


def test_disabling_greeter_cancels_remaining_story_chunks(ctrl, monkeypatch):
    monkeypatch.setattr(mc.time, "sleep", lambda _: None)
    ctrl._greeter_reply_pending = True
    ctrl.state = mc.State.PROCESSING
    ctrl._talking_head = mock.Mock()
    ctrl._rollback_orchestration_turn = mock.Mock()
    encoded = base64.b64encode(b"wav").decode("ascii")
    response = mock.Mock(status_code=200)
    response.iter_lines.return_value = [
        "data: " + json.dumps({"type": "audio", "sequence": i, "audioBytes": encoded})
        for i in range(2)
    ]
    monkeypatch.setattr(mc.requests, "post", mock.Mock(return_value=response))

    def disable(*_):
        ctrl.set_greeter_mode(False)
        return 0

    ctrl.upload_and_play_audio = mock.Mock(side_effect=disable)
    result = ctrl._do_streaming_orchestrate_and_respond(1, b"wav", time.time(), None)
    assert result == {"had_speech": True, "end_conversation": True}
    ctrl.upload_and_play_audio.assert_called_once()
    ctrl.misty_post.assert_called_with("/api/audio/stop")
    ctrl._rollback_orchestration_turn.assert_called_once()


@pytest.mark.parametrize("utterance, enabled", [
    ("Turn greeter mode on.", True),
    ("Please turn greeter mode off!", False),
    ("Start the greeter mode", True),
    ("Disable greeter mode", False),
])
def test_explicit_voice_control_calls_local_api(monkeypatch, utterance, enabled):
    response = mock.Mock()
    response.json.return_value = {"status": "ok", "greeter_mode": enabled}
    post = mock.Mock(return_value=response)
    monkeypatch.setattr(identity.requests, "post", post)
    answer = identity.greeter_response_for_query(utterance)
    assert ("on." if enabled else "off.") in answer
    assert post.call_args.args[0].startswith("http://127.0.0.1:")
    assert post.call_args.args[0].endswith("/api/greeter")
    assert post.call_args.kwargs["json"] == {"enabled": enabled}


def test_conference_commands_and_ordinary_chat_cannot_change_greeter(monkeypatch):
    post = mock.Mock()
    monkeypatch.setattr(identity.requests, "post", post)
    for query in ("Turn conference mode on", "Tell me about greeter mode", "Hello"):
        assert identity.greeter_response_for_query(query) is None
    post.assert_not_called()


def test_voice_control_failure_is_not_acknowledged(monkeypatch):
    post = mock.Mock(side_effect=identity.requests.Timeout("unreachable"))
    monkeypatch.setattr(identity.requests, "post", post)
    assert "couldn't change" in identity.greeter_response_for_query("Turn greeter mode on")


def test_greeter_voice_response_bypasses_chat_model(monkeypatch):
    response = mock.Mock()
    response.json.return_value = {"status": "ok", "greeter_mode": True}
    post = mock.Mock(return_value=response)
    monkeypatch.setattr(identity.requests, "post", post)
    result = svc.language_model_inference(
        "Turn greeter mode on", time.time(), conversation_id="test-greeter-direct",
    )
    assert result["status"] == "ok"
    assert result["responseSource"] == "greeter"
    assert post.call_count == 1
    assert post.call_args.args[0].endswith("/api/greeter")


@pytest.mark.parametrize("body, address, status", [
    (b'{"enabled": true}', "127.0.0.1", 200),
    (b'{"enabled": false}', "::1", 200),
    (b'{"enabled": true}', "192.0.2.12", 403),
    (b'{"enabled": "false"}', "127.0.0.1", 400),
    (b'{"enabled": 1}', "127.0.0.1", 400),
    (b'{"enabled": true, "other": 1}', "127.0.0.1", 400),
    (b'[]', "127.0.0.1", 400),
    (b"x" * 1025, "127.0.0.1", 400),
])
def test_greeter_api_is_local_and_requires_boolean(ctrl, body, address, status):
    handler = mc.ControllerAPIHandler.__new__(mc.ControllerAPIHandler)
    handler.path = "/api/greeter"
    handler.client_address = (address, 12345)
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.controller = ctrl
    handler._send_json = mock.Mock()
    handler.do_POST()
    assert handler._send_json.call_args.args[0] == status

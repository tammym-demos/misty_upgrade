import io
import os
import sys
import threading
import time
import types
import wave
from unittest import mock

import pytest


ROOT = os.path.dirname(os.path.dirname(__file__))
ORCHESTRATION = os.path.join(ROOT, "src", "windows-orchestration")
if ORCHESTRATION not in sys.path:
    sys.path.insert(0, ORCHESTRATION)

import conference_mode as cm
import misty_controller as mc
import orchestration_service as svc


def wav_bytes(seconds=0.01):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * max(1, int(16000 * seconds)))
    return output.getvalue()


def manifest_with_cue(tmp_path, *, presenter=""):
    path = tmp_path / "cue.wav"
    path.write_bytes(wav_bytes())
    return cm.ConferenceManifest(
        script_path="talk.md",
        cues=[
            cm.CueAsset(
                cue_id="cue-1",
                text="Hello",
                asset_source="generated",
                wav_path=str(path),
                duration_s=0.01,
                text_hash="hash",
                misty_filename="cue-1.wav",
                preceding_presenter=presenter,
            )
        ],
    )


def test_failed_audio_upload_is_retryable():
    controller = mc.MistyController.__new__(mc.MistyController)
    controller.misty_post = mock.Mock(return_value=None)
    with pytest.raises(RuntimeError, match="retried"):
        controller.upload_and_play_audio(wav_bytes(), "response.wav")


def test_conversation_state_is_isolated_and_rollback_endpoint_removes_turn():
    first = svc._conversation_state("first")
    second = svc._conversation_state("second")
    first.history[:] = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    second.history[:] = [{"role": "user", "content": "unrelated"}]

    response = svc.app.test_client().delete("/api/conversations/first/last")

    assert response.status_code == 200
    assert first.history == []
    assert second.history == [{"role": "user", "content": "unrelated"}]


def test_followup_empty_laptop_capture_uses_available_misty_fallback(monkeypatch):
    controller = mc.MistyController.__new__(mc.MistyController)
    controller.state = mc.State.IDLE
    controller.state_lock = threading.Lock()
    controller._wake_word_listener = types.SimpleNamespace(
        is_running=True,
        start_recording=lambda: None,
        start_speech_monitor=lambda **kwargs: kwargs["on_speech_end"](),
        stop_speech_monitor=lambda: None,
        stop_recording=lambda: b"",
    )
    controller.set_state = mock.Mock()
    controller.set_led = mock.Mock()
    controller.show_face = mock.Mock()
    controller.move_head = mock.Mock()
    controller._start_configured_laptop_misty_recording = mock.Mock(
        return_value=lambda: True
    )
    controller.get_audio_base64 = mock.Mock(return_value=None)
    controller._recording_cycles = 0
    monkeypatch.setattr(mc, "LAPTOP_MISTY_RECORDING_MODE", "fallback")
    monkeypatch.setattr(mc.time, "sleep", lambda _: None)

    assert controller._listen_for_followup(1) is False
    controller.get_audio_base64.assert_called_once()


def test_conference_failed_play_does_not_advance_cursor(tmp_path):
    controller = cm.ConferenceController(
        manifest_with_cue(tmp_path),
        lambda _: (_ for _ in ()).throw(RuntimeError("upload failed")),
        enabled=True,
    )
    controller.start()
    with pytest.raises(RuntimeError, match="upload failed"):
        controller.play_next()
    assert controller.remaining() == 1
    assert controller.play_count == 0


def test_conference_pause_interrupts_playback_without_advancing(tmp_path):
    started = threading.Event()
    controller = cm.ConferenceController(
        manifest_with_cue(tmp_path),
        lambda _: (started.set() or 5.0),
        enabled=True,
    )
    controller.start()
    worker = threading.Thread(target=controller.play_next)
    worker.start()
    assert started.wait(1)
    controller.pause()
    worker.join(1)
    assert not worker.is_alive()
    assert controller.remaining() == 1


def test_uncertain_presenter_match_requires_manual_advance(tmp_path):
    controller = cm.ConferenceController(
        manifest_with_cue(tmp_path, presenter="Expected presenter sentence"),
        lambda _: 0,
        wait_for_presenter_fn=lambda: True,
        presenter_match_fn=lambda _: False,
        enabled=True,
    )
    controller.start()
    assert controller.auto_advance_once() is None
    assert controller.remaining() == 1


def test_preload_uploads_without_immediate_playback(tmp_path):
    robot = types.SimpleNamespace(
        misty_post=mock.Mock(return_value={"status": "Success"})
    )
    names = cm.preload_conference_assets(robot, manifest_with_cue(tmp_path))
    assert names == ["cue-1.wav"]
    payload = robot.misty_post.call_args.args[1]
    assert payload["ImmediatelyApply"] is False

"""Controller integration tests for proactive face greetings."""

import os
import sys
from unittest import mock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration"),
)

import misty_controller as mc


def test_face_tracking_safe_only_when_running_and_idle():
    ctrl = mc.MistyController.__new__(mc.MistyController)
    ctrl.running = True
    ctrl.state = mc.State.IDLE
    ctrl.state_lock = __import__("threading").Lock()
    assert ctrl._face_tracking_safe() is True
    ctrl.state = mc.State.PLAYING
    assert ctrl._face_tracking_safe() is False
    ctrl.running = False
    ctrl.state = mc.State.IDLE
    assert ctrl._face_tracking_safe() is False


def test_face_greeting_pauses_wake_word_and_restores_idle():
    ctrl = mc.MistyController.__new__(mc.MistyController)
    ctrl.running = True
    ctrl.state = mc.State.IDLE
    listener = mock.MagicMock()
    ctrl._wake_word_listener = listener
    ctrl._preloaded_audio_durations = {mc.FACE_GREETING_AUDIO_FILENAME: 0.0}
    ctrl.set_led = mock.MagicMock()
    ctrl.show_face = mock.MagicMock()
    ctrl.play_preloaded_audio = mock.MagicMock(return_value=True)

    def try_set(expected, new):
        assert expected == mc.State.IDLE
        ctrl.state = new
        return True

    ctrl.try_set_state = mock.MagicMock(side_effect=try_set)
    ctrl.get_state = mock.MagicMock(side_effect=lambda: ctrl.state)
    ctrl.set_state = mock.MagicMock(side_effect=lambda state: setattr(ctrl, "state", state))

    with mock.patch.object(mc.time, "sleep"):
        ctrl._greet_detected_face()

    listener.pause.assert_called_once()
    ctrl.play_preloaded_audio.assert_called_once_with(mc.FACE_GREETING_AUDIO_FILENAME)
    ctrl.set_state.assert_called_once_with(mc.State.IDLE)
    listener.resume.assert_called_once()
    assert ctrl.state == mc.State.IDLE


def test_face_greeting_is_suppressed_when_not_idle():
    ctrl = mc.MistyController.__new__(mc.MistyController)
    ctrl.running = True
    ctrl.try_set_state = mock.MagicMock(return_value=False)
    ctrl._wake_word_listener = mock.MagicMock()
    ctrl.play_preloaded_audio = mock.MagicMock()

    ctrl._greet_detected_face()

    ctrl._wake_word_listener.pause.assert_not_called()
    ctrl.play_preloaded_audio.assert_not_called()

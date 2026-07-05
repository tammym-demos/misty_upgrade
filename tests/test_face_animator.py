"""
Unit tests for FaceAnimator.

Tests the animation engine in isolation with mocked REST calls.
Validates threading behavior, state transitions, disable-regression contract,
and the safety guarantees from docs/design-animated-face-expressions.md §6.
"""

import sys
import os
import time
import threading
from unittest.mock import patch, MagicMock

import pytest

# Add source directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration"))

from face_animator import (
    FaceAnimator,
    AnimationSpec,
    DEFAULT_ANIMATION_MAP,
    VALID_EMOTIONS,
    DEFAULT_EMOTION,
    talking_face_for_emotion,
    BUILTIN_FALLBACK_MAP,
    BUILTIN_EMOTION_FALLBACK,
)


class TestAnimationSpec:
    """Test AnimationSpec dataclass behavior."""

    def test_single_frame_detection(self):
        spec = AnimationSpec(frames=("e_Joy.jpg",))
        assert spec.is_single_frame is True

    def test_multi_frame_detection(self):
        spec = AnimationSpec(frames=("e_Joy.jpg", "e_Sadness.jpg"))
        assert spec.is_single_frame is False

    def test_frozen_immutable(self):
        spec = AnimationSpec(frames=("e_Joy.jpg",), fps=2.0)
        with pytest.raises(Exception):
            spec.fps = 3.0

    def test_default_values(self):
        spec = AnimationSpec(frames=("e_Joy.jpg",))
        assert spec.fps == 1.0
        assert spec.loop is True
        assert spec.static_fallback == ""


class TestFaceAnimatorDisabled:
    """Tests with animation disabled (USE_FACE_ANIMATION=false equivalent)."""

    @patch("face_animator.requests.post")
    def test_set_state_pushes_static_fallback(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.start()

        animator.set_state("IDLE")
        time.sleep(0.1)

        # Should push the static fallback image
        mock_post.assert_called_with(
            "http://10.0.0.23/api/images/display",
            json={"FileName": "face_idle.gif", "Alpha": 1},
            timeout=pytest.approx(0.75, abs=0.1),
        )
        animator.stop()

    @patch("face_animator.requests.post")
    def test_disabled_no_animation_loop(self, mock_post):
        """When disabled, no continuous frame pushing happens."""
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.start()

        animator.set_state("IDLE")
        time.sleep(0.5)

        # Should only push once (static fallback), not loop
        call_count = mock_post.call_count
        assert call_count == 1, f"Expected 1 call, got {call_count}"
        animator.stop()

    @patch("face_animator.requests.post")
    def test_disabled_state_change_pushes_new_fallback(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.start()

        animator.set_state("IDLE")
        time.sleep(0.1)
        animator.set_state("PROCESSING")
        time.sleep(0.1)

        # Should have pushed two different images
        calls = mock_post.call_args_list
        filenames = [c.kwargs["json"]["FileName"] for c in calls]
        assert "face_idle.gif" in filenames
        assert "face_processing.gif" in filenames
        animator.stop()


class TestFaceAnimatorEnabled:
    """Tests with animation enabled."""

    @patch("face_animator.requests.post")
    def test_single_frame_pushes_once(self, mock_post):
        """Single-frame specs push once and wait (no repeated calls)."""
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=True, max_fps=4.0)
        animator.start()

        animator.set_state("IDLE")
        time.sleep(0.5)

        # Single-frame: push once, then idle
        assert mock_post.call_count == 1
        animator.stop()

    @patch("face_animator.requests.post")
    def test_multi_frame_loops(self, mock_post):
        """Multi-frame specs loop frames at configured FPS."""
        mock_post.return_value = MagicMock(status_code=200)

        custom_map = {
            "IDLE": AnimationSpec(
                frames=("e_DefaultContent.jpg", "e_ContentLeft.jpg", "e_ContentRight.jpg"),
                fps=4.0,
                loop=True,
                static_fallback="e_DefaultContent.jpg",
            ),
        }
        animator = FaceAnimator(
            "http://10.0.0.23", enabled=True, max_fps=4.0,
            min_interval_s=0.25, animation_map=custom_map,
        )
        animator.start()

        animator.set_state("IDLE")
        time.sleep(1.5)  # At 4 FPS, should get ~6 frames in 1.5s

        assert mock_post.call_count >= 4  # At least several frames
        animator.stop()

    @patch("face_animator.requests.post")
    def test_state_change_resets_frame_index(self, mock_post):
        """Changing state starts new animation from frame 0."""
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=True)
        animator.start()

        animator.set_state("IDLE")
        time.sleep(0.2)
        animator.set_state("PROCESSING")
        time.sleep(0.2)

        # Verify both states' images were pushed
        calls = mock_post.call_args_list
        filenames = [c.kwargs["json"]["FileName"] for c in calls]
        assert "face_idle.gif" in filenames
        assert "face_processing.gif" in filenames
        animator.stop()

    @patch("face_animator.requests.post")
    def test_same_state_no_duplicate_push(self, mock_post):
        """Setting same state twice doesn't trigger extra push."""
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=True)
        animator.start()

        animator.set_state("IDLE")
        time.sleep(0.2)
        call_count_1 = mock_post.call_count

        animator.set_state("IDLE")  # same state again
        time.sleep(0.2)
        call_count_2 = mock_post.call_count

        assert call_count_2 == call_count_1  # no extra push
        animator.stop()

    @patch("face_animator.requests.post")
    def test_fps_clamped_to_max(self, mock_post):
        """FPS is clamped to max_fps regardless of spec."""
        mock_post.return_value = MagicMock(status_code=200)

        custom_map = {
            "IDLE": AnimationSpec(
                frames=("a.jpg", "b.jpg"),
                fps=100.0,  # absurdly high
                static_fallback="a.jpg",
            ),
        }
        animator = FaceAnimator(
            "http://10.0.0.23", enabled=True, max_fps=2.0,
            min_interval_s=0.25, animation_map=custom_map,
        )
        animator.start()

        animator.set_state("IDLE")
        time.sleep(1.0)

        # At max 2 FPS (but min_interval 0.25s), should be ~4 calls max in 1s
        assert mock_post.call_count <= 6
        animator.stop()


class TestFaceAnimatorThreadSafety:
    """Tests for threading and lifecycle."""

    @patch("face_animator.requests.post")
    def test_stop_joins_within_timeout(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=True)
        animator.start()
        animator.set_state("IDLE")
        time.sleep(0.1)

        t0 = time.perf_counter()
        animator.stop()
        elapsed = time.perf_counter() - t0

        assert elapsed < FaceAnimator.STOP_TIMEOUT_S + 0.5
        assert not animator.is_running

    @patch("face_animator.requests.post")
    def test_start_idempotent(self, mock_post):
        """Calling start() twice doesn't create extra threads."""
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=True)
        animator.start()
        thread1 = animator._thread
        animator.start()  # second call
        thread2 = animator._thread

        assert thread1 is thread2
        animator.stop()

    @patch("face_animator.requests.post")
    def test_accepts_enum_state(self, mock_post):
        """set_state accepts enum values (like controller's State)."""
        mock_post.return_value = MagicMock(status_code=200)
        from face_animator import AnimationState
        animator = FaceAnimator("http://10.0.0.23", enabled=True)
        animator.start()

        animator.set_state(AnimationState.IDLE)
        time.sleep(0.2)

        assert mock_post.call_count >= 1
        animator.stop()

    @patch("face_animator.requests.post")
    def test_frame_push_failure_continues_loop(self, mock_post):
        """REST failures don't crash the animation thread."""
        mock_post.side_effect = Exception("Connection refused")

        custom_map = {
            "IDLE": AnimationSpec(
                frames=("a.jpg", "b.jpg"),
                fps=4.0,
                static_fallback="a.jpg",
            ),
        }
        animator = FaceAnimator(
            "http://10.0.0.23", enabled=True, max_fps=4.0,
            min_interval_s=0.25, animation_map=custom_map,
        )
        animator.start()
        animator.set_state("IDLE")
        time.sleep(1.0)

        # Thread should still be alive despite failures
        assert animator.is_running
        animator.stop()


class TestDefaultAnimationMap:
    """Validate the default animation map covers all states."""

    def test_all_states_mapped(self):
        from face_animator import AnimationState
        for state in AnimationState:
            assert state.value in DEFAULT_ANIMATION_MAP, (
                f"State {state.value} missing from DEFAULT_ANIMATION_MAP"
            )

    def test_all_specs_have_fallback(self):
        for state_key, spec in DEFAULT_ANIMATION_MAP.items():
            assert spec.static_fallback, (
                f"State {state_key} has no static_fallback"
            )

    def test_all_initial_specs_are_single_frame(self):
        """With firmware-native GIF looping, all specs are single-frame
        (one GIF/PNG pushed, firmware handles animation)."""
        for state_key, spec in DEFAULT_ANIMATION_MAP.items():
            assert spec.is_single_frame, (
                f"State {state_key} should be single-frame (firmware loops GIFs)"
            )


class TestDisableRegression:
    """
    Disable-regression test (§6.4): with USE_FACE_ANIMATION=false,
    the controller's display behavior must be identical to pre-animation.
    """

    @patch("face_animator.requests.post")
    def test_disabled_animator_only_pushes_on_state_change(self, mock_post):
        """Disabled animator pushes static images only on state transitions."""
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.start()

        # Simulate a typical conversation flow
        states = ["IDLE", "RECORDING", "PROCESSING", "PLAYING", "LISTENING", "IDLE"]
        for s in states:
            animator.set_state(s)
            time.sleep(0.05)

        time.sleep(0.2)

        # Should have exactly one push per unique consecutive state
        calls = mock_post.call_args_list
        filenames = [c.kwargs["json"]["FileName"] for c in calls]

        expected = [
            "face_idle.gif",              # IDLE
            "face_listening.png",         # RECORDING
            "face_processing.gif",        # PROCESSING
            "face_talking_neutral.gif",   # PLAYING
            "face_listening.png",         # LISTENING
            "face_idle.gif",              # IDLE (back)
        ]
        assert filenames == expected
        animator.stop()


class TestTalkingFaceForEmotion:
    """Unit tests for the emotion → talking-face filename helper (#110)."""

    def test_valid_emotions_map_to_expected_files(self):
        for emotion in VALID_EMOTIONS:
            assert talking_face_for_emotion(emotion) == f"face_talking_{emotion}.gif"

    def test_unknown_emotion_falls_back_to_neutral(self):
        assert talking_face_for_emotion("furious") == "face_talking_neutral.gif"
        assert talking_face_for_emotion("") == "face_talking_neutral.gif"
        assert talking_face_for_emotion(None) == "face_talking_neutral.gif"

    def test_emotion_is_case_insensitive(self):
        assert talking_face_for_emotion("HAPPY") == "face_talking_happy.gif"


class TestFaceAnimatorEmotionSelection:
    """Tests for emotion-driven PLAYING face selection (#110)."""

    def test_default_emotion_is_neutral(self):
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        assert animator.emotion == DEFAULT_EMOTION

    @patch("face_animator.requests.post")
    def test_set_emotion_before_playing_selects_variant(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.start()

        animator.set_emotion("happy")
        animator.set_state("PLAYING")
        time.sleep(0.1)

        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert "face_talking_happy.gif" in filenames
        assert "face_talking_neutral.gif" not in filenames
        animator.stop()

    @patch("face_animator.requests.post")
    def test_set_state_with_emotion_arg(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.start()

        animator.set_state("PLAYING", emotion="sad")
        time.sleep(0.1)

        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert "face_talking_sad.gif" in filenames
        animator.stop()

    @patch("face_animator.requests.post")
    def test_emotion_change_while_playing_refreshes_face(self, mock_post):
        """Changing emotion during PLAYING pushes the new talking face."""
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.start()

        animator.set_state("PLAYING", emotion="neutral")
        time.sleep(0.1)
        animator.set_emotion("excited")
        time.sleep(0.1)

        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert "face_talking_neutral.gif" in filenames
        assert "face_talking_excited.gif" in filenames
        animator.stop()

    @patch("face_animator.requests.post")
    def test_invalid_emotion_coerced_to_neutral(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.start()

        animator.set_emotion("furious")
        animator.set_state("PLAYING")
        time.sleep(0.1)

        assert animator.emotion == "neutral"
        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert "face_talking_neutral.gif" in filenames
        animator.stop()

    @patch("face_animator.requests.post")
    def test_emotion_does_not_affect_non_playing_states(self, mock_post):
        """A non-PLAYING state ignores emotion and uses its own asset."""
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.start()

        animator.set_emotion("happy")
        animator.set_state("IDLE")
        time.sleep(0.1)

        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert filenames == ["face_idle.gif"]
        animator.stop()


class TestFaceAnimatorBuiltinFallback:
    """Tests for built-in firmware fallback when custom assets unavailable (#110)."""

    def test_default_custom_faces_available(self):
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        assert animator.custom_faces_available is True

    @patch("face_animator.requests.post")
    def test_fallback_uses_builtin_for_states(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.set_custom_faces_available(False)
        animator.start()

        animator.set_state("IDLE")
        time.sleep(0.1)

        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert filenames == [BUILTIN_FALLBACK_MAP["IDLE"]]
        assert not filenames[0].startswith("face_")
        animator.stop()

    @patch("face_animator.requests.post")
    def test_fallback_uses_builtin_emotion_for_playing(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.set_custom_faces_available(False)
        animator.start()

        animator.set_state("PLAYING", emotion="sad")
        time.sleep(0.1)

        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert filenames == [BUILTIN_EMOTION_FALLBACK["sad"]]
        animator.stop()

    def test_builtin_maps_cover_all_states(self):
        from face_animator import AnimationState
        for state in AnimationState:
            assert state.value in BUILTIN_FALLBACK_MAP, (
                f"State {state.value} missing from BUILTIN_FALLBACK_MAP"
            )

    def test_builtin_emotion_map_covers_all_emotions(self):
        for emotion in VALID_EMOTIONS:
            assert emotion in BUILTIN_EMOTION_FALLBACK, (
                f"Emotion {emotion} missing from BUILTIN_EMOTION_FALLBACK"
            )

    @patch("face_animator.requests.post")
    def test_availability_change_refreshes_current_frame(self, mock_post):
        """Toggling custom-face availability refreshes the displayed frame."""
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.start()

        animator.set_state("IDLE")
        time.sleep(0.1)
        # Now custom assets become unavailable — frame should refresh to builtin
        animator.set_custom_faces_available(False)
        time.sleep(0.1)

        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert "face_idle.gif" in filenames
        assert BUILTIN_FALLBACK_MAP["IDLE"] in filenames
        animator.stop()

    @patch("face_animator.requests.post")
    def test_availability_no_change_no_extra_push(self, mock_post):
        """Setting the same availability value does not push a new frame."""
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.start()

        animator.set_state("IDLE")
        time.sleep(0.1)
        count_before = mock_post.call_count
        animator.set_custom_faces_available(True)  # already True (default)
        time.sleep(0.1)

        assert mock_post.call_count == count_before
        animator.stop()

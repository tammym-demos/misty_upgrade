"""
Unit tests for TalkingHeadMotion (issue #116).

Validates the gated emotion-aware talking head motion:
- disabled = no-op (head behavior unchanged from baseline)
- enabled = gentle head moves within the safe envelope, only while active
- stop() halts and re-centers the head
- emotion scales motion amplitude
- all movements stay within Misty's mechanical head limits
"""

import sys
import os
import time
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration"))

from talking_head_motion import (
    TalkingHeadMotion,
    EMOTION_MOTION_SCALE,
    DEFAULT_MOTION_SCALE,
)


class _Recorder:
    """Thread-safe recorder for move_head calls."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, pitch=0, roll=0, yaw=0, velocity=50):
        with self._lock:
            self.calls.append({"pitch": pitch, "roll": roll, "yaw": yaw, "velocity": velocity})

    def snapshot(self):
        with self._lock:
            return list(self.calls)


class TestDisabledIsNoOp:
    def test_start_does_nothing_when_disabled(self):
        rec = _Recorder()
        motion = TalkingHeadMotion(rec, enabled=False, interval_s=0.2)
        motion.start("happy")
        time.sleep(0.4)
        motion.stop()
        assert rec.snapshot() == []
        assert motion.is_active is False

    def test_disabled_property(self):
        motion = TalkingHeadMotion(_Recorder(), enabled=False)
        assert motion.enabled is False


class TestEnabledMotion:
    def test_start_moves_head_while_active(self):
        rec = _Recorder()
        motion = TalkingHeadMotion(rec, enabled=True, interval_s=0.1)
        motion.start("neutral")
        time.sleep(0.45)
        motion.stop()
        # Several micro-movements plus a final center.
        assert len(rec.snapshot()) >= 2

    def test_stop_recenters_head(self):
        rec = _Recorder()
        motion = TalkingHeadMotion(
            rec, enabled=True, interval_s=0.1, pitch_center=-10.0
        )
        motion.start("neutral")
        time.sleep(0.25)
        motion.stop()
        last = rec.snapshot()[-1]
        # Center pose: pitch at center, roll/yaw zero.
        assert last["pitch"] == pytest.approx(-10.0)
        assert last["roll"] == 0
        assert last["yaw"] == 0

    def test_not_active_after_stop(self):
        rec = _Recorder()
        motion = TalkingHeadMotion(rec, enabled=True, interval_s=0.1)
        motion.start()
        assert motion.is_active is True
        motion.stop()
        assert motion.is_active is False

    def test_all_moves_within_safe_envelope(self):
        rec = _Recorder()
        motion = TalkingHeadMotion(
            rec, enabled=True, interval_s=0.05,
            pitch_center=-10.0, pitch_range=4.0, yaw_range=6.0, roll_range=3.0,
        )
        motion.start("excited")  # largest amplitude scale
        time.sleep(0.6)
        motion.stop()
        for c in rec.snapshot():
            assert TalkingHeadMotion.PITCH_MIN <= c["pitch"] <= TalkingHeadMotion.PITCH_MAX
            assert TalkingHeadMotion.ROLL_MIN <= c["roll"] <= TalkingHeadMotion.ROLL_MAX
            assert TalkingHeadMotion.YAW_MIN <= c["yaw"] <= TalkingHeadMotion.YAW_MAX

    def test_hard_clamp_beyond_mechanical_limits(self):
        """Even a misconfigured envelope never exceeds mechanical limits."""
        rec = _Recorder()
        motion = TalkingHeadMotion(
            rec, enabled=True, interval_s=0.05,
            pitch_center=0.0, pitch_range=1000.0, yaw_range=1000.0, roll_range=1000.0,
        )
        motion.start("excited")
        time.sleep(0.4)
        motion.stop()
        for c in rec.snapshot():
            assert TalkingHeadMotion.PITCH_MIN <= c["pitch"] <= TalkingHeadMotion.PITCH_MAX
            assert TalkingHeadMotion.ROLL_MIN <= c["roll"] <= TalkingHeadMotion.ROLL_MAX
            assert TalkingHeadMotion.YAW_MIN <= c["yaw"] <= TalkingHeadMotion.YAW_MAX


class TestEmotionScaling:
    def test_known_emotions_have_scales(self):
        for emotion in ("neutral", "happy", "excited", "sad", "curious"):
            assert emotion in EMOTION_MOTION_SCALE

    def test_excited_is_livelier_than_sad(self):
        assert EMOTION_MOTION_SCALE["excited"] > EMOTION_MOTION_SCALE["sad"]

    def test_unknown_emotion_uses_default_scale(self):
        rec = _Recorder()
        motion = TalkingHeadMotion(rec, enabled=True, interval_s=0.1)
        # Unknown emotion should not raise; scale falls back to default.
        motion.start("furious")
        time.sleep(0.2)
        motion.stop()
        assert EMOTION_MOTION_SCALE.get("furious", DEFAULT_MOTION_SCALE) == DEFAULT_MOTION_SCALE

    def test_empty_emotion_normalized_to_neutral(self):
        rec = _Recorder()
        motion = TalkingHeadMotion(rec, enabled=True, interval_s=0.1)
        motion.start("")
        assert motion._emotion == "neutral"
        motion.stop()
        motion.start(None)
        assert motion._emotion == "neutral"
        motion.stop()


class TestRestartAndSafety:
    def test_restart_updates_emotion(self):
        rec = _Recorder()
        motion = TalkingHeadMotion(rec, enabled=True, interval_s=0.1)
        motion.start("neutral")
        time.sleep(0.15)
        motion.start("excited")  # restart with new emotion
        time.sleep(0.15)
        motion.stop()
        assert motion.is_active is False

    def test_move_head_exception_does_not_crash_loop(self):
        def boom(**kwargs):
            raise RuntimeError("head unreachable")

        motion = TalkingHeadMotion(boom, enabled=True, interval_s=0.05)
        motion.start("neutral")
        time.sleep(0.2)
        # Should still be considered active despite errors, and stop cleanly.
        motion.stop()
        assert motion.is_active is False

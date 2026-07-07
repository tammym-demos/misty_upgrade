"""
Unit tests for ExpressionCoordinator (issue #74).

Validates the gated, hardware-free embodied expression coordinator:
- disabled = no-op (body behavior unchanged from baseline)
- every expression intent maps to a bounded choreography spec
- face rendering is delegated (callback) and LED/head/arm actuation is invoked
- head/arm poses stay within Misty's mechanical limits (even if misconfigured)
- choreography is cancellable
- safety gating skips motor gestures but still allows non-motor face/LED cues
- sensor-triggered expressions are rate-limited
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration"))

from expression_coordinator import (  # noqa: E402
    Expression,
    ExpressionCoordinator,
    ChoreographySpec,
    EXPRESSION_CHOREOGRAPHY,
    coerce_expression,
    NEUTRAL_HEAD,
    NEUTRAL_ARMS,
    _SUPPORTED_FACE_EMOTIONS,
)


class _Recorder:
    """Thread-safe recorder of actuation callbacks."""

    def __init__(self):
        self._lock = threading.Lock()
        self.led = []
        self.head = []
        self.arms = []
        self.faces = []
        self.sounds = []

    def set_led(self, r, g, b):
        with self._lock:
            self.led.append((r, g, b))

    def move_head(self, pitch=0, roll=0, yaw=0, velocity=50):
        with self._lock:
            self.head.append({"pitch": pitch, "roll": roll, "yaw": yaw, "velocity": velocity})

    def move_arms(self, left=None, right=None, velocity=50):
        with self._lock:
            self.arms.append({"left": left, "right": right, "velocity": velocity})

    def face(self, emotion, static_fallback):
        with self._lock:
            self.faces.append((emotion, static_fallback))

    def play_sound(self, filename):
        with self._lock:
            self.sounds.append(filename)

    def snapshot(self):
        with self._lock:
            return {
                "led": list(self.led),
                "head": list(self.head),
                "arms": list(self.arms),
                "faces": list(self.faces),
                "sounds": list(self.sounds),
            }


def _make(enabled=True, safety_gate=None, rec=None, **kw):
    rec = rec or _Recorder()
    coord = ExpressionCoordinator(
        set_led=rec.set_led,
        move_head=rec.move_head,
        move_arms=rec.move_arms,
        face_callback=rec.face,
        play_sound=rec.play_sound,
        safety_gate=safety_gate,
        enabled=enabled,
        **kw,
    )
    return coord, rec


def _drain(coord, timeout=1.0):
    """Wait for the current choreography thread to finish."""
    deadline = time.monotonic() + timeout
    while coord.is_active and time.monotonic() < deadline:
        time.sleep(0.01)


class TestCoerceExpression:
    def test_known_string(self):
        assert coerce_expression("joy") is Expression.JOY
        assert coerce_expression("STARTLED") is Expression.STARTLED

    def test_enum_passthrough(self):
        assert coerce_expression(Expression.SAD) is Expression.SAD

    def test_unknown_returns_none(self):
        assert coerce_expression("furious") is None
        assert coerce_expression("") is None
        assert coerce_expression(None) is None
        assert coerce_expression(123) is None


class TestChoreographyMap:
    def test_every_expression_has_spec(self):
        for expr in list(Expression):
            assert expr in EXPRESSION_CHOREOGRAPHY
            assert isinstance(EXPRESSION_CHOREOGRAPHY[expr], ChoreographySpec)

    def test_face_emotions_supported_by_face_layer(self):
        for spec in EXPRESSION_CHOREOGRAPHY.values():
            assert spec.face_emotion in _SUPPORTED_FACE_EMOTIONS

    def test_static_fallback_is_builtin_face(self):
        for spec in EXPRESSION_CHOREOGRAPHY.values():
            assert spec.static_fallback.startswith("e_")
            assert spec.static_fallback.endswith(".jpg")

    def test_specs_within_mechanical_limits(self):
        for spec in EXPRESSION_CHOREOGRAPHY.values():
            if spec.head is not None:
                pitch, roll, yaw = spec.head
                assert ExpressionCoordinator.PITCH_MIN <= pitch <= ExpressionCoordinator.PITCH_MAX
                assert ExpressionCoordinator.ROLL_MIN <= roll <= ExpressionCoordinator.ROLL_MAX
                assert ExpressionCoordinator.YAW_MIN <= yaw <= ExpressionCoordinator.YAW_MAX
            if spec.arms is not None:
                left, right = spec.arms
                assert ExpressionCoordinator.ARM_MIN <= left <= ExpressionCoordinator.ARM_MAX
                assert ExpressionCoordinator.ARM_MIN <= right <= ExpressionCoordinator.ARM_MAX


class TestDisabledIsNoOp:
    def test_express_noop_when_disabled(self):
        coord, rec = _make(enabled=False)
        assert coord.express(Expression.JOY) is False
        time.sleep(0.1)
        assert rec.snapshot() == {"led": [], "head": [], "arms": [], "faces": [], "sounds": []}
        assert coord.is_active is False

    def test_sensor_and_cancel_noop_when_disabled(self):
        coord, rec = _make(enabled=False)
        assert coord.express_for_sensor(Expression.STARTLED) is False
        coord.cancel()  # must not raise
        assert rec.snapshot()["head"] == []

    def test_enabled_property(self):
        coord, _ = _make(enabled=False)
        assert coord.enabled is False


class TestEnabledChoreography:
    def test_express_applies_face_led_head_arms(self):
        coord, rec = _make(enabled=True)
        assert coord.express(Expression.JOY) is True
        _drain(coord)
        snap = rec.snapshot()
        spec = EXPRESSION_CHOREOGRAPHY[Expression.JOY]
        assert snap["faces"] == [(spec.face_emotion, spec.static_fallback)]
        assert snap["led"] == [spec.led]
        assert len(snap["head"]) == 1
        assert len(snap["arms"]) == 1

    def test_unknown_expression_ignored(self):
        coord, rec = _make(enabled=True)
        assert coord.express("furious") is False
        time.sleep(0.05)
        assert rec.snapshot()["led"] == []

    def test_all_expressions_stay_within_limits(self):
        for expr in list(Expression):
            coord, rec = _make(enabled=True)
            coord.express(expr)
            _drain(coord)
            snap = rec.snapshot()
            for h in snap["head"]:
                assert ExpressionCoordinator.PITCH_MIN <= h["pitch"] <= ExpressionCoordinator.PITCH_MAX
                assert ExpressionCoordinator.ROLL_MIN <= h["roll"] <= ExpressionCoordinator.ROLL_MAX
                assert ExpressionCoordinator.YAW_MIN <= h["yaw"] <= ExpressionCoordinator.YAW_MAX
            for a in snap["arms"]:
                assert ExpressionCoordinator.ARM_MIN <= a["left"] <= ExpressionCoordinator.ARM_MAX
                assert ExpressionCoordinator.ARM_MIN <= a["right"] <= ExpressionCoordinator.ARM_MAX

    def test_face_callback_optional(self):
        # No face callback -> LED/motion still applied, no crash.
        rec = _Recorder()
        coord = ExpressionCoordinator(
            set_led=rec.set_led, move_head=rec.move_head, move_arms=rec.move_arms,
            face_callback=None, enabled=True,
        )
        assert coord.express(Expression.SAD) is True
        _drain(coord)
        assert rec.snapshot()["led"] != []
        assert rec.snapshot()["faces"] == []


class TestSafetyGating:
    def test_unsafe_skips_motor_but_allows_face_and_led(self):
        coord, rec = _make(enabled=True, safety_gate=lambda: False)
        coord.express(Expression.STARTLED)
        _drain(coord)
        snap = rec.snapshot()
        # Non-motor cues still fire.
        assert snap["faces"] != []
        assert snap["led"] != []
        # Motor gestures are suppressed while unsafe.
        assert snap["head"] == []
        assert snap["arms"] == []

    def test_safe_gate_allows_motor(self):
        coord, rec = _make(enabled=True, safety_gate=lambda: True)
        coord.express(Expression.JOY)
        _drain(coord)
        assert rec.snapshot()["head"] != []

    def test_raising_safety_gate_treated_as_unsafe(self):
        def boom():
            raise RuntimeError("state read failed")

        coord, rec = _make(enabled=True, safety_gate=boom)
        coord.express(Expression.JOY)
        _drain(coord)
        assert rec.snapshot()["head"] == []


class TestCancellation:
    def test_cancel_stops_and_recenters_when_safe(self):
        coord, rec = _make(enabled=True, recenter=True)
        coord.express(Expression.SAD)
        _drain(coord)
        rec.head.clear()
        rec.arms.clear()
        coord.cancel()
        snap = rec.snapshot()
        # Re-center pose issued.
        assert snap["head"][-1]["pitch"] == pytest.approx(NEUTRAL_HEAD[0])
        assert snap["arms"][-1]["left"] == pytest.approx(NEUTRAL_ARMS[0])
        assert coord.is_active is False

    def test_cancel_does_not_recenter_when_unsafe(self):
        coord, rec = _make(enabled=True, recenter=True, safety_gate=lambda: False)
        coord.express(Expression.SAD)
        _drain(coord)
        coord.cancel()
        # No motor commands at all while unsafe.
        assert rec.snapshot()["head"] == []

    def test_cancel_without_recenter(self):
        coord, rec = _make(enabled=True, recenter=False)
        coord.express(Expression.JOY)
        _drain(coord)
        head_before = len(rec.snapshot()["head"])
        coord.cancel()
        assert len(rec.snapshot()["head"]) == head_before

    def test_new_expression_supersedes_previous(self):
        coord, rec = _make(enabled=True)
        coord.express(Expression.JOY)
        coord.express(Expression.SAD)  # supersedes
        _drain(coord)
        assert coord.is_active is False
        # The most recent face should be the sad spec's emotion.
        assert rec.snapshot()["faces"][-1][0] == EXPRESSION_CHOREOGRAPHY[Expression.SAD].face_emotion


class TestSensorRateLimit:
    def test_repeat_sensor_expression_is_rate_limited(self):
        coord, rec = _make(enabled=True, sensor_min_interval_s=5.0, safety_gate=lambda: True)
        assert coord.express_for_sensor(Expression.STARTLED) is True
        _drain(coord)
        # Immediate repeat is dropped.
        assert coord.express_for_sensor(Expression.STARTLED) is False

    def test_different_sensor_expressions_not_rate_limited_together(self):
        coord, rec = _make(enabled=True, sensor_min_interval_s=5.0)
        assert coord.express_for_sensor(Expression.STARTLED) is True
        _drain(coord)
        assert coord.express_for_sensor(Expression.ANNOYED) is True

    def test_sensor_expression_allowed_after_interval(self):
        coord, rec = _make(enabled=True, sensor_min_interval_s=0.2)
        assert coord.express_for_sensor(Expression.STARTLED) is True
        _drain(coord)
        time.sleep(0.25)
        assert coord.express_for_sensor(Expression.STARTLED) is True


class TestDefensiveBehavior:
    def test_actuator_exception_does_not_crash(self):
        def boom(**kwargs):
            raise RuntimeError("device unreachable")

        coord = ExpressionCoordinator(
            set_led=lambda r, g, b: (_ for _ in ()).throw(RuntimeError("led fail")),
            move_head=boom, move_arms=boom, face_callback=None, enabled=True,
        )
        assert coord.express(Expression.ERROR) is True
        _drain(coord)
        assert coord.is_active is False

"""Unit tests for generic face detection tracking."""

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration"),
)

from face_recognition_service import FaceDetection, select_largest_face
from face_tracker import FaceTracker


def detection(x, y, width, height, frame_width=1000, frame_height=600):
    return FaceDetection(
        x=x,
        y=y,
        width=width,
        height=height,
        confidence=0.9,
        frame_width=frame_width,
        frame_height=frame_height,
    )


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


def make_tracker(recorder, appeared=None, safe=lambda: True, **kwargs):
    return FaceTracker(
        source=None,
        detector=None,
        move_head=recorder,
        safety_gate=safe,
        on_face_appeared=appeared,
        enabled=True,
        confirm_frames=kwargs.pop("confirm_frames", 2),
        missing_frames=kwargs.pop("missing_frames", 2),
        greeting_reset_missing_frames=kwargs.pop("greeting_reset_missing_frames", 2),
        min_face_area_ratio=kwargs.pop("min_face_area_ratio", 0.0),
        **kwargs,
    )


def test_select_largest_face_uses_area():
    small = detection(0, 0, 100, 100)
    large = detection(0, 0, 200, 150)
    assert select_largest_face([small, large]) is large
    assert select_largest_face([]) is None


def test_centered_face_stays_inside_dead_zone():
    recorder = Recorder()
    tracker = make_tracker(recorder, confirm_frames=1)
    tracker.process_detections([detection(400, 250, 200, 100)], now=0.0)
    assert recorder.calls == []


def test_small_background_face_does_not_hold_presence():
    recorder = Recorder()
    greetings = []
    tracker = make_tracker(
        recorder,
        appeared=lambda: greetings.append("hi"),
        confirm_frames=1,
        min_face_area_ratio=0.02,
    )
    tracker.process_detections([detection(0, 0, 50, 50)], now=0.0)
    assert greetings == []
    assert tracker.status()["face_present"] is False


def test_face_right_turns_head_right_with_bounded_step():
    recorder = Recorder()
    tracker = make_tracker(recorder, confirm_frames=1, max_step_deg=3.0)
    tracker.process_detections([detection(800, 250, 100, 100)], now=0.0)
    assert recorder.calls[-1]["yaw"] == pytest.approx(-3.0)
    assert recorder.calls[-1]["roll"] == 0


def test_tracking_respects_safety_gate():
    recorder = Recorder()
    tracker = make_tracker(recorder, confirm_frames=1, safe=lambda: False)
    tracker.process_detections([detection(800, 250, 100, 100)], now=0.0)
    assert recorder.calls == []


def test_pitch_misconfiguration_is_clamped_to_hardware_limits():
    recorder = Recorder()
    tracker = make_tracker(
        recorder,
        confirm_frames=1,
        pitch_min_deg=100.0,
        pitch_max_deg=-100.0,
        pitch_center_deg=100.0,
    )
    tracker._center()
    assert -40.0 <= recorder.calls[-1]["pitch"] <= 26.0


def test_greets_once_until_confirmed_absence():
    recorder = Recorder()
    greetings = []
    tracker = make_tracker(recorder, appeared=lambda: greetings.append("hi"))
    face = detection(400, 250, 200, 100)

    tracker.process_detections([face], now=0.0)
    tracker.process_detections([face], now=0.5)
    tracker.process_detections([face], now=1.0)
    assert greetings == ["hi"]

    tracker.process_detections([], now=1.5)
    tracker.process_detections([], now=2.0)
    tracker.process_detections([face], now=2.5)
    tracker.process_detections([face], now=3.0)
    assert greetings == ["hi", "hi"]


def test_brief_tracking_loss_does_not_reset_greeting_latch():
    recorder = Recorder()
    greetings = []
    tracker = make_tracker(
        recorder,
        appeared=lambda: greetings.append("hi"),
        confirm_frames=1,
        missing_frames=2,
        greeting_reset_missing_frames=5,
    )
    face = detection(400, 250, 200, 100)

    tracker.process_detections([face], now=0.0)
    tracker.process_detections([], now=0.5)
    tracker.process_detections([], now=1.0)
    tracker.process_detections([face], now=1.5)
    assert greetings == ["hi"]

    for index in range(5):
        tracker.process_detections([], now=2.0 + index)
    tracker.process_detections([face], now=8.0)
    assert greetings == ["hi", "hi"]


def test_recenters_after_face_loss_delay():
    recorder = Recorder()
    tracker = make_tracker(
        recorder, confirm_frames=1, missing_frames=1, recenter_delay_s=2.0
    )
    tracker.process_detections([detection(800, 250, 100, 100)], now=0.0)
    tracker.process_detections([], now=1.0)
    tracker.process_detections([], now=2.9)
    assert recorder.calls[-1]["yaw"] != 0
    tracker.process_detections([], now=3.0)
    assert recorder.calls[-1]["yaw"] == 0
    assert recorder.calls[-1]["pitch"] == pytest.approx(-10.0)

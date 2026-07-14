"""Idle-only companion-side face tracking for Misty's RGB camera."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from face_recognition_service import FaceDetection, select_largest_face

logger = logging.getLogger(__name__)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class FaceTracker:
    """Track the largest face with bounded head-only pan/tilt corrections."""

    STOP_TIMEOUT_S = 2.0

    def __init__(
        self,
        source,
        detector,
        move_head: Callable[..., None],
        safety_gate: Callable[[], bool],
        on_face_appeared: Optional[Callable[[], None]] = None,
        *,
        enabled: bool = False,
        poll_interval_s: float = 0.5,
        min_face_area_ratio: float = 0.01,
        horizontal_dead_zone: float = 0.10,
        vertical_dead_zone: float = 0.10,
        yaw_gain: float = 18.0,
        pitch_gain: float = 14.0,
        max_step_deg: float = 5.0,
        yaw_limit_deg: float = 55.0,
        pitch_min_deg: float = -30.0,
        pitch_max_deg: float = 15.0,
        pitch_center_deg: float = -10.0,
        velocity: float = 35.0,
        confirm_frames: int = 2,
        missing_frames: int = 3,
        greeting_reset_missing_frames: int = 30,
        recenter_delay_s: float = 2.0,
    ):
        self._source = source
        self._detector = detector
        self._move_head = move_head
        self._safety_gate = safety_gate
        self._on_face_appeared = on_face_appeared
        self._enabled = bool(enabled)
        self._poll_interval_s = max(0.1, poll_interval_s)
        self._min_face_area_ratio = _clamp(min_face_area_ratio, 0.0, 1.0)
        self._horizontal_dead_zone = max(0.0, horizontal_dead_zone)
        self._vertical_dead_zone = max(0.0, vertical_dead_zone)
        self._yaw_gain = abs(yaw_gain)
        self._pitch_gain = abs(pitch_gain)
        self._max_step_deg = max(0.1, abs(max_step_deg))
        self._yaw_limit_deg = min(81.0, abs(yaw_limit_deg))
        bounded_pitch_min = _clamp(pitch_min_deg, -40.0, 26.0)
        bounded_pitch_max = _clamp(pitch_max_deg, -40.0, 26.0)
        self._pitch_min_deg = min(bounded_pitch_min, bounded_pitch_max)
        self._pitch_max_deg = max(bounded_pitch_min, bounded_pitch_max)
        self._pitch_center_deg = _clamp(
            pitch_center_deg, self._pitch_min_deg, self._pitch_max_deg
        )
        self._velocity = velocity
        self._confirm_frames = max(1, confirm_frames)
        self._missing_frames = max(1, missing_frames)
        self._greeting_reset_missing_frames = max(
            self._missing_frames, greeting_reset_missing_frames
        )
        self._recenter_delay_s = max(0.0, recenter_delay_s)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._present_count = 0
        self._missing_count = 0
        self._greeting_missing_count = 0
        self._face_present = False
        self._greeting_latched = False
        self._lost_at: Optional[float] = None
        self._centered_after_loss = True
        self._yaw = 0.0
        self._pitch = self._pitch_center_deg
        self._last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "active": self.is_active,
                "face_present": self._face_present,
                "yaw": round(self._yaw, 2),
                "pitch": round(self._pitch, 2),
                "error": self._last_error,
            }

    def start(self) -> None:
        if not self._enabled or self.is_active:
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="FaceTracker", daemon=True
        )
        self._thread.start()

    def stop(self, center: bool = True) -> None:
        thread = self._thread
        self._thread = None
        self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.STOP_TIMEOUT_S)
        if center and self._enabled and self._safety_gate():
            self._center()
        close = getattr(self._source, "close", None)
        if callable(close):
            close()

    def process_detections(self, detections, now: float) -> None:
        """Process one detection result; public for deterministic unit tests."""
        if self._stop_event.is_set() or not self._safety_gate():
            return
        nearby_faces = [
            detection
            for detection in detections
            if detection.area_ratio >= self._min_face_area_ratio
        ]
        target = select_largest_face(nearby_faces)
        if target is None:
            self._handle_missing(now)
            return

        self._missing_count = 0
        self._greeting_missing_count = 0
        self._lost_at = None
        self._centered_after_loss = False
        self._present_count += 1
        if not self._face_present and self._present_count >= self._confirm_frames:
            self._face_present = True
            if not self._greeting_latched and self._on_face_appeared is not None:
                self._greeting_latched = True
                self._on_face_appeared()
        if self._face_present and self._safety_gate():
            self._track(target)

    def _handle_missing(self, now: float) -> None:
        self._present_count = 0
        self._missing_count += 1
        self._greeting_missing_count += 1
        if self._face_present and self._missing_count >= self._missing_frames:
            self._face_present = False
            self._lost_at = now
        if self._greeting_missing_count >= self._greeting_reset_missing_frames:
            self._greeting_latched = False
        if (
            not self._face_present
            and self._lost_at is not None
            and not self._centered_after_loss
            and now - self._lost_at >= self._recenter_delay_s
            and self._safety_gate()
        ):
            self._center()
            self._centered_after_loss = True

    def _track(self, target: FaceDetection) -> None:
        x_error = target.center_x_normalized
        y_error = target.center_y_normalized
        yaw_step = 0.0
        pitch_step = 0.0
        if abs(x_error) > self._horizontal_dead_zone:
            yaw_step = _clamp(
                -x_error * self._yaw_gain, -self._max_step_deg, self._max_step_deg
            )
        if abs(y_error) > self._vertical_dead_zone:
            pitch_step = _clamp(
                y_error * self._pitch_gain, -self._max_step_deg, self._max_step_deg
            )
        if yaw_step == 0.0 and pitch_step == 0.0:
            return
        self._yaw = _clamp(
            self._yaw + yaw_step, -self._yaw_limit_deg, self._yaw_limit_deg
        )
        self._pitch = _clamp(
            self._pitch + pitch_step, self._pitch_min_deg, self._pitch_max_deg
        )
        if self._safety_gate():
            self._move_head(
                pitch=self._pitch, roll=0, yaw=self._yaw, velocity=self._velocity
            )

    def _center(self) -> None:
        self._yaw = 0.0
        self._pitch = self._pitch_center_deg
        self._move_head(
            pitch=self._pitch, roll=0, yaw=0, velocity=self._velocity
        )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self._safety_gate():
                self._stop_event.wait(self._poll_interval_s)
                continue
            try:
                frame = self._source.capture()
                detections = self._detector.detect(frame)
                if self._stop_event.is_set() or not self._safety_gate():
                    continue
                with self._lock:
                    self._last_error = None
                self.process_detections(detections, now=time.monotonic())
            except Exception as exc:
                message = str(exc)
                with self._lock:
                    changed = message != self._last_error
                    self._last_error = message
                if changed:
                    logger.warning("Face tracking unavailable: %s", message)
            self._stop_event.wait(self._poll_interval_s)

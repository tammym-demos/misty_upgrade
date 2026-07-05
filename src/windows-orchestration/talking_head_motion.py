"""
Emotion-aware subtle talking head motion for Misty II (issue #116).

Provides a small, self-contained ``TalkingHeadMotion`` driver that makes gentle
head movements while Misty is speaking (controller state ``PLAYING``) and
re-centers the head when speech ends. It is intentionally decoupled from the
drive/locomotion safety subsystem:

- It only ever issues ``/api/head`` commands through a caller-supplied
  ``move_head`` callable — never drive, arm, audio, or keyphrase commands.
- It runs only while explicitly started, and the controller starts it only for
  the ``PLAYING`` state and stops it on any transition away from ``PLAYING``
  (including MOVING, CHARGING, ERROR, reboot, re-arm, and shutdown).
- All movements stay within a safe head envelope well inside Misty's mechanical
  limits (pitch -40..26, roll -40..40, yaw -81..81).
- It is gated by config (``USE_TALKING_HEAD_MOTION``); when disabled it is a
  no-op so head behavior is identical to today.

Usage:
    motion = TalkingHeadMotion(controller.move_head, enabled=USE_TALKING_HEAD_MOTION)
    motion.start("happy")   # begin gentle motion for the PLAYING turn
    ...                     # audio plays
    motion.stop()           # halt + re-center head
"""

import logging
import random
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# Per-emotion amplitude scale for the talking head wobble. Excited is livelier;
# sad is calmer. Unknown emotions use the neutral scale.
EMOTION_MOTION_SCALE: dict[str, float] = {
    "neutral": 1.0,
    "happy": 1.15,
    "excited": 1.4,
    "sad": 0.6,
    "curious": 1.1,
}
DEFAULT_MOTION_SCALE = 1.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class TalkingHeadMotion:
    """Gentle, emotion-aware head motion during speech. Thread-based, bounded.

    Args:
        move_head: Callable ``(pitch, roll, yaw, velocity)`` that issues a head
            move (typically ``MistyController.move_head``). Never called with
            values outside the configured safe envelope.
        enabled: If False, ``start()``/``stop()`` are no-ops (head behavior is
            unchanged from the no-motion baseline).
        pitch_center/pitch_range/yaw_range/roll_range: Safe motion envelope in
            degrees. Movements are centered on ``pitch_center`` and wobble within
            the ranges.
        velocity: Gentle move velocity for each micro-movement.
        interval_s: Seconds between micro-movements.
    """

    STOP_TIMEOUT_S = 2.0

    # Hard safety clamps — even if misconfigured, never exceed these bounds.
    PITCH_MIN, PITCH_MAX = -40.0, 26.0
    ROLL_MIN, ROLL_MAX = -40.0, 40.0
    YAW_MIN, YAW_MAX = -81.0, 81.0

    def __init__(
        self,
        move_head: Callable[..., None],
        enabled: bool = False,
        pitch_center: float = -10.0,
        pitch_range: float = 4.0,
        yaw_range: float = 6.0,
        roll_range: float = 3.0,
        velocity: float = 30.0,
        interval_s: float = 0.8,
    ):
        self._move_head = move_head
        self._enabled = bool(enabled)
        self._pitch_center = pitch_center
        self._pitch_range = abs(pitch_range)
        self._yaw_range = abs(yaw_range)
        self._roll_range = abs(roll_range)
        self._velocity = velocity
        self._interval_s = max(0.2, interval_s)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._emotion = "neutral"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, emotion: str = "neutral") -> None:
        """Begin gentle talking head motion. No-op if disabled.

        Safe to call repeatedly; a running motion is restarted with the new
        emotion. Non-blocking.
        """
        if not self._enabled:
            return
        e = (emotion or "").strip().lower() if isinstance(emotion, str) else "neutral"
        # Restart cleanly if already running so the emotion/scale updates.
        self.stop(center=False)
        with self._lock:
            self._emotion = e
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._motion_loop,
                name="TalkingHeadMotion",
                daemon=True,
            )
            self._thread.start()

    def stop(self, center: bool = True) -> None:
        """Stop talking head motion and (by default) re-center the head.

        Bounded join. No-op if disabled or not running. Best-effort; never
        raises. ``center=False`` is used internally when restarting.
        """
        if not self._enabled:
            return
        with self._lock:
            thread = self._thread
            stop_event = self._stop_event
            self._thread = None
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.STOP_TIMEOUT_S)
            if thread.is_alive():
                logger.debug("TalkingHeadMotion thread did not stop within timeout")
        if center:
            self._center_head()

    def _scaled(self, base_range: float) -> float:
        scale = EMOTION_MOTION_SCALE.get(self._emotion, DEFAULT_MOTION_SCALE)
        return base_range * scale

    def _center_head(self) -> None:
        """Re-center the head to the neutral talking pitch. Best-effort."""
        try:
            pitch = _clamp(self._pitch_center, self.PITCH_MIN, self.PITCH_MAX)
            self._move_head(pitch=pitch, roll=0, yaw=0, velocity=self._velocity)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"TalkingHeadMotion center failed: {e}")

    def _motion_loop(self) -> None:
        stop_event = self._stop_event
        while not stop_event.is_set():
            try:
                pitch = _clamp(
                    self._pitch_center
                    + random.uniform(-self._scaled(self._pitch_range),
                                     self._scaled(self._pitch_range)),
                    self.PITCH_MIN, self.PITCH_MAX,
                )
                yaw = _clamp(
                    random.uniform(-self._scaled(self._yaw_range),
                                   self._scaled(self._yaw_range)),
                    self.YAW_MIN, self.YAW_MAX,
                )
                roll = _clamp(
                    random.uniform(-self._scaled(self._roll_range),
                                   self._scaled(self._roll_range)),
                    self.ROLL_MIN, self.ROLL_MAX,
                )
                self._move_head(pitch=pitch, roll=roll, yaw=yaw, velocity=self._velocity)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"TalkingHeadMotion move failed: {e}")
            # Interruptible wait so stop() is responsive.
            stop_event.wait(timeout=self._interval_s)

"""
State-driven face animation for Misty II.

Provides a FaceAnimator class that maps controller states to animated
face expressions. Runs a single daemon thread that pushes frames to
Misty's display via REST at a configurable FPS.

Design: docs/design-animated-face-expressions.md §5
Phase 1 validation: tools/face_display_probe.py (max ~10 FPS, 4 FPS recommended)

Usage:
    animator = FaceAnimator(misty_base_url, enabled=True)
    animator.start()
    animator.set_state(State.IDLE)       # non-blocking
    animator.set_state(State.LISTENING)  # switches immediately
    animator.stop()                      # bounded join
"""

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# Re-declare State locally to avoid circular import with misty_controller.
# The animator accepts any enum with matching .value strings.
class AnimationState(Enum):
    """Animation states that map to controller states."""
    DISCONNECTED = "DISCONNECTED"
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    PROCESSING = "PROCESSING"
    PLAYING = "PLAYING"
    LISTENING = "LISTENING"
    MOVING = "MOVING"
    REARMING = "REARMING"
    REBOOTING = "REBOOTING"
    CHARGING = "CHARGING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class AnimationSpec:
    """Defines how a state's face animation behaves.

    Attributes:
        frames: Ordered tuple of Misty image filenames. Length >= 1.
        fps: Target frames per second (clamped to max_fps at runtime).
        loop: If True, loop frames continuously. If False, play once and hold last.
        static_fallback: Single image used when animation is disabled or fails.
    """
    frames: tuple
    fps: float = 1.0
    loop: bool = True
    static_fallback: str = ""

    @property
    def is_single_frame(self) -> bool:
        return len(self.frames) <= 1


# Default animation specs — single-frame to match today's behavior.
# Multi-frame animations will be added in Phase 3 after tuning.
DEFAULT_ANIMATION_MAP: dict[str, AnimationSpec] = {
    "DISCONNECTED": AnimationSpec(
        frames=("e_Sadness.jpg",),
        static_fallback="e_Sadness.jpg",
    ),
    "IDLE": AnimationSpec(
        frames=("e_DefaultContent.jpg",),
        fps=0.5,
        static_fallback="e_DefaultContent.jpg",
    ),
    "RECORDING": AnimationSpec(
        frames=("e_Admiration.jpg",),
        static_fallback="e_Admiration.jpg",
    ),
    "PROCESSING": AnimationSpec(
        frames=("e_Contempt.jpg",),
        static_fallback="e_Contempt.jpg",
    ),
    "PLAYING": AnimationSpec(
        frames=("e_EcstacyHilarious.jpg",),
        static_fallback="e_EcstacyHilarious.jpg",
    ),
    "LISTENING": AnimationSpec(
        frames=("e_Joy.jpg",),
        static_fallback="e_Joy.jpg",
    ),
    "MOVING": AnimationSpec(
        frames=("e_Joy2.jpg",),
        static_fallback="e_Joy2.jpg",
    ),
    "REARMING": AnimationSpec(
        frames=("e_DefaultContent.jpg",),
        static_fallback="e_DefaultContent.jpg",
    ),
    "REBOOTING": AnimationSpec(
        frames=("e_ContentLeft.jpg",),
        static_fallback="e_ContentLeft.jpg",
    ),
    "CHARGING": AnimationSpec(
        frames=("e_Sleeping.jpg",),
        static_fallback="e_Sleeping.jpg",
    ),
    "ERROR": AnimationSpec(
        frames=("e_Sadness.jpg",),
        static_fallback="e_Sadness.jpg",
    ),
}


class FaceAnimator:
    """Companion-side face animation driver for Misty II.

    Runs a single daemon thread that pushes display frames to Misty via REST.
    The controller interacts only through set_state() (non-blocking) and
    stop() (bounded join). The animator never touches audio, LED, motors,
    or keyphrase endpoints.

    Args:
        misty_base_url: Base URL for Misty REST API (e.g., "http://10.0.0.23").
        enabled: If False, set_state() pushes static_fallback only (no looping).
        max_fps: Upper bound on animation FPS (from hardware validation).
        min_interval_s: Minimum seconds between REST frame pushes (rate limit).
        animation_map: State-to-AnimationSpec mapping. Defaults to single-frame.
    """

    STOP_TIMEOUT_S = 3.0  # max seconds to wait for thread join on stop()

    def __init__(
        self,
        misty_base_url: str,
        enabled: bool = False,
        max_fps: float = 4.0,
        min_interval_s: float = 0.25,
        animation_map: Optional[dict[str, AnimationSpec]] = None,
    ):
        self._base_url = misty_base_url.rstrip("/")
        self._enabled = enabled
        self._max_fps = max_fps
        self._min_interval_s = min_interval_s
        self._animation_map = animation_map or DEFAULT_ANIMATION_MAP

        self._lock = threading.Lock()
        self._state_changed = threading.Event()
        self._target_state: Optional[str] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_displayed: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self):
        """Start the animation thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._animation_loop,
            name="FaceAnimator",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"FaceAnimator started (enabled={self._enabled}, max_fps={self._max_fps})")

    def stop(self):
        """Stop the animation thread with bounded timeout."""
        self._running = False
        self._state_changed.set()  # wake the thread if sleeping
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=self.STOP_TIMEOUT_S)
            if self._thread.is_alive():
                logger.warning("FaceAnimator thread did not stop within timeout")
        self._thread = None
        logger.info("FaceAnimator stopped")

    def set_state(self, state) -> None:
        """Update the target animation state. Non-blocking.

        Args:
            state: A State enum value or string matching a key in animation_map.
        """
        # Accept both enum and string
        state_key = state.value if hasattr(state, "value") else str(state)

        with self._lock:
            if self._target_state == state_key:
                return  # no change
            self._target_state = state_key

        # If animation is disabled, push static fallback immediately
        if not self._enabled:
            spec = self._animation_map.get(state_key)
            if spec and spec.static_fallback:
                self._push_frame(spec.static_fallback)
            return

        # Wake the animation thread to switch states
        self._state_changed.set()

    def _animation_loop(self):
        """Main animation thread loop."""
        current_state: Optional[str] = None
        frame_idx = 0

        while self._running:
            # Check for state change
            with self._lock:
                new_state = self._target_state

            if new_state != current_state:
                current_state = new_state
                frame_idx = 0
                self._state_changed.clear()

            if current_state is None:
                # No state set yet — wait for one
                self._state_changed.wait(timeout=1.0)
                continue

            spec = self._animation_map.get(current_state)
            if spec is None:
                # Unknown state — wait for next change
                self._state_changed.wait(timeout=1.0)
                continue

            # Single-frame: push once and wait for state change
            if spec.is_single_frame:
                frame = spec.frames[0]
                if self._last_displayed != frame:
                    self._push_frame(frame)
                self._state_changed.wait(timeout=5.0)
                self._state_changed.clear()
                continue

            # Multi-frame animation
            if frame_idx >= len(spec.frames):
                if spec.loop:
                    frame_idx = 0
                else:
                    # Hold last frame, wait for state change
                    self._state_changed.wait(timeout=5.0)
                    self._state_changed.clear()
                    continue

            frame = spec.frames[frame_idx]
            self._push_frame(frame)
            frame_idx += 1

            # Sleep for the frame interval (clamped to min_interval)
            clamped_fps = min(spec.fps, self._max_fps)
            interval = max(1.0 / clamped_fps, self._min_interval_s)

            # Interruptible sleep — wake on state change
            self._state_changed.wait(timeout=interval)
            if self._state_changed.is_set():
                self._state_changed.clear()

    def _push_frame(self, filename: str) -> bool:
        """Push a single display frame to Misty. Best-effort, never raises.

        Uses a short timeout and logs failures at debug level only.
        Returns True on success, False on failure.
        """
        if self._last_displayed == filename:
            return True  # already showing this frame

        url = f"{self._base_url}/api/images/display"
        try:
            resp = requests.post(
                url,
                json={"FileName": filename, "Alpha": 1},
                timeout=self._min_interval_s + 0.5,
            )
            if resp.status_code == 200:
                self._last_displayed = filename
                return True
            else:
                logger.debug(f"FaceAnimator frame push HTTP {resp.status_code}: {filename}")
                return False
        except Exception as e:
            logger.debug(f"FaceAnimator frame push failed: {filename} — {e}")
            return False

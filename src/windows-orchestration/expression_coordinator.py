"""
Embodied expression coordinator for Misty II (issue #74).

Provides a self-contained ``ExpressionCoordinator`` that maps high-level
*expression intents* (joy, curious, confused, ...) to bounded body choreography:
face emotion, LED color, head pose, arm pose, and an optional sound cue. It is
deliberately decoupled from the drive/locomotion safety subsystem and from face
rendering itself:

- **Face rendering is delegated**, not duplicated. The coordinator calls a
  caller-supplied ``face_callback(emotion, static_fallback)`` which, in the live
  system, routes through issue #73's ``FaceAnimator`` (``set_emotion`` /
  ``show_asset``). When #73 is unavailable/disabled, the same callback can fall
  back to a static built-in firmware face, so static face/LED behavior remains
  the fallback path.
- **All actuation goes through injected callables** (``set_led``,
  ``move_head``, ``move_arms``, ``face_callback``, optional ``play_sound``).
  This keeps the module testable without live Misty hardware.
- **Choreography is cancellable and non-blocking.** Each intent runs on a single
  short-lived daemon thread that checks a stop event between steps; ``cancel()``
  halts it promptly. It never blocks audio, safety, reboot, charging, movement
  preemption, or shutdown cleanup.
- **Safety gating.** Before issuing head/arm gestures the coordinator consults a
  caller-supplied ``safety_gate()`` predicate. When it returns ``False`` (e.g.
  MOVING/CHARGING/ERROR/shutdown/movement preemption/recording) the coordinator
  skips motor gestures. Non-motor face/LED cues are still allowed because they
  do not move the robot.
- **Sensor rate-limiting.** ``express_for_sensor()`` drops repeat expressions
  that arrive faster than ``sensor_min_interval_s`` to avoid gesture spam from
  chatty sensor streams (bump, ToF, hazard).
- **Config-gated.** When ``enabled`` is ``False`` (default,
  ``USE_EMBODIED_EXPRESSIONS=false``) every public method is a no-op, so body
  behavior is identical to today.

Only the constrained expression enum below can drive gestures — callers cannot
issue arbitrary arm/head commands through this class, and drive/tread movement
is intentionally out of scope here.

Usage::

    coord = ExpressionCoordinator(
        set_led=controller.set_led,
        move_head=controller.move_head,
        move_arms=controller.move_arms,
        face_callback=lambda emotion, fallback: face_animator.set_emotion(emotion),
        safety_gate=lambda: controller.state not in UNSAFE_STATES,
        enabled=USE_EMBODIED_EXPRESSIONS,
    )
    coord.express(Expression.JOY)          # direct intent
    coord.express_for_sensor(Expression.STARTLED)  # rate-limited sensor intent
    coord.cancel()                         # halt + re-center (safe states only)
"""

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class Expression(str, Enum):
    """Shared expression enum for embodied expression + face animation (#74/#73).

    Kept intentionally small and constrained. The LLM/callers select from these
    named intents only; they never issue raw arm/head commands.
    """

    JOY = "joy"
    CURIOUS = "curious"
    CONFUSED = "confused"
    THINKING = "thinking"
    SASSY = "sassy"
    ANNOYED = "annoyed"
    ANGRY = "angry"  # playful/sassy only, never threatening
    SAD = "sad"
    STARTLED = "startled"
    SLEEPY = "sleepy"
    ERROR = "error"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class ChoreographySpec:
    """Bounded choreography for one expression intent.

    Attributes:
        face_emotion: Emotion passed to the face layer (#73 ``FaceAnimator``
            supports neutral/happy/excited/sad/curious). Chosen as the closest
            supported base face for the intent.
        static_fallback: Built-in firmware face (``e_*.jpg``) used when the face
            layer is unavailable/disabled. Ships with every Misty II, so it never
            fails on a missing file.
        led: ``(r, g, b)`` chest LED color for the intent.
        head: Optional ``(pitch, roll, yaw)`` head pose, or ``None`` to leave the
            head where it is. Values are clamped to the safe head envelope.
        arms: Optional ``(left, right)`` arm pose, or ``None`` to leave the arms
            where they are. Values are clamped to the safe arm envelope.
        sound: Optional Misty sound-cue filename, or ``None`` for silence.
    """

    face_emotion: str
    static_fallback: str
    led: tuple
    head: Optional[tuple] = None
    arms: Optional[tuple] = None
    sound: Optional[str] = None


# Face emotions supported by #73's FaceAnimator. Kept in sync with
# face_animator.VALID_EMOTIONS; expression intents map onto these base faces.
_SUPPORTED_FACE_EMOTIONS = frozenset({"neutral", "happy", "excited", "sad", "curious"})

# Neutral resting pose used to re-center head/arms after a gesture. Slight
# up-tilt for eye contact; arms relaxed down.
NEUTRAL_HEAD = (-10.0, 0.0, 0.0)   # (pitch, roll, yaw)
NEUTRAL_ARMS = (80.0, 80.0)        # (left, right) — relaxed down

# The choreography map. Every expression intent has a bounded spec. Head/arm
# poses stay well inside Misty's mechanical limits (see hard clamps below).
EXPRESSION_CHOREOGRAPHY: dict[Expression, ChoreographySpec] = {
    Expression.JOY: ChoreographySpec(
        face_emotion="happy", static_fallback="e_Joy.jpg",
        led=(0, 200, 0), head=(-18.0, 0.0, 0.0), arms=(-10.0, -10.0),
    ),
    Expression.CURIOUS: ChoreographySpec(
        face_emotion="curious", static_fallback="e_Surprise.jpg",
        led=(0, 150, 200), head=(-8.0, 15.0, 20.0), arms=(60.0, 80.0),
    ),
    Expression.CONFUSED: ChoreographySpec(
        face_emotion="curious", static_fallback="e_Contempt.jpg",
        led=(180, 120, 0), head=(-5.0, 20.0, -15.0), arms=(70.0, 70.0),
    ),
    Expression.THINKING: ChoreographySpec(
        face_emotion="neutral", static_fallback="e_Contempt.jpg",
        led=(120, 0, 200), head=(5.0, 10.0, 10.0), arms=(80.0, 80.0),
    ),
    Expression.SASSY: ChoreographySpec(
        face_emotion="happy", static_fallback="e_Joy2.jpg",
        led=(220, 0, 150), head=(-10.0, -12.0, -18.0), arms=(30.0, 80.0),
    ),
    Expression.ANNOYED: ChoreographySpec(
        face_emotion="sad", static_fallback="e_Contempt.jpg",
        led=(200, 80, 0), head=(0.0, -10.0, 0.0), arms=(80.0, 80.0),
    ),
    Expression.ANGRY: ChoreographySpec(  # playful/sassy only, not threatening
        face_emotion="sad", static_fallback="e_Anger.jpg",
        led=(200, 20, 0), head=(2.0, 0.0, 0.0), arms=(50.0, 50.0),
    ),
    Expression.SAD: ChoreographySpec(
        face_emotion="sad", static_fallback="e_Sadness.jpg",
        led=(0, 0, 180), head=(20.0, 0.0, 0.0), arms=(85.0, 85.0),
    ),
    Expression.STARTLED: ChoreographySpec(
        face_emotion="curious", static_fallback="e_Surprise.jpg",
        led=(255, 255, 0), head=(-30.0, 0.0, 0.0), arms=(0.0, 0.0),
    ),
    Expression.SLEEPY: ChoreographySpec(
        face_emotion="neutral", static_fallback="e_Sleepy.jpg",
        led=(60, 40, 120), head=(20.0, 0.0, 0.0), arms=(85.0, 85.0),
    ),
    Expression.ERROR: ChoreographySpec(
        face_emotion="sad", static_fallback="e_Sadness.jpg",
        led=(200, 0, 0), head=(10.0, 0.0, 0.0), arms=(80.0, 80.0),
    ),
}


def coerce_expression(value) -> Optional[Expression]:
    """Coerce a string/enum into a known ``Expression`` or ``None``.

    Returns ``None`` for unknown/empty values so callers can decide whether to
    skip rather than raise.
    """
    if isinstance(value, Expression):
        return value
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    try:
        return Expression(key)
    except ValueError:
        return None


class ExpressionCoordinator:
    """Maps expression intents to bounded, cancellable body choreography.

    Args:
        set_led: Callable ``(r, g, b)`` to set the chest LED.
        move_head: Callable ``(pitch, roll, yaw, velocity)`` to move the head.
        move_arms: Callable ``(left, right, velocity)`` to move the arms.
        face_callback: Callable ``(emotion, static_fallback)`` that renders the
            face. In the live system this delegates to #73's ``FaceAnimator``;
            when the face layer is unavailable it can display ``static_fallback``.
            Optional — if ``None``, face rendering is skipped (LED/motion still
            apply).
        play_sound: Optional callable ``(filename)`` for a sound cue.
        safety_gate: Optional predicate returning ``True`` when it is safe to
            issue head/arm gestures. When it returns ``False`` motor gestures are
            skipped; face/LED cues still apply. Defaults to always-safe.
        enabled: If ``False`` (default), all public methods are no-ops.
        head_velocity/arm_velocity: Gentle move velocities for gestures.
        sensor_min_interval_s: Minimum seconds between sensor-triggered
            expressions (rate-limit guard against sensor spam).
        recenter: If ``True``, ``cancel()`` returns head/arms to a neutral pose
            (only when the safety gate allows motion).
    """

    STOP_TIMEOUT_S = 2.0

    # Hard safety clamps — even if a spec is misconfigured, never exceed these.
    PITCH_MIN, PITCH_MAX = -40.0, 26.0
    ROLL_MIN, ROLL_MAX = -40.0, 40.0
    YAW_MIN, YAW_MAX = -81.0, 81.0
    ARM_MIN, ARM_MAX = -29.0, 90.0

    def __init__(
        self,
        set_led: Optional[Callable[..., None]] = None,
        move_head: Optional[Callable[..., None]] = None,
        move_arms: Optional[Callable[..., None]] = None,
        face_callback: Optional[Callable[[str, str], None]] = None,
        play_sound: Optional[Callable[[str], None]] = None,
        safety_gate: Optional[Callable[[], bool]] = None,
        enabled: bool = False,
        head_velocity: float = 40.0,
        arm_velocity: float = 40.0,
        sensor_min_interval_s: float = 3.0,
        recenter: bool = True,
    ):
        self._set_led = set_led
        self._move_head = move_head
        self._move_arms = move_arms
        self._face_callback = face_callback
        self._play_sound = play_sound
        self._safety_gate = safety_gate
        self._enabled = bool(enabled)
        self._head_velocity = head_velocity
        self._arm_velocity = arm_velocity
        self._sensor_min_interval_s = max(0.0, sensor_min_interval_s)
        self._recenter = bool(recenter)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_sensor_ts: dict[Expression, float] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _is_safe_to_move(self) -> bool:
        """Whether head/arm gestures may be issued right now."""
        if self._safety_gate is None:
            return True
        try:
            return bool(self._safety_gate())
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"ExpressionCoordinator safety_gate raised: {e}")
            return False

    def express(self, expression, source: str = "direct") -> bool:
        """Run choreography for an expression intent. No-op if disabled.

        Non-blocking: choreography runs on a short-lived daemon thread. Any
        in-flight choreography is cancelled first so the newest intent wins.
        Returns ``True`` if a choreography was started, ``False`` otherwise
        (disabled or unknown expression).
        """
        if not self._enabled:
            return False
        expr = coerce_expression(expression)
        if expr is None:
            logger.debug(f"ExpressionCoordinator: unknown expression {expression!r}; ignoring")
            return False
        spec = EXPRESSION_CHOREOGRAPHY[expr]
        # Newest intent wins: cancel any running gesture without re-centering.
        self._stop_thread(center=False)
        with self._lock:
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run_choreography,
                args=(expr, spec, self._stop_event),
                name=f"ExpressionCoordinator-{expr.value}",
                daemon=True,
            )
            self._thread.start()
        logger.debug(f"ExpressionCoordinator: expressing {expr.value} (source={source})")
        return True

    def express_for_sensor(self, expression) -> bool:
        """Rate-limited expression for sensor-triggered reactions.

        Drops the intent if the same expression fired more recently than
        ``sensor_min_interval_s`` to avoid gesture spam from chatty sensors.
        Returns ``True`` if the expression was accepted and started.
        """
        if not self._enabled:
            return False
        expr = coerce_expression(expression)
        if expr is None:
            return False
        now = time.monotonic()
        with self._lock:
            last = self._last_sensor_ts.get(expr)
            if last is not None and (now - last) < self._sensor_min_interval_s:
                logger.debug(
                    f"ExpressionCoordinator: rate-limited sensor expression {expr.value}"
                )
                return False
            self._last_sensor_ts[expr] = now
        return self.express(expr, source="sensor")

    def cancel(self) -> None:
        """Cancel any in-flight choreography. No-op if disabled.

        Bounded join; best-effort; never raises. Re-centers head/arms when
        ``recenter`` is set and the safety gate permits motion.
        """
        if not self._enabled:
            return
        self._stop_thread(center=self._recenter)

    def _stop_thread(self, center: bool) -> None:
        with self._lock:
            thread = self._thread
            stop_event = self._stop_event
            self._thread = None
        if stop_event is not None:
            stop_event.set()
        stopped = True
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self.STOP_TIMEOUT_S)
            if thread.is_alive():
                # Actuator call may be hung; do NOT issue a re-center from this
                # thread while the prior choreography thread is still running, to
                # avoid concurrent/overlapping motor commands.
                stopped = False
                logger.debug("ExpressionCoordinator thread did not stop within timeout")
        if center and stopped:
            self._recenter_body()

    def _run_choreography(self, expr: Expression, spec: ChoreographySpec, stop_event: threading.Event) -> None:
        """Apply the bounded choreography for ``expr``. Runs on its own thread.

        Order: face + LED first (non-motor, always safe), then head/arm gestures
        only when the safety gate permits. Checks ``stop_event`` between steps so
        cancellation is prompt. Never raises out of the thread.
        """
        # Face (delegated to #73 / static fallback) — non-motor, always safe.
        if not stop_event.is_set():
            self._apply_face(spec)
        # LED — non-motor, always safe.
        if not stop_event.is_set():
            self._apply_led(spec)
        # Head/arm gestures — motor actuation, gated by safety.
        if stop_event.is_set():
            return
        if not self._is_safe_to_move():
            logger.debug(
                f"ExpressionCoordinator: safety gate blocked motion for {expr.value}"
            )
            return
        if not stop_event.is_set() and spec.head is not None:
            self._apply_head(spec.head)
        if not stop_event.is_set() and spec.arms is not None:
            self._apply_arms(spec.arms)
        # Optional sound cue last, still cancellable.
        if not stop_event.is_set() and spec.sound and self._play_sound is not None:
            try:
                self._play_sound(spec.sound)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"ExpressionCoordinator sound cue failed: {e}")

    def _apply_face(self, spec: ChoreographySpec) -> None:
        if self._face_callback is None:
            return
        emotion = spec.face_emotion if spec.face_emotion in _SUPPORTED_FACE_EMOTIONS else "neutral"
        try:
            self._face_callback(emotion, spec.static_fallback)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"ExpressionCoordinator face callback failed: {e}")

    def _apply_led(self, spec: ChoreographySpec) -> None:
        if self._set_led is None:
            return
        try:
            r, g, b = spec.led
            self._set_led(int(_clamp(r, 0, 255)), int(_clamp(g, 0, 255)), int(_clamp(b, 0, 255)))
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"ExpressionCoordinator LED failed: {e}")

    def _apply_head(self, head: tuple) -> None:
        if self._move_head is None:
            return
        try:
            pitch, roll, yaw = head
            self._move_head(
                pitch=_clamp(pitch, self.PITCH_MIN, self.PITCH_MAX),
                roll=_clamp(roll, self.ROLL_MIN, self.ROLL_MAX),
                yaw=_clamp(yaw, self.YAW_MIN, self.YAW_MAX),
                velocity=self._head_velocity,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"ExpressionCoordinator head move failed: {e}")

    def _apply_arms(self, arms: tuple) -> None:
        if self._move_arms is None:
            return
        try:
            left, right = arms
            self._move_arms(
                left=_clamp(left, self.ARM_MIN, self.ARM_MAX),
                right=_clamp(right, self.ARM_MIN, self.ARM_MAX),
                velocity=self._arm_velocity,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"ExpressionCoordinator arm move failed: {e}")

    def _recenter_body(self) -> None:
        """Return head/arms to a neutral resting pose. Motor-gated, best-effort."""
        if not self._is_safe_to_move():
            return
        self._apply_head(NEUTRAL_HEAD)
        self._apply_arms(NEUTRAL_ARMS)

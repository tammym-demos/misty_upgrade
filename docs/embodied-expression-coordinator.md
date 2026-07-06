# Embodied Expression Coordinator (#74)

**Related issue**: #74
**Related feature**: #73 (state-driven animated face)
**Related code**: `src/windows-orchestration/expression_coordinator.py`,
`src/windows-orchestration/config_defaults.py`
**Status**: Implemented, config-gated off, pending hardware validation

---

## 1. Summary

The `ExpressionCoordinator` is an optional companion-side layer that maps
high-level **expression intents** to bounded body choreography — face emotion,
chest LED color, head pose, arm pose, and an optional sound cue — so Misty can
show emotions like joy, curiosity, confusion, playful sass/annoyance, sadness,
and surprise.

It is intentionally separate from face rendering (#73):

- **#73 (`FaceAnimator`)** owns *face rendering* (which frame/GIF is displayed).
- **This feature (`ExpressionCoordinator`)** owns *expression selection and
  body/sensor choreography* around the face layer.

The coordinator **delegates** face rendering to the face layer rather than
duplicating it, and falls back to a static built-in firmware face when the face
layer is unavailable or disabled.

## 2. Expression states

The shared, constrained `Expression` enum (also intended for #73 alignment):

| Intent | Base face emotion | LED | Notes |
|---|---|---|---|
| `joy` | happy | green | wake / recognized speaker / movement accepted |
| `curious` | curious | cyan | wake / tilt to inspect |
| `confused` | curious | amber | no speech / empty STT |
| `thinking` | neutral | purple | processing |
| `sassy` | happy | magenta | movement accepted (playful) |
| `annoyed` | sad | orange | movement blocked / bump follow-up |
| `angry` | sad | red | **playful/sassy only, never threatening** |
| `sad` | sad | blue | movement blocked / error |
| `startled` | curious | yellow | bump contact / hazard / close obstacle |
| `sleepy` | neutral | indigo | low battery / charging |
| `error` | sad | red | error state |

Face emotions map onto the five base faces the #73 `FaceAnimator` supports
(`neutral`, `happy`, `excited`, `sad`, `curious`); the static fallback uses a
built-in `e_*.jpg` firmware face that ships with every Misty II so it never
fails on a missing file.

### Example triggers

- wake detected → `joy` or `curious`
- processing → `thinking`
- no speech / empty STT → `confused`
- recognized speaker → `joy` (+ small wave)
- movement accepted → `joy` or `sassy`
- movement blocked → `annoyed` or `sad`
- bump contact → `startled` then `annoyed`
- hazard / close obstacle → `startled` / cautious
- low battery or charging → `sleepy`
- error → `sad` / `error`

## 3. Configuration flags

All flags live in `config_defaults.py` (single source of truth) and are
mirrored in `.env.example`.

| Flag | Default | Meaning |
|---|---|---|
| `USE_EMBODIED_EXPRESSIONS` | `false` | Master gate. When false the coordinator is a **no-op**; body behavior is identical to today. |
| `EXPRESSION_HEAD_VELOCITY` | `40.0` | Gentle head-move velocity (percent). |
| `EXPRESSION_ARM_VELOCITY` | `40.0` | Gentle arm-move velocity (percent). |
| `EXPRESSION_SENSOR_MIN_INTERVAL_S` | `3.0` | Minimum seconds between repeats of the *same* sensor-triggered expression (rate-limit guard). |

## 4. Safety and constraints

- **Config-gated off** (`USE_EMBODIED_EXPRESSIONS=false`) until hardware
  validation passes.
- **Cancellable and non-blocking.** Each intent runs on a short-lived daemon
  thread that checks a stop event between steps; `cancel()` halts it promptly.
  It never blocks audio, safety, reboot, charging, movement preemption, or
  shutdown cleanup.
- **Safety-gated motion.** Head/arm gestures are issued only when the injected
  `safety_gate()` predicate returns `True`. During safety-critical states
  (`MOVING`, `CHARGING`, `ERROR`, shutdown, reboot, movement preemption, and —
  per the issue's audio caution — recording/listening) the gate returns `False`
  and motor gestures are skipped. Non-motor face/LED cues may still apply.
- **Sensor rate-limiting.** `express_for_sensor()` drops repeat expressions that
  arrive faster than `EXPRESSION_SENSOR_MIN_INTERVAL_S` to avoid gesture spam.
- **Bounded, conservative motion.** Head poses are clamped to the safe head
  envelope (`pitch -40..26`, `roll -40..40`, `yaw -81..81`) and arms to
  (`-29..90`), even if a spec is misconfigured. Velocities are gentle.
- **Constrained enum only.** The LLM/callers select from the named `Expression`
  intents; they cannot issue arbitrary arm/head commands through this class.
- **Drive/tread movement stays separate.** The coordinator never issues drive
  commands; locomotion safety remains authoritative and untouched.

## 5. Integration

The coordinator takes injected callables so it stays decoupled and testable
without hardware:

```python
from expression_coordinator import ExpressionCoordinator, Expression
import config_defaults as cfg

coord = ExpressionCoordinator(
    set_led=controller.set_led,
    move_head=controller.move_head,
    move_arms=controller.move_arms,
    # Delegate face rendering to #73's FaceAnimator; static fallback used when
    # the animator is unavailable.
    face_callback=lambda emotion, fallback: (
        face_animator.set_emotion(emotion)
        if face_animator is not None else controller.show_face(fallback)
    ),
    safety_gate=lambda: controller.state not in UNSAFE_STATES,
    enabled=cfg.USE_EMBODIED_EXPRESSIONS,
    head_velocity=cfg.EXPRESSION_HEAD_VELOCITY,
    arm_velocity=cfg.EXPRESSION_ARM_VELOCITY,
    sensor_min_interval_s=cfg.EXPRESSION_SENSOR_MIN_INTERVAL_S,
)

coord.express(Expression.JOY)                 # direct intent
coord.express_for_sensor(Expression.STARTLED) # rate-limited sensor intent
coord.cancel()                                # halt + re-center (safe states only)
```

When the face layer is disabled or `#73` is not available, the `face_callback`
displays the static built-in fallback face, so static face/LED behavior remains
the fallback path (acceptance criterion).

## 6. Hardware validation steps

Because motor motion cannot be validated in CI, run these on a physical Misty II
before enabling in production:

1. Set `USE_EMBODIED_EXPRESSIONS=true` in `.env`.
2. On a charged, clear, flat surface, trigger each `Expression` intent and
   confirm head/arm poses are gentle, bounded, and non-jarring.
3. Confirm gestures are **skipped** during `MOVING`, `CHARGING`, `ERROR`,
   reboot, shutdown, and movement preemption.
4. **Audio check**: verify arm/head motor noise during a gesture does not
   corrupt STT while recording/listening. If it does, keep motion gated off
   during recording/listening (the default safety gate already suppresses it).
5. Confirm `cancel()` promptly stops motion and re-centers the head/arms.
6. Confirm sensor-triggered expressions (bump/ToF/hazard) are rate-limited and
   do not spam gestures.
7. Confirm the face falls back to a built-in `e_*.jpg` face when custom face
   assets are unavailable.

## 7. Testing

Hardware-free unit tests live in `tests/test_expression_coordinator.py` and
cover mapping, disabled-mode no-op, cancellation, safety gating, sensor
rate-limiting, mechanical-limit clamping, and defensive actuator-failure
handling. Run:

```bash
python -m pytest tests/test_expression_coordinator.py -q
```

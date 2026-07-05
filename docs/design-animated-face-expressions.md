# Design: State-Driven Animated Face Expressions

**Date**: 2026-06-28
**Status**: Proposed
**Deciders**: Tim McClell
**Related issue**: #73
**Related code**: `src/windows-orchestration/misty_controller.py` (`display_image`, `State` enum)

---

## 1. Summary

Misty's face today is a sequence of **single static images** swapped at state
boundaries (`display_image("e_Joy2.jpg")`, `display_image("e_Sadness.jpg")`,
etc.). This makes her feel inert between transitions. This document designs a
companion-side **`FaceAnimator`** that maps controller states to *animated*
expressions — looping short frame sequences so the face feels alive while idle,
listening, thinking, speaking, and on error.

The design is **conservative and optional**: it reuses Misty's existing built-in
images by default, falls back cleanly to the current single-image behavior, and
can be fully disabled with one config flag without touching wake word,
recording, playback, movement safety, or shutdown cleanup.

This is a **design-only** deliverable. It defines the architecture, the
fallback strategy, a required hardware-validation step, the disable/safety
contract, and the asset-licensing boundary. Implementation is deferred to a
follow-up issue gated on the hardware-validation results.

## 2. Inspiration and asset boundary

The pattern is inspired by the **BMO local-AI-agent** project
(`brenpoly/be-more-agent`), which loops PNG frame sequences from per-state
folders (`faces/idle/`, `faces/listening/`, `faces/speaking/`, …) on a
Raspberry Pi LCD via `tkinter` + `Pillow`.

> **Asset boundary — read first.**
> We reuse **only the state→frame-loop _pattern_**. We do **not** include,
> copy, vendor, or redistribute any BMO / *Adventure Time* artwork, character
> identity, voice, or naming. No BMO PNG assets are committed to this
> repository. Any custom frames we create are original Misty-style expressions
> (or Misty's own built-in `e_*.jpg` images). This boundary is normative for
> implementation and review.

Misty also **cannot reuse BMO's GUI approach** — BMO drives a local LCD with a
GUI toolkit, whereas Misty's face is a remote display controlled only over REST
(`POST /api/images/display`). The animation loop therefore runs on the
companion laptop and pushes frames to Misty via REST.

## 3. Current state

| Aspect | Today |
|--------|-------|
| Display API | `display_image(name)` → `POST /api/images/display {"FileName": "e_Joy.jpg", "Alpha": 1}` |
| Trigger | One call per state transition (IDLE, RECORDING, PLAYING, error, …) |
| Assets | Built-in `e_*.jpg` (e.g. `e_Joy.jpg`, `e_Admiration.jpg`, `e_Contempt.jpg`, `e_Sadness.jpg`, `e_DefaultContent.jpg`) |
| Animation | None — face is frozen between transitions |
| On-robot skills | Intentionally avoided (unreliable JS runtime; see ADR-001) |

Controller states (`State` enum) that map to expressions: `DISCONNECTED`,
`IDLE`, `RECORDING`, `PROCESSING`, `PLAYING`, `LISTENING`, `MOVING`,
`REARMING`, `REBOOTING`, `CHARGING`, `ERROR`.

## 4. Goals / non-goals

**Goals**
- A companion-side `FaceAnimator` mapping controller states → animation states.
- Looping animation using existing built-in images or a small original frame set.
- Safe, automatic fallback to today's single static image per state.
- A documented hardware-validation step (frame rate, supported formats).
- A single config flag to disable animation with zero behavioral side effects.

**Non-goals (this iteration)**
- On-robot JavaScript skills (explicitly avoided — ADR-001).
- Rich/long cinematic animations or per-word lip-sync.
- Shipping any third-party (BMO) artwork.
- Committing to a specific custom asset set before hardware validation passes.

## 5. Design

### 5.1 Component overview

```
        Controller state machine
   (IDLE / LISTENING / PROCESSING / ...)
                 │  set_state(...)
                 ▼
        ┌──────────────────────┐
        │     FaceAnimator      │   companion laptop, single daemon thread
        │  state → AnimationSpec │
        │  loop frames @ FPS     │
        └──────────┬───────────┘
                   │ display_image(frame)   (existing REST call)
                   ▼
        POST /api/images/display  ──►  Misty face display
```

`FaceAnimator` owns **one** background daemon thread. The controller never
blocks on it — it only calls `animator.set_state(state)`, which updates a
shared target and returns immediately. The animator thread renders the loop for
the current target state.

### 5.2 Animation spec

Each state maps to an `AnimationSpec`:

```python
@dataclass(frozen=True)
class AnimationSpec:
    frames: tuple[str, ...]   # ordered Misty filenames, length >= 1
    fps: float                # frames per second (clamped to validated max)
    loop: bool = True         # loop vs. play-once-then-hold-last
    static_fallback: str = "" # single image used when animation disabled/unsafe
```

- A **single-frame** spec is exactly today's behavior (no extra REST traffic).
- `static_fallback` is the image shown when animation is disabled, when the
  hardware-validation gate has not passed, or when a frame push fails.

A conservative starter map (built-in images only, no new assets) — illustrative:

| State | frames (illustrative) | fps | fallback |
|-------|----------------------|-----|----------|
| IDLE | `e_DefaultContent.jpg`, `e_ContentLeft.jpg`, `e_ContentRight.jpg` | 0.5 | `e_DefaultContent.jpg` |
| LISTENING | `e_Joy.jpg`, `e_Admiration.jpg` | 1.0 | `e_Admiration.jpg` |
| PROCESSING | `e_Contempt.jpg`, `e_ContentLeft.jpg` | 1.0 | `e_Contempt.jpg` |
| PLAYING | `e_Joy.jpg`, `e_EcstacyHilarious.jpg` | 2.0 | `e_EcstacyHilarious.jpg` |
| ERROR | `e_Sadness.jpg` | 1.0 | `e_Sadness.jpg` |
| CHARGING | `e_Sleeping.jpg` | 1.0 | `e_Sleeping.jpg` |

Final frame lists and FPS are tuned **after** hardware validation (§7).

### 5.3 Control flow / threading

- `set_state(state)` writes the target state under a lock and signals an event.
- The animator thread, on each tick, reads the target. If unchanged, it
  advances to the next frame in the loop and sleeps `1/fps`. If changed, it
  resets the frame index and starts the new spec immediately.
- Frame pushes use a **dedicated best-effort display call**, not the existing
  `display_image()`/`misty_post()` helper directly. The current helper logs
  failures at **error** level and uses a **5s** timeout — both wrong for a
  per-frame loop (error-level log spam and multi-second stalls that desync the
  animation). The animator instead issues `POST /api/images/display` with a
  **short per-frame timeout** (≤ `FACE_ANIMATION_MIN_INTERVAL_S`, e.g. ~0.3s)
  and logs any failure at **debug** level only. On failure the loop continues
  and the face simply holds the last successful frame — animation must never
  raise into, block, or slow the controller. (Single-frame/static fallback
  pushes may still use the standard `display_image()` helper.)
- The thread is a **daemon** and exits on `stop()`; it also stops cleanly when
  the controller shuts down (§6.3).

### 5.4 Configuration

One env var gates the whole feature, mirroring existing flags
(`USE_LAPTOP_WAKE_WORD`, `USE_FACE_RECOGNITION`):

| Variable | Default | Effect |
|----------|---------|--------|
| `USE_FACE_ANIMATION` | `false` | `true` enables looped animation; `false` keeps today's single-image behavior |
| `FACE_ANIMATION_MAX_FPS` | validated cap | Upper bound applied to every spec's `fps` (safety clamp from §7) |
| `FACE_ANIMATION_MIN_INTERVAL_S` | `0.3` | Minimum spacing between REST frame pushes (rate-limit guard) |

Default **off** means merging the implementation changes nothing observable
until explicitly enabled.

## 6. Fallback and safety contract

This section is normative — it is the contract reviewers verify.

### 6.1 Static-image fallback

Animation degrades to a single static image (identical to today) when **any**
of these hold:
1. `USE_FACE_ANIMATION` is `false` (default).
2. The §7 hardware-validation gate has not recorded a passing result.
3. A frame push fails or REST latency exceeds the per-frame budget.
4. The `FaceAnimator` thread is not running for any reason.

In every fallback case the controller calls `display_image(static_fallback)` —
so the face is always at least as expressive as today, never blank.

### 6.2 Independence from critical subsystems

`FaceAnimator` is **display-only**. It must not call, share locks with, or
serialize against:

- **Wake word** (openWakeWord laptop listener / Misty keyphrase) — separate thread, no shared state.
- **Recording** (laptop `sounddevice` or Misty tally recording) — no audio/mic interaction.
- **Playback** (TTS upload/play) — animation runs concurrently; it does not gate or delay audio.
- **Movement safety** (`MOVING` state / preemption priority, #50) — animation reads state but never issues motor commands and never blocks a movement transition.
- **Shutdown cleanup** — see §6.3.

The only Misty API it touches is `POST /api/images/display`. It never touches
`/api/audio*`, `/api/led` ownership (LED scheme is unchanged), motors, or
keyphrase endpoints.

### 6.3 Shutdown cleanup

On controller `_shutdown()` (clean exit) the animator is stopped **before** the
existing cleanup sequence and must not interfere with it:
1. `animator.stop()` — join the daemon thread (bounded timeout).
2. Existing cleanup proceeds unchanged: stop keyphrase, stop recording, cancel
   skills, LED off (tally-light guarantee preserved).

Because the animator only pushes display frames, an unclean exit leaves a
harmless static face; it never leaves the **tally light** on (that is governed
by keyphrase/recording stop, which animation does not touch).

### 6.4 Disable-without-regression test obligation

Implementation must include a test asserting that with `USE_FACE_ANIMATION=false`
the controller's wake/record/play/shutdown call sequences are **byte-for-byte
identical** to current behavior (e.g., via mocked REST call capture). This is
the machine-checkable form of "animation can be disabled without affecting wake
word, recording, playback, movement safety, or shutdown cleanup."

## 7. Hardware-validation step (gate before implementation)

Firmware is final at v2.0.2 with no display-capability docs we trust, so a
small standalone probe script (`tools/face_display_probe.py`, laptop-only,
no controller dependency) must answer the following **before** animation is
enabled by default for any state:

1. **Repeated `/api/images/display` frame rate** — push N built-in images in a
   loop; measure achievable, stable FPS and per-call latency (p50/p95). Record
   the sustainable max → sets `FACE_ANIMATION_MAX_FPS`.
2. **Visual artifacts** — check for flicker, tearing, or load stalls at 0.5,
   1, 2, 4 FPS; record the highest artifact-free rate.
3. **Animated GIF support** — upload and display a small animated GIF; confirm
   whether firmware animates it natively (which would offload looping from the
   laptop) or shows only the first frame.
4. **List/animation endpoints** — probe any image/animation list endpoint on
   v2.0.2 for native sequence support; record availability (expected: none).
5. **Resource impact** — confirm rapid display calls do **not** degrade
   keyphrase/mic (consistent with the Snapdragon 410 cautions in
   `docs/lessons-learned.md`); abort animation if any audio regression appears.

**Gate**: animation defaults to enabled-capable only for FPS/format results the
probe confirms artifact-free and audio-safe. Results are recorded in this doc
(a "Validation results" subsection) or a linked issue comment. Until then,
`USE_FACE_ANIMATION=false` remains the default.

## 8. Rollout plan

1. **Phase 0 (this doc)** — design + asset boundary + validation plan. *(deliverable for #73)*
2. **Phase 1** — implement the standalone hardware probe (`tools/face_display_probe.py`); record results.
3. **Phase 2** — implement `FaceAnimator` (single-frame specs = today's behavior), flag default off, disable-regression test.
4. **Phase 3** — enable multi-frame loops only for validated states; tune FPS/frames from probe data.
5. **Phase 4** — optional original custom frame set (still no BMO assets).

Phases 1–4 are separate follow-up issues; each is independently revertable.

## 9. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| REST frame pushes degrade Snapdragon 410 audio (#22) | Validation step §7.5 gates on no audio regression; `FACE_ANIMATION_MIN_INTERVAL_S` rate-limit; default off |
| Low achievable FPS makes animation look choppy | Use small frame counts / slow loops; fall back to static if below threshold |
| Animator thread interferes with shutdown | Stop+join before existing cleanup; daemon thread; bounded timeout (§6.3) |
| Accidental inclusion of BMO assets | Normative asset boundary (§2); review checklist item; only `e_*.jpg` or original frames committed |
| Feature regresses critical paths | Default off; display-only; disable-regression test (§6.4) |

## 10. Validation results (Phase 1 — 2026-06-29)

Hardware probe run via `tools/face_display_probe.py` against Misty at
`10.0.0.23`, firmware v2.0.2.

### 10.1 Frame rate (Test 1)

| Metric | Value |
|--------|-------|
| Frames tested | 60 (max speed, 0 errors) |
| Achieved FPS | **9.94** |
| Latency p50 | 94.3 ms |
| Latency p95 | 119.9 ms |
| Latency p99 | 268.0 ms |
| Min / Max | 77.6 ms / 268.0 ms |

### 10.2 Visual artifacts (Test 2)

All target FPS levels ran artifact-free with 0 errors:

| Target FPS | Achieved FPS | Frames | Artifacts |
|-----------|-------------|--------|-----------|
| 0.5 | 0.5 | 4 | None observed |
| 1.0 | 1.0 | 8 | None observed |
| 2.0 | 2.1 | 17 | None observed |
| 4.0 | 4.0 | 32 | None observed |

### 10.3 Animated GIF support (Test 3)

**✅ Natively supported.** A 2-frame programmatic GIF (red/blue, 500ms per
frame) was uploaded and displayed — Misty's firmware looped the animation
on-device without companion-side frame pushing. This opens a second animation
strategy: upload GIFs once, let firmware loop them, reducing REST traffic.

### 10.4 Native animation endpoints (Test 4)

| Endpoint | Status | Notes |
|----------|--------|-------|
| `GET /api/images/list` | 200 ✅ | 46 images stored |
| `GET /api/images` | 400 | Requires FileName param |
| `GET /api/animations` | 404 | Not available |
| `GET /api/animations/list` | 404 | Not available |
| `GET /api/display/settings` | 404 | Not available |
| `GET /api/display` | 404 | Not available |

No native sequence/animation API exists. Animation must be driven via repeated
`/api/images/display` calls or native GIF looping.

### 10.5 Audio regression (Test 5)

   Skipped in this run (--skip-audio). The probe attempted a legacy, best-effort
   check of Misty's built-in keyphrase endpoint, but that path is unsupported in
   this repository. The supported wake-word path remains laptop-side OpenWakeWord
   with a custom "Hey Misty" model.

   Because Test 5 was not executed in this run, the audio regression gate should be
   recorded as a partial pass rather than a full pass. The display-validation
   results remain useful for the REST/GIF animation work, but they do not by
   themselves confirm the unsupported on-robot keyphrase path.

   ### 10.6 Recommendation

   ```
   FACE_ANIMATION_MAX_FPS = 4.0
   FACE_ANIMATION_MIN_INTERVAL_S = 0.25  (250ms, well above p95 latency)
   ```

   Two animation strategies are available for Phase 2:
   1. **REST frame loop** — companion pushes frames at ≤4 FPS. Best for
   dynamic/reactive animations that change based on real-time state.
   2. **Native GIF upload** — upload animated GIF once per state, firmware loops
   it on-device. Best for static looping animations (idle blink, etc.) with
   zero ongoing REST traffic.

   **Partial pass.** Hardware validation confirms animation is viable at up to
   4 FPS via REST and natively via GIF, but the audio regression gate remains
   incomplete until Test 5 is executed and validated separately.

---

## 11. Acceptance-criteria mapping

| #73 acceptance criterion | Where satisfied |
|--------------------------|-----------------|
| A feature design exists for state-driven face animation | §1, §4, §5 |
| Design includes safe fallback to current static images | §5.2 (`static_fallback`), §6.1 |
| Design includes a hardware validation step for frame rate and supported animation formats | §7, §10 (results) |
| Animation can be disabled without affecting wake word, recording, playback, movement safety, shutdown cleanup | §5.4 (flag), §6.2, §6.3, §6.4 |
| Documentation notes BMO assets are not included or copied | §2 (asset boundary), §9 |

---

## 12. Persistent, replaceable, emotion-aware face (#116)

Issue #116 builds on the custom face system merged in #110 / PR #115. It makes
the custom face identity persistent (never silently reverting to a firmware
face), centralizes display through one resolver, adds an explicit face
replacement workflow, and adds optional emotion-aware talking head motion.

### 12.1 Single resolver / always-available `FaceAnimator`

The controller now **always constructs** a `FaceAnimator`. `USE_FACE_ANIMATION`
scopes only the continuous frame-loop thread: when it is `false`, the thread is
not started, but `set_state()`, `set_emotion()`, and `show_asset()` still resolve
and push the correct frame synchronously. This guarantees custom face identity,
emotion selection, and built-in fallback work regardless of the flag.

All controller face changes route through the resolver:

- State-driven faces use `set_state()` / `set_emotion()` (already wired through
  `MistyController.set_state()` / `try_set_state()`).
- One-off / transient faces (movement acknowledgment, error blips) use
  `MistyController.show_face(filename)` → `FaceAnimator.show_asset()`, which
  applies fallback but does **not** change the animation state.

No controller path calls `display_image("face_*")` directly any more.

### 12.2 Deterministic built-in fallback

When required assets are missing locally, an upload fails, the inventory is
unreliable, or a custom display would fail, the controller marks custom faces
unavailable (`set_custom_faces_available(False)`). Every custom asset then
resolves to a built-in firmware face (`e_*.jpg`) via
`face_animator.ASSET_BUILTIN_FALLBACK` / `builtin_fallback_for_asset()`. Built-in
faces ship with every Misty II, so a display never fails on a missing file, and
the fallback is display-only (no per-frame error logging).

### 12.3 Face replacement workflow

Custom face assets are uploaded at startup by `ensure_face_assets()`, controlled
by two settings (see `config_defaults.py` / `.env.example`):

| Setting | Values | Effect |
|---------|--------|--------|
| `FACE_ASSETS_DIR` | path | Directory holding the `face_*` assets to upload. Point at a new folder that reuses the required filenames to swap the face. |
| `FACE_ASSETS_SYNC_MODE` | `missing` (default) / `overwrite` | `missing` uploads only assets not already on the device (idempotent startup). `overwrite` force re-uploads every required asset even if a same-named file exists. |
| `FACE_ASSETS_FORCE_UPLOAD` | `true` / unset | Convenience alias that forces `overwrite` for a single run. |

**To replace the face:** put the new assets (same required filenames) in
`FACE_ASSETS_DIR`, set `FACE_ASSETS_SYNC_MODE=overwrite` (or
`FACE_ASSETS_FORCE_UPLOAD=true`), start the controller once so the new assets
overwrite Misty's stored images, then set the mode back to `missing` so normal
startup stays idempotent.

### 12.4 Emotion-aware talking head motion

`USE_TALKING_HEAD_MOTION` (default `false`) enables subtle, emotion-aware head
motion while Misty speaks. Implemented by `talking_head_motion.TalkingHeadMotion`:

- Starts only for state `PLAYING` (normal responses, follow-ups, movement
  acknowledgments, and movement-failure speech) and stops + re-centers the head
  when playback ends.
- `MistyController.set_state()` / `try_set_state()` stop the motion on **any**
  transition away from `PLAYING`, so it never runs during
  `MOVING`/`CHARGING`/`ERROR`/reboot/re-arm; `_shutdown()` also stops it.
- All movements stay within a safe head envelope (configurable in
  `config_defaults.py`) and are hard-clamped to Misty's mechanical limits
  (pitch -40..26, roll -40..40, yaw -81..81).
- It only ever issues `/api/head` commands — never drive/arm/audio/keyphrase —
  keeping it decoupled from movement/drive safety.

### 12.5 Verification (cloud-safe)

`tests/test_face_resolver.py` covers the resolver, `show_asset`, disabled-animation
identity/fallback, and `ensure_face_assets` sync modes.
`tests/test_talking_head_motion.py` covers gating, safe-envelope clamping, emotion
scaling, and stop/center behavior. Live visual tuning on Misty hardware is a
separate manual step and is not required for these unit tests.

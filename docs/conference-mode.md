# Conference Mode — scripted Misty stage dialog

Issue: [#128](https://github.com/tammym-demos/misty_upgrade/issues/128)
Module: `src/windows-orchestration/conference_mode.py`
Tests: `tests/test_conference_mode.py`

## Purpose

Conference Mode lets Misty participate in an on-stage scripted dialog (for
example [`talks/20260710-2.md`](../talks/20260710-2.md)) by playing
**predetermined** audio cues instead of routing each scripted Misty line through
the live STT → LLM → TTS conversation path. The presenter speaks naturally and
Misty plays the next predetermined cue once the presenter finishes speaking,
with manual override controls available at all times for stage safety.

## Design principles

- **Companion-side only.** Misty remains a physical I/O endpoint. All parsing,
  preparation, cue selection, and control logic run on the Windows companion
  laptop. Misty runs no inference or on-robot conference logic
  ([ADR-001](ADR-001-companion-device-over-onrobot-inference.md)).
- **Opt-in and isolated.** Gated by `CONFERENCE_MODE_ENABLED` (default off) and
  implemented as a self-contained module. Normal wake-word conversation behavior
  in `misty_controller.py` is unchanged when the mode is off.
- **Deterministic and testable.** Script parsing, cue-ID assignment, manifest
  generation, and the control state machine are pure companion-side logic. All
  hardware/live dependencies (Misty playback, presenter voice-activity
  detection, Foundry Local TTS) are injected callables, so the logic is fully
  unit-testable without a robot, Foundry Local, or Windows audio.
- **No LLM at showtime.** Runtime never invokes the LLM for a scripted cue unless
  `CONFERENCE_LLM_FALLBACK` is explicitly enabled *and* a cue's predetermined
  audio is missing.

## Talk-script format

The parser reads Markdown talk scripts with:

- slide headers: `### **Slide 1: Title Slide**` (and variants like
  `### **Slide 6 & 7: ...**`);
- speaker lines: `**[You]:** ...` and `**[Misty]:** ...`;
- inline `[cite: 1, 2]` markers, which are stripped from spoken text.

Every `**[Misty]:**` line becomes a **cue** with a stable, deterministic ID
`slide{NN}-misty{MM}`, where `NN` is the sequential slide number (order of slide
headers, 1-based) and `MM` is the Misty-line index within that slide (1-based).
Misty lines before any slide header get slide sequence `00`.

## Workflow

```powershell
cd src/windows-orchestration

# Preview the ordered cue plan (no Misty, no Foundry required)
python conference_mode.py dry-run --script ../../talks/20260710-2.md

# Generate/import/reuse predetermined WAVs and write a manifest.
# Uses the orchestration /api/tts endpoint; reuses cached cues on repeat runs.
python conference_mode.py prepare --script ../../talks/20260710-2.md

# Confirm every cue has playable predetermined audio before showtime
python conference_mode.py verify

# Live interactive stage runner (requires Misty hardware + orchestration)
python conference_mode.py run
```

### Preparation and the manifest

`prepare` resolves each cue's audio in this order:

1. **recorded** — if `--recorded <dir>` contains `{cue_id}.wav`, use it as-is.
2. **reuse** — if a previously generated `{cue_id}.wav` exists and its sidecar
   `{cue_id}.wav.sha256` still matches the cue text hash, keep it.
3. **generate** — otherwise synthesize via the injected TTS backend (live: the
   orchestration `/api/tts` endpoint) and write the WAV plus its hash sidecar.

It writes `conference_manifest.json` mapping each cue ID to its text, asset
source (`generated`/`recorded`), local WAV path, duration, text hash, and the
optional on-Misty filename (`conf_{cue_id}.wav`) used when audio is pre-uploaded
to the robot.

### Control surface

`ConferenceController` is the state machine used by the runner:

| Control | Method | Behavior |
|---|---|---|
| enable/disable | `enable()` / `disable()` | When disabled, all playback/advance calls are strict no-ops. |
| start | `start()` | Arms the runner (no-op + `False` when disabled). |
| play next | `play_next()` | Plays the next predetermined cue. Works even while paused (manual override). |
| replay | `replay()` | Replays the most recent cue. |
| previous | `previous()` | Steps back one cue and plays it. |
| jump to slide | `jump_to_slide(key)` | Repositions to a slide by sequence number or label/title substring. |
| pause / resume | `pause()` / `resume()` | Pauses/resumes auto-advance. |
| auto-advance | `auto_advance_once()` / `run_auto()` | Waits for the presenter to finish speaking (injected VAD), then plays the next cue; respects pause/stop. |
| safe shutdown | `shutdown()` / `stop()` | Releases audio → stops recording → cancels skills → halts movement → returns Misty to rest. Idempotent and hook-failure isolated. |

## Verification

Cloud-safe verification (no hardware, Foundry, or Windows audio):

```powershell
python -m py_compile src/windows-orchestration/conference_mode.py
python -m pytest tests/test_conference_mode.py -q
python src/windows-orchestration/conference_mode.py dry-run --script talks/20260710-2.md
```

## Manual on-stage validation (hardware/live, not cloud-verifiable)

The following require the physical Misty, Foundry Local, and a live stage audio
environment, and must be validated during rehearsal:

- Live TTS generation of cue audio via the orchestration `/api/tts` endpoint.
- Actual cue playback on Misty (`upload_and_play_audio`) and, if used, validating
  pre-uploaded on-Misty audio filenames/storage.
- Real presenter voice-activity detection in the room (audience noise, applause,
  fan noise, room speakers) driving auto-advance thresholds.
- End-to-end safe shutdown on the physical robot (audio/recording/skills/movement
  release and return to rest state).

# Copilot Instructions — Misty II + Foundry Local

## Architecture

This is a two-device conversational AI system for a Misty II robot:

- **Misty II robot**: Audio I/O hardware — microphone, speakers, LED, display. Controlled entirely via REST API + WebSocket from the companion device. No on-robot code runs (the JavaScript skill runtime is unreliable — see [Misty SDK Gotchas](#misty-sdk-gotchas)).
- **Windows companion device** (`src/windows-orchestration/`): Runs two Python processes:
  - `orchestration_service.py` — Flask service for the STT → LLM → TTS inference pipeline
  - `misty_controller.py` — WebSocket + REST controller that drives Misty (wake word events, recording, audio upload/playback, LED/display state)

The robot's hardware (Snapdragon 820 + 410, 2 GB RAM) cannot run inference — 2 GB RAM is the binding constraint. All AI workload runs on the companion laptop via **Foundry Local** (local OpenAI-compatible model server). See `docs/ADR-001-companion-device-over-onrobot-inference.md` for the full decision record.

### Pipeline flow

```
[Misty Controller]                           [Orchestration Service]
  Laptop mic → openWakeWord (optional)           Foundry Local
  OR WebSocket ← KeyPhraseRecognized               ├─ STT (faster-whisper)
  REST → StartRecordingAudio (6s)                  ├─ LLM (Phi-3.5-mini)
  REST → GetAudio (base64)                         └─ TTS (Kokoro / pyttsx3)
  HTTP POST /api/orchestrate ───────────────────►
  HTTP GET /api/audio/<file> ◄──────────────────
  REST → SaveAudio (base64, ImmediatelyApply)
  ┌─ Follow-up: listen 5s, send to orchestrate
  │  Speech detected? → repeat (up to 90s, max 12 turns)
  │  Silence? → fall through to re-arm
  └→ REST → StartKeyPhraseRecognition (re-arm)
      + Resume laptop wake word listener
```

### Locked model stack (v1)

| Role | Model | Notes |
|------|-------|-------|
| Chat | phi-3.5-mini | 3.8B params, uses full model ID `Phi-3.5-mini-instruct-openvino-gpu:2` |
| STT | whisper-tiny | Uses full model ID `openai-whisper-tiny-generic-cpu:3` |
| TTS | kokoro-onnx (primary) | Neural, offline; falls back to pyttsx3 SAPI5 |

## Branch Protection & Code Review

- **Never push directly to `main`.** All changes must go through a pull request.
- **All PRs require a code review** from the repository owner before merging.
- When working on changes, create a feature branch and open a PR.

## Build & Run

```powershell
# 1. Start Foundry Local (must be running before orchestration service)
pip install foundry-local
foundry

# 2. Install and start the orchestration service
cd src\windows-orchestration
pip install -r requirements.txt
python orchestration_service.py

# 3. Start the Misty controller (in a separate terminal)
python misty_controller.py

# 4. Health check
curl http://localhost:5000/api/health
```

**Foundry Local** runs on a dynamic port (not 5000). The orchestration service auto-discovers it via `foundry service status` and strips the path to get the base URL. Override with `FOUNDRY_LOCAL_HOST` env var if needed.

**Misty controller** connects to Misty via WebSocket (`ws://<MISTY_IP>/pubsub`) for wake word events and REST API for all commands. Configure via env vars: `MISTY_IP` (default: `10.0.0.44`), `ORCHESTRATION_URL` (default: `http://10.0.0.58:5000`).

## Tests

```powershell
# Run the full integration test suite
cd tests
python -m pytest test_integration.py -v

# Run a single test class
python -m pytest test_integration.py::TestWindowsOrchestration -v

# Run a single test
python -m pytest test_integration.py::TestWindowsOrchestration::test_health_check -v
```

Tests require live services (orchestration service, Foundry Local, and optionally Misty on the network). Configure via environment variables: `MISTY_HOST`, `WINDOWS_HOST`, `FOUNDRY_LOCAL_HOST`.

### Services & Startup Checks

The system uses three services. They must be started in this order:

| # | Service | Port | Health check | Required by |
|---|---------|------|-------------|-------------|
| 1 | **Foundry Local** | Dynamic (e.g., 64722) | `foundry service status` / `foundry service ps` | Orchestration service, Foundry tests |
| 2 | **Orchestration service** | 5000 | `curl http://localhost:5000/api/health` | Orchestration tests, Misty controller |
| 3 | **Misty controller** | — (outbound only) | Misty LED turns green | End-to-end interaction |

**Foundry Local** runs on a dynamic port — never hardcode it. Both the orchestration service and the test suite auto-discover it by running `foundry service status` and parsing the URL. Override with the `FOUNDRY_LOCAL_HOST` env var.

**Model management:** Only `phi-3.5-mini` should be loaded (`foundry service ps`). Whisper-tiny runs in-process via faster-whisper (not through Foundry). If stray models are present from prior testing (e.g., phi-4-mini), unload them with `foundry model unload <alias>` to free resources.

**TTS (Kokoro-ONNX)** is **not** a Foundry model. It runs as a standalone Python library inside the orchestration service process, with pyttsx3 (Windows SAPI5) as fallback. TTS status is reported in `/api/diagnostics` under the `tts` key, separate from the Foundry `models` dict.

### Test prerequisites by class

| Test class | Requires |
|-----------|----------|
| `TestWindowsOrchestration` | Orchestration service |
| `TestFoundryLocalIntegration` | Foundry Local (auto-discovered or `FOUNDRY_LOCAL_HOST` env var) |
| `TestMistyConnectivity` | Misty robot on the network |
| `TestLatencySLO` | Orchestration service |
| `TestVerificationChecklist` | Varies — some skip automatically |

## Misty REST API Reference

Key endpoints used by the controller (all at `http://<MISTY_IP>/api/`):

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/audio/keyphrase/start` | Start listening for "Hey Misty" (no params) |
| POST | `/api/audio/keyphrase/stop` | Stop listening for wake word |
| POST | `/api/audio/record/start` | Start recording (`{"FileName": "name.wav"}`) |
| POST | `/api/audio/record/stop` | Stop recording |
| GET | `/api/audio?FileName=X&Base64=true` | Get recorded audio as base64 |
| POST | `/api/audio` | Upload + play audio (`{"FileName", "Data" (base64), "ImmediatelyApply": true}`) |
| POST | `/api/led` | Set LED color (`{"red", "green", "blue"}` 0-255) |
| POST | `/api/images/display` | Change face display (`{"FileName": "e_Joy2.jpg"}`) |
| POST | `/api/reboot` | Reboot Misty (`{"Core": true, "SensoryServices": true}` — both params required) |
| GET | `/api/device` | Device info / health check |
| GET | `/api/battery` | Battery status (`chargePercent`, `isCharging`, `healthPercent`, `temperature`) |
| GET | `/api/audio/list` | List audio files stored on Misty |
| GET | `/api/skills/running` | List running on-robot skills |
| POST | `/api/skills/cancel` | Cancel all running skills |
| GET | `/api/skills` | List all installed skills (metadata only, no source code) |
| DELETE | `/api/skills?Skill=<uniqueId>` | Delete an installed skill |
| POST | `/api/head` | Move head (`Pitch` -40↑ to 26↓, `Roll` -40 to 40, `Yaw` -81→ to 81←, `Velocity`) |
| POST | `/api/arms` | Move arms (`LeftArmPosition`/`RightArmPosition` -29↑ to 90↓, `Velocity`) |

**WebSocket**: `ws://<MISTY_IP>/pubsub` — subscribe to `KeyPhraseRecognized`, `BatteryCharge`, etc.

### On-Robot Skills — Cleaned Up

All auto-starting on-robot skills have been **deleted** from Misty to prevent interference with the companion-device pipeline. The `faceDetection` skill (which had `StartupRules: ["Startup", "Robot"]`) was the primary offender — it grabbed the microphone on boot and caused silent keyphrase failures. Four other skills with `Robot` startup rules were also removed.

Skill metadata was backed up to `misty-skills-backup/` before deletion. Skills cannot be restored from metadata alone — they would need to be redeployed from source.

The controller still calls `_cancel_all_skills()` on WebSocket connect as a safety net, but no skills should auto-start after a reboot now.

### Misty SDK Gotchas (historical reference)

> **We abandoned the on-robot JavaScript skill approach** in favor of REST+WebSocket control from the laptop. The skill runtime is fragile and poorly documented. These notes are preserved for reference.

- **SkillImageUri: null crashes skills silently.** Remove the field entirely from the JSON metadata.
- **BroadcastMode** must be string `"off"`, not boolean `false`.
- **StartupRules**: `["Robot"]` auto-starts on boot and conflicts; use `["Manual"]`. All auto-start skills have been deleted from Misty.
- **Rapid deploy/cancel cycles break the skill runtime.** Reboot Misty to recover.
- **Skills exit the "running" list** after top-level code executes but still process events. This looks like a crash but is normal.
- **StartKeyPhraseRecognition inside skills crashes** — use REST API instead.
- **Event callbacks** must be `_eventName()` (underscore prefix matching registered name).
- **Skill log API** (`/api/skills/log`) returns 404 — not functional on firmware v2.0.2.

### Misty Keyphrase Behavior

- After Misty recognizes "Hey Misty", **keyphrase listening auto-stops**. You must re-issue `StartKeyPhraseRecognition` to listen again.
- **Recording auto-stops keyphrase** — they cannot run simultaneously (shared mic on Snapdragon 410).
- **Always stop-then-start keyphrase** when re-arming (stale state causes silent failures).
- Use a **2-second delay** between stop and start calls (`start_keyphrase(force_restart=True)`). 1s was unreliable.
- **⚠️ NEVER use sensory-only reboots** (`POST /api/reboot {"SensoryServices": true, "Core": false}`). They permanently break Misty's microphone until physical power cycle. See #33.

#### WebSocket Subscription Gotchas

- **Stale subscriptions persist globally**: When a controller process is killed, its WebSocket event subscriptions remain active on Misty. A new controller cannot register the same event name → "Cannot register an event with same name" → events silently stop. **Fix**: Use unique timestamped event names (e.g., `WakeWord_{unix_timestamp}`).
- **Unsubscribe only works on the creating connection**: You cannot unsubscribe events from a different WebSocket connection than the one that created them.
- **DebounceMs must be 0**: With `DebounceMs=250`, keyphrase events were being swallowed. Use `DebounceMs=0` for reliable delivery.
- **Registration status uses the same eventName**: `"Registration Status: API event registered."` messages arrive with the same `eventName` as real events. Filter by checking if `message` is a string containing "Registration Status".

#### Re-Arm Strategy

After each conversation ends, `_rearm()` performs:
1. Stop recording + stop keyphrase (cleanup)
2. **5-second audio cooldown** — lets Snapdragon 410 release hardware resources
3. **Full WebSocket reconnect** — close connection, wait 1s, create fresh connection + subscriptions
4. `start_keyphrase(force_restart=True)` — stop→2s delay→start via `_on_ws_open()`

This mimics a fresh controller start on every re-arm, which is the most reliable approach found.

#### Silent Keyphrase Failure (#22) — Known Issue

The Snapdragon 410 sensory services silently stop firing `KeyPhraseRecognized` WebSocket events after ~2 conversation cycles, while the REST API still returns "Success" for `keyphrase/start`. Battery events continue flowing on the same WebSocket (proving the connection is healthy). The mic itself works (direct recording produces real audio). Only the keyphrase detection engine fails.

**Confirmed root causes (all fixed):**
- Stale WebSocket subscriptions from killed controllers (unique event names)
- DebounceMs=250 swallowing events (changed to 0)
- Missing stop-before-start on keyphrase re-arm (force_restart=True)
- Mic health check recording while keyphrase active (false positive empty recordings)

**Unresolved firmware-level issue:**
- The keyphrase engine appears to suffer resource exhaustion after multiple record/play/keyphrase cycles
- No API exists to query keyphrase engine health — `keyphrase/start` always returns "Success"
- Only reliable recovery is a **full Core+Sensory reboot** (~60-90s downtime)
- A **keyphrase watchdog** auto-detects and escalates:
  1. **Soft reset** (after 90s idle): yellow LED → cancel skills → force-restart keyphrase
  2. **Second soft reset** (+60s): darker yellow LED → repeat
  3. **Full reboot** (+60s): red LED → `POST /api/reboot {"Core": true, "SensoryServices": true}`

**Future alternatives under consideration:**
- **openWakeWord on companion laptop** (MIT, no signup needed): Works with Misty's mic via REST polling but accelerates Snapdragon 410 mic degradation (RMS drops to 0 after ~100 poll cycles). **Next approach: use the laptop's own microphone** for wake word detection, only using Misty's mic for the actual 6s conversation recording. Code preserved in git tag `wake-engine-experiment`. See #44.
- Picovoice Porcupine: Requires commercial email signup — blocked for personal/hobbyist use.
- Touch-based trigger using Misty's capacitive sensors

### Proactive Reboot (#22 Mitigation)

Since the keyphrase engine reliably fails after ~2 conversation cycles, the controller now performs a **proactive reboot before failure occurs**:

1. After `PROACTIVE_REBOOT_AFTER_CYCLES` successful conversations (default: 2), the controller triggers a reboot instead of a normal re-arm
2. Misty announces *"I need a quick reset. Be right back!"* via TTS
3. Full Core+Sensory reboot is issued (~60-90s downtime)
4. Controller polls `/api/device` until Misty is back, then reconnects WebSocket and re-arms keyphrase
5. Cycle counter resets to 0

The proactive reboot is skipped if battery is critically low (<10%). Configure via `PROACTIVE_REBOOT_AFTER_CYCLES` env var.

This is a **UX-aware workaround** — the user knows Misty is rebooting rather than experiencing mysterious silence. The watchdog remains as a safety net for unexpected failures between reboots.

## Foundry Local API

Foundry Local uses OpenAI-compatible endpoints but with some quirks:

- **Base URL**: Auto-discovered from `foundry service status`. The CLI reports a URL like `http://127.0.0.1:64722/openai/status` — strip the path component, use only `http://127.0.0.1:<port>`.
- **Chat**: `POST /v1/chat/completions` — works with full model ID (e.g., `Phi-3.5-mini-instruct-openvino-gpu:2`)
- **Models list**: `GET /openai/models` — returns array of model ID strings
- **STT**: Foundry Local does **NOT** expose a REST endpoint for Whisper. We use `faster-whisper` (CTranslate2) locally in Python instead.
- **No `/openai/v1/` prefix** — use `/v1/` directly for inference endpoints

## Misty Hardware Notes

- **Processors**: Qualcomm Snapdragon 820 (main) + Snapdragon 410 (sensory services). 2 GB RAM (soldered, not upgradeable).
- **Battery**: 10,200 mAh, 8.4V Li-ion. ~2.2 hours at max speed, up to 10 hours idle. Abruptly powers down at ~7V (no graceful shutdown).
- **Charging**: Two methods — wireless pad (~6-7 hours full charge) and direct wired via port on bottom near power switch (~3-4 hours, ~2× faster). Different barrel jack sizes; each has its own adapter. Robot does NOT need to be on to charge.
- **Tally light**: Blue LED on side of head indicates camera/mic is active (PII collection indicator). Turns off when keyphrase/recording is stopped. **Important**: If the controller exits uncleanly, the tally light stays on — you must explicitly `POST /api/audio/keyphrase/stop` and `POST /api/audio/record/stop` to turn it off.
- **Battery monitoring**: `GET /api/battery` returns chargePercent, healthPercent, isCharging, voltage, temperature. At very low charge (~5%), mic and keyphrase **silently fail** — APIs return success but produce no data. Minimum ~10% recommended for operation.
- **Shutdown procedure**: Before powering off Misty or ending a session: (1) Stop keyphrase: `POST /api/audio/keyphrase/stop` (2) Stop recording: `POST /api/audio/record/stop` (3) Cancel skills: `POST /api/skills/cancel` (4) LED off: `POST /api/led {"red":0,"green":0,"blue":0}`. This ensures the tally light goes off and hardware resources are released. The controller's `_shutdown()` method does this automatically on clean exit.
- **Power saving**: When not in use, stop keyphrase, cancel skills, and turn LED off (`{"red":0,"green":0,"blue":0}`) to reduce power draw and charge faster. Note: `/api/services` endpoint is not functional on firmware v2.0.2 — cannot disable individual sensor services via API.
- **Fans**: Run continuously — firmware-controlled with no API or user override.
- **Firmware**: v2.0.2.140 / robot OS 2.0.2.11660. Misty Robotics was acquired by Furhat Robotics; no further firmware updates expected. This is the final firmware.

## Key Conventions

- **Latency SLO**: p50 < 3s, p95 < 6s end-to-end (aspirational — currently achieving ~23s, see #21). The orchestration service logs per-stage timing: `[Pipeline Xms] STT=X LLM=X TTS=X history=N`. Measured breakdown: STT ~420ms, LLM ~1200ms, TTS ~6000ms. TTS scales linearly with response length — keep `max_tokens` moderate (currently 40).
- **Misty controller state machine**: DISCONNECTED → IDLE → RECORDING → PROCESSING → PLAYING → [LISTENING → PROCESSING → PLAYING →]* REARMING (soft re-arm ~3s) → IDLE. After every `PROACTIVE_REBOOT_AFTER_CYCLES` (default 2) successful conversations: → REBOOTING (announce → reboot → poll → reconnect) → IDLE. After each response, enters LISTENING state (cyan LED) for up to 90s of follow-up conversation (max 12 turns) without requiring wake word. Silence ends the loop. Re-arm uses soft reset only (WS re-subscribe + keyphrase restart). IDLE ↔ CHARGING (auto-enters at 10% battery, exits at 25%+charging). All state transitions are logged.
- **Wake word detection**: Two modes (configurable via `USE_LAPTOP_WAKE_WORD` env var):
  - **Laptop mic** (recommended): Uses `sounddevice` + openWakeWord on the companion laptop. Zero Misty mic usage for wake detection. Auto-pauses during conversation (self-wake prevention). Enable with `USE_LAPTOP_WAKE_WORD=true`.
  - **Misty keyphrase** (default/fallback): Built-in "Hey Misty" via Snapdragon 410. Subject to silent failure after ~2 cycles (#22). Watchdog auto-recovers.
- **LED color scheme**: 🟢 Green = ready/idle, 🟠 Orange = recording, 🔵 Blue = processing, 🟣 Purple = playing response, 🩵 Cyan = follow-up listening, 🟡 Yellow = watchdog soft reset / low battery warning, ⚫ Off = charging mode, 🔴 Red = error / watchdog full reboot.
- **Keyphrase watchdog**: Detects silent keyphrase failure (Snapdragon 410 bug, see #22) and auto-recovers with 3-level escalation: soft reset (90s) → second soft reset (+60s) → full reboot (+60s). **Never uses sensory-only reboot** (permanently breaks mic, see #33). Only active in IDLE state. Configurable via `WATCHDOG_IDLE_TIMEOUT_S` and `WATCHDOG_ESCALATE_TIMEOUT_S` env vars. Health check runs every 10s.
- **TTS fallback chain**: Kokoro-ONNX is primary TTS (speed 1.4x). If unavailable, pyttsx3 (Windows SAPI5) is used as fallback. Both are lazily initialized. The API response includes `"ttsFallback": true` when fallback is used.
- **Conversation history**: Maintained in-memory, capped at the last 8 messages (4 turns). System prompt is prepended on every call but not stored in history. Context budget: MAX_CONTEXT_CHARS=5000. See #19 for smarter history approaches.
- **System prompt**: Instructs Misty to reply in 1-2 sentences, ~20 words. LLM (Phi-3.5-mini) often exceeds this — `max_tokens=40` and post-LLM truncation (25 words / 2 sentences) enforce the limit.
- **Configuration**: The orchestration service reads from `.env` (copy `.env.example`). The Misty controller reads `MISTY_IP`, `ORCHESTRATION_URL`, and `USE_LAPTOP_WAKE_WORD` from environment.
- **Error responses**: The orchestration service returns structured JSON errors with a `status` field (`"ok"` or `"error"`) and an `error` code (e.g., `"timeout"`, `"stt_failure"`, `"model_load_failure"`).
- **Python version**: Use Python 3.13. Note that Python 3.14 may also be installed — always use `python -m pip` to target the correct version.
- **Official docs**: https://docs.mistyrobotics.com/ — REST API reference at `/misty-ii/reference/rest/`

## Known Issues

| Issue | Summary | Status |
|-------|---------|--------|
| #33 | **CRITICAL**: Sensory-only reboot permanently breaks mic until physical power cycle | Closed — sensory reboot removed from all code paths |
| #44 | Replace Misty keyphrase with laptop-based wake word (openWakeWord) | In progress — laptop mic listener implemented, enable with `USE_LAPTOP_WAKE_WORD=true` |
| #22 | Keyphrase silently fails after ~2 conversation cycles — watchdog + proactive reboot recover. Laptop wake word bypasses this entirely. | Open |
| #28 | Keyphrase re-arm: sensory reboot approach **abandoned** (breaks mic, see #33) | Closed — reverted to soft re-arm |
| #27 | STT accuracy: beam_size=5 + VAD applied, whisper-tiny still garbles follow-ups | Open |
| #21 | End-to-end latency ~2s follow-ups, ~5s first turn (TTS cold start) — target <3s | Improved |
| #24 | LLM ignores brevity — max_tokens=40, post-LLM truncation to 25 words/2 sentences | Mitigated |
| #20 | Recording increased from 4s to 6s (PR #42) — still fixed-duration, no VAD | Mitigated |
| #19 | Conversation history capped at 8 messages (4 turns) to manage latency | Mitigated |
| #23 | Unicode arrow in log messages crashes on Windows cp1252 console | Fixed |

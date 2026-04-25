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
  WebSocket ← KeyPhraseRecognized               Foundry Local
  REST → StartRecordingAudio (4s)                  ├─ STT (Whisper-tiny)
  REST → GetAudio (base64)                         ├─ LLM (Phi-3.5-mini)
  HTTP POST /api/orchestrate ───────────────────►  └─ TTS (Kokoro / pyttsx3)
  HTTP GET /api/audio/<file> ◄──────────────────
  REST → SaveAudio (base64, ImmediatelyApply)
  REST → StartKeyPhraseRecognition (re-arm)
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
| 1 | **Foundry Local** | Dynamic (e.g., 64722) | `foundry service status` | Orchestration service, Foundry tests |
| 2 | **Orchestration service** | 5000 | `curl http://localhost:5000/api/health` | Orchestration tests, Misty controller |
| 3 | **Misty controller** | — (outbound only) | Misty LED turns green | End-to-end interaction |

**Foundry Local** runs on a dynamic port — never hardcode it. Both the orchestration service and the test suite auto-discover it by running `foundry service status` and parsing the URL. Override with the `FOUNDRY_LOCAL_HOST` env var.

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

**WebSocket**: `ws://<MISTY_IP>/pubsub` — subscribe to `KeyPhraseRecognized`, `BatteryCharge`, etc.

### Misty SDK Gotchas

> **We abandoned the on-robot JavaScript skill approach** in favor of REST+WebSocket control from the laptop. The skill runtime is fragile and poorly documented. These notes are preserved for reference.

- **SkillImageUri: null crashes skills silently.** Remove the field entirely from the JSON metadata.
- **BroadcastMode** must be string `"off"`, not boolean `false`.
- **StartupRules**: `["Robot"]` auto-starts on boot and conflicts; use `["Manual"]`.
- **Rapid deploy/cancel cycles break the skill runtime.** Reboot Misty to recover.
- **Skills exit the "running" list** after top-level code executes but still process events. This looks like a crash but is normal.
- **StartKeyPhraseRecognition inside skills crashes** — use REST API instead.
- **Event callbacks** must be `_eventName()` (underscore prefix matching registered name).
- **Skill log API** (`/api/skills/log`) returns 404 — not functional on firmware v2.0.2.
- **Cancel all skills on startup** — the built-in `faceDetection` skill can interfere.

### Misty Keyphrase Behavior

- After Misty recognizes "Hey Misty", **keyphrase listening auto-stops**. You must re-issue `StartKeyPhraseRecognition` to listen again.
- **Recording auto-stops keyphrase** — they cannot run simultaneously.
- **Always stop-then-start keyphrase** when re-arming (stale state causes silent failures).
- Use a **1-second delay** between stop and start calls.

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
- **Tally light**: Blue LED on side of head indicates camera/mic is active (PII collection indicator). Turns off when keyphrase/recording is stopped.
- **Battery monitoring**: `GET /api/battery` returns chargePercent, healthPercent, isCharging, voltage, temperature. At very low charge (~5%), mic and keyphrase **silently fail** — APIs return success but produce no data. Minimum ~10% recommended for operation.
- **Power saving**: When not in use, stop keyphrase, cancel skills, disable unused sensor services, and turn LED off (`{"red":0,"green":0,"blue":0}`) to reduce power draw and charge faster.
- **Fans**: Run continuously — firmware-controlled with no API or user override. Reducing compute load (disabling unused services) is the only lever to reduce thermal output.
- **Firmware**: v2.0.2.140 / robot OS 2.0.2.11660. Misty Robotics was acquired by Furhat Robotics; no further firmware updates expected. This is the final firmware.

## Key Conventions

- **Latency SLO**: p50 < 3s, p95 < 6s end-to-end. The orchestration service enforces per-stage latency budgets (STT: 1500ms, LLM: 2000ms, TTS: 1500ms). Keep responses short (`max_tokens: 150`) to stay within budget.
- **Misty controller state machine**: DISCONNECTED → IDLE → RECORDING → PROCESSING → PLAYING → REARMING → IDLE. IDLE ↔ CHARGING (auto-enters at 10% battery, exits at 25%+charging). All state transitions are logged.
- **LED color scheme**: 🟢 Green = ready/idle, 🟠 Orange = recording, 🔵 Blue = processing, 🟣 Purple = playing response, 🟡 Yellow = low battery warning, ⚫ Off = charging mode, 🔴 Red = error.
- **TTS fallback chain**: Kokoro-ONNX is primary TTS. If unavailable, pyttsx3 (Windows SAPI5) is used as fallback. Both are lazily initialized. The API response includes `"ttsFallback": true` when fallback is used.
- **Conversation history**: Maintained in-memory, capped at the last 10 messages. System prompt is prepended on every call but not stored in history.
- **Configuration**: The orchestration service reads from `.env` (copy `.env.example`). The Misty controller reads `MISTY_IP` and `ORCHESTRATION_URL` from environment.
- **Error responses**: The orchestration service returns structured JSON errors with a `status` field (`"ok"` or `"error"`) and an `error` code (e.g., `"timeout"`, `"stt_failure"`, `"model_load_failure"`).
- **Python version**: Use Python 3.13. Note that Python 3.14 may also be installed — always use `python -m pip` to target the correct version.
- **Official docs**: https://docs.mistyrobotics.com/ — REST API reference at `/misty-ii/reference/rest/`

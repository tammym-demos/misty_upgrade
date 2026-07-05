---
name: misty-runtime
description: Use when working on Misty robot runtime, Windows orchestration, Foundry Local, wake word/audio, hardware safety, REST/WebSocket control, or live robot troubleshooting.
---

# Misty Runtime Skill

Use this skill for code or troubleshooting that touches Misty II, the Windows orchestration services, Foundry Local, wake word/audio, hardware safety, or live robot operation.

## Architecture

Two-device system:

- **Misty II robot**: physical I/O only — speakers, LED, display, movement, camera/mic/tally-light behavior. Controlled from the companion device via REST and WebSocket.
- **Windows companion device** (`src\windows-orchestration`):
  - `orchestration_service.py`: Flask STT -> LLM -> TTS pipeline.
  - `misty_controller.py`: Misty REST/WebSocket controller and state machine.
  - `wake_word_listener.py`: laptop microphone OpenWakeWord, recording, and VAD follow-up capture.

Pipeline:

```text
Laptop mic -> OpenWakeWord -> controller
Laptop mic recording -> /api/orchestrate
orchestration_service.py -> faster-whisper -> Phi-3.5-mini via Foundry Local -> Kokoro/pyttsx3 TTS
controller -> upload/play WAV on Misty -> follow-up listening -> re-arm laptop wake word
```

Misty cannot run inference: Snapdragon 820 + 410, 2 GB RAM, final firmware, degraded Snapdragon 410 ML/audio subsystem.

## Model stack

| Role | Model / implementation | Notes |
|---|---|---|
| Chat | `Phi-3.5-mini-instruct-generic-cpu:2` | Foundry Local, alias `phi-3.5-mini`. |
| STT | `openai-whisper-tiny-generic-cpu:3` | faster-whisper in-process, not Foundry REST. |
| TTS | Kokoro-ONNX | Primary offline neural TTS. |
| TTS fallback | pyttsx3 / SAPI5 | Windows fallback only. |

Foundry Local quirks:

- Runs on a dynamic port. Discover with `foundry service status` and strip `/openai/status` to get the base URL.
- Chat endpoint is `/v1/chat/completions`; models list is `/openai/models`.
- Do not use `/openai/v1/`.
- Keep only `phi-3.5-mini` loaded during normal operation; unload stray models to save resources.

## Wake word policy

- Supported path: laptop-side OpenWakeWord only.
- Target phrase: custom "Hey Misty" model configured by `OWW_CUSTOM_MODEL_PATH`.
- `OWW_MODEL_NAME` defaults to `hey_misty`; `OWW_THRESHOLD` defaults to `0.7`.
- Misty's built-in keyphrase path is unsupported and should not be treated as fallback.
- Keyphrase stop calls may remain for legacy cleanup/audio-resource release only.
- If laptop wake-word dependencies, mic access, or the custom model path are missing, fail fast with actionable guidance.

## Safety rules

- Never use sensory-only reboot: `{"SensoryServices": true, "Core": false}` can break the mic until physical power cycle.
- Safe reboot uses both: `POST /api/reboot {"Core": true, "SensoryServices": true}`.
- Before shutdown or power-off, release resources:
  1. `POST /api/audio/keyphrase/stop`
  2. `POST /api/audio/record/stop`
  3. `POST /api/skills/cancel`
  4. `POST /api/halt`
  5. `POST /api/images/display {"FileName":"face_idle.gif","Alpha":1}`
  6. `POST /api/led {"red":0,"green":0,"blue":0}`
- Treat this stop-cycle sequence as Misty's safe sleep/rest state. Do not call undocumented sleep/power endpoints unless they have been validated on the target firmware.
- At very low battery, mic/audio APIs can return success but produce no useful data. Keep operation above ~10%; movement has stricter battery cutoffs in code.

## Service startup and validation

At the start of every live robot session, resolve Misty's current IP before starting or testing services. Do not assume the default IP is current.

Preferred IP sources, in order:

1. `src\windows-orchestration\.env` if it contains `MISTY_IP=<ip>`.
2. `.misty-services.json` if a previous `npx . start` discovered and persisted `misty.ipAddress`.
3. An explicit user-provided IP, passed with `npx . start --misty-ip <ip>` or `MISTY_IP=<ip>`.
4. CLI auto-discovery from `npx . start`, which scans local private `/24` networks when the configured IP is stale.

Useful IP checks:

```powershell
# Read persistent local config, if present.
Select-String -Path src\windows-orchestration\.env -Pattern '^MISTY_IP\s*='

# Read the last auto-discovered IP, if present.
Get-Content .misty-services.json | ConvertFrom-Json | Select-Object -ExpandProperty misty

# Verify the resolved IP before live testing.
curl http://<MISTY_IP>/api/device
```

If Misty moved networks, prefer `npx . start --misty-ip <ip>` once; the CLI will persist the reachable robot in `.misty-services.json` for future sessions.

Manual startup:

```powershell
foundry
cd src\windows-orchestration
python -m pip install -r requirements.txt
python orchestration_service.py
python misty_controller.py
```

Health checks:

```powershell
foundry service status
foundry service ps
curl http://localhost:5000/api/health
curl http://localhost:5001/api/status
curl http://<MISTY_IP>/api/device
```

Tests:

```powershell
python -m pytest tests\test_integration.py -m "not live" -q
python -m pytest tests\test_integration.py::TestWakeWordConfiguration -q
python -m pytest tests\test_integration.py -m live -v
```

Only run live tests when the robot and services are actually available.

## Key controller behavior

State machine:

```text
DISCONNECTED -> IDLE -> RECORDING -> PROCESSING -> PLAYING
                      -> LISTENING -> PROCESSING -> PLAYING
                      -> REARMING -> IDLE
```

Additional states include `MOVING`, `CHARGING`, `REBOOTING`, and `ERROR`.

Follow-up listening:

- After each response, controller listens for follow-up speech without another wake word.
- Window defaults to 90 seconds, max 12 turns.
- Silence ends the loop and re-arms laptop OpenWakeWord.

LED scheme:

- Green: ready/idle or recording cue.
- Orange: wake/prep.
- Blue: processing.
- Purple: playing response.
- Cyan: follow-up listening.
- Yellow: warning/recovery.
- Off: charging/power saving.
- Red: error/reboot.

Latency:

- Logs use `[Pipeline Xms] STT=X LLM=X TTS=X history=N fallback=F cached=C`.
- TTS usually dominates latency and scales with response length.
- Keep `max_tokens` moderate; short mode currently targets 50 tokens / ~35 words.

## Misty REST endpoints commonly used

Base URL: `http://<MISTY_IP>/api/`

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/device` | Device health/info. |
| GET | `/api/battery` | Battery status. |
| POST | `/api/audio/record/start` | Start recording / tally light. |
| POST | `/api/audio/record/stop` | Stop recording. |
| POST | `/api/audio` | Upload/play WAV. |
| POST | `/api/led` | Set LED. |
| POST | `/api/images/display` | Set face image. |
| POST | `/api/skills/cancel` | Cancel running on-robot skills. |
| POST | `/api/halt` | Halt robot motion during stop/safety cleanup. |
| POST | `/api/reboot` | Full safe reboot with both Core and SensoryServices true. |
| POST | `/api/head` | Move head. |
| POST | `/api/arms` | Move arms. |

WebSocket: `ws://<MISTY_IP>/pubsub` for battery, hazard, ToF, bump, and other telemetry. Built-in `KeyPhraseRecognized` is historical/unsupported.

## Documentation references

- Main overview: `README.md`.
- Companion-device decision: `docs\ADR-001-companion-device-over-onrobot-inference.md`.
- Non-blocking audio pattern: `docs\ADR-002-non-blocking-audio-pattern.md`.
- Historical keyphrase failure notes: `docs\keyphrase-debugging.md`.
- Hardware degradation lessons: `docs\lessons-learned.md`.
- Foundry setup: `docs\FOUNDRY_LOCAL_SETUP.md`.

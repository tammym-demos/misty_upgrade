# Misty II + Foundry Local Conversational Robot

Turn a Misty II robot into a local conversational AI companion. Misty provides the physical robot presence - speakers, LED, display, movement, and tally-light behavior - while a Windows companion laptop runs wake-word detection, speech recording, STT, LLM inference, and TTS.

The project is designed for portable developer demos: local Wi-Fi, no cloud inference, no API keys, and offline operation after the required tools and models are installed.

---

## One-Command Start

Run these from the Windows companion device. One-command local startup/shutdown is available through the repo-local npm CLI:

```powershell
# Start Foundry Local, load Phi-3.5-mini, the orchestration service, and the Misty controller
npx . start

# Check service status
npx . status

# Gracefully stop the controller, release Misty audio/LED resources, and stop owned services
npx . stop
```

The CLI reads `src\windows-orchestration\.env` when present. Startup loads `Phi-3.5-mini-instruct-openvino-gpu:2` into Foundry Local with a 1-hour TTL before starting the controller, so the first chat does not pay model-load latency. Before starting the controller, it checks Misty's REST API at `http://<MISTY_IP>/api/device`. If the configured IP is stale, it scans local private IPv4 `/24` networks for Misty's device API, stores the discovered IP and broadcast/reverse-DNS name in `.misty-services.json`, and reuses that address next time. Override common settings inline:

```powershell
npx . start --misty-ip 10.0.0.44 --orchestration-url http://10.0.0.58:5000
```

Manual startup is also supported — see [Quick Start](#quick-start).

---

## Current Architecture

This is a **two-device system**:

| Device | Role |
|---|---|
| **Misty II** | Physical robot interface controlled over REST + WebSocket. Used for speakers, LED, display, movement, and recording/tally-light state. |
| **Windows companion laptop** | Runs the controller, laptop-mic wake word listener, audio capture, orchestration service, and local model inference. |

```text
Laptop mic -> openWakeWord
Laptop mic -> sounddevice WAV recording
        |
        v
Misty Controller
  - REST + WebSocket robot control
  - State machine and re-arm logic
  - LED/display/audio/movement behavior
        |
        v
Orchestration Service (Flask)
  - STT: faster-whisper
  - LLM: Phi-3.5-mini via Foundry Local
  - TTS: Kokoro-ONNX, fallback to pyttsx3
        |
        v
Misty speakers + LED/display/movement
```

The supported wake path is laptop-side OpenWakeWord with a custom "Hey Misty" model. `USE_LAPTOP_WAKE_WORD=true` is the required mode; Misty's built-in keyphrase engine is not a supported operating path and is only retained in code as a cleanup path for robot audio resources. The controller will fail fast with guidance if laptop wake-word dependencies, microphone access, or a configured `OWW_CUSTOM_MODEL_PATH` are unavailable. In laptop mode, the laptop mic handles both wake word detection and STT recording. Misty's mic is not trusted for intelligence; by default it is used only to activate the robot tally light during recording and to keep fallback audio if laptop capture fails. Operators can reduce robot audio churn with `LAPTOP_MISTY_RECORDING_MODE=tally` for a short tally-light pulse, or `LAPTOP_MISTY_RECORDING_MODE=off` to avoid Misty-side recording entirely.

---

## Why the Laptop Does the AI Work

Misty II's onboard hardware cannot reliably run modern inference workloads. The 2 GB RAM limit, final firmware, and fragile Snapdragon 410 ML/audio subsystem make on-robot AI impractical. The companion laptop avoids those constraints while preserving Misty's physical personality.

Key lessons from testing:

- Misty's on-robot JavaScript skill runtime is unreliable for this use case.
- Misty's built-in keyphrase path is no longer supported; the historical firmware issue is documented in `docs\keyphrase-debugging.md` as rationale for the switch to laptop OpenWakeWord.
- Misty's on-chip face detection/recognition pipeline is effectively non-functional.
- Sensory-only reboot must not be used; full Core+Sensory reboot is the safe recovery path.
- Laptop-side wake word, recording, STT, LLM, and TTS are the reliable path forward.

See:

- `docs\ADR-001-companion-device-over-onrobot-inference.md`
- `docs\ADR-002-non-blocking-audio-pattern.md`
- `docs\lessons-learned.md`
- `docs\keyphrase-debugging.md`

---

## Conversational Flow

1. Laptop mic detects the wake word with openWakeWord.
2. Controller pauses wake-word listening to prevent self-wake.
3. Misty plays a short greeting and turns green to cue speech.
4. Laptop mic records the user's utterance; Misty's recording/tally light runs in parallel.
5. Controller posts WAV audio to `/api/orchestrate`.
6. Orchestration service runs STT -> LLM -> TTS.
7. Controller downloads the generated WAV and uploads it to Misty for playback.
8. Misty enters follow-up listening for continued conversation without requiring another wake word.
9. Silence, movement completion, timeout, or max turn count ends the session and re-arms wake word detection.

Misty's conversation state machine is:

```text
DISCONNECTED -> IDLE -> RECORDING -> PROCESSING -> PLAYING
                      -> LISTENING -> PROCESSING -> PLAYING
                      -> REARMING -> IDLE
```

Movement adds a guarded `MOVING` state with battery, bump, hazard, and sensor freshness checks. Low battery adds `CHARGING`, and proactive recovery uses `REBOOTING`.

---

## Model Stack

| Role | Implementation | Notes |
|---|---|---|
| Wake word | openWakeWord | Runs on the laptop mic via `sounddevice`. |
| STT | faster-whisper / whisper-tiny | Runs in-process in `orchestration_service.py`; not served by Foundry Local. |
| Chat | Phi-3.5-mini | Served by Foundry Local using full model ID `Phi-3.5-mini-instruct-openvino-gpu:2`. |
| TTS | Kokoro-ONNX | Primary offline neural TTS, in-process in the orchestration service. |
| TTS fallback | pyttsx3 / Windows SAPI5 | Used when Kokoro is unavailable. |

Foundry Local runs on a dynamic port. The orchestration service discovers it with `foundry service status` and strips the path to get the base URL. Override with `FOUNDRY_LOCAL_HOST` if needed.

---

## Repository Structure

```text
src\
  windows-orchestration\
    orchestration_service.py   # Flask STT -> LLM -> TTS service
    misty_controller.py        # Misty REST/WebSocket controller and state machine
    wake_word_listener.py      # Laptop mic openWakeWord + recording/VAD support
    requirements.txt           # Python dependencies

tests\
  test_integration.py          # Mixed suite; use @pytest.mark.live to filter live-service tests
  autonomous_test_harness.py   # Long-running physical-system harness

docs\
  ADR-001-companion-device-over-onrobot-inference.md
  ADR-002-non-blocking-audio-pattern.md
  IMPLEMENTATION_GUIDE.md
  FOUNDRY_LOCAL_SETUP.md
  lessons-learned.md
  keyphrase-debugging.md

misty-skills-backup\
  README.md
  all_skills_metadata.json     # Metadata for removed on-robot skills

plans\                         # Historical planning prompts and notes
```

---

## Quick Start

Run these from the Windows companion device. For the fastest path use the [one-command CLI](#one-command-start). Manual startup:

```powershell
# 1. Start Foundry Local
python -m pip install foundry-local
foundry

# 2. Verify the expected model is loaded
foundry service ps

# 3. Install orchestration dependencies
cd src\windows-orchestration
python -m pip install -r requirements.txt

# 4. Start the orchestration service
python orchestration_service.py
```

In a second terminal:

```powershell
cd src\windows-orchestration
$env:MISTY_IP = "10.0.0.44"
$env:ORCHESTRATION_URL = "http://10.0.0.58:5000"
python misty_controller.py
```

Health check:

```powershell
curl http://localhost:5000/api/health
```

Use the actual Misty IP and companion-device IP for your network. `ORCHESTRATION_URL` must be reachable from the controller process.

---

## Configuration

Common environment variables:

| Variable | Default | Purpose |
|---|---:|---|
| `MISTY_IP` | `10.0.0.44` | Misty robot IP address. |
| `ORCHESTRATION_URL` | `http://10.0.0.58:5000` | URL for the Flask orchestration service. |
| `USE_LAPTOP_WAKE_WORD` | `true` | Required wake-word mode; the controller will fail fast if laptop wake-word startup is unavailable. |
| `OWW_CUSTOM_MODEL_PATH` | empty | Path to the custom "Hey Misty" OpenWakeWord model artifact. |
| `OWW_MODEL_NAME` | `hey_misty` | Model label for the configured wake-word artifact. |
| `OWW_THRESHOLD` | `0.7` | Confidence threshold for wake-word inference. |
| `FOUNDRY_LOCAL_HOST` | auto-discovered | Optional Foundry Local base URL override. |
| `FOUNDRY_API_TIMEOUT` | `10.0` | Per-request timeout for Foundry API calls. |
| `SERVICE_TIMEOUT` | `15.0` | Overall service timeout setting. |
| `KOKORO_VOICE` | `af_sky` | Kokoro voice ID. |
| `KOKORO_SPEED` | `1.2` | Kokoro speech speed. |
| `MAX_USER_CHARS` | `400` | Per-utterance prompt character cap. |
| `MAX_CONTEXT_CHARS` | `5000` | Total LLM context character budget. |
| `FOLLOWUP_TIMEOUT_S` | `90` | Follow-up conversation window. |
| `FOLLOWUP_MAX_TURNS` | `12` | Max follow-up recording cycles in one session. |
| `PROACTIVE_REBOOT_AFTER_CYCLES` | `5` | Full reboot after this many successful conversation cycles. |
| `PROACTIVE_REBOOT_AFTER_RECORDINGS` | `15` | Full reboot after this many recording cycles. |
| `LAPTOP_MISTY_RECORDING_MODE` | `fallback` | Misty recorder behavior during conversations: `fallback` keeps full Misty audio as a safe fallback, `tally` records only a short tally-light pulse, and `off` disables Misty-side recording. |
| `LAPTOP_MISTY_TALLY_RECORDING_S` | `1.0` | Length of the tally-light-only Misty recording pulse when `LAPTOP_MISTY_RECORDING_MODE=tally`. |

Copy `src\windows-orchestration\.env.example` to `.env` if you want persistent local settings for the orchestration service. For the supported wake path, train or obtain a custom "Hey Misty" OpenWakeWord model artifact outside this repo, place it on the laptop at a path the controller can access, and set `OWW_CUSTOM_MODEL_PATH` to that path before launching the controller.

If `LAPTOP_MISTY_RECORDING_MODE` is `tally` or `off` and laptop mic capture returns no usable audio, the controller raises a clear retryable error instead of silently falling back to Misty. Check the laptop microphone or switch back to `fallback` when robot-side backup audio is needed.

---

## Orchestration API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/health` | Service and Foundry health. |
| `GET` | `/api/diagnostics` | Current config, model aliases, TTS state, cache stats, latency budget. |
| `POST` | `/api/orchestrate` | Main multipart WAV pipeline: STT -> LLM or movement intent -> TTS. |
| `GET` | `/api/audio/<filename>` | Retrieve generated WAV response audio. |
| `POST` | `/api/tts` | Generate raw WAV bytes for controller use. |
| `POST` | `/api/fallback-tts` | Generate fallback TTS and return an audio URI. |

`/api/orchestrate` returns normal conversational responses or movement responses. Movement commands short-circuit the LLM and return a quick TTS acknowledgment plus a bounded movement command for the controller to execute safely.

### Inline audio bytes (optional)

Add `return_audio_bytes=true` as a form field to receive the generated WAV base64-encoded directly in the JSON response. This avoids a separate `GET /api/audio/<filename>` round trip:

```json
{
  "status": "ok",
  "transcribedText": "...",
  "inferenceResponse": "...",
  "responseAudio": "/api/audio/response_12345.wav",
  "audioBytes": "<base64-encoded WAV>",
  "latencyMs": 1234,
  "ttsFallback": false,
  "ttsCached": false
}
```

The `responseAudio` URI is always included for compatibility. `audioBytes` is only present when `return_audio_bytes=true` is requested. Error responses remain structured JSON regardless.

---

## Controller Behavior

`misty_controller.py` is responsible for:

- WebSocket subscriptions for wake word, battery, hazard, ToF, bump, and optional face events.
- REST calls for LED, display, recording, audio upload/playback, movement, halt, and reboot.
- Laptop wake word listener lifecycle.
- Conversation recording, response playback, follow-up listening, and re-arm.
- Battery-aware charging mode and movement cutoffs.
- Movement safety: bounded drive commands, hazard/bump preemption, sensor freshness checks, and emergency halt.
- Proactive full reboot to avoid Misty's audio/keyphrase degradation.
- A small controller test API on port `5001`.

Controller test API:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/status` | Current state, turn ID, battery, and reboot counters. |
| `GET` | `/api/mic/health` | Misty mic health check. |
| `POST` | `/api/test/trigger` | Start a conversation turn while IDLE. |
| `POST` | `/api/move` | Teleop movement command with strict bounds. |
| `POST` | `/api/sensors` | Hazard and sensor telemetry snapshot. |

---

## Testing

`tests/test_integration.py` includes both fast mocked tests and live-service/hardware tests.
Live-dependent classes are marked with `@pytest.mark.live`.

```powershell
cd tests
python -m pytest test_integration.py -v
```

Quick fast-feedback run (no Misty, Foundry Local, or orchestration service required):

```powershell
python -m pytest test_integration.py -m "not live" -v
```

Live/integration-only run:

```powershell
python -m pytest test_integration.py -m live -v
```

Targeted examples:

```powershell
python -m pytest test_integration.py::TestPromptLimiting -v
python -m pytest test_integration.py::TestWindowsOrchestration -v
python -m pytest test_integration.py::TestFoundryLocalIntegration -v
```

Test prerequisites:

| Test class | Requires |
|---|---|
| `TestPromptLimiting` | No live services; HTTP calls are mocked. |
| `TestWindowsOrchestration` | Orchestration service running. |
| `TestFoundryLocalIntegration` | Foundry Local running or `FOUNDRY_LOCAL_HOST` set. |
| `TestMistyConnectivity` | Misty reachable on the network. |
| `TestLatencySLO` | Orchestration service running. |
| `TestVerificationChecklist` | Mixed; several items are manual/live-system checks. |

For unattended physical testing, see `tests\autonomous_test_harness.py`.

---

## Operating Notes

- Start services in order: Foundry Local, orchestration service, Misty controller.
- Only the Phi-3.5-mini chat model should be loaded in Foundry Local for normal operation.
- Whisper-tiny and Kokoro are not Foundry REST models; they run in the Python orchestration process.
- Do not push changes directly to `main`; use a feature branch and PR.
- Do not rely on Misty's on-robot skills for the current pipeline. Auto-starting skills were removed because they interfered with audio.
- Do not use sensory-only reboot. Use full reboot with both `Core` and `SensoryServices` set to `true`.
- On shutdown, stop recording, cancel skills, and turn the LED off so the tally light and audio resources are released.

---

## Known Issues and Mitigations

| Issue | Status |
|---|---|
| Misty's Snapdragon 410 keyphrase engine silently fails | Resolved: keyphrase removed; laptop openWakeWord is the only supported wake path. |
| Misty's on-chip face detection is dead | Avoided; future/optional recognition should be laptop-side. |
| Sensory-only reboot breaks the mic until physical power cycle | Avoided; code uses full Core+Sensory reboot. |
| Misty mic can degrade after extended recording cycles | Mitigated by laptop STT recording and proactive reboot by recording count. |
| TTS dominates response latency | Mitigated with Kokoro speed tuning and in-memory TTS cache. |
| STT accuracy is limited by whisper-tiny and mic conditions | Beam search and laptop mic help, but noisy environments still need tuning. |

---

## Documentation

- `docs\IMPLEMENTATION_GUIDE.md` - Full setup and troubleshooting.
- `docs\FOUNDRY_LOCAL_SETUP.md` - Foundry Local setup and model management.
- `docs\IMPLEMENTATION_SUMMARY.md` - Historical build summary.
- `docs\ADR-001-companion-device-over-onrobot-inference.md` - Companion-device decision.
- `docs\ADR-002-non-blocking-audio-pattern.md` - Laptop-mic callback/queue/worker pattern.
- `docs\lessons-learned.md` - Operational findings from real hardware testing.
- `docs\keyphrase-debugging.md` - Keyphrase failure history and recovery notes (historical reference).

---

## License

This project is licensed under the [MIT License](LICENSE).

## Disclaimers

Misty is a trademark of its respective owner. This project is not affiliated with or endorsed by Misty Robotics, Furhat Robotics, Microsoft, or any successor entities. Users are responsible for complying with applicable SDK, model, and dependency licenses.


# Misty II + Foundry Local — Conversational AI Robot

> Turn a Misty II robot into a conversational AI assistant using fully local inference — no cloud, no API keys, no internet required after initial model downloads.

## Project Scope

This project integrates a **Misty II** social robot with **Microsoft Foundry Local** running on a Windows companion device to deliver a complete voice-interactive AI experience over local Wi-Fi. The system handles the full conversational loop — wake-word detection, speech-to-text, LLM reasoning, text-to-speech, and audio playback — all running locally on commodity hardware.

**Primary use case:** Travel demos for developer advocacy — a portable, self-contained AI robot setup that works offline at conferences and events.

---

## Architecture

```
┌──────────────────────────────┐         ┌──────────────────────────────────┐
│        Misty II Robot        │         │     Windows Companion Device     │
│                              │  Wi-Fi  │                                  │
│  "Hey, Misty!" (wake word)   │◄───────►│  Misty Controller (Python)      │
│  Microphone / Speakers       │  REST + │    ├─ WebSocket event listener   │
│  LED / Display               │  WS     │    ├─ REST API commands          │
│                              │         │    └─ State machine (IDLE →      │
│  Controlled entirely via     │         │        RECORDING → PROCESSING →  │
│  REST API + WebSocket from   │         │        PLAYING → LISTENING →     │
│  companion device            │         │        REARMING)                 │
│                              │         │  Orchestration Service (Flask)   │
│                              │         │    ├─ STT  (faster-whisper)      │
│                              │         │    ├─ LLM  (Phi-3.5-mini)  ───► Foundry Local
│                              │         │    └─ TTS  (Kokoro / pyttsx3)   │
│                              │         │                                  │
│                              │         │  Foundry Local (LLM inference)   │
└──────────────────────────────┘         └──────────────────────────────────┘
```

**Pipeline flow:** Wake word (WebSocket event) → Record via REST → Download audio → `POST /api/orchestrate` → STT → LLM → TTS → Upload audio to Misty → Playback → Re-arm wake word

> **Note:** We use the REST API + WebSocket approach (code runs on the laptop) instead of Misty's on-robot JavaScript SDK. The skill runtime proved unreliable — see [Implementation Guide](docs/IMPLEMENTATION_GUIDE.md) for details.

### Why a Companion Device?

Misty II's onboard Snapdragon 820 + 410 (2 GB RAM) cannot run modern inference workloads — RAM is the bottleneck. A companion Windows laptop provides the compute while Misty handles audio I/O and interaction. See [ADR-001](docs/ADR-001-companion-device-over-onrobot-inference.md) for the full decision record.

---

## Repository Structure

```
├── src/
│   ├── misty-skill/                    # Legacy JavaScript skill (deprecated — kept for reference)
│   │   ├── FoundryLocalSkill.json      #   Skill metadata
│   │   └── FoundryLocalSkill.js        #   On-robot skill code (not used in current architecture)
│   │
│   └── windows-orchestration/          # Python services on companion device
│       ├── orchestration_service.py    #   STT → LLM → TTS pipeline (~500 LOC)
│       ├── misty_controller.py         #   REST+WebSocket controller for Misty (~800 LOC)
│       ├── requirements.txt            #   Dependencies (Flask, requests, websocket-client, faster-whisper)
│       └── .env.example                #   Configuration template
│
├── misty-skills-backup/                # Backup of deleted on-robot skills
│   ├── README.md                       #   Why skills were removed, restoration notes
│   └── all_skills_metadata.json        #   Metadata for all 11 skills (pre-cleanup)
│
├── tests/
│   └── test_integration.py             # Integration test suite (health, connectivity, latency)
│
├── docs/
│   ├── ADR-001-companion-device-over-onrobot-inference.md
│   ├── FOUNDRY_LOCAL_SETUP.md
│   ├── IMPLEMENTATION_GUIDE.md         # Full setup & troubleshooting guide
│   └── IMPLEMENTATION_SUMMARY.md       # Build log and design decisions
│
└── plans/                              # Prompt plans for various deployment scenarios
    ├── planConversationalRobot.prompt.md
    ├── planRaspberryPi.prompt.md
    ├── planTravelDemo-GHCP.prompt.md
    └── planWindowsFoundry.prompt.md
```

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Inference location | Companion device (laptop) | Misty hardware too constrained; laptop already in travel kit |
| Model server | Foundry Local | Local-only, OpenAI-compatible API, no cloud dependency |
| Chat model | Phi-3.5-mini (3.8B) via **Foundry Local** | Fast, high-quality, fits CPU inference budget. Served by Foundry Local at `/v1/chat/completions`. |
| STT model | Whisper-tiny via **faster-whisper** (in-process) | Foundry Local has no REST endpoint for Whisper — only C#/Rust SDK. We use [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) which loads the model directly in the orchestration Python process. Model auto-downloaded from HuggingFace on first run (~75 MB). |
| TTS model | Kokoro v1.0 ONNX (in-process), pyttsx3 fallback | [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) runs inference in the orchestration process via ONNX Runtime. Model files (`kokoro-v1.0.int8.onnx` 88 MB + `voices-v1.0.bin` 27 MB) stored locally. Falls back to pyttsx3 (Windows SAPI5) if Kokoro unavailable. |
| Latency SLO | p50 < 3s, p95 < 6s (aspirational) | Currently ~23s end-to-end; see [#21](https://github.com/tammym-demos/misty_upgrade/issues/21) |
| Network | Local Wi-Fi only | Privacy-first, offline-capable after initial model download |
| Hardware | CPU-only (GPU optional) | Broad laptop compatibility |

### Model Runtime Summary

| Model | Purpose | Runtime | Origin |
|-------|---------|---------|--------|
| Phi-3.5-mini | Chat / LLM | **Foundry Local** (HTTP API) | Bundled with Foundry Local, managed via `foundry model` CLI |
| Whisper-tiny | Speech-to-text | **faster-whisper** (in-process) | Auto-downloaded from [HuggingFace](https://huggingface.co/Systran/faster-whisper-tiny) on first run |
| Kokoro v1.0 | Text-to-speech | **kokoro-onnx** (in-process) | Manually downloaded ONNX model files in `src/windows-orchestration/` |
| pyttsx3 | TTS fallback | **pyttsx3** (in-process) | Windows built-in SAPI5 voices, no model files needed |

---

## Configuration

The orchestration service is configured via environment variables (copy `.env.example` to `.env`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `FOUNDRY_LOCAL_HOST` | Auto-discovered | Foundry Local base URL (overrides CLI discovery) |
| `FOUNDRY_API_TIMEOUT` | `5.0` | Per-request timeout (seconds) for Foundry API calls |
| `SERVICE_TIMEOUT` | `6.0` | Overall orchestration pipeline timeout (seconds) |
| `KOKORO_VOICE` | `af_heart` | Kokoro TTS voice ID |
| `SYSTEM_PROMPT` | *(see below)* | System prompt for the LLM; if unset, uses the built-in Misty persona prompt |
| `MAX_USER_CHARS` | `400` | Maximum characters for a single transcribed user utterance. Longer inputs are truncated before LLM inference to reduce token count and latency. |
| `MAX_CONTEXT_CHARS` | `3000` | Maximum total characters across all messages (system prompt + history) sent to the LLM. Oldest turns are removed first; the system prompt and latest user message are always preserved. Set to `0` to disable. |

**Latency note:** `MAX_USER_CHARS` and `MAX_CONTEXT_CHARS` are the primary latency levers. Reducing these values lowers input token count and directly improves inference time, at the cost of context completeness.

---

## Quick Start

```powershell
# 1. Start Foundry Local on the companion device
pip install foundry-local
foundry

# 2. Verify only the expected model is loaded (unload strays from prior testing)
foundry service ps
# Expected: phi-3.5-mini only. If others appear: foundry model unload <alias>

# 3. Install and run the orchestration service
cd src\windows-orchestration
pip install -r requirements.txt
python orchestration_service.py

# 4. Verify the service is healthy (port is auto-discovered from `foundry service status`)
Invoke-RestMethod -Uri http://localhost:5000/api/health

# 5. Start the Misty controller (separate terminal)
#    Set MISTY_IP and ORCHESTRATION_URL env vars if not using defaults
python misty_controller.py
```

The Misty controller connects to Misty via WebSocket and REST API — no skill deployment needed. Misty's LED turns green when ready. Say **"Hey, Misty!"** followed by your question. After Misty responds, she'll keep listening (cyan LED) for up to 60 seconds — just keep talking without saying the wake word again. Silence ends the conversation and re-arms the wake word.

### Expressive Behavior

During conversations, Misty uses head movement and face animations to signal her state:
- **Listening**: Looks up for eye contact, wide-eyed attentive face
- **Processing**: Tilts head to the side, thoughtful expression
- **Speaking**: Faces forward, animated expression
- **Follow-up**: Slight head tilt, warm expectant face
- Head recenters when re-arming for the next wake word

### On-Robot Skills — Cleaned Up

All auto-starting on-robot skills have been **deleted** from Misty to prevent microphone interference. The `faceDetection` skill previously auto-started on boot, grabbed the mic, and caused silent keyphrase failures. The controller also cancels all running skills on connect as a safety net.

Skill metadata was backed up to `misty-skills-backup/` before deletion.

### Keyphrase Watchdog

Misty's sensory services (Snapdragon 410) can silently stop firing wake word events — a known firmware bug with no programmatic detection. The controller includes an automatic watchdog that detects this and self-recovers:

1. **Soft reset** (2 min): 🟡 Yellow LED flash → cancel skills → restart keyphrase
2. **Sensory reboot** (+1 min): 🔴 Red LED → reboot sensory services only
3. **Full reboot** (+1 min): 🔴 Red LED → full system reboot

The watchdog only activates in IDLE state. Configure timeouts via `WATCHDOG_IDLE_TIMEOUT_S` and `WATCHDOG_ESCALATE_TIMEOUT_S` environment variables.

### Startup Verification

All three services must be running. Start them in order — each depends on the previous:

| # | Service | Verify | Notes |
|---|---------|--------|-------|
| 1 | **Foundry Local** | `foundry service status` / `foundry service ps` | Dynamic port; auto-discovered by orchestration service |
| 2 | **Orchestration service** | `curl http://localhost:5000/api/health` | Reports Foundry and TTS status |
| 3 | **Misty controller** | Misty LED turns green | Connects via WebSocket + REST |

**Model management:** Only `phi-3.5-mini` should be loaded in Foundry (`foundry service ps`). Whisper-tiny STT runs in-process via faster-whisper (not through Foundry). Kokoro TTS runs in-process in the orchestration service — neither STT nor TTS are Foundry models and won't appear in `foundry model list`.

---

## Documentation

- **[Implementation Guide](docs/IMPLEMENTATION_GUIDE.md)** — Full setup, deployment phases, and troubleshooting
- **[Implementation Summary](docs/IMPLEMENTATION_SUMMARY.md)** — What was built, code stats, and known limitations
- **[ADR-001](docs/ADR-001-companion-device-over-onrobot-inference.md)** — Why companion device over on-robot or backpack inference
- **[Foundry Local Setup](docs/FOUNDRY_LOCAL_SETUP.md)** — Model server installation and configuration

---

## Status

**v1.0 — REST+WebSocket Architecture, Orchestration Service Complete**

The project has moved from on-robot JavaScript skills to a laptop-driven REST+WebSocket architecture. The Misty controller and orchestration service run on the companion device. Wake word detection, recording, and audio playback are all handled via Misty's REST API.

See [IMPLEMENTATION_SUMMARY.md](docs/IMPLEMENTATION_SUMMARY.md) for the full build log, known limitations, and future enhancement roadmap.

### Known Issues

| Issue | Summary |
|-------|---------|
| [#28](https://github.com/tammym-demos/misty_upgrade/issues/28) | Keyphrase re-arm: proactive sensory reboot after each conversation (~20s reset) |
| [#27](https://github.com/tammym-demos/misty_upgrade/issues/27) | STT accuracy: beam_size=5 + VAD applied, whisper-tiny still garbles follow-ups |
| [#22](https://github.com/tammym-demos/misty_upgrade/issues/22) | Keyphrase silently fails after conversation — mitigated by #28 sensory reboot |
| [#21](https://github.com/tammym-demos/misty_upgrade/issues/21) | End-to-end latency ~23s (target <6s) — TTS is 82% of pipeline |
| [#24](https://github.com/tammym-demos/misty_upgrade/issues/24) | LLM ignores brevity instructions, generates verbose responses |
| [#20](https://github.com/tammym-demos/misty_upgrade/issues/20) | Fixed 4s recording — needs voice activity detection |
| [#19](https://github.com/tammym-demos/misty_upgrade/issues/19) | Conversation history needs smarter management for latency |

---

## License

This project is licensed under the [MIT License](LICENSE).

---

## Disclaimers

- **Trademark:** Misty is a trademark of its respective owner.
- **Affiliation:** This project is not affiliated with or endorsed by Misty Robotics or its successor entities.
- **SDK:** This project requires the Misty SDK; users must comply with the Misty SDK license and terms of use.

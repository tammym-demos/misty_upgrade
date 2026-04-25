
# Misty II + Foundry Local — Conversational AI Robot

> Turn a Misty II robot into a conversational AI assistant using fully local inference — no cloud, no API keys, no internet required after setup.

## Project Scope

This project integrates a **Misty II** social robot with **Microsoft Foundry Local** running on a Windows companion device to deliver a complete voice-interactive AI experience over local Wi-Fi. The system handles the full conversational loop — wake-word detection, speech-to-text, LLM reasoning, text-to-speech, and audio playback — all with sub-6-second latency on commodity hardware.

**Primary use case:** Travel demos for developer advocacy — a portable, self-contained AI robot setup that works offline at conferences and events.

---

## Architecture

```
┌──────────────────────────────┐         ┌──────────────────────────────────┐
│        Misty II Robot        │         │     Windows Companion Device     │
│                              │  Wi-Fi  │                                  │
│  "Hey, Misty!" (wake word)   │────────▶│  Orchestration Service (Flask)   │
│  Record audio (up to 10s)    │  POST   │    ├─ STT  (Whisper-tiny)        │
│                              │         │    ├─ LLM  (Phi-3.5-mini)        │
│  Play response audio ◀───────│────────◀│    └─ TTS  (Kokoro)              │
│  Re-arm wake word listener   │  JSON   │                                  │
│                              │         │  Foundry Local (model server)    │
└──────────────────────────────┘         └──────────────────────────────────┘
```

**Pipeline flow:** Wake word → Record → `POST /api/orchestrate` → STT → LLM → TTS → Audio response → Playback

### Why a Companion Device?

Misty II's onboard Snapdragon 212 (4× Cortex-A7, 2 GB RAM) cannot run modern inference workloads. A companion Windows laptop provides the compute while Misty handles audio I/O and interaction. See [ADR-001](docs/ADR-001-companion-device-over-onrobot-inference.md) for the full decision record.

---

## Repository Structure

```
├── src/
│   ├── misty-skill/                    # JavaScript skill deployed to Misty II
│   │   ├── FoundryLocalSkill.json      #   Skill metadata & config
│   │   └── FoundryLocalSkill.js        #   Wake word → record → request → play (~350 LOC)
│   │
│   └── windows-orchestration/          # Python Flask service on companion device
│       ├── orchestration_service.py    #   STT → LLM → TTS pipeline (~450 LOC)
│       ├── requirements.txt            #   Dependencies (Flask, requests, Flask-CORS)
│       └── .env.example                #   Configuration template
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
| Chat model | Phi-3.5-mini (3.8B) | Fast, high-quality, fits CPU inference budget |
| STT model | Whisper-tiny | Minimal latency, CPU-friendly |
| TTS model | Kokoro v0.19 | Fast synthesis with natural voice quality |
| Latency SLO | p50 < 3s, p95 < 6s | Must feel conversational |
| Network | Local Wi-Fi only | Privacy-first, offline-capable after initial model download |
| Hardware | CPU-only (GPU optional) | Broad laptop compatibility |

---

## Quick Start

```powershell
# 1. Start Foundry Local on the companion device
pip install foundry-local
foundry --port 5000 --host 0.0.0.0

# 2. Install and run the orchestration service
cd src\windows-orchestration
pip install -r requirements.txt
python orchestration_service.py

# 3. Verify the service is healthy
Invoke-RestMethod -Uri http://localhost:5000/api/health

# 4. Configure the Misty skill with the companion device IP
#    Edit src/misty-skill/FoundryLocalSkill.js → WINDOWS_HOST

# 5. Deploy the skill to Misty via web interface or REST API
```

Then say **"Hey, Misty!"** followed by your question.

---

## Documentation

- **[Implementation Guide](docs/IMPLEMENTATION_GUIDE.md)** — Full setup, deployment phases, and troubleshooting
- **[Implementation Summary](docs/IMPLEMENTATION_SUMMARY.md)** — What was built, code stats, and known limitations
- **[ADR-001](docs/ADR-001-companion-device-over-onrobot-inference.md)** — Why companion device over on-robot or backpack inference
- **[Foundry Local Setup](docs/FOUNDRY_LOCAL_SETUP.md)** — Model server installation and configuration

---

## Status

**v1.0 — Implementation Complete, Ready for Deployment**

See [IMPLEMENTATION_SUMMARY.md](docs/IMPLEMENTATION_SUMMARY.md) for the full build log, known limitations, and future enhancement roadmap.

---

## License

This project is licensed under the [MIT License](LICENSE).

---

## Disclaimers

- **Trademark:** Misty is a trademark of its respective owner.
- **Affiliation:** This project is not affiliated with or endorsed by Misty Robotics or its successor entities.
- **SDK:** This project requires the Misty SDK; users must comply with the Misty SDK license and terms of use.

# Misty + Foundry Local Implementation Guide

## Overview

This implementation integrates Misty II robot with Microsoft Foundry Local for on-device AI inference. The system architecture separates concerns:
- **Misty**: Audio interface (wake word detection, recording, playback)
- **Windows Companion**: Inference orchestration (STT → LLM → TTS)
- **Foundry Local**: Local inference engine (models, endpoints)

**Latency SLO**: p50 < 3s, p95 < 6s from wake word to audible response (aspirational — currently ~23s, see #21 for optimization plan)

---

## Architecture

```
┌──────────────┐          Wi-Fi (local)           ┌──────────────────┐
│   Misty II   │◄───────────────────────────────►│ Windows Companion │
│              │         REST/HTTP (port 80)      │     (port 5000)  │
│ • Wake word  │                                  │                  │
│ • Recording  │                                  │ Orchestration:   │
│ • Playback   │                                  │ • STT pipeline   │
│ • JavaScript │                                  │ • LLM inference  │
│   skill      │                                  │ • TTS synthesis  │
└──────────────┘                                  └────────┬─────────┘
                                                           │
                                              ┌────────────┼────────────┐
                                              │            │            │
                                      Foundry Local    kokoro-onnx  pyttsx3
                                      (localhost)      (standalone) (fallback)
                                       ├─ /chat/compl    └─ ONNX       └─ SAPI5
                                       └─ /transcribe      model
```

---

## Components

### 1. Misty Controller (Python — replaces JavaScript Skill)
**Location**: `src/windows-orchestration/misty_controller.py`

The Misty controller runs on the companion device and drives the robot entirely via REST API + WebSocket. This replaces the on-robot JavaScript skill, which proved unreliable (see [Why Not On-Robot Skills?](#why-not-on-robot-skills) below).

**State Machine**: `DISCONNECTED → IDLE → RECORDING → PROCESSING → PLAYING → [LISTENING →]* REARMING → IDLE`

**Functionality**:
- Connects to Misty via WebSocket (`ws://<ip>/pubsub`) for `KeyPhraseRecognized` events
- Issues REST API calls for recording, audio upload/download, LED, display, head/arm movement
- Manages the full conversation turn cycle in a worker thread
- **Cancels all on-robot skills** on WebSocket connect to prevent microphone interference
- **Expressive head movement + face animations** per state (attentive→thinking→animated→warm)
- **Follow-up listening**: after each response, enters LISTENING state for up to 60s — records short clips and sends them through the pipeline. If speech is detected, processes and responds; if silence, ends the conversation and re-arms the wake word.
- Auto-reconnects on WebSocket disconnect with exponential backoff
- Periodic health checks for Misty and orchestration service

**LED Color Scheme**:
- 🟢 Green = IDLE (listening for wake word)
- 🟠 Orange = RECORDING (capturing user speech)
- 🔵 Blue = PROCESSING (STT → LLM → TTS)
- 🟣 Purple = PLAYING (speaking response)
- 🩵 Cyan = LISTENING (follow-up listening window)
- 🔴 Red = ERROR (will auto-recover)

**Expressive Behavior** (per state):
| State | Face Expression | Head Position |
|-------|----------------|---------------|
| RECORDING | `e_Admiration.jpg` (wide-eyed attentive) | Pitch up — eye contact |
| PROCESSING | `e_ContentRight.jpg` (thoughtful) | Tilted to side — pondering |
| PLAYING | `e_EcstacyHilarious.jpg` (animated) | Forward — direct engagement |
| LISTENING | `e_Joy.jpg` (warm expectant) | Slight tilt — attentive |
| IDLE | `e_DefaultContent.jpg` (neutral) | Centered |
| ERROR | `e_Sadness.jpg` (sad) | — |

**Key Configuration** (environment variables):
- `MISTY_IP`: Robot IP address (default: `10.0.0.44`)
- `ORCHESTRATION_URL`: Orchestration service URL (default: `http://10.0.0.58:5000`)
- `RECORDING_DURATION_S`: How long to record after wake word (default: `4` seconds)
- `FOLLOWUP_LISTEN_S`: Duration of each follow-up listen clip (default: `4` seconds)
- `FOLLOWUP_TIMEOUT_S`: Max follow-up conversation window (default: `60` seconds)
- `WATCHDOG_IDLE_TIMEOUT_S`: Time before watchdog soft-resets keyphrase (default: `120` seconds)
- `WATCHDOG_ESCALATE_TIMEOUT_S`: Time before watchdog escalates recovery (default: `60` seconds)

**Keyphrase Watchdog**:
The Snapdragon 410 sensory services silently stop firing `KeyPhraseRecognized` events while the REST API still returns "Success". The watchdog detects this and auto-recovers:
1. **Soft reset** (after 2 min in IDLE with no wake events): 🟡 yellow LED → cancel skills → stop/start keyphrase
2. **Sensory reboot** (if soft reset fails after 1 min): 🔴 red LED → reboot sensory services only
3. **Full reboot** (if sensory reboot fails after 1 min): 🔴 red LED → full Core+Sensory reboot

The watchdog only triggers when in IDLE state. It uses separate timestamps for actual wake events vs recovery attempts to avoid false positives. Health checks run every 10s.

### 2. Windows Orchestration Service (Python/Flask)
**Location**: `src/windows-orchestration/`

**Files**:
- `orchestration_service.py` — Main service
- `requirements.txt` — Python dependencies
- `.env.example` — Configuration template

**Endpoints**:
- `GET /api/health` — Service and Foundry Local status
- `GET /api/diagnostics` — Configuration and model info
- `POST /api/orchestrate` — Main orchestration (WAV → response audio)
- `GET /api/audio/<filename>` — Retrieve generated audio
- `POST /api/fallback-tts` — Fallback text-to-speech

**Pipeline**:
1. Receives WAV file from Misty
2. Transcribes audio using **faster-whisper** in-process (STT — Whisper-tiny via CTranslate2)
3. Calls Foundry Local `/v1/chat/completions` (LLM — Phi-3.5-mini)
4. Synthesises speech locally via kokoro-onnx (TTS — Kokoro); falls back to pyttsx3/SAPI5 if unavailable
5. Returns response audio URI to Misty

> **Note:** STT and TTS both run **in-process** in the orchestration service —
> they are **not** Foundry Local models. Foundry Local does not expose a REST
> endpoint for Whisper. We use [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
> (CTranslate2) which loads the whisper-tiny model directly in the Python process.
> The model is auto-downloaded from HuggingFace on first run (~75 MB).
> Kokoro TTS runs as a standalone Python library (`kokoro-onnx`) with its own
> ONNX model file and voice pack. See [TTS Architecture](#tts-architecture) below.

**Error Handling**:
- Timeout detection at each stage
- Model load failure recovery
- Empty response detection
- Graceful fallback to Misty-side error handling

**Configuration (environment variables)**:

| Variable | Default | Purpose |
|----------|---------|---------|
| `FOUNDRY_LOCAL_HOST` | Auto-discovered | Foundry Local base URL (overrides CLI discovery) |
| `FOUNDRY_API_TIMEOUT` | `5.0` | Per-request timeout (seconds) for Foundry API calls |
| `SERVICE_TIMEOUT` | `6.0` | Overall orchestration pipeline timeout (seconds) |
| `KOKORO_VOICE` | `af_heart` | Kokoro TTS voice ID |
| `SYSTEM_PROMPT` | *(built-in Misty persona)* | Full system prompt for the LLM. Override to change persona without code changes. The default includes summarization and brevity instructions. |
| `MAX_USER_CHARS` | `400` | Hard character limit on the transcribed user utterance. Inputs longer than this are silently truncated **before** being sent to Foundry, reducing input token count and LLM latency. Tune down for faster responses, up for richer context. |
| `MAX_CONTEXT_CHARS` | `3000` | Maximum total characters across all messages (system prompt + conversation history) included in each LLM request. When exceeded, oldest turns are removed first while the system prompt and the latest user message are always kept. Set to `0` to disable trimming. |


**Deployment**: Local machine (same as Windows companion)

**Model Stack (Locked v1)**:
- Chat: `phi-3.5-mini` — Lightweight LLM (~3.8B params) — **via Foundry Local**
- STT: `whisper-tiny` — Fast speech-to-text — **via faster-whisper (in-process, not Foundry)**

**Foundry Local Endpoints Used**:
- `POST /v1/chat/completions` — OpenAI-compatible LLM (use full model ID, e.g., `Phi-3.5-mini-instruct-openvino-gpu:2`)
- `GET /openai/models` — List loaded models (returns array of model ID strings)

> **Important:** Foundry Local does **not** expose a REST endpoint for Whisper STT.
> The orchestration service uses `faster-whisper` (CTranslate2) to run whisper-tiny
> in-process. The Foundry whisper-tiny download is not required for the current architecture.

> **Important:** Foundry's `service status` CLI reports a URL like `http://127.0.0.1:64722/openai/status`.
> The orchestration service must strip the `/openai/status` path and use only `http://127.0.0.1:64722`
> as the base URL. Inference endpoints use `/v1/` prefix directly (not `/openai/v1/`).

### 4. TTS Architecture
<a id="tts-architecture"></a>

**Kokoro is not served by Foundry Local.** It is not present in the Foundry
model catalog. Instead, the orchestration service runs Kokoro as a standalone
Python library with a built-in fallback chain:

| Priority | Engine | Package | Quality | Notes |
|----------|--------|---------|---------|-------|
| Primary | Kokoro v1.0 (ONNX) | `kokoro-onnx` + `soundfile` | Natural, neural | Requires `kokoro-v1.0.int8.onnx` and `voices-v1.0.bin` from [GitHub releases](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0) |
| Fallback | pyttsx3 (SAPI5) | `pyttsx3` | Robotic but functional | Uses Windows built-in speech engine; no extra downloads needed |

**Setup for Kokoro (primary TTS):**
```powershell
pip install kokoro-onnx soundfile

# Download model files into the windows-orchestration directory
# - kokoro-v1.0.int8.onnx
# - voices-v1.0.bin
# From: https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0
```

If Kokoro is not installed or fails at runtime, the service automatically falls
back to pyttsx3 with no manual intervention required.

---

## Setup Instructions

### Phase 1: Windows Companion Setup

**1.1 Install Python**
```powershell
# Verify Python 3.8+
python --version

# Upgrade pip
python -m pip install --upgrade pip
```

**1.2 Install Foundry Local**
```powershell
# Install via winget (recommended)
winget install Microsoft.FoundryLocal

# Verify installation
foundry --version
foundry --help
```

See [FOUNDRY_LOCAL_SETUP.md](FOUNDRY_LOCAL_SETUP.md) for detailed Foundry
installation and troubleshooting.

**1.3 Start the Foundry Local Service**
```powershell
foundry service start
foundry service status

# Note the endpoint URL printed — you will need it later.
# Do NOT assume localhost:5000; the port may vary.
```

**1.4 Download Foundry Local Models (First-Run Only)**

⚠️ Requires internet. Downloads can take 5-15 minutes.

```powershell
# Download the chat model served by Foundry Local
foundry model download phi-3.5-mini

# Load it into the running service
foundry model load phi-3.5-mini

# Note: whisper-tiny is NOT served by Foundry Local.
# The orchestration service uses faster-whisper (CTranslate2) which
# auto-downloads the model from HuggingFace on first run (~75 MB).
```

**1.5 Verify Foundry Local Models**
```powershell
# Check which models are actually loaded in the service
foundry service ps

# Expected: only phi-3.5-mini loaded. Whisper-tiny loads on demand.
# If stray models are loaded (e.g., phi-4-mini), unload them:
#   foundry model unload phi-4-mini

# Check the models endpoint (replace PORT with your actual port)
# Returns a plain JSON array of model ID strings
Invoke-RestMethod -Uri http://localhost:PORT/openai/models

# Should list phi-3.5-mini and whisper-tiny.
# Kokoro TTS will NOT appear here — it runs outside Foundry Local.
```

**1.6 Install Kokoro TTS (Primary Voice Engine)**

Kokoro is **not** a Foundry Local model. Install it as a standalone Python
library alongside its ONNX model file and voice pack.

```powershell
# Install the Python packages
pip install kokoro-onnx soundfile

# Download model files from HuggingFace into the orchestration directory
cd src\windows-orchestration
# Place these two files here (download from https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0):
#   - kokoro-v1.0.int8.onnx
#   - voices-v1.0.bin
```

> **If you skip this step**, the orchestration service will automatically fall
> back to pyttsx3 (Windows SAPI5) — functional but robotic-sounding.

**1.7 Install Orchestration Service Dependencies**
```powershell
cd src\windows-orchestration
pip install -r requirements.txt

# Copy .env.example to .env and update FOUNDRY_LOCAL_HOST with the
# endpoint URL from step 1.3
copy .env.example .env
```

**1.8 Start Orchestration Service**
```powershell
cd src\windows-orchestration
python orchestration_service.py

# Should output:
# * Running on http://0.0.0.0:5000
# * WARNING: This is a development server
```

**1.9 Verify Orchestration Service**
```powershell
# In another terminal
Invoke-RestMethod -Uri http://localhost:5000/api/health

# Expected response:
# {
#   "status": "ok",
#   "orchestration": "ready",
#   "foundry_local": "ok",
#   "models": { "chat": "phi-3.5-mini", "stt": "whisper-tiny" }
# }
```

### Phase 2: Misty Robot Setup

**2.1 Connect Misty to the Same Wi-Fi Network**

Misty and the Windows companion must be on the same local network. Use the
Misty companion app or Misty's web interface to connect the robot to Wi-Fi.

Find Misty's IP address:
```powershell
# Option A: Check the Misty companion app — it shows the IP on the home screen
# Option B: Look at your router's DHCP client list
# Option C: If Misty is already connected, query it directly
Invoke-RestMethod -Uri http://<misty-ip>/api/device
```

**2.2 Verify Network Connectivity**
```powershell
# Ping Misty from the Windows companion
ping <misty-ip>

# Verify Misty REST API is reachable
Invoke-RestMethod -Uri http://<misty-ip>/api/device

# Verify Misty can reach the Windows companion (test from Misty's perspective
# by checking that the orchestration service is listening)
Invoke-RestMethod -Uri http://localhost:5000/api/health
```

**2.3 Start the Misty Controller**

In a separate terminal, start the Misty controller:

```powershell
cd src\windows-orchestration

# Set environment variables (or use defaults)
$env:MISTY_IP = "10.0.0.44"          # ← Replace with your Misty's IP
$env:ORCHESTRATION_URL = "http://10.0.0.58:5000"  # ← Replace with your laptop IP

python misty_controller.py
```

The controller will:
1. Connect to Misty via WebSocket
2. Subscribe to `KeyPhraseRecognized` events
3. Start wake word recognition via REST API
4. Set LED to green (ready)

**Finding Misty's IP:**
```powershell
# Scan your local subnet for Misty (responds on port 80)
1..254 | ForEach-Object -Parallel {
    $ip = "10.0.0.$_"
    if (Test-Connection $ip -Count 1 -Quiet -TimeoutSeconds 1) {
        try {
            $r = Invoke-RestMethod -Uri "http://$ip/api/device" -TimeoutSec 2
            Write-Output "Misty found at $ip"
        } catch {}
    }
} -ThrottleLimit 50
```

**2.4 Test Wake Word Detection**
1. LED should be **green** (IDLE state)
2. Say **"Hey, Misty!"** near the robot
3. Misty beeps and LED turns **orange** (recording for 4 seconds)
4. LED turns **blue** (processing via orchestration)
5. LED turns **purple** (playing response audio)
6. LED turns **cyan** (follow-up listening — speak to continue, or wait for silence)
7. If you speak: repeats steps 4-6 for the follow-up (up to 60s total)
8. If silence: LED returns to **green** (re-armed, ready for next "Hey, Misty!")

---

## Deployment Validation

### Test 1: Network Connectivity
```powershell
# From Windows companion
ping 192.168.1.100  # Misty
Invoke-RestMethod -Uri http://192.168.1.100/api/device

# From Misty (if accessible via SSH/terminal)
ping <windows-companion-ip>
curl http://<windows-companion-ip>:5000/api/health
```

### Test 2: Service Health
```powershell
# Check all services running
Invoke-RestMethod -Uri http://localhost:5000/api/health
Invoke-RestMethod -Uri http://localhost:5000/api/diagnostics
Invoke-RestMethod -Uri http://localhost:5000/openai/models
```

### Test 3: Direct Orchestration Test
```powershell
# Create a test WAV file (or use existing)
# POST to /api/orchestrate with WAV file
# Expect response with transcribed text and response audio URI

# Example using curl (PowerShell equivalent):
$response = Invoke-RestMethod `
  -Uri "http://localhost:5000/api/orchestrate" `
  -Method Post `
  -InFile "test_audio.wav"

# Should return:
# {
#   "status": "ok",
#   "transcribedText": "...",
#   "inferenceResponse": "...",
#   "responseAudio": "/api/audio/response_1234567890.wav",
#   "latencyMs": 2500.5
# }
```

### Test 4: End-to-End Interaction
1. Start orchestration service: `python orchestration_service.py`
2. Deploy Misty skill
3. Say "Hey, Misty! What is 2+2?"
4. Robot should transcribe, infer, speak response within 6s

---

## Troubleshooting

### Issue: Misty Cannot Reach Windows Companion
**Symptom**: Orchestration requests fail with "SERVICE_UNREACHABLE"
**Causes**:
- Windows IP/port incorrect in skill config
- Firewall blocking port 5000
- Windows companion not running

**Solution**:
```powershell
# Verify IP and port
ipconfig  # Find IPv4 address of Windows machine
netstat -ano | findstr :5000  # Verify port 5000 is listening

# Open firewall
netsh advfirewall firewall add rule name="Foundry Orchestration" dir=in action=allow protocol=tcp localport=5000

# Restart service
python orchestration_service.py
```

### Issue: Foundry Local Not Responding
**Symptom**: Orchestration service logs "Foundry Local unreachable" or health returns "degraded"
**Causes**:
- Foundry Local process not running
- Wrong endpoint URL (auto-discovery extracts path — must strip to base URL)
- Models not fully loaded

**Solution**:
```powershell
# Check Foundry Local status
foundry service status
# Output: "Model management service is running on http://127.0.0.1:<PORT>/openai/status"
# NOTE: The orchestration service should use only http://127.0.0.1:<PORT> (no path)

# If not running, start it
foundry

# Verify models are loaded
curl http://127.0.0.1:<PORT>/openai/models
# Should list whisper-tiny and phi-3.5-mini model IDs

# Verify orchestration can reach it
curl http://localhost:5000/api/health
```

### Issue: High Latency (p95 > 6s)

> **Current state (2026-04-25):** End-to-end latency is ~23s (down from ~50s after tuning). TTS is 82% of the pipeline. See issue #21 for full optimization plan.

**Measured pipeline breakdown:**

| Stage | Time | Notes |
|-------|------|-------|
| STT (faster-whisper) | ~420ms | ✅ Fast |
| LLM (Phi-3.5-mini) | ~1,200ms | ✅ Acceptable |
| TTS (Kokoro-ONNX) | ~6,000ms | 🔴 Bottleneck — scales with response length |
| Network + playback | ~15,000ms | Includes audio upload/download and playback |

**Tuning applied:**
- `max_tokens`: reduced from 150 → 100 → 40 (forces shorter responses)
- System prompt: "ONE short sentence. 15 words max." (LLM often exceeds this — see #24)
- Kokoro speed: 1.1x → 1.2x
- Conversation history: capped at 6 messages (3 turns)

**Further solutions (see #21):**
1. Streaming TTS — synthesize in chunks as LLM tokens arrive
2. Evaluate faster TTS engines (piper-tts)
3. Pre-generate common responses
4. Reduce `RECORDING_DURATION_S` env var (default 4s)
5. Implement VAD to stop recording when user stops speaking (#20)

### Issue: Wake Word Not Triggering
**Causes**:
- Keyphrase recognition not started or in stale state
- Misty controller not connected (check WebSocket status)
- On-robot skills grabbing the microphone (all auto-start skills have been deleted, but check `GET /api/skills/running` to verify none are active)
- Microphone muted or battery too low (<10%)

**Solution**:
```powershell
$MISTY_IP = "10.0.0.44"

# Check for running skills (should be empty)
Invoke-RestMethod -Uri "http://$MISTY_IP/api/skills/running"

# Cancel any running on-robot skills (safety net)
Invoke-RestMethod -Uri "http://$MISTY_IP/api/skills/cancel" -Method POST

# Stop then restart keyphrase (always stop-start, never just start)
Invoke-RestMethod -Uri "http://$MISTY_IP/api/audio/keyphrase/stop" -Method POST
Start-Sleep 1
Invoke-RestMethod -Uri "http://$MISTY_IP/api/audio/keyphrase/start" -Method POST

# If still not working, reboot Misty (both params required!)
Invoke-RestMethod -Uri "http://$MISTY_IP/api/reboot" -Method POST `
    -ContentType "application/json" -Body '{"Core": true, "SensoryServices": true}'
```

> **Note:** All auto-starting skills (faceDetection, kids, mistycog, MistyReads, AnnounceKnownPerson) have been deleted from Misty. The controller also calls `_cancel_all_skills()` on WebSocket connect as a safety net. If new skills are ever deployed, ensure their `StartupRules` are set to `["Manual"]` only.

> **Keyphrase watchdog:** The controller now includes an automatic watchdog that detects silent keyphrase failure and self-recovers with escalating strategy (soft reset → sensory reboot → full reboot). If wake word stops working, wait up to 4 minutes for the watchdog to recover automatically before manual intervention.

### Issue: Skill Runtime Stuck (legacy — if using JS skills)
**Symptom**: Skills deploy successfully but don't execute. LED doesn't change. No skills in running list.
**Cause**: Rapid deploy/cancel cycles can break the skill runtime.
**Solution**: Reboot Misty. Cancel all skills first, then reboot.

<a id="why-not-on-robot-skills"></a>
### Why Not On-Robot JavaScript Skills?

We initially tried Misty's JavaScript SDK but abandoned it due to:
- **Silent crashes**: `SkillImageUri: null` in metadata crashes skills with no error
- **Runtime instability**: Rapid deploy/cancel cycles break the skill runtime entirely
- **API inconsistencies**: `BroadcastMode` must be string `"off"` not boolean; `StartupRules` values undocumented
- **Skills deleted**: All auto-start skills (faceDetection, kids, mistycog, MistyReads, AnnounceKnownPerson) have been removed. Metadata backed up to `misty-skills-backup/`
- **No debug logging**: `/api/skills/log` endpoint returns 404 on firmware v2.0.2
- **StartKeyPhraseRecognition crashes** inside skills — only works via REST API
- **Unreliable event model**: Callback naming conventions (`_eventName`) are fragile

The REST API + WebSocket approach is more reliable, easier to debug (all output on laptop), and avoids all skill runtime issues.
4. **Caching**: Foundry Local caches models in memory; warmup in advance

---

## Model Versions

All versions are locked for v1 reproducibility:

| Component | Model | Version | Provider | Size | Runtime |
|-----------|-------|---------|----------|------|---------|
| Chat | Phi-3.5-mini | latest | Microsoft/Hugging Face | ~3.8B | Foundry Local |
| STT | Whisper-tiny | latest | OpenAI/Hugging Face | ~39M | **faster-whisper** (in-process CTranslate2) |
| TTS | Kokoro | v1.0 (ONNX) | Hugging Face | ~82M | Standalone (`kokoro-onnx`) |
| TTS (fallback) | SAPI5 | — | Windows built-in | — | `pyttsx3` |

To update model versions:
1. For Chat: Edit `MODELS` dict in `orchestration_service.py`, verify new model is available in Foundry Local
2. For STT: Update the model name in `_get_whisper_model()` in `orchestration_service.py` (uses faster-whisper, auto-downloads from HuggingFace)
3. For TTS: Update the Kokoro ONNX model file and voice pack, or swap the TTS backend in the `text_to_speech()` function
4. Test latency against SLO
5. Update this document and `plans/planWindowsFoundry.prompt.md`

---

## Verification Checklist

- [ ] **Network**: Misty reaches Windows companion over local Wi-Fi
- [ ] **Wake word**: "Hey, Misty!" reliably triggers on robot
- [ ] **Recording**: Misty captures and sends WAV files
- [ ] **Foundry Local**: Models loaded and cold-start completed (~5 min)
- [ ] **Warm cache**: Latency p50 < 3s, p95 < 6s with models in memory
- [ ] **Offline**: After first-run, service works without internet
- [ ] **Fallbacks**: Service returns graceful errors for timeouts and failures
- [ ] **End-to-end**: Full interaction works (wake → transcribe → infer → speak)

---

## References

- Misty II API: https://docs.mistyrobotics.com/
- Foundry Local: https://learn.microsoft.com/en-us/azure/foundry-local/
- OpenAI API (compatible): https://platform.openai.com/docs/api-reference
- Phi-3.5-mini: https://huggingface.co/microsoft/Phi-3.5-mini-instruct
- Whisper-tiny: https://huggingface.co/openai/whisper-tiny
- Kokoro TTS: https://huggingface.co/hexgrad/Kokoro-82M

---

## Support & Debugging

**Logs**:
- Misty skill: View in Misty web interface or robot terminal
- Orchestration service: `orchestration.log` in service directory
- Foundry Local: Check Foundry Local process output

**Common Commands**:
```powershell
# Check if all services running
netstat -ano | findstr :5000  # Orchestration
netstat -ano | findstr :80    # Misty

# Test orchestration directly
Invoke-RestMethod -Uri http://localhost:5000/api/health

# Monitor latency
Get-Content -Path orchestration.log -Tail 20

# Kill stuck processes (if needed)
taskkill /PID <pid> /F
```

---

**Document Version**: 1.1  
**Last Updated**: 2026-04-24  
**Status**: Implementation Ready

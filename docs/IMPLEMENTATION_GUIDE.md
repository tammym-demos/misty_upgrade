# Misty + Foundry Local Implementation Guide

## Overview

This implementation integrates Misty II robot with Microsoft Foundry Local for on-device AI inference. The system architecture separates concerns:
- **Misty**: Audio interface (wake word detection, recording, playback)
- **Windows Companion**: Inference orchestration (STT → LLM → TTS)
- **Foundry Local**: Local inference engine (models, endpoints)

**Latency SLO**: p50 < 3s, p95 < 6s from wake word to audible response

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

**State Machine**: `DISCONNECTED → IDLE → RECORDING → PROCESSING → PLAYING → REARMING → IDLE`

**Functionality**:
- Connects to Misty via WebSocket (`ws://<ip>/pubsub`) for `KeyPhraseRecognized` events
- Issues REST API calls for recording, audio upload/download, LED, display
- Manages the full conversation turn cycle in a worker thread
- Auto-reconnects on WebSocket disconnect with exponential backoff
- Periodic health checks for Misty and orchestration service

**LED Color Scheme**:
- 🟢 Green = IDLE (listening for wake word)
- 🟠 Orange = RECORDING (capturing user speech)
- 🔵 Blue = PROCESSING (STT → LLM → TTS)
- 🟣 Purple = PLAYING (speaking response)
- 🔴 Red = ERROR (will auto-recover)

**Key Configuration** (environment variables):
- `MISTY_IP`: Robot IP address (default: `10.0.0.44`)
- `ORCHESTRATION_URL`: Orchestration service URL (default: `http://10.0.0.58:5000`)
- `RECORDING_DURATION_S`: How long to record after wake word (default: `4` seconds)

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
2. Calls Foundry Local `/v1/audio/transcriptions` (STT — Whisper-tiny)
3. Calls Foundry Local `/v1/chat/completions` (LLM — Phi-3.5-mini)
4. Synthesises speech locally via kokoro-onnx (TTS — Kokoro); falls back to pyttsx3/SAPI5 if unavailable
5. Returns response audio URI to Misty

> **Note:** Kokoro is **not** a Foundry Local model. It runs as a standalone
> Python library (`kokoro-onnx`) with its own ONNX model file and voice pack
> downloaded from HuggingFace. See the [TTS Architecture](#tts-architecture)
> section below for details.

**Error Handling**:
- Timeout detection at each stage
- Model load failure recovery
- Empty response detection
- Graceful fallback to Misty-side error handling

### 3. Foundry Local
**Deployment**: Local machine (same as Windows companion)

**Model Stack (Locked v1)**:
- Chat: `phi-3.5-mini` — Lightweight LLM (~3.8B params) — **via Foundry Local**
- STT: `whisper-tiny` — Fast speech-to-text — **via Foundry Local**

**Endpoints Used**:
- `POST /v1/chat/completions` — OpenAI-compatible LLM (use full model ID, e.g., `Phi-3.5-mini-instruct-openvino-gpu:2`)
- `POST /v1/audio/transcriptions` — Speech-to-text (use full model ID, e.g., `openai-whisper-tiny-generic-cpu:3`)
- `GET /openai/models` — List loaded models (returns array of model ID strings)

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
# Download the two models served by Foundry Local
foundry model download phi-3.5-mini
foundry model download whisper-tiny

# Load them into the running service
foundry model load phi-3.5-mini
foundry model load whisper-tiny
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
6. LED returns to **green** (ready for next interaction)

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
**Causes**:
- Large model sizes
- Network latency between laptop and Misty
- Recording duration too long
- Slow disk I/O for audio files

**Solutions**:
1. Verify warm-cache behavior: second request should be ~2s faster
2. Reduce output token limit in orchestration service (line ~200)
3. Reduce `RECORDING_DURATION_S` env var (default 4s)
4. Use SSD for response audio storage on Windows

### Issue: Wake Word Not Triggering
**Causes**:
- Keyphrase recognition not started or in stale state
- Misty controller not connected (check WebSocket status)
- Interfering on-robot skills (e.g., built-in faceDetection)
- Microphone muted

**Solution**:
```powershell
$MISTY_IP = "10.0.0.44"

# Cancel any running on-robot skills (they can interfere)
Invoke-RestMethod -Uri "http://$MISTY_IP/api/skills/cancel" -Method POST

# Stop then restart keyphrase (always stop-start, never just start)
Invoke-RestMethod -Uri "http://$MISTY_IP/api/audio/keyphrase/stop" -Method POST
Start-Sleep 1
Invoke-RestMethod -Uri "http://$MISTY_IP/api/audio/keyphrase/start" -Method POST

# If still not working, reboot Misty (both params required!)
Invoke-RestMethod -Uri "http://$MISTY_IP/api/reboot" -Method POST `
    -ContentType "application/json" -Body '{"Core": true, "SensoryServices": true}'
```

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
| STT | Whisper-tiny | latest | OpenAI/Hugging Face | ~39M | Foundry Local |
| TTS | Kokoro | v1.0 (ONNX) | Hugging Face | ~82M | Standalone (`kokoro-onnx`) |
| TTS (fallback) | SAPI5 | — | Windows built-in | — | `pyttsx3` |

To update model versions:
1. For Chat/STT: Edit `MODELS` dict in `orchestration_service.py`, verify new model is available in Foundry Local
2. For TTS: Update the Kokoro ONNX model file and voice pack, or swap the TTS backend in the `text_to_speech()` function
3. Test latency against SLO
4. Update this document and `plans/planWindowsFoundry.prompt.md`

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

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

### 1. Misty Skill (JavaScript)
**Location**: `src/misty-skill/`

**Files**:
- `FoundryLocalSkill.json` — Metadata for skill deployment
- `FoundryLocalSkill.js` — Main skill logic

**Functionality**:
- Listens for "Hey, Misty!" wake word
- Records audio (max 10s, ends on 800ms silence)
- Sends WAV to Windows companion `/api/orchestrate`
- Plays response audio via Misty speakers
- Handles fallback responses for errors

**Key Configuration**:
- `WINDOWS_HOST`: IP and port of Windows companion (default: `http://192.168.1.100:5000`)
- `RESPONSE_TIMEOUT_MS`: 6s max wait for orchestration
- Fallback messages for service unavailable, timeout, model load failure

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
- `POST /v1/chat/completions` — OpenAI-compatible LLM
- `POST /v1/audio/transcriptions` — Speech-to-text
- `GET /openai/models` — Model availability

### 4. TTS Architecture
<a id="tts-architecture"></a>

**Kokoro is not served by Foundry Local.** It is not present in the Foundry
model catalog. Instead, the orchestration service runs Kokoro as a standalone
Python library with a built-in fallback chain:

| Priority | Engine | Package | Quality | Notes |
|----------|--------|---------|---------|-------|
| Primary | Kokoro v1.0 (ONNX) | `kokoro-onnx` + `soundfile` | Natural, neural | Requires `kokoro-v1.0-quantized.onnx` and `voices-v1.0.bin` from [HuggingFace](https://huggingface.co/hexgrad/Kokoro-82M) |
| Fallback | pyttsx3 (SAPI5) | `pyttsx3` | Robotic but functional | Uses Windows built-in speech engine; no extra downloads needed |

**Setup for Kokoro (primary TTS):**
```powershell
pip install kokoro-onnx soundfile

# Download model files into the windows-orchestration directory
# - kokoro-v1.0-quantized.onnx
# - voices-v1.0.bin
# From: https://huggingface.co/hexgrad/Kokoro-82M
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
# Check the models endpoint (replace PORT with your actual port)
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
# Place these two files here (download from https://huggingface.co/hexgrad/Kokoro-82M):
#   - kokoro-v1.0-quantized.onnx
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

**2.3 Update Skill Configuration**

Edit `src/misty-skill/FoundryLocalSkill.js` and set `WINDOWS_HOST` to the
Windows companion's local IP address and orchestration port:

```javascript
const CONFIG = {
  WINDOWS_HOST: "http://192.168.1.XXX:5000",  // ← Replace with your Windows IP
  // ... rest of config stays the same
};
```

To find your Windows IP:
```powershell
(Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias Wi-Fi).IPAddress
```

**2.4 Deploy Skill to Misty via REST API**

Upload both skill files to Misty using its REST API:

```powershell
# Set your Misty IP
$MISTY_IP = "192.168.1.XXX"   # ← Replace with Misty's IP

# Upload the skill (both .json metadata and .js code)
# Option A: Use Misty's Skill Runner web interface
#   1. Open http://$MISTY_IP in a browser
#   2. Navigate to the Skill Runner page
#   3. Upload FoundryLocalSkill.json and FoundryLocalSkill.js
#   4. Click "Upload & Run"

# Option B: Use the REST API to save the skill
$metaJson = Get-Content -Raw "src\misty-skill\FoundryLocalSkill.json"
$codeJs   = Get-Content -Raw "src\misty-skill\FoundryLocalSkill.js"
$body = @{
    Meta = ($metaJson | ConvertFrom-Json)
    Code = $codeJs
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://$MISTY_IP/api/skills" `
    -Method Post `
    -ContentType "application/json" `
    -Body $body
```

**2.5 Start the Skill on Misty**
```powershell
# Run the skill by its unique ID (from FoundryLocalSkill.json)
Invoke-RestMethod -Uri "http://$MISTY_IP/api/skills/start" `
    -Method Post `
    -ContentType "application/json" `
    -Body '{"UniqueId": "5f2b3c2b-4d3c-4e2b-8f1a-9d8c7b6a5e4d"}'

# Verify the skill is running
Invoke-RestMethod -Uri "http://$MISTY_IP/api/skills/running"
```

**2.6 Test Wake Word Detection**
1. Say **"Hey, Misty!"** near the robot
2. Robot should respond with a confirmation beep or LED change
3. Speak your question (up to 10 seconds; recording stops after 800ms of silence)
4. Misty should play back a spoken response within ~6 seconds

**2.7 Check Skill Logs (if something goes wrong)**
```powershell
# Get recent skill log messages from Misty
Invoke-RestMethod -Uri "http://$MISTY_IP/api/skills/log?UniqueId=5f2b3c2b-4d3c-4e2b-8f1a-9d8c7b6a5e4d"

# Look for:
#   "FoundryLocalSkill initialized"   → skill started OK
#   "Wake word detected"               → mic is working
#   "Recording complete"               → audio captured
#   "Orchestration response received"  → round-trip to Windows succeeded
```

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
**Symptom**: Orchestration service logs "Foundry Local unreachable"
**Causes**:
- Foundry Local process crashed
- Port conflict (5000 in use)
- Models not fully loaded

**Solution**:
```powershell
# Check Foundry Local is running
tasklist | findstr python  # Look for foundry process

# Restart Foundry Local
foundry --port 5000 --host 0.0.0.0

# Wait 30s for models to initialize
Start-Sleep -Seconds 30

# Verify
Invoke-RestMethod -Uri http://localhost:5000/api/health
```

### Issue: High Latency (p95 > 6s)
**Causes**:
- Large model sizes
- Network latency between Misty and Windows
- Silence detection timeout on Misty
- Slow disk I/O for audio files

**Solutions**:
1. Verify warm-cache behavior: second request should be ~2s faster
2. Reduce output token limit in orchestration service (line ~200)
3. Trim silence aggressively in Misty skill
4. Use SSD for response audio storage on Windows

### Issue: Wake Word Not Triggering
**Causes**:
- Skill not deployed correctly
- Microphone muted or disconnected
- Wake word model not initialized

**Solution**:
```powershell
# SSH into Misty (if available)
# Check skill logs for errors
# Verify microphone level via Misty API

# Re-deploy skill from scratch
# Restart Misty if needed
```

---

## Latency Tuning

**Latency Budget Breakdown** (target p50 < 3s):
- Audio recording (user): 1000ms (user talks)
- Network transmission: 100ms
- STT processing: 1500ms
- LLM inference: 2000ms
- TTS synthesis: 1500ms
- Audio playback: 500ms (starts immediately)
- **Total: ~5.2s for p50** (achievable with warm models)

**Optimization Knobs**:
1. **Silence trimming**: Adjust `SILENCE_THRESHOLD_MS` in Misty skill
2. **LLM token limit**: Reduce `max_tokens` in orchestration service
3. **Model selection**: Use smaller models (Phi-3.5-mini is already minimal)
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

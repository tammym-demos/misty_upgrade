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
                                                   localhost:5000
                                                           │
                                                   ┌───────▼─────────┐
                                                   │ Foundry Local   │
                                                   │ • Model server  │
                                                   │ • Endpoints:    │
                                                   │   /chat/compl   │
                                                   │   /transcribe   │
                                                   │   /speech       │
                                                   └─────────────────┘
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
2. Calls Foundry Local `/v1/audio/transcriptions` (STT)
3. Calls Foundry Local `/v1/chat/completions` (LLM)
4. Calls Foundry Local `/v1/audio/speech` (TTS)
5. Returns response audio URI to Misty

**Error Handling**:
- Timeout detection at each stage
- Model load failure recovery
- Empty response detection
- Graceful fallback to Misty-side error handling

### 3. Foundry Local
**Deployment**: Local machine (same as Windows companion)

**Model Stack (Locked v1)**:
- Chat: `phi-3.5-mini` — Lightweight LLM (~3.8B params)
- STT: `whisper-tiny` — Fast speech-to-text
- TTS: `kokoro-v0_19` — Voice synthesis

**Endpoints Used**:
- `POST /v1/chat/completions` — OpenAI-compatible LLM
- `POST /v1/audio/transcriptions` — Speech-to-text
- `POST /v1/audio/speech` — Text-to-speech
- `GET /openai/models` — Model availability

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
# Follow Microsoft's Foundry Local setup guide
# https://learn.microsoft.com/en-us/azure/foundry-local/

# Typical installation
pip install foundry-local
```

**1.3 Configure Foundry Local**
```powershell
# Set Foundry to serve on all interfaces (not just localhost)
# Edit Foundry Local config to expose port 5000

# Start Foundry Local
foundry --port 5000 --host 0.0.0.0
```

**1.4 Verify Foundry Local is Running**
```powershell
# Check models endpoint
Invoke-RestMethod -Uri http://localhost:5000/openai/models

# Should return list of available models including:
# - phi-3.5-mini
# - whisper-tiny
# - kokoro-v0_19
```

**1.5 Install Orchestration Service**
```powershell
cd src\windows-orchestration
pip install -r requirements.txt

# Copy .env.example to .env and update FOUNDRY_LOCAL_HOST if needed
copy .env.example .env
```

**1.6 Start Orchestration Service**
```powershell
cd src\windows-orchestration
python orchestration_service.py

# Should output:
# * Running on http://0.0.0.0:5000
# * WARNING: This is a development server
```

**1.7 Verify Orchestration Service**
```powershell
# In another terminal
Invoke-RestMethod -Uri http://localhost:5000/api/health

# Should return:
# {
#   "status": "ok",
#   "orchestration": "ready",
#   "models": { "chat": "phi-3.5-mini", "stt": "whisper-tiny", "tts": "kokoro-v0_19" }
# }
```

### Phase 2: Misty Robot Setup

**2.1 Verify Network Connectivity**
```powershell
# Ping Misty (replace IP as needed)
ping 192.168.1.100

# Verify REST API is reachable
Invoke-RestMethod -Uri http://192.168.1.100/api/device
```

**2.2 Update Skill Configuration**
Edit `src/misty-skill/FoundryLocalSkill.js`:
```javascript
const CONFIG = {
  WINDOWS_HOST: "http://192.168.1.XXX:5000",  // Update with Windows companion IP
  // ... rest of config
};
```

**2.3 Deploy Skill to Misty**
Using Misty's skill deployment tool (web interface or SDK):
1. Upload `FoundryLocalSkill.json` and `FoundryLocalSkill.js`
2. Set skill to start on robot startup
3. Verify skill appears in skill list

**2.4 Test Wake Word Detection**
1. Say "Hey, Misty!" near the robot
2. Robot should respond with a confirmation beep or LED change
3. Check robot logs for `"Wake word detected"` messages

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

| Component | Model | Version | Provider | Size |
|-----------|-------|---------|----------|------|
| Chat | Phi-3.5-mini | latest | Microsoft/Hugging Face | ~3.8B |
| STT | Whisper-tiny | latest | OpenAI/Hugging Face | ~39M |
| TTS | Kokoro | v0_19 | Hugging Face | ~100M |

To update model versions:
1. Edit `MODELS` dict in `orchestration_service.py`
2. Verify new model is available in Foundry Local
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

**Document Version**: 1.0  
**Last Updated**: 2026-04-19  
**Status**: Implementation Ready

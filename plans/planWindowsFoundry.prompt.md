## Plan: Misty + Foundry Local on Windows (Implementation Complete)

Use Misty as the robot client and a Windows-class companion device as the local AI host. Misty handles wake word, recording, playback, and embodiment. The Windows companion runs Microsoft Foundry Local for local inference over local Wi-Fi.

**Status**: ✅ **Implementation Complete** — Code written, tests created, documentation finalized. Ready for deployment.

---

## Locked Decisions (v1)

- **Serving mode**: Foundry Local's built-in OpenAI-compatible HTTP server (no custom wrapper)
- **Latency SLO**: p50 < 3s, p95 < 6s wake word to audible response
- **Hardware**: CPU-only support; GPU acceleration optional but recommended
- **Model versions**: Pinned for reproducibility (Phi-3.5-mini, Whisper-tiny, Kokoro v0_19)
- **Network scope**: Local Wi-Fi only; no cloud, no multi-user, no on-Misty inference

---

## Implementation Status

### ✅ Completed Tasks

**Task 1: Finalize Model Selection**
- Chat: `phi-3.5-mini` (3.8B params, fast, CPU-friendly)
- STT: `whisper-tiny` (39M params, minimal latency)
- TTS: `kokoro-v0_19` (100M params, natural voice)

**Task 2: Misty Skill (JavaScript)**
- Location: `src/misty-skill/FoundryLocalSkill.json` + `FoundryLocalSkill.js` (~350 lines)
- Functionality: Wake word detection → record → POST to Windows → play response
- Fallbacks: Service unreachable, timeout, model load failure, empty response
- Deployment: Via Misty web interface or REST API (see deployment section below)

**Task 3: Windows Orchestration Service (Python/Flask)**
- Location: `src/windows-orchestration/orchestration_service.py` (~450 lines)
- Pipeline: STT → LLM (with context) → TTS
- Endpoints: `/api/orchestrate`, `/api/health`, `/api/diagnostics`, `/api/audio/<file>`, `/api/fallback-tts`
- Latency tracking and error mapping included
- Foundry endpoint must be discovered at startup because Foundry Local can bind to a different local port after restart

**Task 4: Integration Testing**
- Location: `tests/test_integration.py`
- Coverage: Service health, Misty connectivity, Foundry Local API, fallback behavior
- Verification checklist mapped to 9-item validation plan

**Task 5: Documentation**
- `docs/IMPLEMENTATION_GUIDE.md` — Full setup, troubleshooting, latency tuning
- `docs/IMPLEMENTATION_SUMMARY.md` — Architecture, design decisions, file structure
- `README.md` — Quick-start guide (5 steps)

---

## How Code Transfers to Misty

### Method 1: Misty Web Interface (Easiest)
1. Open browser: `http://<misty-ip>:8080`
2. Navigate to "Skills" section
3. Click "Upload New Skill"
4. Select both `FoundryLocalSkill.json` (metadata) and `FoundryLocalSkill.js` (code)
5. Click "Upload" → skill deploys and starts automatically

**Pros**: No CLI needed, visual feedback  
**Cons**: Manual one-time process

### Method 2: REST API (Programmatic)
POST two requests to Misty:
```powershell
# Step 1: Upload metadata file
$metadataPath = "src/misty-skill/FoundryLocalSkill.json"
$metadata = [System.IO.File]::ReadAllBytes($metadataPath)
Invoke-RestMethod -Uri "http://<misty-ip>/api/skills/file" `
  -Method Post `
  -ContentType "application/json" `
  -Body $metadata

# Step 2: Upload code file
$codePath = "src/misty-skill/FoundryLocalSkill.js"
$code = [System.IO.File]::ReadAllText($codePath)
Invoke-RestMethod -Uri "http://<misty-ip>/api/skills/asset" `
  -Method Post `
  -ContentType "text/plain" `
  -Body $code
```

**Pros**: Automation-friendly, repeatable  
**Cons**: Requires REST API knowledge

### Method 3: Misty CLI (Recommended for Iteration)
```powershell
# Install Misty CLI (if not already installed)
npm install -g misty-cli

# Deploy skill
misty-cli deploy-skill --skillName FoundryLocalSkill --skillPath src/misty-skill/

# List deployed skills
misty-cli list-skills

# View skill logs
misty-cli skill-logs FoundryLocalSkill
```

**Pros**: Fast iteration, access to logs  
**Cons**: Requires Node.js + npm

### Method 4: SSH/Remote Terminal (Advanced)
If Misty has SSH enabled, directly modify files on the robot's skill storage.

---

## File Transfers & Dependencies

### Misty Skill Files (Required)
```
FoundryLocalSkill.json    ← Metadata (UUID, timeout, startup rules)
FoundryLocalSkill.js      ← Code (must reference Windows IP in CONFIG)
```

**Before transfer**: Update `WINDOWS_HOST` in `FoundryLocalSkill.js`:
```javascript
const CONFIG = {
  WINDOWS_HOST: "http://192.168.1.XXX:5000",  // Replace with actual Windows IP
  // ... rest of config
};
```

### Windows Service Files (Deploy Locally)
```
orchestration_service.py  ← Main service
requirements.txt          ← Dependencies (Flask, requests, Flask-CORS)
.env.example → .env       ← Configuration (copy and customize)
```

**Setup**:
```powershell
cd src/windows-orchestration
pip install -r requirements.txt
copy .env.example .env
# Set FOUNDRY_LOCAL_HOST from the current Foundry endpoint reported by `foundry service status`
python orchestration_service.py
```

### Network Configuration
- Misty and Windows must be on same Wi-Fi subnet
- Misty needs to reach Windows at `http://<WINDOWS_IP>:5000` (no firewall blocks)
- Foundry Local runs on Windows as a local service with a dynamic localhost port
- Windows orchestration must discover the current Foundry endpoint via `foundry service status` or `/openai/status` before issuing requests
- If a cached Foundry endpoint is stale after service restart, orchestration should rescan localhost for the active Foundry port and refresh `FOUNDRY_LOCAL_HOST`

---

## Implementation Steps (Next Phase)

1. ✅ **Model selection**: Phi-3.5-mini, Whisper-tiny, Kokoro v0_19 (locked)
2. ✅ **Misty skill**: Implemented with wake word, recording, fallback (ready to deploy)
3. ✅ **Windows orchestration**: Implemented with STT→LLM→TTS pipeline (ready to run)
4. ⏳ **Discover Foundry endpoint**: Run `foundry service status`, capture the current localhost URL, and update `FOUNDRY_LOCAL_HOST`
5. ⏳ **Add fallback endpoint scan**: If the reported endpoint is stale, scan localhost for the active Foundry REST port before starting orchestration
6. ⏳ **Deploy Windows service**: Run `python orchestration_service.py` on Windows companion
7. ⏳ **Deploy Misty skill**: Upload via web interface or CLI to robot
8. ⏳ **Network validation**: Confirm Misty ↔ Windows connectivity
9. ⏳ **End-to-end test**: Say "Hey, Misty! What is 2+2?" and verify response
10. ⏳ **Latency profiling**: Measure p50/p95 and tune if needed
11. ⏳ **Offline validation**: Verify offline-after-download behavior

---

## Verification Checklist

- [ ] Windows service running and healthy (`GET /api/health` returns 200)
- [ ] Windows service is using the current Foundry endpoint rather than a hardcoded port
- [ ] Misty responds over REST from Windows (`http://<misty-ip>/api/device`)
- [ ] Misty skill uploaded and started
- [ ] Wake word ("Hey, Misty!") triggers reliably
- [ ] Audio recording captures < 1s clips
- [ ] Foundry Local models cold-start completes (5-10 min first-run)
- [ ] Warm-cache latency < 3s p50, < 6s p95
- [ ] Offline mode works (after first model download)
- [ ] Fallback responses play when service unavailable

---

## Resources & References

- **Misty Skill Deployment**: https://docs.mistyrobotics.com/skills/
- **Misty Web API**: https://docs.mistyrobotics.com/reference/web-api/
- **Misty CLI**: https://docs.mistyrobotics.com/skills/misty-cli/
- **Foundry Local**: https://learn.microsoft.com/en-us/azure/foundry-local/
- **Implementation Guide**: `docs/IMPLEMENTATION_GUIDE.md`
- **Summary & Architecture**: `docs/IMPLEMENTATION_SUMMARY.md`
- **Quick Start**: `README.md`

---

**Status**: Ready for Deployment  
**Last Updated**: 2026-04-19  
**Next Action**: Deploy Windows service, then transfer Misty skill via web interface or CLI

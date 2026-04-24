# Plan: Misty as a Conversational Robot

Make Misty a fully working conversational robot: wake word → STT → specialist-personality LLM → neural TTS → expressive face + LED feedback.

---

## Architecture

```
"Hey, Misty!" → [Misty: KeyPhraseRecognized]
    → [Misty: StartRecordingAudio]
    → [Misty: GetAudioFile → SendExternalRequest POST WAV to Windows /api/orchestrate]
        → [Windows: whisper-tiny STT via Foundry Local]
        → [Windows: phi-3.5-mini LLM via Foundry Local + system prompt]
        → [Windows: kokoro-onnx TTS → WAV file]
    → [Misty: SendExternalRequest GET WAV → SaveAudio → PlayAudio]
    → [Misty: face expression + LED color per state]
    → [loop: re-arm wake word]
```

**Boundary:** All AI runs on the Windows laptop. Misty handles embodiment (wake word, audio I/O, face, LED). They communicate over local Wi-Fi via the Flask orchestration service.

**Misty SDK constraints** (verified against https://docs.mistyrobotics.com/misty-ii/coding-misty/javascript-sdk-architecture):
- External HTTP calls must use `misty.SendExternalRequest(method, url, contentType, data, headers, uniqueId)` — not `misty.SendRequest()`
- `misty.PlayAudio()` only plays files on Misty's onboard storage — cannot stream from URLs
- File uploads require manual multipart body construction using `misty.GetAudioFile()` bytes
- Response handling uses registered event callbacks, not inline callback parameters

---

## Locked Decisions

| Decision | Choice | Reason |
|---|---|---|
| Chat model | `phi-3.5-mini` | Low latency, CPU-friendly on ARM64 |
| STT model | `whisper-tiny` | Minimal memory, fast on CPU |
| TTS (primary) | `kokoro-onnx` + `af_heart` voice | Neural quality, fully offline, native win-arm64 via onnxruntime |
| TTS (fallback) | `pyttsx3` SAPI5 | Guaranteed on any Windows, no install friction |
| Foundry port | Auto-discovered via `foundry service status` | Port changes on restart; never hardcode |
| Flask port | `5050` | Avoids conflict with Foundry Local (which may use 5000) |
| Personality | `SYSTEM_PROMPT` env var | Configurable without code changes |
| Network | Local Wi-Fi only | No cloud, no internet dependency after first-run |
| Latency SLO | p50 < 3s, p95 < 6s | Wake word to audible response |

---

## Evaluation Findings (2026-04-19)

Cross-referenced against Misty SDK docs, Foundry Local docs, and Kokoro HuggingFace page. Full analysis in session plan file. **16 findings total — 10 critical/high, 6 medium.**

### 🔴 Critical: Architecture-Level (Misty SDK Mismatches)

| # | Finding | File | Fix |
|---|---|---|---|
| 1 | `misty.SendRequest()` is wrong API — must use `misty.SendExternalRequest()` with positional params | FoundryLocalSkill.js:106-114 | Rewrite all HTTP calls |
| 2 | `misty.PlayAudio()` can't play from URLs — only onboard files | FoundryLocalSkill.js:150 | Fetch WAV → `SaveAudio()` → `PlayAudio()` |
| 3 | No built-in multipart upload — must manually construct body | FoundryLocalSkill.js:106-114 | `GetAudioFile()` → manual multipart body |

### 🔴 Critical: Integration Bugs

| # | Finding | File | Fix |
|---|---|---|---|
| 4 | Flask and Foundry Local both on port 5000 | orchestration_service.py:487 | Flask → port 5050 |
| 5 | Latency budget: cumulative elapsed vs per-stage cap = false timeouts | orchestration_service.py:220,268,366 | Use cumulative deadlines |
| 6 | Response audio URIs are relative — Misty needs absolute URLs | orchestration_service.py:202 | Prepend base URL |
| 7 | Fallback TTS: JS reads `audioUri`, Python returns `audio_uri` | FoundryLocalSkill.js:181 | Align key names |

### 🟡 High: Reliability

| # | Finding | File | Fix |
|---|---|---|---|
| 8 | Recording timeout fires once at startup, never enforces 10s limit | FoundryLocalSkill.js:80-88 | Start timer in wake word handler |
| 9 | `recordingStartTime` zeroed before duration computed | FoundryLocalSkill.js:74-76,94-96 | Capture duration first |
| 10 | Wake word rearms before fallback audio finishes → self-trigger | FoundryLocalSkill.js:120-123 | Rearm in playback callback |

### 🟢 Medium: Robustness & Docs

| # | Finding | Fix |
|---|---|---|
| 11 | Foundry endpoint discovered only at startup; stale after restart | Re-discover on connection failure |
| 12 | Global conversation history: not thread-safe, orphaned on failure | Per-session or lock + rollback |
| 13 | Test asserts `tts` key in MODELS dict — doesn't exist | Remove assertion |
| 14 | `model_load_failure` error code never returned by service | Map Foundry errors |
| 15 | Docs reference `kokoro-v0_19` Foundry model; 3 Misty links are 404 | Update docs + links |
| 16 | Kokoro model files hardcoded with no download guidance | Add env vars + docs |

### Reference Link Audit

| Link | Status |
|---|---|
| https://docs.mistyrobotics.com/ | ✅ Live |
| https://docs.mistyrobotics.com/misty-ii/coding-misty/javascript-sdk-architecture | ✅ Live |
| https://docs.mistyrobotics.com/skills/ | ❌ 404 |
| https://docs.mistyrobotics.com/reference/web-api/ | ❌ 404 |
| https://docs.mistyrobotics.com/skills/misty-cli/ | ❌ 404 |
| https://learn.microsoft.com/en-us/azure/foundry-local/ | ✅ Live |
| https://huggingface.co/hexgrad/Kokoro-82M | ✅ Live (v1.0 current, not v0_19) |

### ✅ Decisions Verified as Sound

| Decision | Verdict |
|---|---|
| phi-3.5-mini for chat | ✅ Good — low latency, CPU-friendly |
| whisper-tiny for STT | ✅ Good — minimal memory, fast |
| kokoro-onnx for TTS | ✅ Good — neural quality, offline, win-arm64 |
| pyttsx3 as TTS fallback | ✅ Good — zero-install safety net |
| Local Wi-Fi only | ✅ Good — privacy, reliability |
| SYSTEM_PROMPT as env var | ✅ Good — configurable |
| Auto-discover Foundry port | ✅ Good intent — needs retry logic (#11) |

---

## Implementation Status

### ✅ Completed

**`src/windows-orchestration/orchestration_service.py`**
- `load_dotenv()` called before all `os.getenv()` — `.env` file is loaded correctly
- Dynamic Foundry endpoint discovery at startup via `foundry service status` subprocess + regex parse; env var override takes precedence
- TTS replaced: `kokoro-onnx` primary (neural WAV via `soundfile`), `pyttsx3` SAPI5 fallback; both lazy-loaded on first use
- Personality system prompt: `SYSTEM_PROMPT` env var prepended to every LLM call (not stored in conversation history)
- `KOKORO_VOICE` env var controls the kokoro voice (default: `af_heart`)

**`src/windows-orchestration/requirements.txt`**
- Added: `kokoro-onnx`, `onnxruntime`, `soundfile`, `pyttsx3`

### ⬜ Must Fix Before Deployment

**Phase 2a — Fix Misty skill SDK usage** (Critical — Findings #1-3)

Rewrite `FoundryLocalSkill.js` HTTP and audio handling to match actual Misty JS SDK:
- Replace `misty.SendRequest()` with `misty.SendExternalRequest(method, url, contentType, data, headers, uniqueId)`
- Use `misty.GetAudioFile("foundry_input.wav", true)` to retrieve recorded WAV bytes
- Manually construct multipart/form-data body with boundary for WAV upload
- Register event callbacks for `SendExternalRequest` responses
- Implement download → `misty.SaveAudio()` → `misty.PlayAudio(filename)` flow for response audio

**Phase 2b — Fix orchestration service bugs** (Critical — Findings #4-7)

- Change Flask port from 5000 to 5050 (`orchestration_service.py` line 487)
- Fix latency budget: use cumulative deadline thresholds (STT=1500ms, LLM=3500ms, TTS=5000ms)
- Return absolute audio URLs (prepend `http://<host>:<port>` to `/api/audio/...` paths)
- Align fallback TTS JSON key: return `audioUri` (camelCase) to match JS client

**Phase 2c — Fix recording and rearm timing** (High — Findings #8-10)

- Start recording timeout timer inside `KeyPhraseRecognized` handler (not at skill startup)
- Capture recording duration before clearing `recordingStartTime`
- Move `rearmWakeWord()` into fallback playback completion callback

---

### ⬜ Remaining (original phases)

**Phase 3 — `src/misty-skill/FoundryLocalSkill.js`: expressions + LED**

Add `setRobotState(state)` helper using Misty's built-in expression images and RGB chest LED. Wire into all existing event handlers.

| State | Trigger | `misty.DisplayImage()` | `misty.ChangeLED()` |
|---|---|---|---|
| `idle` | Startup + after each response | `e_ContentLeft.jpg` | Blue (0, 80, 255) |
| `recording` | Wake word fires | `e_Surprise.jpg` | Cyan (0, 200, 200) |
| `thinking` | Recording stops, awaiting response | `e_Disoriented.jpg` | Purple (150, 0, 200) |
| `speaking` | Playback starts | `e_Joy.jpg` | Green (0, 200, 0) |
| `error` | Any fallback/failure path | `e_Anger.jpg` | Red (255, 0, 0) → 2s → idle |

Wire points in `FoundryLocalSkill.js`:
- `startWakeWordDetection()` → `setRobotState("idle")`
- `KeyPhraseRecognized` callback → `setRobotState("recording")`
- `handleRecordingComplete()` → `setRobotState("thinking")`
- Successful `misty.PlayAudio()` call → `setRobotState("speaking")`
- All `playFallback()` paths → `setRobotState("error")`

**Phase 4 — `src/windows-orchestration/.env.example`**

Add:
```
SYSTEM_PROMPT=You are Misty, a helpful and friendly robot assistant. Keep answers concise and conversational — no more than 2-3 sentences. Be warm and engaging.
KOKORO_VOICE=af_heart
KOKORO_MODEL_PATH=kokoro-v1.0-quantized.onnx
KOKORO_VOICES_PATH=voices-v1.0.bin
```

---

## Deploy & Verify

### Step 1: Install Python dependencies
```powershell
cd "C:\Users\tmcclell\OneDrive - Microsoft\Source\Misty\src\windows-orchestration"
pip install -r requirements.txt
```

### Step 2: Install espeak-ng (required by kokoro-onnx for phoneme fallback)
Download and run the x64 MSI from:
https://github.com/espeak-ng/espeak-ng/releases

> Runs via x64 emulation on Snapdragon Windows — this is expected and fine.

### Step 3: Create `.env`
```powershell
copy .env.example .env
```
Edit `.env`:
- Set `SYSTEM_PROMPT` to your specialist personality text
- Optionally set `KOKORO_VOICE` (see voice list below)
- Leave `FOUNDRY_LOCAL_HOST` blank for auto-discovery, or paste the URL from `foundry service status`

**Voice options for `KOKORO_VOICE`:**
| Value | Description |
|---|---|
| `af_heart` | Warm American female (default) |
| `am_fenrir` | Authoritative American male |
| `bf_emma` | British female |

### Step 4: Find your Windows IP
```powershell
ipconfig
```
Update `CONFIG.WINDOWS_HOST` in `src/misty-skill/FoundryLocalSkill.js` line ~7 (port 5050).

### Step 5: Start Foundry Local models
```powershell
foundry model load phi-3.5-mini
foundry model load whisper-tiny
```

### Step 6: Start orchestration service
```powershell
python orchestration_service.py
```
Check startup log — it should print the resolved Foundry endpoint (not `localhost:5000`). Flask runs on port 5050.

### Step 7: Verify health
```
GET http://localhost:5050/api/health
```
Expected: `{"status": "ok", "foundry_local": "ok"}`

### Step 8: Smoke-test TTS
```
POST http://localhost:5050/api/fallback-tts
{"text": "Hello, I am Misty"}
```
Expected: returns `{"audioUri": "http://<host>:5050/api/audio/response_<timestamp>.wav"}` → GET that URI returns a valid WAV file.

### Step 9: Deploy Misty skill
Upload both files to Misty via Skill Runner UI (http://<misty-ip>:8080) or REST API.

### Step 10: End-to-end test
Say **"Hey, Misty!"** — verify the full sequence:
1. Face shows `e_Surprise.jpg` + cyan LED
2. Speak a question (e.g., "What is the speed of light?")
3. Face goes `e_Disoriented.jpg` + purple (thinking)
4. Misty speaks the answer in a natural voice
5. Face shows `e_Joy.jpg` + green LED during playback
6. Returns to `e_ContentLeft.jpg` + blue (idle/listening)

**Error path:** Stop the Windows service → say "Hey, Misty!" → Misty should show `e_Anger.jpg` + red LED + speak a fallback error message.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `foundry_local: unreachable` in health check | Run `foundry service status` — paste the URL into `FOUNDRY_LOCAL_HOST` in `.env` |
| kokoro-onnx fails on first run | Check espeak-ng is installed; it downloads ~80 MB model on first call |
| pyttsx3 saves empty WAV | Ensure `engine.runAndWait()` is called after `save_to_file` |
| Misty skill won't start | Check `FoundryLocalSkill.json` UUID is unique; verify both files uploaded |
| No audio on Misty | Confirm `CONFIG.WINDOWS_HOST` IP is the Windows laptop's LAN IP with port 5050, not `127.0.0.1` |
| `KeyPhraseRecognized` never fires | Misty must not be recording simultaneously; confirm `StopRecordingAudio` was called before `StartKeyPhraseRecognition` |
| `SendExternalRequest` fails silently | Register the response event callback; check Misty debug logs for errors |
| Response audio doesn't play | Verify download→SaveAudio→PlayAudio flow; check file is WAV PCM 16-bit 48kHz mono |

---

## Files

| File | Purpose | Status |
|---|---|---|
| `src/misty-skill/FoundryLocalSkill.js` | Misty robot skill | ⬜ Needs SDK fixes (Phase 2a) + expressions/LED (Phase 3) |
| `src/misty-skill/FoundryLocalSkill.json` | Skill manifest | ✅ Ready |
| `src/windows-orchestration/orchestration_service.py` | Flask pipeline: STT → LLM → TTS | ⬜ Needs bug fixes (Phase 2b) |
| `src/windows-orchestration/requirements.txt` | Python dependencies | ✅ Updated |
| `src/windows-orchestration/.env.example` | Config template | ⬜ Needs SYSTEM_PROMPT + KOKORO_VOICE + model paths (Phase 4) |
| `docs/FOUNDRY_LOCAL_SETUP.md` | Foundry CLI setup guide | ✅ Current |

---

## References

| Resource | URL | Status |
|---|---|---|
| Misty II JS SDK Architecture | https://docs.mistyrobotics.com/misty-ii/coding-misty/javascript-sdk-architecture | ✅ Live |
| Misty II Main Docs | https://docs.mistyrobotics.com/ | ✅ Live (older version warning) |
| Foundry Local | https://learn.microsoft.com/en-us/azure/foundry-local/ | ✅ Live |
| Kokoro TTS (v1.0) | https://huggingface.co/hexgrad/Kokoro-82M | ✅ Live |
| Phi-3.5-mini | https://huggingface.co/microsoft/Phi-3.5-mini-instruct | Reference |
| Whisper-tiny | https://huggingface.co/openai/whisper-tiny | Reference |

---

**Version**: 3.0
**Last Updated**: 2026-04-19
**Status**: Blocked on Phase 2a-2c fixes before deployment

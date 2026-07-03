# Foundry Local Setup for Windows

Complete step-by-step installation guide for Foundry Local on Windows PC.

---

## Prerequisites

- **Windows 10/11** with internet connection (first-run only)
- **Administrator rights** to install local software
- **~10 GB disk space** (for models + runtime)
- **4+ GB RAM** recommended (8GB+ for comfortable operation)
- Optional on Intel NPU systems: install the Intel NPU driver for best acceleration

---

## Step 1: Install Foundry Local CLI

Install Foundry Local directly with `winget`:

```powershell
winget install Microsoft.FoundryLocal
```

### Verify Installation
```powershell
foundry --version
foundry --help
```

If `foundry` installs but the local service is unreachable later, restart it:

```powershell
foundry service restart
```

---

## Step 2: Verify the Local Service

```powershell
foundry service status
```

This confirms the Foundry Local service is installed and shows the current local endpoint.

**Expected result:** the service reports as running and prints a local URL.

If the service is not running:

```powershell
foundry service start
foundry service status
```

---

## Step 3: Trigger Execution Provider Setup

Run the model catalog once so Foundry Local can download any hardware-specific execution providers it needs for this PC.

```powershell
foundry model list
```

On first run, this can take a few minutes while Foundry Local downloads the required execution providers for your hardware.

You can also filter the list if you want to see CPU-friendly chat models first:

```powershell
foundry model list --filter task=chat-completion
```

---

## Step 4: Verify CLI Installation

```powershell
foundry --help
foundry service status
```

**Expected output:**
```
Foundry Local CLI help text displays
The service reports a running state and a local endpoint URL
```

**Note for Windows on ARM:** you might still see a `cpuinfo` or `Unknown CPU vendor` warning in some Foundry Local operations. If the CLI commands succeed and the service reports a valid endpoint, continue.

---

## Step 5: Download Models (First-Run Only)

**⚠️ WARNING**: First-run model download takes **5-15 minutes** and requires stable internet.

**Recommended chat model for this project:** keep `phi-3.5-mini` as the default. It is the better fit for Misty's real-time interaction loop because it keeps latency and memory pressure lower on this laptop.

**Optional alternative:** `phi-4-mini` can improve response quality, but it is more likely to increase latency. Only switch to it if you benchmark the full wake-word-to-response path and confirm the user experience is still acceptable.

**Important TTS note:** Kokoro TTS is **not** a Foundry model — it runs in-process in the orchestration service via `kokoro-onnx`. Similarly, **STT uses faster-whisper in-process**, not Foundry Local's Whisper. Foundry Local only serves the **chat/LLM** model.

Download the chat model that Foundry serves:

```powershell
foundry model download phi-3.5-mini
```

> **Note:** You do not need to download whisper-tiny via Foundry. The orchestration
> service uses `faster-whisper` (CTranslate2) which auto-downloads the whisper-tiny
> model from HuggingFace on first run (~75 MB).

Then inspect the catalog before choosing a TTS path:

```powershell
foundry model list
```

**Expected output:**
```
Downloading model artifacts and cache progress
Download completes successfully for the selected models
✅ Chat and STT models ready for offline use
```

Check what is now cached locally:

```powershell
foundry cache list
```

**Recommended resolution for TTS right now:** Kokoro-ONNX runs in-process in the orchestration service. pyttsx3 (Windows SAPI5) is the fallback. Neither requires Foundry.

**⏱️ Estimated time: 10-15 minutes**

---

## Step 6: Run Foundry Local as REST Server

For Misty integration, Foundry Local must have the service running and the required models loaded.

### Start or restart the service

```powershell
foundry service start
```

If the service was already installed but looks unhealthy:

```powershell
foundry service restart
```

### Load the chat model into the service

```powershell
foundry model load phi-3.5-mini
```

> **Note:** Do not load whisper-tiny into Foundry — STT runs via faster-whisper
> in the orchestration service process, not through Foundry Local.

If you decide to test the quality-first option later, replace `phi-3.5-mini` with `phi-4-mini` in both the download and load steps, then recheck latency before using it with Misty.

TTS and STT both run in-process in the orchestration service and do not require Foundry.

### Confirm the endpoint and loaded models

```powershell
foundry service status
foundry service ps
```

**Important:** Foundry Local uses a local endpoint that can change when the service restarts. Do not hardcode `http://localhost:5000` for the Foundry service itself. Use the endpoint shown by `foundry service status`.

---

## Step 7: Verify Server is Running

Open a **new PowerShell window** and test:

```powershell
# Get the current endpoint first
foundry service status

# Then call the Foundry Local status endpoint using the URL returned above
# Example only - replace PORT with the actual port from 'foundry service status'
Invoke-RestMethod -Uri http://localhost:PORT/openai/status
```

The response should include the current endpoint list and model directory path.

---

## Step 8: Verify Models Are Available

```powershell
# Check which models are currently loaded in the service
foundry service ps

# Expected output:
# Models running in service:
#     Alias                          Model ID
# 🟢  phi-3.5-mini                   Phi-3.5-mini-instruct-generic-cpu:2

# Check the full model catalog (replace PORT with the actual port)
Invoke-RestMethod -Uri http://localhost:PORT/openai/models

# Returns a plain JSON array of model ID strings, e.g.:
# ["openai-whisper-tiny-generic-cpu:3", "Phi-3.5-mini-instruct-generic-cpu:2"]
#
# NOTE: Kokoro TTS will NOT appear here — it runs in-process in the
# orchestration service, not through Foundry Local.
```

### Verify only expected models are loaded

Only `phi-3.5-mini` should be loaded at startup. Whisper-tiny loads on demand for STT requests. If stray models are loaded (e.g., phi-4-mini from prior testing), unload them to free resources:

```powershell
# Check what's running
foundry service ps

# If unexpected models appear, unload them
foundry model unload <model-alias-or-id>
```

---

## Keep Server Running

**Important**: The Foundry Local REST server must stay running while using Misty + Foundry Local integration.

### Option 1: Keep Terminal Open
Keep the Foundry Local service running and leave your orchestration service running in its own terminal.

### Option 2: Create Windows Service (Advanced)
Foundry Local already runs as a local service. Use these commands to control it:

```powershell
foundry service start
foundry service stop
foundry service restart
foundry service status
```

### Option 3: Task Scheduler
Only needed for your own orchestration service, not for Foundry Local itself.

---

## Next: Deploy Orchestration Service

Once Foundry Local is installed, the service is running, and the models are loaded, set your orchestration service to use the current Foundry endpoint from `foundry service status`.

```powershell
cd C:\Users\tmcclell\OneDrive - Microsoft\Source\Misty\src\windows-orchestration

pip install -r requirements.txt
set FOUNDRY_LOCAL_HOST=http://localhost:PORT
python orchestration_service.py
```

This will:
- Start the orchestration API locally
- Call Foundry Local internally for LLM chat completions
- Run STT (faster-whisper) and TTS (kokoro-onnx / pyttsx3) in-process
- Expose orchestration endpoints to Misty robot

Replace `PORT` with the port shown by `foundry service status`.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `foundry: command not found` | Reopen PowerShell after install or verify `winget install Microsoft.FoundryLocal` completed |
| `Request to local service failed` | Run `foundry service restart`, then `foundry service status` |
| Models not downloading | Check internet connection; first-time model and EP downloads can take 10-15 minutes |
| No models listed in cache | Run `foundry model download <model>` or `foundry model run <model>` |
| `kokoro-v0_19` not found | Kokoro TTS is **not** a Foundry model. It runs in-process in the orchestration service via `kokoro-onnx`. See the Implementation Guide for setup. |
| Stray models loaded | Run `foundry service ps` to check; `foundry model unload <alias>` to remove unwanted models (e.g., phi-4-mini from prior testing) |
| Service endpoint changed | Run `foundry service status` again and update `FOUNDRY_LOCAL_HOST` |
| Out of disk space | Need ~10 GB total; clear models with `foundry cache list` and `foundry cache remove <model>` |
| Windows on ARM cpuinfo warning | If commands still succeed, treat it as non-fatal and continue |

---

## Performance Tips

1. **Warm-up before first use**: Models load faster on second request
2. **Keep server running**: Starting/stopping adds 2-5s overhead per request
3. **SSD recommended**: Faster model loading than HDD
4. **Monitor CPU**: First inference request uses significant CPU (normal)
5. **Offline mode**: After first-run, server works without internet
6. **Prefer `phi-3.5-mini` for robot UX**: Use `phi-4-mini` only if you accept slower replies in exchange for better answer quality

---

## Offline Usage

After first-run model download, Foundry Local works completely **offline**:
- No internet required
- No cloud connectivity
- All models cached locally
- Full privacy guarantee

---

## Uninstall / Clean Up

To remove Foundry Local:

```powershell
# Remove cached models if needed
foundry cache list

# Uninstall Foundry Local
winget uninstall Microsoft.FoundryLocal
```

---

## References

- **Foundry Local Docs**: https://learn.microsoft.com/en-us/azure/foundry-local/
- **Node.js**: https://nodejs.org/
- **Models on Hugging Face**:
  - Phi-3.5-mini: https://huggingface.co/microsoft/Phi-3.5-mini-instruct
  - Whisper-tiny: https://huggingface.co/openai/whisper-tiny
  - Kokoro: https://huggingface.co/hexgrad/Kokoro-82M

---

## Timeline

**First-Run Setup**:
- Node.js installation: 5-10 minutes
- Foundry Local SDK install: 5 minutes
- Model download: 10-15 minutes
- **Total: 20-30 minutes (one-time)**

**After First-Run**:
- Server startup: < 1 second
- First inference: ~2-3 seconds (p50)
- Subsequent requests: < 3 seconds (warm cache)

---

**Version**: 1.0  
**Last Updated**: 2026-04-19  
**Status**: Ready for Deployment

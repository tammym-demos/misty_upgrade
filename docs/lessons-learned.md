# Lessons Learned

Operational knowledge gained from building and running the Misty II conversational AI system. These are hard-won findings from real testing — not theoretical concerns.

---

## Facial Recognition — Misty's On-Chip Face Detection Is Dead

**Date discovered**: 2026-05-04  
**Issue**: #16  
**Severity**: Hardware-level, unfixable

### Symptoms

- `POST /api/faces/training/start` returns `"Face training successfully started"` — but the face is **never stored**
- `GET /api/faces` always returns only faces trained years ago (Holly, Matthew)
- Face recognition WebSocket events (`FaceRecognition`) **never fire** — zero events in 10+ seconds of active recognition with a person standing directly in front of the camera
- The 4K RGB camera **works fine** for raw photo capture (`GET /api/cameras/rgb` returns 740KB+ images)

### Root Cause

The **face detection ML pipeline** on the Snapdragon 410 sensory processor is non-functional. The camera hardware captures images, but the on-chip face detection model (which feeds both recognition and training) produces zero output. This is the same chip that suffers keyphrase degradation (#22) — the Snapdragon 410's ML capabilities have degraded over time.

### What We Tried

| Attempt | Result |
|---------|--------|
| Training from 2 feet, well-lit, face directly at camera | Not saved |
| Training with 30-second wait (instead of 15) | Not saved |
| Different FaceId names ("Tammy", "TestFace") | Not saved |
| Full Core+Sensory reboot then immediate training | Not saved |
| Starting face detection first to "warm up" pipeline | Not saved |
| Starting face recognition, confirming detection works, then training | Zero events |
| Different room/lighting/position | Not saved |
| Cancel + retry cycles | Not saved |
| WebSocket subscription to FaceRecognition events | Zero events fired |

### Key Evidence

1. `GET /api/cameras/rgb` → 740KB image (camera hardware works)
2. `POST /api/faces/recognition/start` → `"Success"` (API accepts the command)
3. WebSocket FaceRecognition subscription → zero events in 10 seconds (ML pipeline dead)
4. `GET /api/faces` → Holly, Matthew (old faces from ~2020 still persist in storage)
5. `POST /api/faces/training/start` → `"Success"` then nothing saved (training starts but face detection finds nothing to train on)

### Why We Cannot Fix It

1. **No firmware updates available** — `POST /api/system/update` returns `false`. Misty Robotics was acquired by Furhat Robotics; v2.0.2 is the final firmware
2. **Bootloader is locked** — no custom firmware flashing (no JTAG, no fastboot, no recovery mode)
3. **Proprietary ML models** — face detection is a compiled neural network in Qualcomm SNPE format on the Snapdragon 410; we cannot retrain, patch, or replace it
4. **~2000 units sold** — no community has developed jailbreak/flash tools (no economic incentive)

### Solution: Laptop-Side Face Recognition

Move face recognition to the companion laptop (same pattern as wake word → openWakeWord, same pattern as STT → faster-whisper). Use OpenCV + ONNX Runtime for detection and embeddings via the laptop webcam. The `speaker_name` pipeline (PR #64) is camera-agnostic — only the source of the name changes.

> **Implemented (#125).** `src/windows-orchestration/face_recognition_service.py` provides a laptop-side `FaceRecognizer` (enroll/recognize with local, gitignored embedding profiles) fronted by `tools/enroll_face.py` and `tools/recognize_face.py`, and wired into `misty_controller.py` behind `USE_LAPTOP_FACE_RECOGNITION`. It captures from Misty's RGB camera or the laptop webcam, requires multi-frame agreement to avoid false positives, and fails open (no name) when a face is unknown or the model/camera is unavailable. The deprecated Misty-native `USE_FACE_RECOGNITION` (#16) path is kept disabled. See the README "Laptop-side face recognition" section for enrollment, testing, enabling, privacy behavior, and the live validation checklist.

### Broader Lesson

**Every ML-dependent feature on the Snapdragon 410 should be assumed fragile.** The chip's ML capabilities are degrading:
- Keyphrase detection: silently fails after ~2 conversations (#22)
- Face detection: completely non-functional (zero events)
- Face training: starts but never completes (depends on face detection)

**Strategy**: Route ALL intelligence through the companion laptop. Misty hardware should only be used for: motors, LED, display, speakers, and as a physical mic/tally-light indicator. Never depend on the Snapdragon 410 for ML inference.

---

## Keyphrase Recognition — Silent Failure After ~2 Cycles

**Date discovered**: 2026-04-25  
**Issue**: #22  
**Severity**: Reliability-critical, mitigated

### Symptoms

- `StartKeyPhraseRecognition` API returns "Success"
- No `KeyPhraseRecognized` WebSocket events fire
- Battery events continue on same WebSocket (connection is healthy)
- Direct mic recording works (hardware is functional)

### Root Cause

The Snapdragon 410 keyphrase detection engine suffers resource exhaustion after multiple record/play/keyphrase cycles. No API exists to query engine health.

### Solution

Moved wake word detection to companion laptop (openWakeWord + sounddevice). Misty's mic is only used for tally light indicator. Watchdog + proactive reboot remain as safety nets.

### Broader Lesson

Same as face detection — **never trust the Snapdragon 410 for sustained ML inference**.

---

## Sensory-Only Reboots — Permanently Breaks Microphone

**Date discovered**: 2026-04-27  
**Issue**: #33  
**Severity**: CRITICAL — requires physical power cycle to recover

### Symptoms

After `POST /api/reboot {"SensoryServices": true, "Core": false}`:
- Microphone produces only 44-byte empty WAV files
- Keyphrase recognition is dead
- REST API says "Success" for all commands (no error indication)
- Only a **physical power cycle** (flip switch off/on) recovers

### Root Cause

Sensory-only reboot leaves the Snapdragon 410 in an unrecoverable state. The mic hardware lock is not properly released during partial reboot.

### Solution

**NEVER use sensory-only reboot.** Always use full reboot: `POST /api/reboot {"Core": true, "SensoryServices": true}`. This was removed from all code paths in the controller.

---

## Mic Degradation — RMS=0 After Extended Recording

**Date discovered**: 2026-05-01  
**Severity**: Hardware degradation

### Symptoms

After ~20+ recording cycles in a session:
- Audio files appear normal size (600KB+)
- Content is pure silence (RMS=0.0, all-zero PCM samples)
- Software reboot does NOT recover
- Physical power cycle required

### Solution

- Proactive reboot after 5 conversations OR 20 recording cycles (whichever first)
- Laptop mic as primary audio source (bypasses Misty mic entirely)
- Misty mic only used for tally light indicator

---

## WebSocket Subscriptions — Stale State Persists

**Date discovered**: 2026-04-26  
**Severity**: Medium — causes silent event loss

### Symptoms

- After controller crash/kill, WebSocket subscriptions remain active on Misty
- New controller cannot register same event name → "Cannot register an event with same name"
- Events silently stop flowing

### Solution

- Use **unique timestamped event names** (e.g., `WakeWord_{unix_timestamp}`)
- Full WebSocket reconnect on every re-arm cycle
- `DebounceMs=0` for all subscriptions (250ms was swallowing events)

---

## Windows MME Microphone Stream Dies After Idle — Intermittent Zero-Out

**Date discovered**: 2026-07-12
**Severity**: Operational, workaround available

### Symptoms

- Laptop mic (MME device 2, 16kHz mono) works fine for the first few conversation turns
- After 2-5 minutes of idle or after several turns, the audio stream starts returning all-zero PCM data
- Speech monitor shows `RMS=0, 0, 0, 0, 2, 1, 1` for the entire recording
- Wake word still triggers (OWW can hallucinate on zero data) but recordings contain no speech
- Restarting the controller (re-opening the audio stream) fixes it immediately

### Root Cause

The Windows MME audio device appears to release or go dormant after a period without active reading, or when system audio policy changes occur (e.g., another app briefly claims the device, power management, or USB audio sleep). Unlike DirectSound (which zeros out when WASAPI apps run), MME zeroes out spontaneously even with no competing app visible.

This was observed with Teams **not** running. The issue is likely Windows audio session management or power-saving behavior on the USB/internal mic hardware.

### What We Tried

| Attempt | Result |
|---------|--------|
| DirectSound (device 9) | Zeroed out immediately when Teams/Slack use WASAPI |
| WASAPI (device 18) | Doesn't support 16kHz sample rate (-9997 error) |
| MME (device 2) | Works initially, intermittently zeros after idle |
| Closing Teams/Slack | MME still zeros — not caused by competing apps |

### Workaround (current)

- Restart the controller to re-open the mic stream
- The wake word listener and recording both use the same `sounddevice.InputStream` opened at startup
- A fresh open always recovers the mic

### Recommended Fix (TODO)

Add mic health monitoring to the wake word listener:
- Track rolling RMS average over the last N frames
- If RMS stays below a threshold (e.g., < 5) for more than 10 seconds of continuous streaming, log a warning and automatically re-open the audio stream
- This would make the system self-healing without manual restarts

### Related Issues

- DirectSound vs WASAPI conflict (resolved by switching to MME)
- OWW false triggers on zero-data (resolved with energy validation at trigger time)
- ADR-001: Companion device architecture means all audio flows through the laptop mic

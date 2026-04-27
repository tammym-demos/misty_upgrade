# Keyphrase Silent Failure — Debugging Guide

> **Issue**: [#22](https://github.com/tammym-demos/misty_upgrade/issues/22)
> **Status**: Open — firmware-level issue partially mitigated, not fully resolved
> **Last updated**: April 2026

## Symptom

After ~2 successful conversation cycles, Misty stops responding to "Hey Misty". The chest LED stays green (IDLE state), battery events continue flowing on WebSocket, and `POST /api/audio/keyphrase/start` returns `"Success"` — but no `KeyPhraseRecognized` events are ever fired. The mic itself works fine (direct recording produces real audio data).

## Root Cause Summary

The keyphrase detection engine on Misty's Snapdragon 410 (sensory services processor) silently stops working after multiple audio subsystem cycles (record → play → keyphrase start/stop). There is no API to query keyphrase engine health — the only signal is the absence of events.

Multiple contributing issues were discovered and fixed, but the core firmware-level degradation remains.

## Fixed Issues

### 1. Stale WebSocket Subscriptions

**Problem**: When a controller process is killed (e.g., `Stop-Process`), its WebSocket event subscriptions persist on Misty. A new controller attempting to register the same event name (`WakeWord`) gets `"Cannot register an event with same name (WakeWord) as a previously registered event"` — but this appears as a normal message on the WebSocket, not an error. No events are delivered.

**Fix**: Use unique timestamped event names: `WakeWord_{unix_timestamp}`. Each controller instance gets a unique subscription that never collides with stale ones.

**Code**: `_ws_subscribe_keyphrase()` in `misty_controller.py`

### 2. DebounceMs Swallowing Events

**Problem**: With `DebounceMs: 250` in the WebSocket subscription, keyphrase events were being silently dropped. A standalone Python test with `DebounceMs: 0` reliably received events.

**Fix**: Changed `DebounceMs` from 250 to 0 in the keyphrase subscription.

### 3. Missing Stop-Before-Start on Re-Arm

**Problem**: Calling `keyphrase/start` without `keyphrase/stop` first returns "Success" but the keyphrase engine doesn't actually reset from its previous state. This is especially problematic after a previous controller was killed.

**Fix**: Always use `start_keyphrase(force_restart=True)` which issues `keyphrase/stop`, waits 2 seconds, then `keyphrase/start`. The 2s delay was found necessary through testing — 1s was unreliable.

### 4. Mic Health Check False Positives

**Problem**: `check_mic_health()` was recording while keyphrase was active. Since keyphrase and recording share the same mic hardware on the Snapdragon 410, the recording produced 44-byte empty WAV files. This was misdiagnosed as "mic broken" when the mic was actually fine.

**Fix**: Stop keyphrase before performing mic health check recording, then restart it.

### 5. WebSocket Subscription Staleness After Events

**Problem**: After real `KeyPhraseRecognized` events pass through a WebSocket subscription, Misty's firmware stops routing subsequent events to that subscription. The subscription appears healthy (battery events still flow) but keyphrase events stop.

**Fix**: Full WebSocket reconnect on every re-arm — close connection, create fresh connection, create fresh subscriptions. This mimics a fresh controller start.

## Unresolved: Firmware-Level Audio Degradation

After ~2 conversation cycles (each involving keyphrase detect → record → play → re-arm), the keyphrase engine stops detecting even with:
- Fresh WebSocket connection and subscription
- Successful `keyphrase/stop` → 2s delay → `keyphrase/start`
- 5-second audio cooldown before re-arm
- Battery events still flowing (WebSocket healthy)
- Direct mic recording producing real audio (mic healthy)

**Theory**: The Snapdragon 410 audio pipeline has a resource leak that accumulates across recording/playback/keyphrase cycles. After ~2 full cycles, the keyphrase engine's internal audio buffers or DSP resources are exhausted.

**Only known recovery**: Full Core+Sensory reboot (`POST /api/reboot {"Core": true, "SensoryServices": true}`), which takes ~60-90 seconds.

## Current Mitigations

### Proactive Reboot (Primary)

Since the firmware-level failure is predictable (~2 cycles), the controller now reboots **before** failure occurs:

1. Tracks successful conversation cycles (wake → response → rearm)
2. After `PROACTIVE_REBOOT_AFTER_CYCLES` (default: 2) cycles, triggers proactive reboot
3. Misty announces "I need a quick reset. Be right back!" via TTS
4. Full Core+Sensory reboot (~60-90s downtime)
5. Controller polls `/api/device` until back, reconnects WebSocket, re-arms keyphrase
6. Cycle counter resets to 0

Skipped if battery <10%. Configure via `PROACTIVE_REBOOT_AFTER_CYCLES` env var.

### Keyphrase Watchdog (Safety Net)

The controller runs a watchdog that detects missing wake events and escalates:

| Level | Trigger | Action | LED |
|-------|---------|--------|-----|
| 0 → 1 | 90s idle with no wake events | Cancel skills + force-restart keyphrase | Yellow flash |
| 1 → 2 | +60s still no events | Second force-restart keyphrase | Dark yellow |
| 2 → 0 | +60s still no events | Full Core+Sensory reboot | Red |

Total time from failure to reboot: ~210 seconds (~3.5 minutes).

Configurable via env vars:
- `WATCHDOG_IDLE_TIMEOUT_S` (default: 90)
- `WATCHDOG_ESCALATE_TIMEOUT_S` (default: 60)

### Re-Arm Sequence

After each conversation ends, `_rearm()` performs:
1. `stop_recording()` + `keyphrase/stop` (cleanup any active audio)
2. 5-second audio cooldown (let Snapdragon 410 release resources)
3. Close WebSocket, wait 1s
4. Create fresh WebSocket connection + fresh event subscriptions
5. `start_keyphrase(force_restart=True)` (stop→2s→start)
6. Wait 6s for everything to settle

Total re-arm time: ~14 seconds.

## Debugging Checklist

When keyphrase stops working:

1. **Check WebSocket health**: Are battery events still flowing in the controller log? If yes, WebSocket is fine — the issue is keyphrase-specific.

2. **Check mic**: Try a direct recording via REST API:
   ```
   POST /api/audio/keyphrase/stop
   POST /api/audio/record/start {"FileName": "test.wav"}
   (wait 2s)
   POST /api/audio/record/stop
   GET /api/audio?FileName=test.wav&Base64=true
   ```
   If the base64 data decodes to >1000 bytes, the mic is working.

3. **Check controller state**: Hit `http://localhost:5001/status` for current state machine state. If stuck in RECORDING/PROCESSING/PLAYING, the pipeline is blocked.

4. **Check for stale skills**: `GET /api/skills/running` — if faceDetection or other skills are running, they may hold the mic.

5. **Nuclear option**: Full reboot restores keyphrase every time:
   ```
   POST /api/audio/keyphrase/stop
   POST /api/reboot {"Core": true, "SensoryServices": true}
   ```
   Wait 60-90s, then restart the controller.

## ⚠️ What NOT to Do

- **NEVER** use sensory-only reboot (`{"SensoryServices": true, "Core": false}`). This permanently breaks the mic until physical power cycle. See #33.
- **NEVER** kill the controller process without stopping keyphrase first. Use graceful shutdown (Ctrl+C / SIGTERM) or call `keyphrase/stop` before killing.
- **NEVER** start recording while keyphrase is active — they share the mic and one will silently fail.

## Future Improvements Under Consideration

1. **Python-based wake word detection (Picovoice Porcupine)**: Replace Misty's built-in keyphrase engine with Porcupine running on the companion laptop. Actively maintained, free tier with custom wake words, Windows support. Would require continuous audio streaming from Misty's mic to the laptop via REST API polling. Eliminates the Snapdragon 410 firmware bug entirely.

2. **openWakeWord**: Open-source alternative to Porcupine (MIT licensed, ONNX-based). Can train custom "Hey Misty" model. However, the repo hasn't been actively maintained since Feb 2024 (v0.6.0). 330+ open issues.

3. **Touch-based trigger**: Use Misty's capacitive touch sensors (head, chin, scruff) as an alternative wake trigger. No mic dependency.

4. **Continuous recording with VAD**: Instead of keyphrase → record → process, continuously stream audio and use voice activity detection to identify speech. Eliminates the keyphrase engine entirely.

## Timeline of Discovery

| Date | Finding |
|------|---------|
| Apr 26 | Stale WS subscriptions from killed controller prevent event registration |
| Apr 26 | DebounceMs=0 required (250 swallows events) |
| Apr 26 | force_restart=True needed (stop→2s→start) |
| Apr 26 | Mic health check false positives (recording during keyphrase) |
| Apr 26 | Fresh WS reconnect on re-arm helps but doesn't prevent failure after ~2 cycles |
| Apr 26 | 5s audio cooldown added — firmware-level degradation confirmed as root cause |
| Apr 26 | Only full Core+Sensory reboot reliably recovers keyphrase engine |

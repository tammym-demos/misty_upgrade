# ADR-002: Non-Blocking Audio Pattern for Wake Word Detection

**Date**: 2026-04-27
**Status**: Accepted
**Deciders**: Tim McClell
**Related**: #22 (keyphrase failure), #33 (sensory reboot breaks mic), #44 (laptop wake word)

---

## Context

Misty II's Snapdragon 410 sensory processor silently stops firing `KeyPhraseRecognized` events after ~2 conversation cycles. The firmware is final (v2.0.2) — no vendor fix is possible. Our previous approach polled Misty's mic via REST API for wake word detection, but rapid record/stop cycles accelerated the same hardware degradation (#44).

We needed an audio capture pattern that:
1. Never touches Misty's mic for wake word detection
2. Runs continuously without blocking the main controller thread
3. Can be paused/resumed to prevent self-wake during playback
4. Handles backpressure gracefully (slow processing doesn't crash capture)

## Decision

We adopted a **callback-queue-worker** pattern using the `sounddevice` library on the companion laptop's microphone, inspired by the [JD Robot Assistant](https://github.com/abeerrai01/JD_BARE_ASSITANT) project's audio architecture.

## The Pattern

### Architecture

```
┌─────────────────────┐     ┌──────────────┐     ┌──────────────────┐
│  sounddevice thread  │     │  Queue (100)  │     │  Worker thread   │
│  (audio callback)    │────>│  bounded      │────>│  (detection)     │
│  - runs at 16kHz     │     │  drop on full │     │  - openWakeWord  │
│  - 80ms frames       │     └──────────────┘     │  - fires callback│
│  - never blocks      │                           └──────────────────┘
└─────────────────────┘
```

### Three components

**1. Audio Callback (sounddevice thread)**

Called by the OS audio subsystem for every 80ms frame (1280 samples at 16kHz). This runs in a real-time audio thread — it must be fast and never block.

```python
def _audio_callback(self, indata, frames, time_info, status):
    if status:
        logger.debug(f"Audio stream status: {status}")
    if self._paused:
        return  # drop audio during conversation
    try:
        self._audio_queue.put_nowait(bytes(indata))
    except queue.Full:
        pass  # drop frame if processing can't keep up
```

**Key design choices:**
- `put_nowait()` — never blocks the audio thread. If the queue is full, the frame is silently dropped. This prevents audio buffer underruns that would crash the stream.
- `bytes(indata)` — copies the data out of sounddevice's buffer immediately. The buffer is reused on the next callback.
- Paused check happens here (not in the worker) so audio data isn't even queued during conversation, keeping the queue empty for fast resume.

**2. Bounded Queue (bridge)**

```python
self._audio_queue = queue.Queue(maxsize=100)
```

The queue decouples capture from processing. At 80ms per frame, 100 frames = 8 seconds of buffer. This absorbs temporary processing spikes without dropping audio.

**3. Worker Thread (processing)**

Pulls frames from the queue and runs openWakeWord inference:

```python
def _process_loop(self):
    while self._running:
        self._pause_event.wait(timeout=1.0)  # block while paused
        if not self._running:
            break

        try:
            audio_data = self._audio_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        pcm = np.frombuffer(audio_data, dtype=np.int16)

        # Self-wake cooldown after resume
        in_cooldown = (time.time() - self._resume_time) < RESUME_COOLDOWN_S

        if self._oww_model and not in_cooldown:
            predictions = self._oww_model.predict(pcm)
            for model_name, score in predictions.items():
                if score >= self.threshold:
                    self._oww_model.reset()
                    self.on_wake_word()
                    break
```

**Key design choices:**
- `get(timeout=0.5)` — blocks with timeout so the thread can check `self._running` periodically for clean shutdown.
- Cooldown after resume prevents Misty's own speaker audio (still echoing) from triggering a false wake.

### Self-Wake Prevention

When Misty speaks, her speaker audio reaches the laptop mic. Without protection, this triggers an immediate false wake word detection. The solution has two layers:

```
PLAYING state → listener.pause()
  - Audio callback drops all frames (not queued)
  - Queue is drained on pause to discard buffered audio
  
REARMING state → listener.resume()
  - openWakeWord model state reset (clears stale activations)
  - 1.5s cooldown ignores detections (speaker echo decay)
```

This mirrors the JD Robot's `is_robot_speaking` flag pattern but is more robust — we drain the queue and reset model state, not just skip frames.

### Comparison: Misty REST Polling vs Laptop Mic

| Aspect | Misty REST Polling (old) | Laptop Mic (new) |
|--------|--------------------------|-------------------|
| Mic hardware | Snapdragon 410 (degrades) | Laptop (reliable) |
| Capture method | record/stop/fetch REST cycle | Continuous stream callback |
| Cycle time | ~2s per poll (1.5s record + cooldown) | 80ms frames, continuous |
| Backpressure | REST timeouts crash the loop | Queue drops frames gracefully |
| Resource contention | Shares mic with keyphrase/recording | Independent mic, no contention |
| Degradation | ~100 polls before mic fails | No degradation observed |

### Comparison: JD Robot vs Our Implementation

The JD Robot Assistant (`JD_BARE_ASSITANT/test.py`) pioneered this pattern for robotics:

| Aspect | JD Robot | Our Implementation |
|--------|----------|-------------------|
| Audio library | sounddevice | sounddevice |
| STT engine | Vosk (KaldiRecognizer) | openWakeWord (wake only) |
| Queue | `queue.Queue` (unbounded) | `queue.Queue(maxsize=100)` |
| Backpressure | None (queue grows forever) | Bounded queue, drop on full |
| Self-wake | `is_robot_speaking` flag (commented out) | Pause + drain + cooldown |
| Output queue | `ActuationTask` queue for speech/actions | N/A (controller handles this) |
| Wake detection | String match on Vosk text ("hey jd") | Neural network (openWakeWord) |

**What we improved:**
- Bounded queue prevents memory growth during long pauses
- Queue drain on pause prevents stale audio from triggering false detections
- Resume cooldown handles speaker echo decay
- Separate wake word engine (openWakeWord) is more accurate than string-matching STT output

## Consequences

**Positive:**
- Misty's mic is only used for the 6s conversation recording — dramatically fewer audio cycles
- Proactive reboot interval can potentially be extended from 2 to 10+ cycles
- Custom wake words possible via openWakeWord Colab training
- Pattern is reusable for any future audio processing (e.g., laptop-based VAD)

**Negative:**
- User must be within range of the laptop mic, not just Misty
- Adds `sounddevice` and `openwakeword` as dependencies
- Laptop mic quality varies — may need gain/threshold tuning per device

**Risks:**
- Extended follow-up recording (up to 90s / 12 turns) still uses Misty's mic — degradation rate under this pattern needs empirical measurement
- False wake rate from ambient noise needs testing in real environments

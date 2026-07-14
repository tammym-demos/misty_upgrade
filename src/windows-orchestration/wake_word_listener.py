"""
Wake Word Listener — Laptop microphone + openWakeWord for Misty II.

Listens on the companion laptop's own microphone using sounddevice,
running openWakeWord inference locally. This completely avoids using
Misty's Snapdragon 410 audio subsystem for wake word detection,
eliminating the firmware degradation that causes silent keyphrase
failure (#22, #44).

Architecture (inspired by JD Robot Assistant's non-blocking audio pattern):
  - sounddevice.RawInputStream with callback feeds audio into a queue
  - Separate processing thread pulls frames and runs openWakeWord
  - Detection fires the on_wake_word callback
  - Listener auto-suppresses during conversation (self-wake prevention)

Usage:
    listener = WakeWordListener(on_wake_word=my_callback)
    listener.start()
    # ... later ...
    listener.pause()   # during conversation (prevent self-wake from Misty's speaker)
    listener.resume()  # after conversation ends
    listener.stop()    # shutdown
"""

import os
import time
import logging
import threading
import queue

import numpy as np

import config_defaults

logger = logging.getLogger("wake_word_listener")

# ============================================================================
# CONFIGURATION
# ============================================================================

# openWakeWord settings
OWW_MODEL_NAME = os.getenv("OWW_MODEL_NAME", config_defaults.OWW_MODEL_NAME).strip()
OWW_THRESHOLD = float(os.getenv("OWW_THRESHOLD", str(config_defaults.OWW_THRESHOLD)))
OWW_VAD_THRESHOLD = float(
    os.getenv("OWW_VAD_THRESHOLD", str(config_defaults.OWW_VAD_THRESHOLD))
)
OWW_CUSTOM_MODEL_PATH = os.getenv(
    "OWW_CUSTOM_MODEL_PATH",
    config_defaults.OWW_CUSTOM_MODEL_PATH,
).strip() or config_defaults.OWW_CUSTOM_MODEL_PATH
OWW_TRIGGER_FRAMES = max(
    1,
    int(os.getenv("OWW_TRIGGER_FRAMES", str(config_defaults.OWW_TRIGGER_FRAMES))),
)

# Audio capture settings (laptop mic)
_LAPTOP_MIC_DEVICE_RAW = os.getenv("LAPTOP_MIC_DEVICE", config_defaults.LAPTOP_MIC_DEVICE).strip()
LAPTOP_MIC_DEVICE = (
    int(_LAPTOP_MIC_DEVICE_RAW)
    if _LAPTOP_MIC_DEVICE_RAW.isdigit()
    else (_LAPTOP_MIC_DEVICE_RAW or None)
)
SAMPLE_RATE = 16000       # openWakeWord native rate
FRAME_SAMPLES = 1280      # 80ms at 16kHz — openWakeWord's expected frame size
BLOCK_SIZE = 1280          # match frame size for 1:1 callback-to-frame ratio
MAX_RECORDING_BYTES = 5 * 1024 * 1024  # 5MB max recording buffer (~160s at 16kHz mono)

# Self-wake suppression: ignore detections for this many seconds after resume
# (prevents Misty's speaker echo from triggering a false wake)
RESUME_COOLDOWN_S = float(
    os.getenv(
        "WAKE_WORD_RESUME_COOLDOWN_S",
        str(config_defaults.WAKE_WORD_RESUME_COOLDOWN_S),
    )
)

# Minimum RMS energy required to run wake word inference.  Frames below
# this threshold are silence/noise and would cause false-positive detections.
WAKE_WORD_MIN_RMS = int(
    os.getenv("WAKE_WORD_MIN_RMS", str(config_defaults.WAKE_WORD_MIN_RMS))
)

if not 0.0 <= OWW_THRESHOLD <= 1.0:
    raise ValueError("OWW_THRESHOLD must be between 0.0 and 1.0")
if not 0.0 <= OWW_VAD_THRESHOLD <= 1.0:
    raise ValueError("OWW_VAD_THRESHOLD must be between 0.0 and 1.0")
if not 1 <= OWW_TRIGGER_FRAMES <= 20:
    raise ValueError("OWW_TRIGGER_FRAMES must be between 1 and 20")
if WAKE_WORD_MIN_RMS < 0:
    raise ValueError("WAKE_WORD_MIN_RMS must be non-negative")
if RESUME_COOLDOWN_S < 0:
    raise ValueError("WAKE_WORD_RESUME_COOLDOWN_S must be non-negative")

# Speech monitor settings (for VAD-controlled recording)
SPEECH_RMS_THRESHOLD = int(
    os.getenv("SPEECH_RMS_THRESHOLD", str(config_defaults.SPEECH_RMS_THRESHOLD))
)
SPEECH_SILENCE_DURATION_S = float(os.getenv("SPEECH_SILENCE_DURATION_S", "1.5"))
SPEECH_MIN_DURATION_S = float(
    os.getenv("SPEECH_MIN_DURATION_S", str(config_defaults.RECORDING_DURATION_S))
)
SPEECH_MAX_DURATION_S = float(os.getenv("SPEECH_MAX_DURATION_S", "15.0"))
SPEECH_NO_SPEECH_TIMEOUT_S = float(os.getenv("SPEECH_NO_SPEECH_TIMEOUT_S", "4.0"))

# Health monitoring
MAX_CONSECUTIVE_ERRORS = 10


class WakeWordListener:
    """Listens on the laptop microphone and runs openWakeWord detection."""

    def __init__(
        self,
        on_wake_word: callable,
        model_name: str = OWW_MODEL_NAME,
        threshold: float = OWW_THRESHOLD,
        custom_model_path: str = OWW_CUSTOM_MODEL_PATH,
    ):
        self.on_wake_word = on_wake_word
        self.model_name = model_name
        self.threshold = threshold
        self.custom_model_path = custom_model_path
        self.trigger_frames = OWW_TRIGGER_FRAMES

        self._running = False
        self._paused = False
        self._pause_event = threading.Event()
        self._pause_event.set()  # start unpaused
        self._process_thread: threading.Thread | None = None
        self._audio_queue: queue.Queue = queue.Queue(maxsize=100)

        # Health tracking
        self._consecutive_errors = 0
        self._total_frames = 0
        self._total_detections = 0
        self._last_detection_time = 0.0
        self._start_time = 0.0
        self._resume_time = 0.0  # for self-wake cooldown
        self._detection_streaks: dict[str, int] = {}
        self._recent_rms: list[int] = []  # rolling window for energy validation
        self._accepted_candidates = 0
        self._rejected_candidates = 0
        self._last_candidate: dict | None = None

        # Speech monitor state (for VAD-controlled recording)
        self._speech_monitor_active = False
        self._speech_monitor_callback = None
        self._speech_monitor_start_time = 0.0
        self._speech_detected = False
        self._last_speech_time = 0.0
        self._speech_monitor_min_s = SPEECH_MIN_DURATION_S
        self._speech_monitor_max_s = SPEECH_MAX_DURATION_S
        self._speech_monitor_silence_s = SPEECH_SILENCE_DURATION_S
        self._speech_rms_threshold = SPEECH_RMS_THRESHOLD  # dynamic, set by calibration
        self._calibration_samples: list[int] = []  # RMS samples during calibration
        self._calibration_done = False

        # Laptop mic recording (capture audio for STT instead of Misty's mic)
        self._recording = False
        self._recorded_frames: list[bytes] = []
        self._recorded_bytes_total = 0  # O(1) tracking instead of summing every callback

        # openWakeWord model (lazy init)
        self._oww_model = None

        # sounddevice stream
        self._stream = None

    def _init_model(self) -> bool:
        """Initialize the openWakeWord model."""
        try:
            from openwakeword.model import Model as OWWModel

            if self.custom_model_path:
                if not os.path.exists(self.custom_model_path):
                    logger.error(
                        "Configured custom wake word model path does not exist: "
                        f"{self.custom_model_path}. Set OWW_CUSTOM_MODEL_PATH to a trained "
                        "'Hey Misty' model artifact or place the model files in the expected path."
                    )
                    return False

                logger.info(
                    f"Loading custom wake word model: {self.custom_model_path} "
                    f"(model_name={self.model_name}, threshold={self.threshold})"
                )
                self._oww_model = OWWModel(
                    wakeword_models=[self.custom_model_path],
                    vad_threshold=OWW_VAD_THRESHOLD,
                    inference_framework="onnx",
                )
            else:
                logger.error(
                    "No custom wake word model configured for the supported 'Hey Misty' wake phrase. "
                    "Restore the bundled models/hey_misty.onnx artifact or set OWW_CUSTOM_MODEL_PATH "
                    "to a trained model artifact and optionally OWW_MODEL_NAME/OWW_THRESHOLD to match it."
                )
                return False

            logger.info(
                f"openWakeWord ready (models={list(self._oww_model.models.keys())}, "
                f"threshold={self.threshold}, trigger_frames={self.trigger_frames})"
            )
            return True
        except ImportError:
            logger.error(
                "openWakeWord not installed. Install with: pip install openwakeword"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to initialize openWakeWord: {e}")
            return False

    def start(self) -> bool:
        """Start the wake word listener on the laptop mic. Returns True if started."""
        if self._running:
            logger.warning("Wake word listener already running")
            return True

        if not self._init_model():
            return False

        # Verify sounddevice is available
        try:
            import sounddevice as sd
        except ImportError:
            logger.error(
                "sounddevice not installed. Install with: pip install sounddevice"
            )
            return False

        self._running = True
        self._paused = False
        self._pause_event.set()
        self._start_time = time.time()
        self._resume_time = time.time()

        # Start the audio processing thread
        self._process_thread = threading.Thread(
            target=self._process_loop,
            name="wake-word-processor",
            daemon=True,
        )
        self._process_thread.start()

        # Start the audio capture stream
        try:
            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                dtype="int16",
                channels=1,
                device=LAPTOP_MIC_DEVICE,
                callback=self._audio_callback,
            )
            self._stream.start()
            device_label = f"device={LAPTOP_MIC_DEVICE}" if LAPTOP_MIC_DEVICE is not None else "default device"
            logger.info(
                f"Wake word listener started on laptop mic "
                f"({device_label}, rate={SAMPLE_RATE}, block={BLOCK_SIZE})"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to open laptop microphone: {e}")
            self._running = False
            return False

    def stop(self):
        """Stop the wake word listener and release the microphone."""
        self._running = False
        self._pause_event.set()  # unblock if paused

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.warning(f"Error closing audio stream: {e}")
            self._stream = None

        if self._process_thread and self._process_thread.is_alive():
            self._process_thread.join(timeout=5.0)

        self._oww_model = None
        logger.info(
            f"Wake word listener stopped "
            f"(frames={self._total_frames}, detections={self._total_detections})"
        )

    def pause(self):
        """Pause detection (call during conversation to prevent self-wake)."""
        self._paused = True
        self._pause_event.clear()
        # Drain the audio queue to discard buffered audio
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
        self._detection_streaks.clear()
        logger.debug("Wake word listener paused (self-wake prevention)")

    @property
    def speech_detected(self) -> bool:
        """Whether the active speech monitor has detected speech."""
        return self._speech_detected

    def resume(self):
        """Resume detection after conversation ends."""
        self._paused = False
        self._resume_time = time.time()
        self._pause_event.set()
        self._detection_streaks.clear()
        # Reset openWakeWord state to avoid stale activations
        if self._oww_model:
            self._oww_model.reset()
        logger.debug("Wake word listener resumed")

    def start_speech_monitor(
        self,
        on_speech_end: callable,
        min_duration: float = SPEECH_MIN_DURATION_S,
        max_duration: float = SPEECH_MAX_DURATION_S,
        silence_duration: float = SPEECH_SILENCE_DURATION_S,
        rms_threshold: int | None = None,
    ):
        """Begin monitoring laptop mic for speech end during Misty recording.
        
        Uses RMS-based voice activity detection on the same audio stream.
        Fires on_speech_end when silence is detected after speech, or when
        max_duration is reached.
        
        Args:
            on_speech_end: Callback fired when speech ends (silence detected)
            min_duration: Minimum monitoring time before allowing early stop
            max_duration: Maximum monitoring time (hard cap)
            silence_duration: Trailing silence required after speech
            rms_threshold: Initial speech energy threshold before calibration
        """
        self._speech_monitor_callback = on_speech_end
        self._speech_monitor_start_time = time.time()
        self._speech_detected = False
        self._last_speech_time = 0.0
        self._speech_monitor_min_s = min_duration
        self._speech_monitor_max_s = max_duration
        self._speech_monitor_silence_s = silence_duration
        self._calibration_samples = []
        self._calibration_done = False
        self._speech_rms_threshold = (
            SPEECH_RMS_THRESHOLD if rms_threshold is None else int(rms_threshold)
        )
        # Drain any stale audio frames from the queue before starting
        drained = 0
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        self._speech_monitor_active = True
        # Ensure audio flows even if paused for wake word
        self._pause_event.set()
        logger.info(
            f"Speech monitor started (min={min_duration}s, max={max_duration}s, "
            f"silence={silence_duration}s, calibrating noise floor..., "
            f"drained {drained} stale frames)"
        )

    def stop_speech_monitor(self):
        """Stop speech monitoring."""
        was_active = self._speech_monitor_active
        self._speech_monitor_active = False
        self._speech_monitor_callback = None
        if was_active:
            elapsed = time.time() - self._speech_monitor_start_time
            logger.info(
                f"Speech monitor stopped after {elapsed:.1f}s "
                f"(speech_detected={self._speech_detected})"
            )

    def start_recording(self):
        """Start capturing audio frames from the laptop mic for STT.
        
        Call this instead of (or alongside) Misty's recording to use the
        laptop mic as the audio source. Frames are buffered in memory.
        """
        self._recorded_frames = []
        self._recorded_bytes_total = 0
        self._recording = True
        # Ensure audio flows even if paused
        self._pause_event.set()
        logger.info("Laptop mic recording started")

    def stop_recording(self) -> bytes:
        """Stop capturing and return WAV audio bytes.
        
        Returns:
            WAV-format audio bytes (16kHz, 16-bit mono) ready for STT.
        """
        self._recording = False
        frames = self._recorded_frames
        self._recorded_frames = []
        self._recorded_bytes_total = 0

        if not frames:
            logger.warning("Laptop mic recording: no frames captured")
            return b""

        # Concatenate all PCM frames
        pcm_data = b"".join(frames)
        logger.info(
            f"Laptop mic recording stopped: {len(frames)} frames, "
            f"{len(pcm_data)} bytes PCM, "
            f"{len(pcm_data) / (SAMPLE_RATE * 2):.1f}s"
        )

        # Wrap in WAV header (16kHz, 16-bit, mono)
        import struct
        wav_header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF',
            36 + len(pcm_data),
            b'WAVE',
            b'fmt ',
            16,       # chunk size
            1,        # PCM format
            1,        # mono
            SAMPLE_RATE,
            SAMPLE_RATE * 2,  # byte rate
            2,        # block align
            16,       # bits per sample
            b'data',
            len(pcm_data),
        )
        return wav_header + pcm_data

    @property
    def is_running(self) -> bool:
        return self._running and self._stream is not None

    @property
    def is_paused(self) -> bool:
        return self._paused

    def get_health(self) -> dict:
        """Return health metrics for diagnostics."""
        uptime = time.time() - self._start_time if self._start_time else 0
        return {
            "running": self.is_running,
            "paused": self.is_paused,
            "total_frames": self._total_frames,
            "total_detections": self._total_detections,
            "consecutive_errors": self._consecutive_errors,
            "uptime_s": round(uptime),
            "model": self.model_name,
            "threshold": self.threshold,
            "trigger_frames": self.trigger_frames,
            "vad_threshold": OWW_VAD_THRESHOLD,
            "min_rms": WAKE_WORD_MIN_RMS,
            "resume_cooldown_s": RESUME_COOLDOWN_S,
            "accepted_candidates": self._accepted_candidates,
            "rejected_candidates": self._rejected_candidates,
            "last_candidate": self._last_candidate,
            "source": "laptop_mic",
            "custom_model_path": self.custom_model_path or None,
            "model_source": "custom" if self.custom_model_path else "missing_config",
        }

    # --- Audio capture callback (runs in sounddevice's audio thread) ---

    def _audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice for each audio block. Must be fast — just queue the data."""
        if status:
            logger.debug(f"Audio stream status: {status}")
        
        # Buffer frames for laptop mic recording (independent of wake word state)
        if self._recording:
            if self._recorded_bytes_total + len(indata) > MAX_RECORDING_BYTES:
                logger.warning("Recording buffer full — stopping capture")
                self._recording = False
            else:
                frame_bytes = bytes(indata)
                self._recorded_frames.append(frame_bytes)
                self._recorded_bytes_total += len(frame_bytes)

        if self._paused and not self._speech_monitor_active and not self._recording:
            return  # drop audio during conversation (unless speech monitoring or recording)
        data = bytes(indata)
        try:
            self._audio_queue.put_nowait(data)
        except queue.Full:
            pass  # drop frame if processing can't keep up

    # --- Processing thread ---

    def _process_loop(self):
        """Pull audio frames from queue and run wake word or speech monitoring."""
        logger.info("Wake word processing loop started")

        while self._running:
            # Block until unpaused (or speech monitoring is active)
            self._pause_event.wait(timeout=1.0)
            if not self._running:
                break
            if self._paused and not self._speech_monitor_active:
                continue

            try:
                # Get audio frame (block with timeout to allow shutdown check)
                audio_data = self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                # Even with no audio, check speech monitor timeouts
                if self._speech_monitor_active:
                    self._check_speech_monitor_timeout()
                continue

            try:
                self._total_frames += 1

                # Convert to numpy int16 array
                pcm = np.frombuffer(audio_data, dtype=np.int16)

                # Speech monitoring mode — RMS-based VAD
                if self._speech_monitor_active:
                    self._process_speech_monitor_frame(pcm)
                    continue  # skip wake word detection during speech monitoring

                # Self-wake cooldown: ignore detections shortly after resume
                in_cooldown = (time.time() - self._resume_time) < RESUME_COOLDOWN_S

                # Track frame energy for wake word validation
                frame_rms = int(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
                self._recent_rms.append(frame_rms)
                if len(self._recent_rms) > 20:
                    self._recent_rms = self._recent_rms[-10:]

                self._run_wake_inference(pcm, in_cooldown)

                self._consecutive_errors = 0

            except Exception as e:
                self._consecutive_errors += 1
                if self._consecutive_errors <= 3 or self._consecutive_errors % 50 == 0:
                    logger.warning(
                        f"Wake word processing error "
                        f"({self._consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}"
                    )
                if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.error(
                        "Wake word: too many consecutive errors, pausing 5s"
                    )
                    time.sleep(5.0)
                    self._consecutive_errors = 0

        logger.info("Wake word processing loop ended")

    def _run_wake_inference(self, pcm: np.ndarray, in_cooldown: bool) -> bool:
        """Feed every frame to OpenWakeWord so its temporal context stays intact."""
        if in_cooldown:
            self._detection_streaks.clear()
            return False
        if not self._oww_model:
            return False

        predictions = self._oww_model.predict(pcm)
        return self._handle_wake_predictions(predictions)

    def _handle_wake_predictions(self, predictions: dict[str, float]) -> bool:
        """Handle openWakeWord scores and fire only after sustained evidence."""
        for model_name, score in predictions.items():
            if score < self.threshold:
                self._detection_streaks[model_name] = 0
                if score >= max(0.1, self.threshold * 0.8):
                    self._record_candidate(
                        model_name, score, 0, "below_threshold", accepted=False
                    )
                continue

            streak = self._detection_streaks.get(model_name, 0) + 1
            self._detection_streaks[model_name] = streak
            if streak < self.trigger_frames:
                logger.debug(
                    f"Wake word '{model_name}' candidate score={score:.3f} "
                    f"({streak}/{self.trigger_frames} frames)"
                )
                continue

            # Energy validation: reject if no recent frame had speech-level RMS.
            # This prevents false triggers on silence/ambient noise.
            max_recent_rms = (
                max(self._recent_rms[-10:]) if self._recent_rms else None
            )
            if max_recent_rms is not None and max_recent_rms < WAKE_WORD_MIN_RMS:
                logger.debug(
                    f"Wake word '{model_name}' rejected — low energy "
                    f"(max_rms={max_recent_rms}, min={WAKE_WORD_MIN_RMS})"
                )
                self._record_candidate(
                    model_name, score, streak, "low_energy", accepted=False
                )
                self._detection_streaks[model_name] = 0
                continue

            self._total_detections += 1
            self._last_detection_time = time.time()
            logger.info(
                f"Wake word '{model_name}' detected! score={score:.3f} "
                f"(threshold={self.threshold}, frames={streak}/{self.trigger_frames})"
            )
            self._record_candidate(model_name, score, streak, "accepted", accepted=True)
            self._detection_streaks.clear()
            # Reset model to prevent repeated triggers
            if self._oww_model:
                self._oww_model.reset()
            # Fire callback
            try:
                self.on_wake_word()
            except Exception as e:
                logger.error(f"Wake word callback error: {e}")
            return True

        return False

    def _record_candidate(
        self,
        model_name: str,
        score: float,
        streak: int,
        reason: str,
        *,
        accepted: bool,
    ) -> None:
        max_recent_rms = max(self._recent_rms[-10:]) if self._recent_rms else None
        self._last_candidate = {
            "model": model_name,
            "score": round(float(score), 4),
            "streak": streak,
            "max_recent_rms": max_recent_rms,
            "reason": reason,
            "accepted": accepted,
            "timestamp": time.time(),
        }
        if accepted:
            self._accepted_candidates += 1
        else:
            self._rejected_candidates += 1

    # --- Speech monitor helpers ---

    def _process_speech_monitor_frame(self, pcm: np.ndarray):
        """Process a single audio frame for speech/silence detection using RMS.
        
        First ~1.5s: calibrate noise floor (fan noise, ambient).
        After calibration: detect speech as RMS significantly above noise floor.
        """
        now = time.time()
        elapsed = now - self._speech_monitor_start_time

        # Compute RMS energy
        rms = int(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))

        # Learn ambient noise without discarding initial presenter speech. Frames
        # above the initial threshold are treated as speech, not calibration data.
        CALIBRATION_DURATION_S = 1.5
        if not self._calibration_done:
            speech_during_calibration = (
                self._speech_detected or rms > self._speech_rms_threshold
            )
            if not speech_during_calibration:
                self._calibration_samples.append(rms)
            if elapsed >= CALIBRATION_DURATION_S:
                # Set threshold from quiet samples only so speech cannot inflate it.
                if self._calibration_samples and not speech_during_calibration:
                    noise_mean = np.mean(self._calibration_samples)
                    noise_std = np.std(self._calibration_samples)
                    margin = max(noise_std * 3, 30)
                    self._speech_rms_threshold = int(noise_mean + margin)
                else:
                    noise_mean = (
                        float(np.mean(self._calibration_samples))
                        if self._calibration_samples
                        else 0
                    )
                    noise_std = (
                        float(np.std(self._calibration_samples))
                        if self._calibration_samples
                        else 0
                    )
                self._calibration_done = True
                logger.info(
                    f"Speech monitor: calibrated — noise_floor={noise_mean:.0f} "
                    f"std={noise_std:.0f} → threshold={self._speech_rms_threshold}"
                )

        # Detect speech during calibration too, using the initial threshold.
        is_speech = rms > self._speech_rms_threshold

        # Log RMS periodically for diagnostics (every ~0.5s = ~6 frames at 80ms)
        frame_count = int(elapsed / 0.08)
        if frame_count % 6 == 0:
            logger.info(f"Speech monitor: RMS={rms} threshold={self._speech_rms_threshold} speech={is_speech} elapsed={elapsed:.1f}s")

        if is_speech:
            if not self._speech_detected:
                logger.info(f"Speech monitor: speech started (RMS={rms}, threshold={self._speech_rms_threshold}, elapsed={elapsed:.1f}s)")
            self._speech_detected = True
            self._last_speech_time = now

        # Check max duration — hard cap
        if elapsed >= self._speech_monitor_max_s:
            logger.info(f"Speech monitor: max duration reached ({self._speech_monitor_max_s}s)")
            self._fire_speech_end()
            return

        # Check no-speech timeout — if laptop mic can't hear speech,
        # fall back to minimum recording duration (don't cut short)
        if not self._speech_detected and elapsed >= SPEECH_NO_SPEECH_TIMEOUT_S:
            if elapsed >= self._speech_monitor_min_s:
                logger.info(f"Speech monitor: no speech on laptop mic after {elapsed:.1f}s — falling back to min duration")
                self._fire_speech_end()
                return
            # Otherwise keep recording until min_duration — user may be speaking to Misty
            # and laptop mic just can't hear them

        # Check silence after speech — end of utterance
        if self._speech_detected and not is_speech:
            silence_duration = now - self._last_speech_time
            if silence_duration >= self._speech_monitor_silence_s and elapsed >= self._speech_monitor_min_s:
                logger.info(
                    f"Speech monitor: end of utterance detected "
                    f"(silence={silence_duration:.1f}s, total={elapsed:.1f}s)"
                )
                self._fire_speech_end()

    def _check_speech_monitor_timeout(self):
        """Check for speech monitor timeouts when no audio frames arrive."""
        if not self._speech_monitor_active:
            return
        elapsed = time.time() - self._speech_monitor_start_time
        if elapsed >= self._speech_monitor_max_s:
            logger.info(f"Speech monitor: max duration reached (no audio, {elapsed:.1f}s)")
            self._fire_speech_end()

    def _fire_speech_end(self):
        """Fire the speech end callback and deactivate monitoring."""
        callback = self._speech_monitor_callback
        self._speech_monitor_active = False
        self._speech_monitor_callback = None
        if callback:
            try:
                callback()
            except Exception as e:
                logger.error(f"Speech end callback error: {e}")

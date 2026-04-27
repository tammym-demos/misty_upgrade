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

logger = logging.getLogger("wake_word_listener")

# ============================================================================
# CONFIGURATION
# ============================================================================

# openWakeWord settings
OWW_MODEL_NAME = os.getenv("OWW_MODEL_NAME", "hey_jarvis_v0.1")
OWW_THRESHOLD = float(os.getenv("OWW_THRESHOLD", "0.7"))
OWW_VAD_THRESHOLD = float(os.getenv("OWW_VAD_THRESHOLD", "0"))  # 0 = disabled
OWW_CUSTOM_MODEL_PATH = os.getenv("OWW_CUSTOM_MODEL_PATH", "")

# Audio capture settings (laptop mic)
SAMPLE_RATE = 16000       # openWakeWord native rate
FRAME_SAMPLES = 1280      # 80ms at 16kHz — openWakeWord's expected frame size
BLOCK_SIZE = 1280          # match frame size for 1:1 callback-to-frame ratio

# Self-wake suppression: ignore detections for this many seconds after resume
# (prevents Misty's speaker echo from triggering a false wake)
RESUME_COOLDOWN_S = float(os.getenv("WAKE_WORD_RESUME_COOLDOWN_S", "1.5"))

# Speech monitor settings (for VAD-controlled recording)
SPEECH_RMS_THRESHOLD = int(os.getenv("SPEECH_RMS_THRESHOLD", "300"))
SPEECH_SILENCE_DURATION_S = float(os.getenv("SPEECH_SILENCE_DURATION_S", "1.5"))
SPEECH_MIN_DURATION_S = float(os.getenv("SPEECH_MIN_DURATION_S", "3.0"))
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

        # Speech monitor state (for VAD-controlled recording)
        self._speech_monitor_active = False
        self._speech_monitor_callback = None
        self._speech_monitor_start_time = 0.0
        self._speech_detected = False
        self._last_speech_time = 0.0
        self._speech_monitor_min_s = SPEECH_MIN_DURATION_S
        self._speech_monitor_max_s = SPEECH_MAX_DURATION_S

        # openWakeWord model (lazy init)
        self._oww_model = None

        # sounddevice stream
        self._stream = None

    def _init_model(self) -> bool:
        """Initialize the openWakeWord model."""
        try:
            from openwakeword.model import Model as OWWModel

            if self.custom_model_path and os.path.exists(self.custom_model_path):
                logger.info(f"Loading custom wake word model: {self.custom_model_path}")
                self._oww_model = OWWModel(
                    wakeword_models=[self.custom_model_path],
                    vad_threshold=OWW_VAD_THRESHOLD,
                    inference_framework="onnx",
                )
            else:
                logger.info(f"Loading built-in wake word model: {self.model_name}")
                self._oww_model = OWWModel(
                    wakeword_models=[self.model_name],
                    vad_threshold=OWW_VAD_THRESHOLD,
                    inference_framework="onnx",
                )

            logger.info(
                f"openWakeWord ready (models={list(self._oww_model.models.keys())}, "
                f"threshold={self.threshold})"
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
                callback=self._audio_callback,
            )
            self._stream.start()
            logger.info(
                f"Wake word listener started on laptop mic "
                f"(rate={SAMPLE_RATE}, block={BLOCK_SIZE})"
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
        logger.debug("Wake word listener paused (self-wake prevention)")

    def resume(self):
        """Resume detection after conversation ends."""
        self._paused = False
        self._resume_time = time.time()
        self._pause_event.set()
        # Reset openWakeWord state to avoid stale activations
        if self._oww_model:
            self._oww_model.reset()
        logger.debug("Wake word listener resumed")

    def start_speech_monitor(
        self,
        on_speech_end: callable,
        min_duration: float = SPEECH_MIN_DURATION_S,
        max_duration: float = SPEECH_MAX_DURATION_S,
    ):
        """Begin monitoring laptop mic for speech end during Misty recording.
        
        Uses RMS-based voice activity detection on the same audio stream.
        Fires on_speech_end when silence is detected after speech, or when
        max_duration is reached.
        
        Args:
            on_speech_end: Callback fired when speech ends (silence detected)
            min_duration: Minimum monitoring time before allowing early stop
            max_duration: Maximum monitoring time (hard cap)
        """
        self._speech_monitor_callback = on_speech_end
        self._speech_monitor_start_time = time.time()
        self._speech_detected = False
        self._last_speech_time = 0.0
        self._speech_monitor_min_s = min_duration
        self._speech_monitor_max_s = max_duration
        self._speech_monitor_active = True
        # Ensure audio flows even if paused for wake word
        self._pause_event.set()
        logger.info(
            f"Speech monitor started (min={min_duration}s, max={max_duration}s, "
            f"rms_threshold={SPEECH_RMS_THRESHOLD})"
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
            "source": "laptop_mic",
        }

    # --- Audio capture callback (runs in sounddevice's audio thread) ---

    def _audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice for each audio block. Must be fast — just queue the data."""
        if status:
            logger.debug(f"Audio stream status: {status}")
        if self._paused and not self._speech_monitor_active:
            return  # drop audio during conversation (unless speech monitoring)
        try:
            self._audio_queue.put_nowait(bytes(indata))
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

                # Run openWakeWord inference
                if self._oww_model and not in_cooldown:
                    predictions = self._oww_model.predict(pcm)
                    for model_name, score in predictions.items():
                        if score >= self.threshold:
                            self._total_detections += 1
                            self._last_detection_time = time.time()
                            logger.info(
                                f"Wake word '{model_name}' detected! "
                                f"score={score:.3f} (threshold={self.threshold})"
                            )
                            # Reset model to prevent repeated triggers
                            self._oww_model.reset()
                            # Fire callback
                            try:
                                self.on_wake_word()
                            except Exception as e:
                                logger.error(f"Wake word callback error: {e}")
                            break

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

    # --- Speech monitor helpers ---

    def _process_speech_monitor_frame(self, pcm: np.ndarray):
        """Process a single audio frame for speech/silence detection using RMS."""
        now = time.time()
        elapsed = now - self._speech_monitor_start_time

        # Compute RMS energy
        rms = int(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
        is_speech = rms > SPEECH_RMS_THRESHOLD

        if is_speech:
            if not self._speech_detected:
                logger.info(f"Speech monitor: speech started (RMS={rms}, elapsed={elapsed:.1f}s)")
            self._speech_detected = True
            self._last_speech_time = now

        # Check max duration — hard cap
        if elapsed >= self._speech_monitor_max_s:
            logger.info(f"Speech monitor: max duration reached ({self._speech_monitor_max_s}s)")
            self._fire_speech_end()
            return

        # Check no-speech timeout — no speech detected at all
        if not self._speech_detected and elapsed >= SPEECH_NO_SPEECH_TIMEOUT_S:
            logger.info(f"Speech monitor: no speech detected after {SPEECH_NO_SPEECH_TIMEOUT_S}s")
            self._fire_speech_end()
            return

        # Check silence after speech — end of utterance
        if self._speech_detected and not is_speech:
            silence_duration = now - self._last_speech_time
            if silence_duration >= SPEECH_SILENCE_DURATION_S and elapsed >= self._speech_monitor_min_s:
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

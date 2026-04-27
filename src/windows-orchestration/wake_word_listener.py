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
OWW_THRESHOLD = float(os.getenv("OWW_THRESHOLD", "0.5"))
OWW_VAD_THRESHOLD = float(os.getenv("OWW_VAD_THRESHOLD", "0"))  # 0 = disabled
OWW_CUSTOM_MODEL_PATH = os.getenv("OWW_CUSTOM_MODEL_PATH", "")

# Audio capture settings (laptop mic)
SAMPLE_RATE = 16000       # openWakeWord native rate
FRAME_SAMPLES = 1280      # 80ms at 16kHz — openWakeWord's expected frame size
BLOCK_SIZE = 1280          # match frame size for 1:1 callback-to-frame ratio

# Self-wake suppression: ignore detections for this many seconds after resume
# (prevents Misty's speaker echo from triggering a false wake)
RESUME_COOLDOWN_S = float(os.getenv("WAKE_WORD_RESUME_COOLDOWN_S", "1.5"))

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
        if self._paused:
            return  # drop audio during conversation
        try:
            self._audio_queue.put_nowait(bytes(indata))
        except queue.Full:
            pass  # drop frame if processing can't keep up

    # --- Processing thread ---

    def _process_loop(self):
        """Pull audio frames from queue and run wake word detection."""
        logger.info("Wake word processing loop started")

        while self._running:
            # Block until unpaused
            self._pause_event.wait(timeout=1.0)
            if not self._running:
                break
            if self._paused:
                continue

            try:
                # Get audio frame (block with timeout to allow shutdown check)
                audio_data = self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self._total_frames += 1

                # Convert to numpy int16 array
                pcm = np.frombuffer(audio_data, dtype=np.int16)

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

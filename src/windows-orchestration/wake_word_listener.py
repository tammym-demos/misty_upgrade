"""
Wake Word Listener — openWakeWord integration for Misty II.

Continuously polls Misty's microphone via REST API and runs wake word
detection on the companion laptop using openWakeWord. This bypasses
Misty's unreliable built-in keyphrase engine (Snapdragon 410 firmware
bug — see #22).

Architecture:
  - Background thread records short audio clips from Misty (REST API)
  - Audio is resampled from 48kHz (Misty native) to 16kHz (openWakeWord)
  - openWakeWord processes 80ms frames and returns confidence scores
  - When confidence exceeds threshold, fires the on_wake_word callback
  - Listener pauses during conversation (mic is shared resource)

Usage:
    listener = WakeWordListener(
        misty_base="http://10.0.0.44",
        on_wake_word=my_callback,
    )
    listener.start()
    # ... later ...
    listener.pause()   # during conversation (mic in use)
    listener.resume()  # after conversation ends
    listener.stop()    # shutdown
"""

import os
import io
import time
import wave
import base64
import struct
import logging
import threading
import numpy as np
import requests

from openwakeword.model import Model as OWWModel

logger = logging.getLogger("wake_word_listener")

# ============================================================================
# CONFIGURATION
# ============================================================================

# Recording settings for wake word polling
POLL_RECORD_DURATION_S = float(os.getenv("WAKE_WORD_POLL_DURATION_S", "1.5"))
POLL_FILENAME = "wakeword_poll.wav"
POLL_COOLDOWN_S = 0.3  # delay between fetch and next record cycle

# openWakeWord settings
OWW_MODEL_NAME = os.getenv("OWW_MODEL_NAME", "hey_jarvis_v0.1")
OWW_THRESHOLD = float(os.getenv("OWW_THRESHOLD", "0.5"))
OWW_VAD_THRESHOLD = float(os.getenv("OWW_VAD_THRESHOLD", "0"))  # 0 = disabled
OWW_CUSTOM_MODEL_PATH = os.getenv("OWW_CUSTOM_MODEL_PATH", "")  # path to custom .onnx

# Misty audio format
MISTY_SAMPLE_RATE = 48000
OWW_SAMPLE_RATE = 16000
OWW_FRAME_SAMPLES = 1280  # 80ms at 16kHz

# Health monitoring
MAX_CONSECUTIVE_FAILURES = 5
MAX_CONSECUTIVE_EMPTY = 10
EMPTY_RECORDING_THRESHOLD = 500  # bytes below this = empty/silent


class WakeWordListener:
    """Polls Misty's mic and runs openWakeWord detection on the companion laptop."""

    def __init__(
        self,
        misty_base: str,
        on_wake_word: callable,
        model_name: str = OWW_MODEL_NAME,
        threshold: float = OWW_THRESHOLD,
        custom_model_path: str = OWW_CUSTOM_MODEL_PATH,
    ):
        self.misty_base = misty_base.rstrip("/")
        self.on_wake_word = on_wake_word
        self.model_name = model_name
        self.threshold = threshold
        self.custom_model_path = custom_model_path

        self._running = False
        self._paused = False
        self._pause_event = threading.Event()
        self._pause_event.set()  # start unpaused
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Health tracking
        self._consecutive_failures = 0
        self._consecutive_empty = 0
        self._total_polls = 0
        self._total_detections = 0
        self._last_detection_time = 0.0
        self._start_time = 0.0

        # openWakeWord model (lazy init)
        self._oww_model: OWWModel | None = None

    def _init_model(self) -> bool:
        """Initialize the openWakeWord model."""
        try:
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

            logger.info(f"openWakeWord ready (models={list(self._oww_model.models.keys())}, "
                        f"threshold={self.threshold})")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize openWakeWord: {e}")
            return False

    def start(self) -> bool:
        """Start the wake word listener. Returns True if started successfully."""
        if self._running:
            logger.warning("Wake word listener already running")
            return True

        if not self._init_model():
            return False

        self._running = True
        self._paused = False
        self._pause_event.set()
        self._start_time = time.time()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="wake-word-listener",
            daemon=True,
        )
        self._thread.start()
        logger.info("Wake word listener started")
        return True

    def stop(self):
        """Stop the wake word listener."""
        self._running = False
        self._pause_event.set()  # unblock if paused
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        self._oww_model = None
        logger.info(f"Wake word listener stopped (polls={self._total_polls}, "
                     f"detections={self._total_detections})")

    def pause(self):
        """Pause the listener (e.g., during conversation when mic is in use)."""
        self._paused = True
        self._pause_event.clear()
        logger.debug("Wake word listener paused")

    def resume(self):
        """Resume the listener after conversation ends."""
        self._paused = False
        self._pause_event.set()
        # Reset openWakeWord model state to avoid stale activations
        if self._oww_model:
            self._oww_model.reset()
        logger.debug("Wake word listener resumed")

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return self._paused

    def get_health(self) -> dict:
        """Return health metrics for monitoring."""
        uptime = time.time() - self._start_time if self._start_time else 0
        return {
            "running": self.is_running,
            "paused": self.is_paused,
            "total_polls": self._total_polls,
            "total_detections": self._total_detections,
            "consecutive_failures": self._consecutive_failures,
            "consecutive_empty": self._consecutive_empty,
            "uptime_s": round(uptime),
            "model": self.model_name,
            "threshold": self.threshold,
        }

    # --- Internal polling loop ---

    def _poll_loop(self):
        """Main polling loop: record → fetch → detect → repeat."""
        logger.info("Wake word poll loop started")

        while self._running:
            # Wait if paused (blocks until resume() is called)
            self._pause_event.wait(timeout=1.0)
            if not self._running:
                break
            if self._paused:
                continue

            try:
                self._do_poll_cycle()
                self._consecutive_failures = 0
            except Exception as e:
                self._consecutive_failures += 1
                logger.warning(f"Wake word poll failed ({self._consecutive_failures}/"
                               f"{MAX_CONSECUTIVE_FAILURES}): {e}")
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error("Wake word listener: too many consecutive failures, "
                                 "pausing for 10s before retry")
                    time.sleep(10.0)
                    self._consecutive_failures = 0
                else:
                    time.sleep(1.0)  # brief backoff on failure

        logger.info("Wake word poll loop ended")

    def _do_poll_cycle(self):
        """Single poll cycle: record from Misty, resample, run detection."""
        self._total_polls += 1

        # 1. Record short audio clip from Misty
        audio_bytes = self._record_from_misty()
        if audio_bytes is None:
            logger.debug(f"Poll #{self._total_polls}: no audio returned")
            return

        if len(audio_bytes) < EMPTY_RECORDING_THRESHOLD:
            self._consecutive_empty += 1
            logger.debug(f"Poll #{self._total_polls}: empty recording ({len(audio_bytes)} bytes)")
            if self._consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                logger.warning(f"Wake word: {self._consecutive_empty} consecutive empty recordings")
                self._consecutive_empty = 0
            return
        self._consecutive_empty = 0

        # 2. Resample 48kHz → 16kHz
        pcm_16k = self._resample_audio(audio_bytes)
        if pcm_16k is None:
            logger.debug(f"Poll #{self._total_polls}: resample failed")
            return

        # Log audio energy periodically for diagnostics
        if self._total_polls % 20 == 0:
            rms = float(np.sqrt(np.mean(pcm_16k.astype(np.float64) ** 2)))
            logger.info(f"Poll #{self._total_polls}: {len(audio_bytes)} bytes, "
                        f"{len(pcm_16k)} samples@16kHz, RMS={rms:.0f}")

        # 3. Feed to openWakeWord in frame-sized chunks
        detected = self._run_detection(pcm_16k)
        if detected:
            self._total_detections += 1
            self._last_detection_time = time.time()
            logger.info(f"Wake word detected! (total={self._total_detections})")
            # Fire the callback
            try:
                self.on_wake_word()
            except Exception as e:
                logger.error(f"Wake word callback error: {e}")

    def _record_from_misty(self) -> bytes | None:
        """Record a short audio clip from Misty via REST API."""
        try:
            # Start recording
            r = requests.post(
                f"{self.misty_base}/api/audio/record/start",
                json={"FileName": POLL_FILENAME},
                timeout=5.0,
            )
            if r.status_code != 200:
                logger.warning(f"Record start failed: {r.status_code}")
                return None

            # Wait for recording duration
            time.sleep(POLL_RECORD_DURATION_S)

            # Stop recording
            requests.post(f"{self.misty_base}/api/audio/record/stop", timeout=5.0)
            time.sleep(POLL_COOLDOWN_S)

            # Fetch the audio
            r = requests.get(
                f"{self.misty_base}/api/audio",
                params={"FileName": POLL_FILENAME, "Base64": "true"},
                timeout=5.0,
            )
            if r.status_code != 200:
                logger.warning(f"Audio fetch failed: {r.status_code}")
                return None

            data = r.json()
            if data.get("status") != "Success":
                return None

            b64_audio = data["result"]["base64"]
            return base64.b64decode(b64_audio)

        except requests.exceptions.Timeout:
            logger.warning("Misty REST timeout during wake word poll")
            return None
        except Exception as e:
            logger.warning(f"Wake word recording error: {e}")
            return None

    @staticmethod
    def _resample_audio(wav_bytes: bytes) -> np.ndarray | None:
        """Resample WAV audio from Misty's 48kHz to openWakeWord's 16kHz.
        Returns numpy array of int16 samples at 16kHz, or None on error.
        """
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                src_rate = wf.getframerate()
                raw_data = wf.readframes(wf.getnframes())

            # Convert to numpy array
            if sample_width == 2:
                samples = np.frombuffer(raw_data, dtype=np.int16)
            elif sample_width == 4:
                samples = (np.frombuffer(raw_data, dtype=np.int32) >> 16).astype(np.int16)
            else:
                logger.warning(f"Unsupported sample width: {sample_width}")
                return None

            # Mono downmix if stereo
            if channels > 1:
                samples = samples[::channels]  # take first channel

            # Resample using simple decimation (48000/16000 = 3:1 ratio)
            if src_rate == OWW_SAMPLE_RATE:
                return samples
            elif src_rate == MISTY_SAMPLE_RATE:
                # 48kHz → 16kHz = take every 3rd sample
                return samples[::3].copy()
            else:
                # General case: linear interpolation
                ratio = OWW_SAMPLE_RATE / src_rate
                n_out = int(len(samples) * ratio)
                indices = np.arange(n_out) / ratio
                indices = np.clip(indices, 0, len(samples) - 1).astype(int)
                return samples[indices].copy()

        except Exception as e:
            logger.warning(f"Audio resample error: {e}")
            return None

    def _run_detection(self, pcm_16k: np.ndarray) -> bool:
        """Run openWakeWord on resampled audio frames. Returns True if wake word detected."""
        if self._oww_model is None:
            return False

        # Process in frame-sized chunks (1280 samples = 80ms)
        n_frames = len(pcm_16k) // OWW_FRAME_SAMPLES
        for i in range(n_frames):
            start = i * OWW_FRAME_SAMPLES
            frame = pcm_16k[start:start + OWW_FRAME_SAMPLES]

            predictions = self._oww_model.predict(frame)

            # Check all model predictions against threshold
            for model_name, score in predictions.items():
                if score >= self.threshold:
                    logger.info(f"Wake word '{model_name}' score={score:.3f} "
                                f"(threshold={self.threshold})")
                    # Reset model to prevent repeated triggers
                    self._oww_model.reset()
                    return True

        return False

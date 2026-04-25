"""
Misty Controller — REST API + WebSocket approach.
Drives Misty II from the laptop, avoiding on-robot skill runtime issues.

Architecture:
  - WebSocket subscription for KeyPhraseRecognized events
  - REST API calls for LED, recording, audio upload/playback
  - Calls orchestration service for STT→LLM→TTS pipeline
  - State machine: IDLE → RECORDING → PROCESSING → PLAYING → REARMING
"""

import os
import json
import time
import base64
import wave
import struct
import logging
import threading
import requests
import websocket

from enum import Enum
from datetime import datetime

# ============================================================================
# CONFIGURATION
# ============================================================================

MISTY_IP = os.getenv("MISTY_IP", "10.0.0.44")
MISTY_BASE = f"http://{MISTY_IP}"
MISTY_WS = f"ws://{MISTY_IP}/pubsub"
ORCHESTRATION_URL = os.getenv("ORCHESTRATION_URL", "http://10.0.0.58:5000")
RECORDING_DURATION_S = float(os.getenv("RECORDING_DURATION_S", "4"))
RECORDING_FILENAME = "foundry_input.wav"
RESPONSE_FILENAME = "foundry_response.wav"
REARM_DELAY_S = 1.0  # delay after playback before re-arming wake word
WS_RECONNECT_BASE_S = 2.0
WS_RECONNECT_MAX_S = 30.0
HEALTH_CHECK_INTERVAL_S = 30.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("misty_controller.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("misty_controller")


# ============================================================================
# STATE MACHINE
# ============================================================================

class State(Enum):
    DISCONNECTED = "DISCONNECTED"
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    PROCESSING = "PROCESSING"
    PLAYING = "PLAYING"
    REARMING = "REARMING"
    ERROR = "ERROR"


class MistyController:
    def __init__(self):
        self.state = State.DISCONNECTED
        self.state_lock = threading.Lock()
        self.ready_time = 0.0  # ignore wake events before this timestamp
        self.ws: websocket.WebSocketApp | None = None
        self.ws_thread: threading.Thread | None = None
        self.reconnect_attempts = 0
        self.turn_id = 0
        self.running = True

    # --- State transitions ---

    def set_state(self, new_state: State):
        with self.state_lock:
            old = self.state
            self.state = new_state
        if old != new_state:
            logger.info(f"State: {old.value} -> {new_state.value}")

    def get_state(self) -> State:
        with self.state_lock:
            return self.state

    # --- Misty REST helpers ---

    def misty_post(self, endpoint: str, body: dict = None, timeout: float = 5.0):
        url = f"{MISTY_BASE}{endpoint}"
        try:
            r = requests.post(url, json=body, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"POST {endpoint} failed: {e}")
            return None

    def misty_get(self, endpoint: str, params: dict = None, timeout: float = 10.0):
        url = f"{MISTY_BASE}{endpoint}"
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"GET {endpoint} failed: {e}")
            return None

    def set_led(self, r: int, g: int, b: int):
        self.misty_post("/api/led", {"red": r, "green": g, "blue": b})

    def display_image(self, filename: str):
        self.misty_post("/api/images/display", {"FileName": filename, "Alpha": 1})

    def start_keyphrase(self, force_restart=False):
        if force_restart:
            self.misty_post("/api/audio/keyphrase/stop")
            time.sleep(1.0)
        result = self.misty_post("/api/audio/keyphrase/start")
        if result and result.get("status") == "Success":
            logger.info("Wake word listening active")
            return True
        logger.error(f"Failed to start keyphrase: {result}")
        return False

    def start_recording(self, filename: str):
        return self.misty_post("/api/audio/record/start", {"FileName": filename})

    def stop_recording(self):
        return self.misty_post("/api/audio/record/stop")

    def get_audio_base64(self, filename: str) -> str | None:
        result = self.misty_get("/api/audio", {"FileName": filename, "Base64": "true"})
        if result and result.get("status") == "Success":
            return result["result"]["base64"]
        logger.error(f"Failed to get audio: {result}")
        return None

    def upload_and_play_audio(self, wav_bytes: bytes, filename: str) -> float:
        """Upload audio to Misty and play it. Returns estimated duration in seconds."""
        b64_data = base64.b64encode(wav_bytes).decode("ascii")
        result = self.misty_post("/api/audio", {
            "FileName": filename,
            "Data": b64_data,
            "ImmediatelyApply": True,
            "OverwriteExisting": True,
        }, timeout=15.0)

        # Estimate playback duration from WAV data
        duration = self._wav_duration(wav_bytes)
        if result and result.get("status") == "Success":
            logger.info(f"Playing response audio ({duration:.1f}s)")
        else:
            logger.error(f"SaveAudio failed: {result}")
        return duration

    @staticmethod
    def _wav_duration(wav_bytes: bytes) -> float:
        """Extract duration from WAV header."""
        try:
            import io
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                return frames / rate if rate > 0 else 3.0
        except Exception:
            return 3.0  # safe fallback

    # --- Health check ---

    def check_misty_health(self) -> bool:
        result = self.misty_get("/api/device", timeout=3.0)
        return result is not None and result.get("status") == "Success"

    def check_orchestration_health(self) -> bool:
        try:
            r = requests.get(f"{ORCHESTRATION_URL}/api/health", timeout=3.0)
            # Accept 200 (healthy) or 503 (degraded but running)
            if r.status_code in (200, 503):
                data = r.json()
                status = data.get("status", "")
                if status in ("ok", "degraded"):
                    logger.info(f"Orchestration: {status}")
                    return True
            return False
        except Exception:
            return False

    # --- WebSocket ---

    def _ws_subscribe_keyphrase(self):
        if self.ws:
            # Unsubscribe first to clear stale subscriptions
            unsub = json.dumps({"Operation": "unsubscribe", "EventName": "WakeWord"})
            self.ws.send(unsub)
            time.sleep(0.3)
            # Fresh subscribe
            msg = json.dumps({
                "Operation": "subscribe",
                "Type": "KeyPhraseRecognized",
                "DebounceMs": 250,
                "EventName": "WakeWord",
                "ReturnProperty": None,
                "EventConditions": [],
            })
            self.ws.send(msg)
            logger.info("Subscribed to KeyPhraseRecognized events")

    def _on_ws_open(self, ws):
        logger.info("WebSocket connected")
        self.reconnect_attempts = 0
        self._ws_subscribe_keyphrase()
        # Start keyphrase recognition
        if self.start_keyphrase():
            self.set_led(0, 255, 0)
            self.display_image("e_DefaultContent.jpg")
            # Grace period: ignore wake events for 3s after connect (spurious residual events)
            self.ready_time = time.time() + 3.0
            self.set_state(State.IDLE)
        else:
            self.set_state(State.ERROR)

    def _on_ws_message(self, ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        event_name = data.get("eventName") or data.get("EventName", "")
        msg_content = data.get("message", "")

        # Ignore registration status messages
        if isinstance(msg_content, str) and "Registration Status" in msg_content:
            logger.debug(f"WS registration: {msg_content}")
            return

        if event_name == "WakeWord":
            if self.get_state() == State.IDLE and time.time() >= self.ready_time:
                logger.info("[Wake] Wake word detected!")
                self.turn_id += 1
                threading.Thread(
                    target=self._handle_conversation_turn,
                    name=f"turn-{self.turn_id}",
                    daemon=True,
                ).start()
            else:
                logger.debug(f"Wake word ignored (state={self.get_state().value}, grace={time.time() < self.ready_time})")

    def _on_ws_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")

    def _on_ws_close(self, ws, close_status_code, close_msg):
        logger.warning(f"WebSocket closed (code={close_status_code})")
        if self.running:
            self.set_state(State.DISCONNECTED)
            self._schedule_reconnect()

    def _schedule_reconnect(self):
        delay = min(
            WS_RECONNECT_BASE_S * (2 ** self.reconnect_attempts),
            WS_RECONNECT_MAX_S,
        )
        self.reconnect_attempts += 1
        logger.info(f"Reconnecting in {delay:.1f}s (attempt {self.reconnect_attempts})")
        time.sleep(delay)
        if self.running:
            self._connect_ws()

    def _connect_ws(self):
        self.ws = websocket.WebSocketApp(
            MISTY_WS,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        self.ws_thread = threading.Thread(
            target=self.ws.run_forever,
            name="ws-thread",
            daemon=True,
        )
        self.ws_thread.start()

    # --- Conversation turn (runs in worker thread) ---

    def _handle_conversation_turn(self):
        turn = self.turn_id
        turn_start = time.time()
        logger.info(f"[Turn {turn}] Starting conversation turn")

        try:
            # 1. Visual feedback — recording
            self.set_state(State.RECORDING)
            self.set_led(255, 140, 0)  # orange
            self.display_image("e_SystemCamera.jpg")

            # 2. Record audio
            self.start_recording(RECORDING_FILENAME)
            time.sleep(RECORDING_DURATION_S)
            self.stop_recording()
            logger.info(f"[Turn {turn}] Recorded {RECORDING_DURATION_S}s")

            # Small delay for Misty to finalize the file
            time.sleep(0.5)

            # 3. Retrieve recorded audio
            self.set_state(State.PROCESSING)
            self.set_led(0, 0, 255)  # blue = processing
            self.display_image("e_SystemLogoPrompt.jpg")

            audio_b64 = self.get_audio_base64(RECORDING_FILENAME)
            if not audio_b64:
                raise RuntimeError("Failed to retrieve recorded audio from Misty")

            audio_bytes = base64.b64decode(audio_b64)
            logger.info(f"[Turn {turn}] Retrieved {len(audio_bytes)} bytes of audio")

            if len(audio_bytes) < 1000:
                raise RuntimeError(f"Recording too small ({len(audio_bytes)} bytes) — likely empty")

            # 4. Send to orchestration service
            response = requests.post(
                f"{ORCHESTRATION_URL}/api/orchestrate",
                files={"file": (RECORDING_FILENAME, audio_bytes, "audio/wav")},
                timeout=30.0,
            )
            response.raise_for_status()
            result = response.json()

            if result.get("status") != "ok":
                raise RuntimeError(f"Orchestration error: {result.get('error', 'unknown')}")

            transcribed = result.get("transcribedText", "")
            llm_response = result.get("inferenceResponse", "")
            audio_uri = result.get("responseAudio", "")
            latency = result.get("latencyMs", 0)
            logger.info(f"[Turn {turn}] User: '{transcribed}' → Misty: '{llm_response}' ({latency:.0f}ms)")

            if result.get("ttsFallback"):
                logger.warning(f"[Turn {turn}] WARNING: TTS FALLBACK was used")

            # 5. Download response audio
            if not audio_uri:
                raise RuntimeError("No response audio URI")

            audio_url = f"{ORCHESTRATION_URL}{audio_uri}"
            audio_resp = requests.get(audio_url, timeout=10.0)
            audio_resp.raise_for_status()
            response_wav = audio_resp.content
            logger.info(f"[Turn {turn}] Downloaded response audio: {len(response_wav)} bytes")

            # 6. Upload to Misty and play
            self.set_state(State.PLAYING)
            self.set_led(148, 0, 211)  # purple = speaking
            self.display_image("e_Joy2.jpg")

            play_duration = self.upload_and_play_audio(response_wav, RESPONSE_FILENAME)

            # Wait for playback to finish
            time.sleep(play_duration + 0.5)

            elapsed = time.time() - turn_start
            logger.info(f"[Turn {turn}] Complete in {elapsed:.1f}s")

        except Exception as e:
            logger.error(f"[Turn {turn}] Error: {e}", exc_info=True)
            self.set_led(255, 0, 0)  # red = error
            self.display_image("e_Sadness.jpg")
            time.sleep(2)

        finally:
            # Always re-arm
            self._rearm()

    def _rearm(self):
        self.set_state(State.REARMING)
        time.sleep(REARM_DELAY_S)
        if self.start_keyphrase(force_restart=True):
            self.set_led(0, 255, 0)  # green = ready
            self.display_image("e_DefaultContent.jpg")
            self.set_state(State.IDLE)
        else:
            logger.error("Failed to re-arm wake word")
            self.set_led(255, 0, 0)
            self.set_state(State.ERROR)
            # Retry after delay
            time.sleep(5)
            self._rearm()

    # --- Main loop ---

    def start(self):
        logger.info("=" * 60)
        logger.info("Misty Controller starting")
        logger.info(f"  Misty:         {MISTY_BASE}")
        logger.info(f"  Orchestration: {ORCHESTRATION_URL}")
        logger.info(f"  Recording:     {RECORDING_DURATION_S}s")
        logger.info("=" * 60)

        # Pre-flight checks
        if not self.check_misty_health():
            logger.error("Cannot reach Misty! Check network/IP.")
            return

        orch_ok = self.check_orchestration_health()
        if not orch_ok:
            logger.warning("Orchestration service not reachable — will retry during turns")

        # Connect WebSocket
        self._connect_ws()

        # Health check loop
        try:
            while self.running:
                time.sleep(HEALTH_CHECK_INTERVAL_S)
                state = self.get_state()
                if state == State.IDLE:
                    if not self.check_misty_health():
                        logger.warning("Misty health check failed")
                        self.set_state(State.DISCONNECTED)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            self.running = False
            if self.ws:
                self.ws.close()
            self.set_led(0, 0, 0)
            logger.info("Goodbye!")


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    controller = MistyController()
    controller.start()

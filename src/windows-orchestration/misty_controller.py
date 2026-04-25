"""
Misty Controller — REST API + WebSocket approach.
Drives Misty II from the laptop, avoiding on-robot skill runtime issues.

Architecture:
  - WebSocket subscription for KeyPhraseRecognized and BatteryCharge events
  - REST API calls for LED, recording, audio upload/playback
  - Calls orchestration service for STT→LLM→TTS pipeline
  - State machine: IDLE → RECORDING → PROCESSING → PLAYING → REARMING
  - Battery management: IDLE ↔ CHARGING (auto at 10%/25%)
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
from dataclasses import dataclass
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

# Battery thresholds (as fractions 0.0–1.0)
BATTERY_LOW_WARN = 0.20       # yellow LED warning
BATTERY_LOW_CRITICAL = 0.10   # auto-enter charging mode
BATTERY_RESUME = 0.25         # exit charging mode (must also be charging)
BATTERY_TEMP_WARN_C = 45.0    # log warning
BATTERY_TEMP_THROTTLE_C = 50.0  # add delay between turns

# Idle timeout
IDLE_TIMEOUT_S = float(os.getenv("IDLE_TIMEOUT_S", "900"))  # 15 minutes

# Unused sensor services to disable on startup for power savings
DISABLE_SERVICES = ["LocomotionService", "3DToFService"]

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
# BATTERY STATE
# ============================================================================

@dataclass
class BatteryState:
    charge_percent: float = 0.0   # 0.0–1.0
    voltage: float = 0.0
    is_charging: bool = False
    health_percent: float = 0.0   # 0.0–1.0
    temperature: float = 0.0      # Celsius
    last_updated: float = 0.0     # time.time()


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
    CHARGING = "CHARGING"
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

        # Battery monitoring
        self.battery = BatteryState()
        self.battery_lock = threading.Lock()
        self._low_battery_warned = False

        # Idle timeout
        self.last_activity_time = time.time()
        self._is_dimmed = False

    # --- State transitions ---

    def set_state(self, new_state: State):
        with self.state_lock:
            old = self.state
            self.state = new_state
        if old != new_state:
            logger.info(f"State: {old.value} -> {new_state.value}")

    def try_set_state(self, expected: State, new_state: State) -> bool:
        """Atomic compare-and-swap for state transitions. Returns True if successful."""
        with self.state_lock:
            if self.state == expected:
                old = self.state
                self.state = new_state
                logger.info(f"State: {old.value} -> {new_state.value}")
                return True
            return False

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

    # --- Battery management ---

    def check_battery(self) -> BatteryState | None:
        """Poll battery status via REST API and update internal state."""
        result = self.misty_get("/api/battery", timeout=3.0)
        if result and result.get("status") == "Success":
            data = result.get("result", {})
            with self.battery_lock:
                self.battery.charge_percent = data.get("chargePercent", 0.0)
                self.battery.voltage = data.get("voltage", 0.0)
                self.battery.is_charging = data.get("isCharging", False)
                self.battery.health_percent = data.get("healthPercent", 0.0)
                self.battery.temperature = data.get("temperature", 0.0)
                self.battery.last_updated = time.time()
                battery_snapshot = BatteryState(
                    charge_percent=self.battery.charge_percent,
                    voltage=self.battery.voltage,
                    is_charging=self.battery.is_charging,
                    health_percent=self.battery.health_percent,
                    temperature=self.battery.temperature,
                    last_updated=self.battery.last_updated,
                )
            self._log_battery(battery_snapshot)
            return battery_snapshot
        logger.warning("Failed to read battery status")
        return None

    def _update_battery_from_event(self, data: dict):
        """Update battery state from a WebSocket BatteryCharge event."""
        with self.battery_lock:
            self.battery.charge_percent = data.get("chargePercent", self.battery.charge_percent)
            self.battery.voltage = data.get("voltage", self.battery.voltage)
            self.battery.is_charging = data.get("isCharging", self.battery.is_charging)
            self.battery.health_percent = data.get("healthPercent", self.battery.health_percent)
            self.battery.temperature = data.get("temperature", self.battery.temperature)
            self.battery.last_updated = time.time()
            battery_snapshot = BatteryState(
                charge_percent=self.battery.charge_percent,
                voltage=self.battery.voltage,
                is_charging=self.battery.is_charging,
                health_percent=self.battery.health_percent,
                temperature=self.battery.temperature,
                last_updated=self.battery.last_updated,
            )
        self._log_battery(battery_snapshot)
        self._evaluate_battery_thresholds(battery_snapshot)

    def _log_battery(self, b: BatteryState):
        logger.info(
            f"Battery: {b.charge_percent*100:.0f}% | {b.voltage:.1f}V | "
            f"charging={b.is_charging} | health={b.health_percent*100:.0f}% | "
            f"temp={b.temperature:.0f}°C"
        )
        if b.temperature >= BATTERY_TEMP_THROTTLE_C:
            logger.warning(f"Battery temperature {b.temperature:.0f}°C exceeds throttle threshold ({BATTERY_TEMP_THROTTLE_C}°C)")
        elif b.temperature >= BATTERY_TEMP_WARN_C:
            logger.warning(f"Battery temperature {b.temperature:.0f}°C exceeds warning threshold ({BATTERY_TEMP_WARN_C}°C)")

    def _evaluate_battery_thresholds(self, b: BatteryState):
        """Check battery levels and trigger state changes as needed."""
        # Critical: auto-enter charging mode (atomic transition)
        if b.charge_percent < BATTERY_LOW_CRITICAL:
            if self.try_set_state(State.IDLE, State.CHARGING):
                logger.warning(f"Battery critically low ({b.charge_percent*100:.0f}%) — entering charging mode")
                self._apply_charging_mode()
            return

        # Exit charging mode when sufficiently charged (charge level alone, no is_charging requirement)
        if self.get_state() == State.CHARGING and b.charge_percent >= BATTERY_RESUME:
            self.exit_charging_mode()
            return

        # Warning: flash yellow LED briefly (non-blocking)
        if b.charge_percent < BATTERY_LOW_WARN and not self._low_battery_warned and self.get_state() == State.IDLE:
            self._low_battery_warned = True
            logger.warning(f"Battery low ({b.charge_percent*100:.0f}%) — warning")
            self.set_led(255, 200, 0)  # yellow
            threading.Timer(1.0, self._restore_idle_led).start()
        elif b.charge_percent >= BATTERY_LOW_WARN:
            self._low_battery_warned = False

    def _restore_idle_led(self):
        """Restore green LED after low-battery warning flash (runs on timer thread)."""
        if self.get_state() == State.IDLE:
            self.set_led(0, 255, 0)

    def get_battery_snapshot(self) -> BatteryState:
        with self.battery_lock:
            return BatteryState(
                charge_percent=self.battery.charge_percent,
                voltage=self.battery.voltage,
                is_charging=self.battery.is_charging,
                health_percent=self.battery.health_percent,
                temperature=self.battery.temperature,
                last_updated=self.battery.last_updated,
            )

    # --- Charging mode ---

    def enter_charging_mode(self):
        """Minimize power draw for faster charging. Atomic state transition."""
        if self.try_set_state(State.IDLE, State.CHARGING):
            self._apply_charging_mode()

    def _apply_charging_mode(self):
        """Apply charging mode side effects (call after state is already CHARGING)."""
        self.misty_post("/api/audio/keyphrase/stop")
        self.misty_post("/api/skills/cancel")
        self.set_led(0, 0, 0)
        self.display_image("e_Sleeping.jpg")
        logger.info("Charging mode active — keyphrase off, LED off, display sleeping")

    def exit_charging_mode(self):
        """Resume normal operation from charging mode."""
        if self.start_keyphrase(force_restart=True):
            self.set_led(0, 255, 0)
            self.display_image("e_DefaultContent.jpg")
            self.last_activity_time = time.time()
            self._is_dimmed = False
            self.set_state(State.IDLE)
            logger.info("Exited charging mode — resumed normal operation")
        else:
            logger.error("Failed to resume from charging mode")
            self.set_state(State.ERROR)

    # --- Sensor service management ---

    def _disable_unused_services(self):
        """Disable sensor services not needed for conversation to reduce power/heat."""
        for service in DISABLE_SERVICES:
            result = self.misty_post("/api/services", {"Name": service, "Enabled": False})
            if result:
                logger.info(f"Disabled service: {service}")
            else:
                logger.warning(f"Failed to disable service: {service}")

    def _restore_services(self):
        """Re-enable sensor services on shutdown."""
        for service in DISABLE_SERVICES:
            self.misty_post("/api/services", {"Name": service, "Enabled": True})
            logger.info(f"Re-enabled service: {service}")

    # --- Shutdown ---

    def _shutdown(self):
        """Centralized cleanup on exit."""
        logger.info("Shutting down...")
        self.running = False
        # Log final battery state
        battery = self.check_battery()
        if battery:
            logger.info(f"Final battery: {battery.charge_percent*100:.0f}% | {battery.voltage:.1f}V")
        # Restore services, stop keyphrase, LED off
        self._restore_services()
        self.misty_post("/api/audio/keyphrase/stop")
        if self.ws:
            self.ws.close()
        self.set_led(0, 0, 0)
        logger.info("Goodbye!")

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

    def _ws_subscribe_battery(self):
        if self.ws:
            unsub = json.dumps({"Operation": "unsubscribe", "EventName": "BatteryMonitor"})
            self.ws.send(unsub)
            time.sleep(0.3)
            msg = json.dumps({
                "Operation": "subscribe",
                "Type": "BatteryCharge",
                "DebounceMs": 60000,
                "EventName": "BatteryMonitor",
                "ReturnProperty": None,
                "EventConditions": [],
            })
            self.ws.send(msg)
            logger.info("Subscribed to BatteryCharge events")

    def _on_ws_open(self, ws):
        logger.info("WebSocket connected")
        self.reconnect_attempts = 0
        # Always subscribe to events
        self._ws_subscribe_keyphrase()
        self._ws_subscribe_battery()

        current_state = self.get_state()
        if current_state == State.CHARGING:
            # Reconnected during charging — stay in charging mode
            logger.info("WebSocket reconnected in CHARGING mode — not restarting keyphrase")
            return

        # Normal startup: start keyphrase recognition
        if self.start_keyphrase():
            self.set_led(0, 255, 0)
            self.display_image("e_DefaultContent.jpg")
            # Grace period: ignore wake events for 3s after connect (spurious residual events)
            self.ready_time = time.time() + 3.0
            self.last_activity_time = time.time()
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

        if event_name == "BatteryMonitor":
            if isinstance(msg_content, dict):
                self._update_battery_from_event(msg_content)
            return

        if event_name == "WakeWord":
            self.last_activity_time = time.time()
            # Restore from dimmed state on activity
            if self._is_dimmed and self.get_state() == State.IDLE:
                self._is_dimmed = False
                self.set_led(0, 255, 0)
                self.display_image("e_DefaultContent.jpg")
                logger.info("Restored from idle-dim on wake word")

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

        # Battery guard: enter charging mode if battery critically low
        battery = self.get_battery_snapshot()
        if battery.last_updated > 0 and battery.charge_percent < BATTERY_LOW_CRITICAL:
            logger.warning(f"[Turn {turn}] Skipping — battery too low ({battery.charge_percent*100:.0f}%)")
            if self.try_set_state(State.IDLE, State.CHARGING):
                self._apply_charging_mode()
            return

        # Temperature throttle: add delay if overheating
        if battery.last_updated > 0 and battery.temperature >= BATTERY_TEMP_THROTTLE_C:
            logger.warning(f"[Turn {turn}] Thermal throttle — waiting 2s (temp={battery.temperature:.0f}°C)")
            time.sleep(2.0)

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
        logger.info(f"  Idle timeout:  {IDLE_TIMEOUT_S}s")
        logger.info("=" * 60)

        # Pre-flight checks
        if not self.check_misty_health():
            logger.error("Cannot reach Misty! Check network/IP.")
            return

        # Initial battery check
        battery = self.check_battery()
        start_in_charging = battery and battery.charge_percent < BATTERY_LOW_CRITICAL
        if start_in_charging:
            logger.warning(f"Battery critically low at startup ({battery.charge_percent*100:.0f}%) — starting in charging mode")

        orch_ok = self.check_orchestration_health()
        if not orch_ok:
            logger.warning("Orchestration service not reachable — will retry during turns")

        # Disable unused sensor services to reduce power/heat
        self._disable_unused_services()

        # Cancel any lingering skills (e.g., built-in faceDetection)
        self.misty_post("/api/skills/cancel")

        # Connect WebSocket (will enter IDLE or stay DISCONNECTED)
        self._connect_ws()

        # If battery was critically low at startup, override to charging mode
        # (give WS a moment to connect first)
        if start_in_charging:
            time.sleep(2.0)
            self.set_state(State.CHARGING)
            self._apply_charging_mode()

        # Health check loop
        try:
            while self.running:
                time.sleep(HEALTH_CHECK_INTERVAL_S)
                state = self.get_state()

                # Battery monitoring (every health check cycle)
                battery = self.check_battery()
                if battery:
                    self._evaluate_battery_thresholds(battery)

                # Idle timeout: dim LED after inactivity
                if state == State.IDLE and not self._is_dimmed:
                    idle_duration = time.time() - self.last_activity_time
                    if idle_duration > IDLE_TIMEOUT_S:
                        self._is_dimmed = True
                        self.set_led(0, 50, 0)  # dim green
                        logger.info(f"Idle for {idle_duration/60:.0f}min — dimming LED")

                # Health check (only when idle)
                if state == State.IDLE:
                    if not self.check_misty_health():
                        logger.warning("Misty health check failed")
                        self.set_state(State.DISCONNECTED)
        except KeyboardInterrupt:
            self._shutdown()


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    controller = MistyController()
    controller.start()

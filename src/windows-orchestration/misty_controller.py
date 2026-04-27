"""
Misty Controller — REST API + WebSocket approach.
Drives Misty II from the laptop, avoiding on-robot skill runtime issues.

Architecture:
  - WebSocket subscription for KeyPhraseRecognized and BatteryCharge events
  - REST API calls for LED, recording, audio upload/playback
  - Calls orchestration service for STT→LLM→TTS pipeline
  - State machine: IDLE → RECORDING → PROCESSING → PLAYING → [LISTENING → ...] → REARMING
  - Follow-up listening: after each response, listens for continued speech (up to 60s)
    without requiring the wake word again. Silence ends the conversation.
  - Battery management: IDLE ↔ CHARGING (auto at 10%/25%)
"""

import os
import sys
import json
import time
import base64
import wave
import struct
import signal
import atexit
import logging
import threading
import requests
import websocket

from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================================================
# CONFIGURATION
# ============================================================================

MISTY_IP = os.getenv("MISTY_IP", "10.0.0.44")
MISTY_BASE = f"http://{MISTY_IP}"
MISTY_WS = f"ws://{MISTY_IP}/pubsub"
ORCHESTRATION_URL = os.getenv("ORCHESTRATION_URL", "http://10.0.0.58:5000")
RECORDING_DURATION_S = float(os.getenv("RECORDING_DURATION_S", "6"))
RECORDING_FILENAME = "foundry_input.wav"
RESPONSE_FILENAME = "foundry_response.wav"
REARM_DELAY_S = 3.0  # delay after playback before re-arming wake word (increased from 1.0 for reliability)
FOLLOWUP_LISTEN_S = float(os.getenv("FOLLOWUP_LISTEN_S", "5"))  # seconds to listen for follow-up
FOLLOWUP_TIMEOUT_S = float(os.getenv("FOLLOWUP_TIMEOUT_S", "90"))  # max follow-up window (extended from 60)
FOLLOWUP_SILENCE_THRESHOLD = 1000  # audio bytes below this = silence (no speech)
FOLLOWUP_MAX_TURNS = int(os.getenv("FOLLOWUP_MAX_TURNS", "12"))  # cap recording cycles per session
WS_RECONNECT_BASE_S = 2.0
WS_RECONNECT_MAX_S = 30.0
HEALTH_CHECK_INTERVAL_S = 10.0  # reduced from 30s for watchdog responsiveness

# Laptop wake word listener (issue #44) — use laptop mic instead of Misty's keyphrase engine
USE_LAPTOP_WAKE_WORD = os.getenv("USE_LAPTOP_WAKE_WORD", "").lower() in ("1", "true", "yes")

# Keyphrase watchdog — detects silent failures and auto-recovers
WATCHDOG_IDLE_TIMEOUT_S = float(os.getenv("WATCHDOG_IDLE_TIMEOUT_S", "90"))  # 90s after rearm with no wake event
WATCHDOG_ESCALATE_TIMEOUT_S = float(os.getenv("WATCHDOG_ESCALATE_TIMEOUT_S", "60"))  # 60s after recovery attempt

# Battery thresholds (as fractions 0.0–1.0)
BATTERY_LOW_WARN = 0.20       # yellow LED warning
BATTERY_LOW_CRITICAL = 0.10   # auto-enter charging mode
BATTERY_RESUME = 0.25         # exit charging mode (must also be charging)
BATTERY_TEMP_WARN_C = 45.0    # log warning
BATTERY_TEMP_THROTTLE_C = 50.0  # add delay between turns

# Idle timeout
IDLE_TIMEOUT_S = float(os.getenv("IDLE_TIMEOUT_S", "900"))  # 15 minutes

# Proactive reboot — keyphrase engine degrades after ~2 conversation cycles (#22)
PROACTIVE_REBOOT_AFTER_CYCLES = int(os.getenv("PROACTIVE_REBOOT_AFTER_CYCLES", "5"))
# Max recording cycles before proactive reboot — each record/play cycle stresses
# the Snapdragon 410 mic hardware. With follow-up conversations, a single "cycle"
# can have 8+ recordings. Reboot before hardware exhaustion.
PROACTIVE_REBOOT_AFTER_RECORDINGS = int(os.getenv("PROACTIVE_REBOOT_AFTER_RECORDINGS", "15"))
REBOOT_POLL_INTERVAL_S = 5.0   # poll interval while waiting for Misty to come back
REBOOT_TIMEOUT_S = 120.0       # max wait for Misty to come back after reboot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("misty_controller.log", encoding="utf-8"),
        logging.StreamHandler(open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)),
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
    LISTENING = "LISTENING"  # follow-up listening after response
    REARMING = "REARMING"
    REBOOTING = "REBOOTING"  # proactive reboot in progress (#22)
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

        # Keyphrase watchdog — detect silent failures
        self._last_wake_event_time = 0.0       # last ACTUAL wake word event received
        self._last_keyphrase_armed_time = 0.0   # last time keyphrase was started/re-armed
        self._watchdog_recovery_level = 0       # 0=none, 1=soft reset done, 2=sensory reboot done
        self._watchdog_recovery_time = 0.0      # when the last recovery attempt was made

        # Laptop wake word listener (optional, #44)
        self._wake_word_listener = None

        # Proactive reboot — counts successful conversation cycles (wake→response→rearm)
        self._conversation_cycles = 0
        self._recording_cycles = 0  # total recordings since last reboot

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

    def move_head(self, pitch: float = 0, roll: float = 0, yaw: float = 0, velocity: float = 50):
        """Move Misty's head. Pitch: -40(up) to 26(down). Roll: -40 to 40. Yaw: -81(right) to 81(left)."""
        self.misty_post("/api/head", {
            "Pitch": pitch, "Roll": roll, "Yaw": yaw, "Velocity": velocity
        })

    def move_arms(self, left: float = None, right: float = None, velocity: float = 50):
        """Move arms. Position: -29(up) to 90(down)."""
        body = {"Velocity": velocity}
        if left is not None:
            body["LeftArmPosition"] = left
        if right is not None:
            body["RightArmPosition"] = right
        self.misty_post("/api/arms", body)

    def start_keyphrase(self, force_restart=False):
        if force_restart:
            self.misty_post("/api/audio/keyphrase/stop")
            time.sleep(2.0)  # 2s delay — Misty needs time to fully release audio resources (#22)
        result = self.misty_post("/api/audio/keyphrase/start")
        if result and result.get("status") == "Success":
            logger.info("Wake word listening active")
            self._last_keyphrase_armed_time = time.time()
            return True
        logger.error(f"Failed to start keyphrase: {result}")
        return False

    def _rearm_keyphrase_after_ignored_event(self):
        """Re-arm keyphrase after an ignored WakeWord event (grace period or wrong state).
        
        Misty auto-stops keyphrase after ANY KeyPhraseRecognized event, even stale ones
        replayed on WebSocket subscription. If we don't re-arm, keyphrase is dead. (#22)
        """
        time.sleep(1.5)  # brief delay to let Misty finish stopping keyphrase
        self.start_keyphrase(force_restart=True)
        logger.info("Keyphrase re-armed after ignored wake event")

    def _cancel_all_skills(self):
        """Cancel all running on-robot skills (e.g., faceDetection auto-start)."""
        try:
            running = self.misty_get("/api/skills/running")
            if running and running.get("result"):
                names = [s.get("name", "unknown") for s in running["result"]]
                logger.info(f"Cancelling {len(names)} running skill(s): {names}")
                self.misty_post("/api/skills/cancel")
                time.sleep(1.0)  # let skills stop before we start keyphrase
            else:
                logger.info("No on-robot skills running")
        except Exception as e:
            logger.warning(f"Failed to cancel skills: {e}")

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
        # Only log battery when values change significantly (reduce log noise)
        last = getattr(self, "_last_logged_battery", None)
        charge_pct = round(b.charge_percent * 100)
        should_log = (
            last is None
            or abs(charge_pct - last.get("charge", 0)) >= 5
            or b.is_charging != last.get("charging")
            or abs(b.voltage - last.get("voltage", 0)) >= 0.3
        )
        if should_log:
            logger.info(
                f"Battery: {charge_pct}% | {b.voltage:.1f}V | "
                f"charging={b.is_charging} | health={b.health_percent*100:.0f}% | "
                f"temp={b.temperature:.0f}\u00b0C"
            )
            self._last_logged_battery = {
                "charge": charge_pct, "charging": b.is_charging, "voltage": b.voltage
            }
        if b.temperature >= BATTERY_TEMP_THROTTLE_C:
            logger.warning(f"Battery temperature {b.temperature:.0f}\u00b0C exceeds throttle threshold ({BATTERY_TEMP_THROTTLE_C}\u00b0C)")
        elif b.temperature >= BATTERY_TEMP_WARN_C:
            logger.warning(f"Battery temperature {b.temperature:.0f}\u00b0C exceeds warning threshold ({BATTERY_TEMP_WARN_C}\u00b0C)")

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

    # --- Keyphrase watchdog ---

    def _watchdog_check(self):
        """Detect silent keyphrase failure and auto-recover with escalating strategy.

        Only runs when state is IDLE and keyphrase has been armed. Uses the gap
        between keyphrase-armed time and now (with no wake events) as the signal,
        NOT just "no wake events in N minutes" — avoids false positives when
        nobody is talking to Misty.

        Disabled when laptop wake word is active — Misty's keyphrase is just a
        backup in that mode, and the watchdog would falsely trigger reboots since
        no Misty keyphrase events fire.
        """
        if self._wake_word_listener:
            return  # laptop wake word handles detection; skip keyphrase watchdog
        if self.get_state() != State.IDLE:
            return
        if self._last_keyphrase_armed_time == 0.0:
            return

        now = time.time()

        # The watchdog window starts from whichever is later:
        # the last actual wake event, or the last keyphrase arm/recovery
        baseline = max(self._last_wake_event_time, self._last_keyphrase_armed_time,
                       self._watchdog_recovery_time)
        idle_since = now - baseline

        if self._watchdog_recovery_level == 0:
            # Level 0 → 1: Soft reset (stop/start keyphrase + cancel skills)
            if idle_since > WATCHDOG_IDLE_TIMEOUT_S:
                logger.warning(f"Watchdog: no wake events in {idle_since:.0f}s — soft-resetting keyphrase")
                self.set_led(255, 200, 0)  # yellow flash
                self._cancel_all_skills()
                self.start_keyphrase(force_restart=True)
                self._watchdog_recovery_level = 1
                self._watchdog_recovery_time = time.time()
                # Restore LED after brief flash
                threading.Timer(2.0, self._restore_idle_led).start()

        elif self._watchdog_recovery_level == 1:
            # Level 1 → 2: Second soft reset (skip sensory reboot — it breaks mic permanently, see #33)
            if idle_since > WATCHDOG_ESCALATE_TIMEOUT_S:
                logger.warning("Watchdog: first soft reset failed — trying second soft reset")
                self.set_led(255, 100, 0)  # darker yellow
                self._cancel_all_skills()
                self.start_keyphrase(force_restart=True)
                self._watchdog_recovery_level = 2
                self._watchdog_recovery_time = time.time()
                threading.Timer(2.0, self._restore_idle_led).start()

        elif self._watchdog_recovery_level == 2:
            # Level 2 → full reboot (nuclear option, skips sensory-only reboot)
            # NOTE: Sensory-only reboot permanently breaks the mic until physical power cycle (#33)
            if idle_since > WATCHDOG_ESCALATE_TIMEOUT_S:
                logger.critical("Watchdog: soft resets failed — full reboot (last resort)")
                self.set_led(255, 0, 0)
                self.misty_post("/api/reboot", {"Core": True, "SensoryServices": True})
                # Full reboot kills WS — controller auto-reconnects via existing logic
                # Reset watchdog so it starts fresh after reboot
                self._watchdog_recovery_level = 0
                self._watchdog_recovery_time = time.time()


    def check_mic_health(self) -> bool:
        """Quick mic health check: record 1s and verify we get real audio data.
        Returns True if mic is working, False if it returns empty recordings.
        NOTE: Stops keyphrase before recording since they can't run simultaneously."""
        try:
            # Must stop keyphrase first — it locks the mic (#22)
            self.misty_post("/api/audio/keyphrase/stop")
            time.sleep(0.5)
            resp = self.misty_post("/api/audio/record/start", {"FileName": "mic_health_check.wav"})
            if not resp:
                return False
            time.sleep(1.5)
            self.misty_post("/api/audio/record/stop")
            time.sleep(0.5)
            audio = self.misty_get("/api/audio", params={"FileName": "mic_health_check.wav", "Base64": "true"})
            if not audio or "result" not in audio:
                return False
            b64 = audio["result"].get("base64", "")
            byte_count = len(b64) * 3 // 4 if b64 else 0
            if byte_count < 100:
                logger.error(f"Mic health check FAILED: only {byte_count} bytes (mic likely broken, needs physical power cycle — see #33)")
                return False
            logger.info(f"Mic health check passed: {byte_count} bytes")
            return True
        except Exception as e:
            logger.error(f"Mic health check error: {e}")
            return False

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

    # --- Shutdown ---

    def _shutdown(self):
        """Centralized cleanup on exit. Stops keyphrase to release mic lock."""
        if not self.running:
            return  # Already shut down
        logger.info("Shutting down...")
        self.running = False
        # Stop laptop wake word listener
        if self._wake_word_listener:
            self._wake_word_listener.stop()
            self._wake_word_listener = None
        # Log final battery state
        battery = self.check_battery()
        if battery:
            logger.info(f"Final battery: {battery.charge_percent*100:.0f}% | {battery.voltage:.1f}V")
        # Stop keyphrase, LED off
        self.misty_post("/api/audio/keyphrase/stop")
        if self.ws:
            self.ws.close()
        self.set_led(0, 0, 0)
        logger.info("Goodbye!")

    # --- WebSocket ---

    def _ws_subscribe_keyphrase(self):
        if self.ws:
            # Save previous name for cleanup on reconnect
            prev = getattr(self, '_wake_event_name', None)
            # Use unique event name to avoid "Cannot register event with same name" 
            # when previous controller died without unsubscribing (#22)
            self._wake_event_name = f"WakeWord_{int(time.time())}"
            
            # Try to unsubscribe any old names (best effort)
            for old_name in ["WakeWord", prev or '']:
                if old_name:
                    unsub = json.dumps({"Operation": "unsubscribe", "EventName": old_name})
                    self.ws.send(unsub)
            time.sleep(0.3)
            
            # Fresh subscribe with unique name
            msg = json.dumps({
                "Operation": "subscribe",
                "Type": "KeyPhraseRecognized",
                "DebounceMs": 0,
                "EventName": self._wake_event_name,
                "ReturnProperty": None,
                "EventConditions": [],
            })
            self.ws.send(msg)
            logger.info(f"Subscribed to KeyPhraseRecognized events (name={self._wake_event_name})")

    def _ws_subscribe_battery(self):
        if self.ws:
            prev = getattr(self, '_battery_event_name', None)
            self._battery_event_name = f"BatteryMonitor_{int(time.time())}"
            
            for old_name in ["BatteryMonitor", prev or '']:
                if old_name:
                    unsub = json.dumps({"Operation": "unsubscribe", "EventName": old_name})
                    self.ws.send(unsub)
            time.sleep(0.3)
            
            msg = json.dumps({
                "Operation": "subscribe",
                "Type": "BatteryCharge",
                "DebounceMs": 60000,
                "EventName": self._battery_event_name,
                "ReturnProperty": None,
                "EventConditions": [],
            })
            self.ws.send(msg)
            logger.info(f"Subscribed to BatteryCharge events (name={self._battery_event_name})")

    def _on_ws_open(self, ws):
        logger.info("WebSocket connected")
        self.reconnect_attempts = 0
        # Always subscribe to battery events
        self._ws_subscribe_battery()

        # In laptop wake word mode, skip keyphrase entirely — the laptop mic
        # handles wake word detection. Starting Misty's keyphrase would:
        # 1. Hold the Snapdragon 410 mic, requiring a 2s delay before recording
        # 2. Allow "Hey Misty" to trigger conversations on a degraded mic → crash
        if not self._wake_word_listener:
            self._ws_subscribe_keyphrase()

        current_state = self.get_state()
        if current_state == State.CHARGING:
            # Reconnected during charging — stay in charging mode
            logger.info("WebSocket reconnected in CHARGING mode — not restarting keyphrase")
            return

        # Cancel any auto-started skills (e.g., faceDetection)
        self._cancel_all_skills()

        # Start keyphrase recognition (only in non-laptop mode)
        # In laptop wake word mode, the laptop mic handles wake word detection.
        # MUST use force_restart=True to stop stale keyphrase from previous
        # controller sessions. Without stop-first, keyphrase/start returns
        # "Success" but the engine doesn't actually reset (#22).
        if self._wake_word_listener:
            # Laptop mode — no keyphrase needed, just go to IDLE
            # Explicitly stop keyphrase to ensure mic is free
            self.misty_post("/api/audio/keyphrase/stop")
            self.set_led(0, 255, 0)
            self.display_image("e_DefaultContent.jpg")
            if current_state in (State.REARMING, State.REBOOTING):
                self.last_activity_time = time.time()
                self.set_state(State.IDLE)
                logger.info(f"{'Reboot' if current_state == State.REBOOTING else 'Re-arm'} complete — laptop wake word active (no keyphrase)")
            else:
                self.ready_time = time.time() + 3.0
                self.last_activity_time = time.time()
                self.set_state(State.IDLE)
        elif self.start_keyphrase(force_restart=True):
            self.set_led(0, 255, 0)
            self.display_image("e_DefaultContent.jpg")
            if current_state in (State.REARMING, State.REBOOTING):
                # Re-arm or post-reboot reconnect — no grace period, go straight to IDLE
                self.last_activity_time = time.time()
                self.set_state(State.IDLE)
                logger.info(f"{'Reboot' if current_state == State.REBOOTING else 'Re-arm'} complete — keyphrase active (fresh WebSocket)")
            else:
                # Initial startup — grace period to ignore stale events
                self.ready_time = time.time() + 3.0
                self.last_activity_time = time.time()
                self.set_state(State.IDLE)
        else:
            self.set_state(State.ERROR)

    def _on_ws_message(self, ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning(f"WS non-JSON message: {message[:200]}")
            return

        event_name = data.get("eventName") or data.get("EventName", "")
        msg_content = data.get("message", "")

        # Log ALL WebSocket messages for debugging keyphrase issues (#22)
        if event_name:
            msg_preview = str(msg_content)[:200] if msg_content else "(empty)"
            logger.info(f"WS event: {event_name} | msg_type={type(msg_content).__name__} | msg={msg_preview}")
        else:
            logger.info(f"WS raw: {str(message)[:300]}")

        # Ignore registration status messages
        if isinstance(msg_content, str) and "Registration Status" in msg_content:
            logger.debug(f"WS registration: {msg_content}")
            return

        if event_name == getattr(self, '_battery_event_name', 'BatteryMonitor') or event_name == "BatteryMonitor":
            if isinstance(msg_content, dict):
                self._update_battery_from_event(msg_content)
            return

        if event_name == getattr(self, '_wake_event_name', 'WakeWord') or event_name == "WakeWord":
            self.last_activity_time = time.time()
            self._last_wake_event_time = time.time()
            # Wake event received — keyphrase is working, reset watchdog
            self._watchdog_recovery_level = 0
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
                # CRITICAL: Misty auto-stops keyphrase after ANY recognition event,
                # including stale events replayed on subscription. If we ignore
                # the event (grace period / wrong state), we MUST re-arm keyphrase
                # or it stays dead forever. (#22)
                logger.warning(f"Wake word during grace/wrong state — re-arming keyphrase "
                               f"(state={self.get_state().value}, grace={time.time() < self.ready_time})")
                threading.Thread(
                    target=self._rearm_keyphrase_after_ignored_event,
                    name="rearm-grace",
                    daemon=True,
                ).start()

    def _on_ws_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")

    def _on_ws_close(self, ws, close_status_code, close_msg):
        logger.warning(f"WebSocket closed (code={close_status_code})")
        if self.running and self.get_state() not in (State.REARMING, State.REBOOTING):
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

    # --- Laptop wake word listener (issue #44) ---

    def _start_laptop_wake_word(self):
        """Initialize and start the laptop-based wake word listener."""
        try:
            from wake_word_listener import WakeWordListener
            self._wake_word_listener = WakeWordListener(
                on_wake_word=self._on_laptop_wake_word,
            )
            if self._wake_word_listener.start():
                logger.info("Laptop wake word listener active — Misty keyphrase is backup only")
            else:
                logger.warning("Laptop wake word listener failed to start — using Misty keyphrase only")
                self._wake_word_listener = None
        except ImportError as e:
            logger.warning(f"Laptop wake word not available ({e}) — using Misty keyphrase only")
            self._wake_word_listener = None

    def _on_laptop_wake_word(self):
        """Callback fired by laptop wake word listener on detection."""
        self.last_activity_time = time.time()
        self._last_wake_event_time = time.time()
        self._watchdog_recovery_level = 0

        if self._is_dimmed and self.get_state() == State.IDLE:
            self._is_dimmed = False
            self.set_led(0, 255, 0)
            self.display_image("e_DefaultContent.jpg")
            logger.info("Restored from idle-dim on laptop wake word")

        if self.get_state() == State.IDLE and time.time() >= self.ready_time:
            logger.info("[Wake] Laptop mic wake word detected!")
            self.turn_id += 1
            # Pause listener during conversation to prevent self-wake
            if self._wake_word_listener:
                self._wake_word_listener.pause()
            threading.Thread(
                target=self._handle_conversation_turn,
                name=f"turn-{self.turn_id}",
                daemon=True,
            ).start()
        else:
            logger.debug(f"Laptop wake word ignored (state={self.get_state().value})")

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
            had_speech = self._do_conversation_exchange(turn, turn_start)

            # Only count cycles with actual speech for proactive reboot tracking.
            # Empty STT (user too far from Misty's mic) shouldn't trigger a reboot.
            if had_speech:
                self._conversation_cycles += 1
                logger.info(f"[Turn {turn}] Conversation cycle {self._conversation_cycles}/{PROACTIVE_REBOOT_AFTER_CYCLES}")
            else:
                logger.info(f"[Turn {turn}] No speech — not counting toward reboot cycles")

            # Follow-up listening loop — listen for continued conversation
            # without requiring the wake word again
            followup_start = time.time()
            followup_count = 0
            while (time.time() - followup_start) < FOLLOWUP_TIMEOUT_S:
                followup_count += 1

                # Cap recording cycles to prevent Snapdragon 410 mic degradation
                if followup_count > FOLLOWUP_MAX_TURNS:
                    logger.info(f"[Turn {turn}] Follow-up turn cap reached ({FOLLOWUP_MAX_TURNS}) — ending conversation")
                    break

                remaining = FOLLOWUP_TIMEOUT_S - (time.time() - followup_start)
                logger.info(f"[Turn {turn}] Follow-up listen #{followup_count} "
                            f"({remaining:.0f}s remaining in window)")

                had_speech = self._listen_for_followup(turn)
                if not had_speech:
                    logger.info(f"[Turn {turn}] No follow-up speech — ending conversation")
                    break

        except Exception as e:
            logger.error(f"[Turn {turn}] Error: {e}", exc_info=True)
            self.set_led(255, 0, 0)  # red = error
            self.display_image("e_Sadness.jpg")
            time.sleep(2)

        finally:
            # Always re-arm wake word
            self._rearm()

    def _do_conversation_exchange(self, turn: int, turn_start: float):
        """Record from Misty, orchestrate STT→LLM→TTS, play response.
        
        Returns True if speech was detected and a response was played,
        False if no speech was detected (empty STT).
        """
        # 1. Visual feedback — listening attentively
        self.set_state(State.RECORDING)
        self.set_led(255, 140, 0)  # orange
        self.display_image("e_Admiration.jpg")  # wide-eyed, attentive
        self.move_head(pitch=-10, roll=0, yaw=0, velocity=60)  # look up slightly — eye contact

        # Stop keyphrase before recording — shared mic on Snapdragon 410.
        # In Misty-keyphrase mode, keyphrase auto-stops on detection.
        # In laptop wake word mode, keyphrase is NOT running (we don't start it),
        # so just do a belt-and-suspenders cleanup with minimal delay.
        if self._wake_word_listener:
            logger.info(f"[Turn {turn}] Clearing mic before recording (laptop wake word mode)")
            self.misty_post("/api/audio/record/stop")  # belt-and-suspenders cleanup
            time.sleep(0.5)  # minimal delay — no keyphrase to release

        # Play a short "ready" chime so the user knows Misty is preparing.
        # After the chime, LED changes to bright green = "speak now".
        try:
            self.misty_post("/api/audio/play", {"FileName": "s_Awe3.wav", "Volume": 30})
            time.sleep(0.8)  # let the chime play before recording starts
        except Exception as e:
            logger.debug(f"[Turn {turn}] Ready chime failed: {e}")

        # 2. Record audio — bright green LED + tally light = "I'm listening, speak now!"
        self.set_led(0, 255, 0)  # green = recording active, speak now
        self.start_recording(RECORDING_FILENAME)
        record_start = time.time()
        
        if self._wake_word_listener and self._wake_word_listener.is_running:
            # Dynamic recording: laptop mic monitors speech and signals when to stop
            speech_ended = threading.Event()
            self._wake_word_listener.start_speech_monitor(
                on_speech_end=lambda: speech_ended.set(),
                min_duration=RECORDING_DURATION_S,  # at least the standard duration
                max_duration=15.0,
            )
            speech_ended.wait(timeout=15.0)
            self._wake_word_listener.stop_speech_monitor()
        else:
            # Fallback: fixed duration recording
            time.sleep(RECORDING_DURATION_S)
        
        self.stop_recording()
        self._recording_cycles += 1
        record_duration = time.time() - record_start
        logger.info(f"[Turn {turn}] Recorded {record_duration:.1f}s (cycle {self._recording_cycles})")

        # Small delay for Misty to finalize the file
        time.sleep(0.5)

        # 3. Retrieve recorded audio — wondering face + thinking sound
        self.set_state(State.PROCESSING)
        self.set_led(0, 0, 255)  # blue = processing
        self.display_image("e_Contempt.jpg")  # one eyebrow raised — "hmm, let me think..."
        self.move_head(pitch=-5, roll=5, yaw=20, velocity=40)  # tilt head — pondering

        # Play a wondering sound so the user knows Misty heard them
        try:
            self.misty_post("/api/audio/play", {"FileName": "s_Amazement.wav", "Volume": 25})
        except Exception as e:
            logger.debug(f"[Turn {turn}] Thinking sound failed: {e}")

        audio_b64 = self.get_audio_base64(RECORDING_FILENAME)
        if not audio_b64:
            raise RuntimeError("Failed to retrieve recorded audio from Misty")

        audio_bytes = base64.b64decode(audio_b64)
        logger.info(f"[Turn {turn}] Retrieved {len(audio_bytes)} bytes of audio")

        if len(audio_bytes) < FOLLOWUP_SILENCE_THRESHOLD:
            raise RuntimeError(f"Recording too small ({len(audio_bytes)} bytes) — likely empty")

        # 4-6. Orchestrate and play response
        return self._do_orchestrate_and_respond(turn, audio_bytes, turn_start)

    def _do_orchestrate_and_respond(self, turn: int, audio_bytes: bytes, turn_start: float):
        """Send audio to orchestration service and play response on Misty.
        
        Returns True if speech was detected and a response was played,
        False if no speech was detected (empty STT).
        """
        # Processing state already set by caller — just send to orchestration

        # Send to orchestration service
        response = requests.post(
            f"{ORCHESTRATION_URL}/api/orchestrate",
            files={"file": (RECORDING_FILENAME, audio_bytes, "audio/wav")},
            timeout=30.0,
        )
        result = response.json()

        # Handle empty STT gracefully — not an error, just no speech detected
        if result.get("error") == "empty_stt" or response.status_code == 400:
            logger.info(f"[Turn {turn}] No speech detected in recording (empty STT) — treating as silence")
            return False

        if response.status_code != 200:
            raise RuntimeError(f"Orchestration HTTP {response.status_code}: {result.get('error', 'unknown')}")

        if result.get("status") != "ok":
            raise RuntimeError(f"Orchestration error: {result.get('error', 'unknown')}")

        transcribed = result.get("transcribedText", "")
        llm_response = result.get("inferenceResponse", "")
        audio_uri = result.get("responseAudio", "")
        latency = result.get("latencyMs", 0)
        logger.info(f"[Turn {turn}] User: '{transcribed}' -> Misty: '{llm_response}' ({latency:.0f}ms)")

        if result.get("ttsFallback"):
            logger.warning(f"[Turn {turn}] WARNING: TTS FALLBACK was used")

        # Download response audio
        if not audio_uri:
            raise RuntimeError("No response audio URI")

        audio_url = f"{ORCHESTRATION_URL}{audio_uri}"
        audio_resp = requests.get(audio_url, timeout=10.0)
        audio_resp.raise_for_status()
        response_wav = audio_resp.content
        logger.info(f"[Turn {turn}] Downloaded response audio: {len(response_wav)} bytes")

        # Upload to Misty and play — animated, looking at user
        self.set_state(State.PLAYING)
        self.set_led(148, 0, 211)  # purple = speaking
        self.display_image("e_EcstacyHilarious.jpg")  # big expressive face
        self.move_head(pitch=-10, roll=0, yaw=0, velocity=60)  # face forward — eye contact

        play_duration = self.upload_and_play_audio(response_wav, RESPONSE_FILENAME)

        # Wait for playback to finish (generous buffer — no completion callback from Misty)
        time.sleep(play_duration + 2.0)

        elapsed = time.time() - turn_start
        logger.info(f"[Turn {turn}] Exchange complete in {elapsed:.1f}s")
        return True

    def _listen_for_followup(self, turn: int) -> bool:
        """Listen briefly for follow-up speech. Returns True if speech was detected and responded to."""
        self.set_state(State.LISTENING)
        self.set_led(0, 200, 200)  # cyan = listening for follow-up
        self.display_image("e_Joy.jpg")  # warm, expectant — "go on..."
        self.move_head(pitch=-10, roll=-3, yaw=-10, velocity=40)  # slight head tilt — attentive

        # Record a short clip — use VAD if available
        self.start_recording(RECORDING_FILENAME)
        if self._wake_word_listener and self._wake_word_listener.is_running:
            speech_ended = threading.Event()
            self._wake_word_listener.start_speech_monitor(
                on_speech_end=lambda: speech_ended.set(),
                min_duration=2.0,   # shorter min for follow-ups
                max_duration=10.0,  # shorter max for follow-ups
            )
            speech_ended.wait(timeout=10.0)
            self._wake_word_listener.stop_speech_monitor()
        else:
            time.sleep(FOLLOWUP_LISTEN_S)
        self.stop_recording()
        self._recording_cycles += 1
        time.sleep(0.5)  # finalize

        audio_b64 = self.get_audio_base64(RECORDING_FILENAME)
        if not audio_b64:
            logger.warning(f"[Turn {turn}] Follow-up: failed to retrieve audio")
            return False

        audio_bytes = base64.b64decode(audio_b64)
        logger.info(f"[Turn {turn}] Follow-up recording: {len(audio_bytes)} bytes")

        # Very small recordings are certainly silence
        if len(audio_bytes) < FOLLOWUP_SILENCE_THRESHOLD:
            return False

        # Show thinking face while processing follow-up
        self.set_state(State.PROCESSING)
        self.set_led(0, 0, 255)  # blue = processing
        self.display_image("e_Contempt.jpg")  # wondering face
        self.move_head(pitch=-5, roll=5, yaw=20, velocity=40)

        # Send through the full pipeline — orchestration returns empty_stt error
        # if no speech was detected, which we treat as silence
        try:
            response = requests.post(
                f"{ORCHESTRATION_URL}/api/orchestrate",
                files={"file": (RECORDING_FILENAME, audio_bytes, "audio/wav")},
                timeout=30.0,
            )
            result = response.json()

            # empty_stt = silence, not an error
            if result.get("error") == "empty_stt":
                logger.info(f"[Turn {turn}] Follow-up: silence (empty STT)")
                return False

            if response.status_code != 200 or result.get("status") != "ok":
                logger.warning(f"[Turn {turn}] Follow-up: orchestration error: {result.get('error', 'unknown')}")
                return False

            # Speech detected — download and play the response
            transcribed = result.get("transcribedText", "")
            llm_response = result.get("inferenceResponse", "")
            audio_uri = result.get("responseAudio", "")
            latency = result.get("latencyMs", 0)
            logger.info(f"[Turn {turn}] Follow-up: '{transcribed}' -> '{llm_response}' ({latency:.0f}ms)")

            if result.get("ttsFallback"):
                logger.warning(f"[Turn {turn}] WARNING: TTS FALLBACK was used")

            if not audio_uri:
                logger.error(f"[Turn {turn}] Follow-up: no response audio URI")
                return False

            audio_url = f"{ORCHESTRATION_URL}{audio_uri}"
            audio_resp = requests.get(audio_url, timeout=10.0)
            audio_resp.raise_for_status()
            response_wav = audio_resp.content
            logger.info(f"[Turn {turn}] Follow-up audio: {len(response_wav)} bytes")

            # Play the response
            self.set_state(State.PLAYING)
            self.set_led(148, 0, 211)  # purple = speaking
            self.display_image("e_EcstacyHilarious.jpg")  # animated speaking face
            self.move_head(pitch=-10, roll=0, yaw=0, velocity=60)  # face forward

            play_duration = self.upload_and_play_audio(response_wav, RESPONSE_FILENAME)
            time.sleep(play_duration + 0.5)
            return True

        except Exception as e:
            logger.error(f"[Turn {turn}] Follow-up error: {e}")
            return False

    def _rearm(self):
        self.set_state(State.REARMING)
        self.move_head(pitch=0, roll=0, yaw=0, velocity=40)  # center head

        # Check if proactive reboot is needed (#22)
        # Trigger on conversation cycles OR total recording cycles — whichever hits first.
        # With follow-up conversations, a single "cycle" can have 8+ recordings,
        # exhausting the Snapdragon 410 mic hardware before the cycle limit is reached.
        needs_reboot = (self._conversation_cycles >= PROACTIVE_REBOOT_AFTER_CYCLES or
                        self._recording_cycles >= PROACTIVE_REBOOT_AFTER_RECORDINGS)
        if needs_reboot:
            logger.info(f"Proactive reboot trigger: {self._conversation_cycles} conversations, "
                        f"{self._recording_cycles} recordings")
            self._proactive_reboot()
            return

        # Normal re-arm: audio resource cleanup before re-arming.
        self.stop_recording()
        if self._wake_word_listener:
            # Laptop mode — no keyphrase to restart, shorter cooldown needed
            logger.info("Re-arm: audio cleanup (2s) — laptop wake word mode")
            time.sleep(2.0)
        else:
            # Misty keyphrase mode — aggressive cleanup needed for Snapdragon 410
            # hardware to fully release resources before keyphrase restart (#22).
            self.misty_post("/api/audio/keyphrase/stop")
            logger.info("Re-arm: audio cooldown (5s) to let hardware release resources")
            time.sleep(5.0)

        # Full WebSocket reconnect for fresh event subscription
        logger.info("Re-arm: closing WebSocket for fresh reconnect")
        if self.ws:
            self.ws.close()
            time.sleep(1.0)
        self.reconnect_attempts = 0
        self._connect_ws()
        # _on_ws_open handles: subscribe + keyphrase (or laptop mode setup)
        # Wait for the connection to establish
        if self._wake_word_listener:
            time.sleep(3.0)  # shorter — no keyphrase to start
        else:
            time.sleep(6.0)  # keyphrase needs time to arm

        # Resume laptop wake word listener after conversation ends
        if self._wake_word_listener:
            self._wake_word_listener.resume()
            logger.info("Laptop wake word listener resumed")

    def _proactive_reboot(self):
        """Proactive reboot to prevent keyphrase silent failure (#22).
        
        The Snapdragon 410 keyphrase engine degrades after ~2 conversation cycles.
        Instead of waiting for failure, reboot preemptively and tell the user.
        """
        logger.warning(f"Proactive reboot: {self._conversation_cycles} cycles reached "
                       f"(limit={PROACTIVE_REBOOT_AFTER_CYCLES})")

        # Battery check — skip reboot if battery is critically low
        battery = self.get_battery_snapshot()
        if battery.last_updated > 0 and battery.charge_percent < BATTERY_LOW_CRITICAL:
            logger.warning(f"Proactive reboot skipped — battery too low ({battery.charge_percent*100:.0f}%)")
            self._conversation_cycles = 0  # reset to avoid infinite skip loop
            self._recording_cycles = 0
            # Fall back to normal re-arm
            self.stop_recording()
            self.misty_post("/api/audio/keyphrase/stop")
            time.sleep(5.0)
            if self.ws:
                self.ws.close()
                time.sleep(1.0)
            self.reconnect_attempts = 0
            self._connect_ws()
            time.sleep(6.0)
            return

        self.set_state(State.REBOOTING)

        # Announce the reboot to the user
        self.set_led(255, 200, 0)  # yellow = maintenance
        self.display_image("e_ContentLeft.jpg")  # calm face
        self._play_reboot_announcement()

        # Stop all audio before reboot
        self.stop_recording()
        self.misty_post("/api/audio/keyphrase/stop")
        time.sleep(1.0)

        # Close WebSocket cleanly (state=REBOOTING suppresses auto-reconnect)
        if self.ws:
            self.ws.close()
            time.sleep(0.5)

        # Issue full reboot
        logger.info("Proactive reboot: issuing Core+Sensory reboot")
        self.misty_post("/api/reboot", {"Core": True, "SensoryServices": True})

        # Wait for Misty to come back online
        logger.info(f"Proactive reboot: waiting up to {REBOOT_TIMEOUT_S:.0f}s for Misty to come back")
        time.sleep(10.0)  # Misty needs time to start shutting down

        reboot_start = time.time()
        while time.time() - reboot_start < REBOOT_TIMEOUT_S:
            time.sleep(REBOOT_POLL_INTERVAL_S)
            if self.check_misty_health():
                elapsed = time.time() - reboot_start
                logger.info(f"Proactive reboot: Misty back online after {elapsed:.0f}s")
                break
        else:
            logger.error("Proactive reboot: timeout waiting for Misty — attempting reconnect anyway")

        # Reset all reboot-related bookkeeping
        self._conversation_cycles = 0
        self._recording_cycles = 0
        self._watchdog_recovery_level = 0
        self._watchdog_recovery_time = time.time()
        self._last_keyphrase_armed_time = 0.0
        self._last_wake_event_time = 0.0

        # Reconnect WebSocket — _on_ws_open handles subscribe + keyphrase start
        self.reconnect_attempts = 0
        self._connect_ws()
        # Wait for full readiness (WS connected + keyphrase armed)
        time.sleep(8.0)

        # Verify we made it back to IDLE
        if self.get_state() == State.IDLE:
            logger.info("Proactive reboot: fully recovered — ready for conversations")
        else:
            logger.warning(f"Proactive reboot: recovery incomplete (state={self.get_state().value})")

        # Resume laptop wake word listener after reboot recovery
        if self._wake_word_listener:
            self._wake_word_listener.resume()
            logger.info("Proactive reboot: laptop wake word listener resumed")

    def _play_reboot_announcement(self):
        """Play a reboot announcement on Misty via orchestration TTS.
        Falls back to Misty's built-in system sound if TTS is unavailable.
        """
        try:
            response = requests.post(
                f"{ORCHESTRATION_URL}/api/fallback-tts",
                json={"text": "I need a quick reset. Be right back!"},
                timeout=10.0,
            )
            if response.status_code == 200:
                result = response.json()
                audio_uri = result.get("audio_uri", "")
                if audio_uri:
                    audio_url = f"{ORCHESTRATION_URL}{audio_uri}"
                    audio_resp = requests.get(audio_url, timeout=10.0)
                    audio_resp.raise_for_status()
                    wav_bytes = audio_resp.content
                    duration = self.upload_and_play_audio(wav_bytes, "reboot_announce.wav")
                    time.sleep(duration + 1.0)
                    logger.info("Proactive reboot: announcement played")
                    return
            logger.warning(f"Proactive reboot: TTS unavailable (status={response.status_code})")
        except Exception as e:
            logger.warning(f"Proactive reboot: announcement failed: {e}")

        # Fallback: play Misty's built-in system sound
        try:
            self.misty_post("/api/audio/play", {"FileName": "s_Awe2.wav"})
        except Exception as e:
            logger.debug(f"Proactive reboot: built-in fallback sound failed: {e}")
            pass

    # --- Main loop ---

    def start(self):
        logger.info("=" * 60)
        logger.info("Misty Controller starting")
        logger.info(f"  Misty:         {MISTY_BASE}")
        logger.info(f"  Orchestration: {ORCHESTRATION_URL}")
        logger.info(f"  Recording:     {RECORDING_DURATION_S}s")
        logger.info(f"  Idle timeout:  {IDLE_TIMEOUT_S}s")
        logger.info(f"  Watchdog:      soft={WATCHDOG_IDLE_TIMEOUT_S}s, escalate={WATCHDOG_ESCALATE_TIMEOUT_S}s")
        logger.info(f"  Wake word:     {'laptop mic (openWakeWord)' if USE_LAPTOP_WAKE_WORD else 'Misty keyphrase (Snapdragon 410)'}")
        logger.info(f"  Follow-up:     {FOLLOWUP_TIMEOUT_S}s window, max {FOLLOWUP_MAX_TURNS} turns")
        logger.info(f"  Proactive reboot: every {PROACTIVE_REBOOT_AFTER_CYCLES} conversations or {PROACTIVE_REBOOT_AFTER_RECORDINGS} recordings")
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

        # Cancel any lingering skills (e.g., built-in faceDetection)
        self.misty_post("/api/skills/cancel")

        # Connect WebSocket (will enter IDLE or stay DISCONNECTED)
        self._connect_ws()

        # Start laptop wake word listener if enabled (#44)
        if USE_LAPTOP_WAKE_WORD:
            self._start_laptop_wake_word()

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

                # Keyphrase watchdog (only when idle)
                if state == State.IDLE:
                    self._watchdog_check()
        except KeyboardInterrupt:
            self._shutdown()


# ============================================================================
# TEST API — HTTP endpoint for programmatic test triggers
# ============================================================================

CONTROLLER_API_PORT = int(os.getenv("CONTROLLER_API_PORT", "5001"))


class ControllerAPIHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for test triggers and status queries."""

    controller: "MistyController" = None  # set before server starts

    def log_message(self, format, *args):
        logger.debug(f"API: {format % args}")

    def _send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        if self.path == "/api/status":
            ctrl = self.controller
            battery = ctrl.get_battery_snapshot()
            self._send_json(200, {
                "state": ctrl.get_state().name,
                "turn_id": ctrl.turn_id,
                "conversation_cycles": ctrl._conversation_cycles,
                "proactive_reboot_at": PROACTIVE_REBOOT_AFTER_CYCLES,
                "battery_percent": round(battery.charge_percent * 100),
                "battery_charging": battery.is_charging,
                "uptime_s": round(time.time() - ctrl._start_time) if hasattr(ctrl, "_start_time") else None,
            })
        elif self.path == "/api/mic/health":
            ctrl = self.controller
            healthy = ctrl.check_mic_health()
            self._send_json(200 if healthy else 503, {
                "mic_healthy": healthy,
                "message": "Mic OK" if healthy else "Mic broken — needs physical power cycle (see #33)",
            })
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path == "/api/test/trigger":
            ctrl = self.controller
            state = ctrl.get_state()

            if state != State.IDLE:
                self._send_json(409, {
                    "error": "not_idle",
                    "state": state.name,
                    "message": f"Controller is in {state.name} state, must be IDLE to trigger",
                })
                return

            # Simulate wake word event — start a conversation turn
            # Stop keyphrase first (normally auto-stopped on wake word detection)
            logger.info("[Test API] Triggering conversation turn via test endpoint")
            ctrl.misty_post("/api/audio/keyphrase/stop")
            time.sleep(0.5)

            ctrl.last_activity_time = time.time()
            ctrl._last_wake_event_time = time.time()
            ctrl._is_dimmed = False
            ctrl._watchdog_recovery_level = 0

            # Run conversation in worker thread (same as wake word handler)
            ctrl.turn_id += 1
            t = threading.Thread(
                target=ctrl._handle_conversation_turn,
                name=f"turn-{ctrl.turn_id}",
                daemon=True,
            )
            t.start()

            self._send_json(200, {
                "status": "triggered",
                "turn_id": ctrl.turn_id,
                "message": "Conversation turn started — Misty is recording",
            })
        else:
            self._send_json(404, {"error": "not_found"})


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    controller = MistyController()
    controller._start_time = time.time()

    # Graceful shutdown on SIGTERM/SIGINT — stops keyphrase to prevent mic lock
    def _signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        controller._shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    atexit.register(controller._shutdown)

    # Start test API server in background thread
    ControllerAPIHandler.controller = controller
    api_server = HTTPServer(("0.0.0.0", CONTROLLER_API_PORT), ControllerAPIHandler)
    api_thread = threading.Thread(target=api_server.serve_forever, name="api-server", daemon=True)
    api_thread.start()
    logger.info(f"Test API server on port {CONTROLLER_API_PORT}")

    controller.start()

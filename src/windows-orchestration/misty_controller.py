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

from dotenv import load_dotenv
load_dotenv()  # Load .env before any os.getenv() calls

from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import config_defaults  # canonical source for all shared default values (see config_defaults.py)

# ============================================================================
# CONFIGURATION
# ============================================================================

MISTY_IP = os.getenv("MISTY_IP", config_defaults.MISTY_IP)
MISTY_BASE = f"http://{MISTY_IP}"
MISTY_WS = f"ws://{MISTY_IP}/pubsub"
ORCHESTRATION_URL = os.getenv("ORCHESTRATION_URL", config_defaults.ORCHESTRATION_URL)
RECORDING_DURATION_S = float(os.getenv("RECORDING_DURATION_S", str(config_defaults.RECORDING_DURATION_S)))
RECORDING_FILENAME = "foundry_input.wav"
RESPONSE_FILENAME = "foundry_response.wav"
REARM_DELAY_S = 3.0  # delay after playback before re-arming wake word (increased from 1.0 for reliability)
FOLLOWUP_ENABLED = os.getenv("FOLLOWUP_ENABLED", str(config_defaults.FOLLOWUP_ENABLED)).lower() in ("1", "true", "yes")
FOLLOWUP_LISTEN_S = float(os.getenv("FOLLOWUP_LISTEN_S", str(config_defaults.FOLLOWUP_LISTEN_S)))  # seconds to listen for follow-up
FOLLOWUP_TIMEOUT_S = float(os.getenv("FOLLOWUP_TIMEOUT_S", str(config_defaults.FOLLOWUP_TIMEOUT_S)))  # max follow-up window (extended from 60)
FOLLOWUP_SILENCE_THRESHOLD = 1000  # audio bytes below this = silence (no speech)
FOLLOWUP_MAX_TURNS = int(os.getenv("FOLLOWUP_MAX_TURNS", str(config_defaults.FOLLOWUP_MAX_TURNS)))  # cap recording cycles per session
WS_RECONNECT_BASE_S = 2.0
WS_RECONNECT_MAX_S = 30.0
HEALTH_CHECK_INTERVAL_S = 10.0  # reduced from 30s for watchdog responsiveness

# Laptop wake word listener (issue #44) — use laptop mic instead of Misty's keyphrase engine
USE_LAPTOP_WAKE_WORD = os.getenv("USE_LAPTOP_WAKE_WORD", "true").lower() in ("1", "true", "yes")
_RAW_LAPTOP_MISTY_RECORDING_MODE = os.getenv("LAPTOP_MISTY_RECORDING_MODE", config_defaults.LAPTOP_MISTY_RECORDING_MODE).strip().lower()
LAPTOP_MISTY_RECORDING_MODE = (
    _RAW_LAPTOP_MISTY_RECORDING_MODE
    if _RAW_LAPTOP_MISTY_RECORDING_MODE in ("fallback", "tally", "off")
    else "fallback"
)
LAPTOP_MISTY_TALLY_RECORDING_S = float(os.getenv("LAPTOP_MISTY_TALLY_RECORDING_S", str(config_defaults.LAPTOP_MISTY_TALLY_RECORDING_S)))

# Face recognition (#16) — use Misty's camera to identify people
USE_FACE_RECOGNITION = os.getenv("USE_FACE_RECOGNITION", "").lower() in ("1", "true", "yes")
FACE_RECOGNITION_TIMEOUT_S = float(os.getenv("FACE_RECOGNITION_TIMEOUT_S", str(config_defaults.FACE_RECOGNITION_TIMEOUT_S)))

# Laptop-side face recognition (#125) — replaces Misty's unreliable on-chip
# /api/faces pipeline with a laptop-side recognizer that identifies enrolled
# people and feeds the existing speaker_name path. Off by default; enable
# INSTEAD of USE_FACE_RECOGNITION after enrolling a profile (tools/enroll_face.py).
USE_LAPTOP_FACE_RECOGNITION = os.getenv("USE_LAPTOP_FACE_RECOGNITION", "").lower() in ("1", "true", "yes")
FACE_PROFILE_DIR = os.getenv("FACE_PROFILE_DIR", config_defaults.FACE_PROFILE_DIR)
FACE_RECOGNITION_SOURCE = os.getenv("FACE_RECOGNITION_SOURCE", config_defaults.FACE_RECOGNITION_SOURCE).strip().lower()
FACE_RECOGNITION_THRESHOLD = float(os.getenv("FACE_RECOGNITION_THRESHOLD", str(config_defaults.FACE_RECOGNITION_THRESHOLD)))
FACE_RECOGNITION_MIN_CONSISTENT_FRAMES = int(os.getenv("FACE_RECOGNITION_MIN_CONSISTENT_FRAMES", str(config_defaults.FACE_RECOGNITION_MIN_CONSISTENT_FRAMES)))
FACE_RECOGNITION_MIN_SAMPLES = int(os.getenv("FACE_RECOGNITION_MIN_SAMPLES", str(config_defaults.FACE_RECOGNITION_MIN_SAMPLES)))
FACE_DETECTOR_MODEL_PATH = os.getenv("FACE_DETECTOR_MODEL_PATH", config_defaults.FACE_DETECTOR_MODEL_PATH)
FACE_EMBEDDER_MODEL_PATH = os.getenv("FACE_EMBEDDER_MODEL_PATH", config_defaults.FACE_EMBEDDER_MODEL_PATH)

# Face animation (#73) — state-driven animated expressions
USE_FACE_ANIMATION = os.getenv("USE_FACE_ANIMATION", "").lower() in ("1", "true", "yes")
FACE_ANIMATION_MAX_FPS = float(os.getenv("FACE_ANIMATION_MAX_FPS", str(config_defaults.FACE_ANIMATION_MAX_FPS)))
FACE_ANIMATION_MIN_INTERVAL_S = float(os.getenv("FACE_ANIMATION_MIN_INTERVAL_S", str(config_defaults.FACE_ANIMATION_MIN_INTERVAL_S)))

# Custom face assets uploaded to Misty at startup (#110). These are the
# Pillow-generated faces referenced by FaceAnimator / display flows. They are
# uploaded idempotently at startup; if any are unavailable, the animator falls
# back to built-in firmware faces. Defaults come from config_defaults (single
# source of truth); FACE_ASSETS_DIR remains env-overridable.
FACE_ASSETS_DIR = os.getenv("FACE_ASSETS_DIR", config_defaults.FACE_ASSETS_DIR)
REQUIRED_FACE_ASSETS = config_defaults.REQUIRED_FACE_ASSETS

# Face asset replacement / sync behavior (#116). "missing" (default) uploads
# only assets not already on the device (idempotent startup); "overwrite" force
# re-uploads every required asset so a new face reusing the same filenames
# replaces Misty's stored assets. FACE_ASSETS_FORCE_UPLOAD=true is a convenience
# alias that forces overwrite for a single run.
_RAW_FACE_ASSETS_SYNC_MODE = os.getenv(
    "FACE_ASSETS_SYNC_MODE", config_defaults.FACE_ASSETS_SYNC_MODE
).strip().lower()
FACE_ASSETS_SYNC_MODE = (
    _RAW_FACE_ASSETS_SYNC_MODE
    if _RAW_FACE_ASSETS_SYNC_MODE in ("missing", "overwrite")
    else "missing"
)
if os.getenv("FACE_ASSETS_FORCE_UPLOAD", "").lower() in ("1", "true", "yes"):
    FACE_ASSETS_SYNC_MODE = "overwrite"

# Emotion-aware subtle talking head motion (#116) — off by default, gated here.
# Only active during PLAYING and always within safe head limits; never runs
# during MOVING/CHARGING/ERROR/shutdown or drive commands.
USE_TALKING_HEAD_MOTION = os.getenv(
    "USE_TALKING_HEAD_MOTION", str(config_defaults.USE_TALKING_HEAD_MOTION)
).lower() in ("1", "true", "yes")

# Embodied expression coordinator (#74) — off by default, gated here. Maps
# constrained expression intents to bounded face/LED/head/arm choreography.
# Cancellable, non-blocking, safety-gated (motor gestures only in safe states),
# and sensor rate-limited; never issues drive/tread commands.
USE_EMBODIED_EXPRESSIONS = os.getenv(
    "USE_EMBODIED_EXPRESSIONS", str(config_defaults.USE_EMBODIED_EXPRESSIONS)
).lower() in ("1", "true", "yes")
# Tunable expression gesture parameters (env-overridable, defaults from config).
EXPRESSION_HEAD_VELOCITY = float(
    os.getenv("EXPRESSION_HEAD_VELOCITY", str(config_defaults.EXPRESSION_HEAD_VELOCITY))
)
EXPRESSION_ARM_VELOCITY = float(
    os.getenv("EXPRESSION_ARM_VELOCITY", str(config_defaults.EXPRESSION_ARM_VELOCITY))
)
EXPRESSION_SENSOR_MIN_INTERVAL_S = float(
    os.getenv(
        "EXPRESSION_SENSOR_MIN_INTERVAL_S",
        str(config_defaults.EXPRESSION_SENSOR_MIN_INTERVAL_S),
    )
)

# Keyphrase watchdog — detects silent failures and auto-recovers
WATCHDOG_IDLE_TIMEOUT_S = float(os.getenv("WATCHDOG_IDLE_TIMEOUT_S", str(config_defaults.WATCHDOG_IDLE_TIMEOUT_S)))  # 90s after rearm with no wake event
WATCHDOG_ESCALATE_TIMEOUT_S = float(os.getenv("WATCHDOG_ESCALATE_TIMEOUT_S", str(config_defaults.WATCHDOG_ESCALATE_TIMEOUT_S)))  # 60s after recovery attempt

# Battery thresholds (as fractions 0.0–1.0)
BATTERY_LOW_WARN = 0.20       # yellow LED warning
BATTERY_LOW_CRITICAL = 0.10   # auto-enter charging mode
BATTERY_RESUME = 0.25         # exit charging mode (must also be charging)
BATTERY_TEMP_WARN_C = 45.0    # log warning
BATTERY_TEMP_THROTTLE_C = 50.0  # add delay between turns

# Movement-specific battery thresholds (#52)
BATTERY_MOVEMENT_CUTOFF = 0.25     # deny movement below 25% (motors draw heavy current)
BATTERY_MOVEMENT_VOLTAGE_MIN = 7.5  # deny movement below 7.5V (near abrupt shutdown ~7V)
BATTERY_SLAM_CUTOFF = 0.35         # deny SLAM below 35% (memory-intensive)
BATTERY_VOLTAGE_DROP_HALT = 0.3    # halt if voltage drops >0.3V between readings (load-induced sag)

# Idle timeout
IDLE_TIMEOUT_S = float(os.getenv("IDLE_TIMEOUT_S", str(config_defaults.IDLE_TIMEOUT_S)))  # 15 minutes

# Proactive reboot — keyphrase engine degrades after ~2 conversation cycles (#22)
PROACTIVE_REBOOT_AFTER_CYCLES = int(os.getenv("PROACTIVE_REBOOT_AFTER_CYCLES", str(config_defaults.PROACTIVE_REBOOT_AFTER_CYCLES)))
# Max recording cycles before proactive reboot — each record/play cycle stresses
# the Snapdragon 410 mic hardware. With follow-up conversations, a single "cycle"
# can have 8+ recordings. Reboot before hardware exhaustion.
PROACTIVE_REBOOT_AFTER_RECORDINGS = int(os.getenv("PROACTIVE_REBOOT_AFTER_RECORDINGS", str(config_defaults.PROACTIVE_REBOOT_AFTER_RECORDINGS)))
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

if LAPTOP_MISTY_RECORDING_MODE != _RAW_LAPTOP_MISTY_RECORDING_MODE:
    logger.warning(
        "Invalid LAPTOP_MISTY_RECORDING_MODE=%r; using safe default 'fallback'. "
        "Valid values: fallback, tally, off.",
        _RAW_LAPTOP_MISTY_RECORDING_MODE,
    )


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
# HAZARD / SENSOR TELEMETRY (#49)
# ============================================================================

# ToF sensor positions on Misty (from docs)
TOF_SENSORS = {
    "toffc": "front_center",      # front center
    "toffr": "front_right",       # front right
    "toffl": "front_left",        # front left
    "tofr": "rear",               # rear center
    "tofdfc": "edge_front_center",  # downward front center (edge)
    "tofdfr": "edge_front_right",   # downward front right (edge)
    "tofdfl": "edge_front_left",    # downward front left (edge)
    "tofdr": "edge_rear",           # downward rear (edge)
}

# Direction → required sensors for movement safety
TOF_FORWARD_SENSORS = {"toffc", "toffr", "toffl", "tofdfc", "tofdfr", "tofdfl"}
TOF_REVERSE_SENSORS = {"tofr", "tofdr"}
TOF_EDGE_SENSORS = {"tofdfc", "tofdfr", "tofdfl", "tofdr"}

# Telemetry watchdog — per-sensor freshness during movement
TELEMETRY_STALE_TIMEOUT_S = 1.0  # sensor data older than this = stale (halt if moving)


@dataclass
class ToFReading:
    """Single Time-of-Flight sensor reading."""
    sensor_id: str = ""           # e.g., "toffc"
    distance_mm: float = 0.0     # millimeters (0 = invalid/no return)
    status: int = 0              # 0=valid, 100-class=warn, 200-class=error
    is_valid: bool = False       # True only if status indicates reliable reading
    last_updated: float = 0.0   # time.time()


@dataclass
class HazardState:
    """Aggregated hazard/sensor state for safety decision-making."""
    # Active hazards from HazardNotification events
    active_hazards: list = None       # list of hazard dicts from last event
    last_hazard_time: float = 0.0     # when last HazardNotification arrived
    hazard_halt_issued: bool = False  # True if we auto-halted due to hazard

    # Per-sensor ToF readings (keyed by sensor_id, e.g., "toffc")
    tof_readings: dict = None         # {sensor_id: ToFReading}

    # Bump sensor states (keyed by sensor position)
    bump_states: dict = None          # {sensor_name: {"is_pressed": bool, "last_updated": float}}
    last_bump_time: float = 0.0       # when last BumpSensor event arrived
    any_bump_active: bool = False     # True if any bump sensor is currently pressed

    def __post_init__(self):
        if self.active_hazards is None:
            self.active_hazards = []
        if self.tof_readings is None:
            self.tof_readings = {sid: ToFReading(sensor_id=sid) for sid in TOF_SENSORS}
        if self.bump_states is None:
            self.bump_states = {}


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
    MOVING = "MOVING"        # robot is in motion (#50)
    REARMING = "REARMING"
    REBOOTING = "REBOOTING"  # proactive reboot in progress (#22)
    CHARGING = "CHARGING"
    ERROR = "ERROR"


# Movement preemption priority (highest priority first) — determines what can
# interrupt an active movement command. (#50)
PREEMPTION_PRIORITY = [
    "hazard_stop",       # firmware-level HazardNotification (auto-halts motors)
    "battery_critical",  # battery < BATTERY_LOW_CRITICAL
    "emergency_halt",    # explicit halt() call (e.g., from teleop kill switch)
    "bump_contact",      # physical bump sensor contact
    "telemetry_stale",   # safety sensors went dark (fail closed)
    "wake_word",         # user wants to talk — stop and listen
    "move_complete",     # normal movement completion
]


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

        # Laptop wake word listener (supported wake path)
        self._wake_word_listener = None
        self._wake_word_source = "unsupported"
        self._wake_word_model_name = None
        self._wake_word_model_path = None
        self._wake_word_threshold = None
        self._wake_word_config_error = None

        # Proactive reboot — counts successful conversation cycles (wake→response→rearm)
        self._conversation_cycles = 0
        self._recording_cycles = 0  # total recordings since last reboot

        # Hazard / sensor telemetry (#49)
        self.hazard = HazardState()
        self.hazard_lock = threading.Lock()

        # Face recognition (#16) — identify who's talking to Misty
        self._recognized_face: str | None = None  # last recognized face name
        self._face_recognition_event = threading.Event()  # signaled when face recognized
        self._face_event_name: str | None = None  # WebSocket event name for face recognition
        self._trained_faces: list[str] = []  # cached list of trained face IDs

        # Laptop-side face recognition (#125) — lazily-built recognizer that
        # replaces Misty's unreliable on-chip pipeline. None until first use;
        # set to False if construction fails so we don't retry every turn.
        self._laptop_face_recognizer = None

        # Face animation (#73) — state-driven animated expressions.
        # The FaceAnimator is ALWAYS constructed so custom face identity, emotion
        # selection, and built-in fallback are available regardless of
        # USE_FACE_ANIMATION. That flag now scopes ONLY the continuous frame-loop
        # thread: when disabled, set_state()/set_emotion()/show_asset() still
        # resolve and push the correct frame synchronously (#116).
        from face_animator import FaceAnimator
        self._face_animator = FaceAnimator(
            misty_base_url=MISTY_BASE,
            enabled=USE_FACE_ANIMATION,
            max_fps=FACE_ANIMATION_MAX_FPS,
            min_interval_s=FACE_ANIMATION_MIN_INTERVAL_S,
        )
        # Only run the frame-loop thread when animation is enabled; identity,
        # emotion and fallback resolution work without the thread when disabled.
        if USE_FACE_ANIMATION:
            self._face_animator.start()

        # Emotion-aware talking head motion (#116) — gently moves the head while
        # speaking (PLAYING only). Always constructed; a no-op when disabled.
        from talking_head_motion import TalkingHeadMotion
        self._talking_head = TalkingHeadMotion(
            move_head=self.move_head,
            enabled=USE_TALKING_HEAD_MOTION,
            pitch_center=config_defaults.TALKING_HEAD_PITCH_CENTER,
            pitch_range=config_defaults.TALKING_HEAD_PITCH_RANGE,
            yaw_range=config_defaults.TALKING_HEAD_YAW_RANGE,
            roll_range=config_defaults.TALKING_HEAD_ROLL_RANGE,
            velocity=config_defaults.TALKING_HEAD_VELOCITY,
            interval_s=config_defaults.TALKING_HEAD_INTERVAL_S,
        )

        # Embodied expression coordinator (#74) — always constructed; a no-op
        # when disabled (USE_EMBODIED_EXPRESSIONS=false). Maps constrained
        # expression intents to bounded face/LED/head/arm choreography. Face
        # rendering is delegated to the FaceAnimator (with its own built-in
        # fallback); motor gestures are gated to safe states only and drive/tread
        # movement is never issued here.
        from expression_coordinator import ExpressionCoordinator
        self._expression_coordinator = ExpressionCoordinator(
            set_led=self.set_led,
            move_head=self.move_head,
            move_arms=self.move_arms,
            face_callback=lambda emotion, fallback: (
                self._face_animator.set_emotion(emotion)
                if (self._face_animator is not None and self.state == State.PLAYING)
                else self.show_face(fallback)
            ),
            safety_gate=self._expressions_safe_to_move,
            enabled=USE_EMBODIED_EXPRESSIONS,
            head_velocity=EXPRESSION_HEAD_VELOCITY,
            arm_velocity=EXPRESSION_ARM_VELOCITY,
            sensor_min_interval_s=EXPRESSION_SENSOR_MIN_INTERVAL_S,
        )

    # States in which expressive head/arm gestures are allowed. Motor gestures
    # are suppressed everywhere else (movement, charging, error, reboot, re-arm,
    # recording/listening audio capture, disconnected, and during shutdown) so
    # gestures never interfere with safety-critical behavior or audio (#74).
    SAFE_EXPRESSION_STATES = frozenset({State.IDLE, State.PROCESSING, State.PLAYING})

    def _expressions_safe_to_move(self) -> bool:
        """Safety gate for the ExpressionCoordinator's motor gestures (#74)."""
        return bool(getattr(self, "running", False)) and self.state in self.SAFE_EXPRESSION_STATES

    # Representative state -> expression intent mapping (#74). Only a small,
    # unambiguous subset drives expressions from state transitions; richer
    # trigger wiring (wake, sensors, movement outcomes) can build on this.
    # Values are intent strings coerced by the coordinator.
    STATE_EXPRESSION_MAP = {
        State.PROCESSING: "thinking",
        State.CHARGING: "sleepy",
        State.ERROR: "error",
    }

    RESPONSE_EMOTION_EXPRESSION_MAP = {
        "excited": "joy",
        "happy": "joy",
        "curious": "curious",
        "sad": "sad",
    }

    def _express_for_state(self, new_state: State) -> None:
        """Drive a bounded expression for a state transition. No-op if disabled."""
        coord = getattr(self, "_expression_coordinator", None)
        if coord is None or not coord.enabled:
            return
        intent = self.STATE_EXPRESSION_MAP.get(new_state)
        if intent:
            coord.express(intent, source="state")

    def _express_for_response_emotion(self, emotion: str) -> bool:
        """Drive bounded body expression from a spoken response emotion."""
        coord = getattr(self, "_expression_coordinator", None)
        if coord is None or not coord.enabled:
            return False
        intent = self.RESPONSE_EMOTION_EXPRESSION_MAP.get(str(emotion or "").lower())
        if not intent:
            return False
        return coord.express(intent, source="response")

    # --- State transitions ---

    def set_state(self, new_state: State):
        with self.state_lock:
            old = self.state
            self.state = new_state
        if old != new_state:
            logger.info(f"State: {old.value} -> {new_state.value}")
            if self._face_animator:
                self._face_animator.set_state(new_state)
            # Stop (and re-center) talking head motion whenever we leave PLAYING.
            # This guarantees motion never persists into MOVING/CHARGING/ERROR/
            # reboot/re-arm or any other state (#116).
            if new_state != State.PLAYING and getattr(self, "_talking_head", None):
                self._talking_head.stop()
            self._express_for_state(new_state)

    def try_set_state(self, expected: State, new_state: State) -> bool:
        """Atomic compare-and-swap for state transitions. Returns True if successful."""
        transitioned = False
        with self.state_lock:
            if self.state == expected:
                old = self.state
                self.state = new_state
                logger.info(f"State: {old.value} -> {new_state.value}")
                if self._face_animator:
                    self._face_animator.set_state(new_state)
                if new_state != State.PLAYING and getattr(self, "_talking_head", None):
                    self._talking_head.stop()
                transitioned = True
        if transitioned:
            # Outside the state lock: the coordinator is non-blocking but may join
            # a prior bounded gesture thread; keep that off the state lock.
            self._express_for_state(new_state)
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

    def show_face(self, filename: str):
        """Single entry point for direct/transient custom-face display (#116).

        Routes through the always-available FaceAnimator so built-in firmware
        fallback (e_*.jpg) applies automatically when custom assets are missing
        or failed to upload. Does not change the animation state, so it is safe
        for one-off faces (movement acknowledgment, error blips). Prefer
        ``set_state()`` for state-driven faces (emotion is applied via the
        FaceAnimator, e.g. ``self._face_animator.set_emotion()``); use this only
        where a specific asset must be shown outside a normal state transition.
        """
        if self._face_animator is not None:
            self._face_animator.show_asset(filename)
        else:  # pragma: no cover - animator is always constructed in __init__
            self.display_image(filename)

    def move_head(self, pitch: float = 0, roll: float = 0, yaw: float = 0, velocity: float = 50):
        """Move Misty's head. Pitch: -40(up) to 26(down). Roll: -40 to 40. Yaw: -81(right) to 81(left)."""
        self.misty_post("/api/head", {
            "Pitch": pitch, "Roll": roll, "Yaw": yaw, "Velocity": velocity
        })

    def move_arms(self, left: float = None, right: float = None, velocity: float = 50):
        """Move arms. Position: -29(up) to 90(down)."""
        if left is None and right is None:
            return
        if left is not None and right is not None and left == right:
            self.misty_post("/api/arms", {"Arm": "both", "Position": left, "Velocity": velocity})
            return
        if left is not None:
            self.misty_post("/api/arms", {"Arm": "left", "Position": left, "Velocity": velocity})
        if right is not None:
            self.misty_post("/api/arms", {"Arm": "right", "Position": right, "Velocity": velocity})

    # --- Drive / Locomotion (#48) ---

    # Safety bounds — enforced on all drive commands
    DRIVE_MAX_LINEAR_PCT = 30      # max linear velocity percent
    DRIVE_MAX_ANGULAR_PCT = 30     # max angular velocity percent
    DRIVE_MAX_DURATION_MS = 5000   # max single drive command duration (5s)

    def drive(self, linear_velocity: float, angular_velocity: float):
        """Drive Misty continuously until stop() or halt() is called.

        Args:
            linear_velocity: -100 (full backward) to 100 (full forward).
            angular_velocity: -100 (clockwise) to 100 (counter-clockwise).
        """
        linear_velocity = max(-self.DRIVE_MAX_LINEAR_PCT, min(self.DRIVE_MAX_LINEAR_PCT, linear_velocity))
        angular_velocity = max(-self.DRIVE_MAX_ANGULAR_PCT, min(self.DRIVE_MAX_ANGULAR_PCT, angular_velocity))
        logger.info(f"Drive: linear={linear_velocity:.0f}%, angular={angular_velocity:.0f}%")
        return self.misty_post("/api/drive", {
            "LinearVelocity": linear_velocity,
            "AngularVelocity": angular_velocity,
        })

    def drive_time(self, linear_velocity: float, angular_velocity: float, time_ms: int):
        """Drive Misty for a specified duration then stop automatically.

        Args:
            linear_velocity: -100 (full backward) to 100 (full forward).
            angular_velocity: -100 (clockwise) to 100 (counter-clockwise).
            time_ms: Duration in milliseconds (min 100, max DRIVE_MAX_DURATION_MS).
        """
        linear_velocity = max(-self.DRIVE_MAX_LINEAR_PCT, min(self.DRIVE_MAX_LINEAR_PCT, linear_velocity))
        angular_velocity = max(-self.DRIVE_MAX_ANGULAR_PCT, min(self.DRIVE_MAX_ANGULAR_PCT, angular_velocity))
        time_ms = max(100, min(self.DRIVE_MAX_DURATION_MS, int(time_ms)))
        logger.info(f"DriveTime: linear={linear_velocity:.0f}%, angular={angular_velocity:.0f}%, duration={time_ms}ms")
        return self.misty_post("/api/drive/time", {
            "LinearVelocity": linear_velocity,
            "AngularVelocity": angular_velocity,
            "TimeMs": time_ms,
        })

    def drive_track(self, left_speed: float, right_speed: float):
        """Drive Misty using individual track speeds.

        Args:
            left_speed: -100 (full backward) to 100 (full forward).
            right_speed: -100 (full backward) to 100 (full forward).
        """
        left_speed = max(-self.DRIVE_MAX_LINEAR_PCT, min(self.DRIVE_MAX_LINEAR_PCT, left_speed))
        right_speed = max(-self.DRIVE_MAX_LINEAR_PCT, min(self.DRIVE_MAX_LINEAR_PCT, right_speed))
        logger.info(f"DriveTrack: left={left_speed:.0f}%, right={right_speed:.0f}%")
        return self.misty_post("/api/drive/track", {
            "LeftTrackSpeed": left_speed,
            "RightTrackSpeed": right_speed,
        })

    def halt(self):
        """Emergency stop — halts ALL motors (drive, head, arms). Use for safety stops."""
        logger.warning("HALT: stopping all motors")
        return self.misty_post("/api/halt")

    def stop_driving(self):
        """Stop drive motors only (head/arms unaffected)."""
        logger.info("Stop driving")
        return self.misty_post("/api/drive/stop")

    def drive_arc(self, heading: float, radius: float, time_ms: int, reverse: bool = False):
        """Drive in an arc to reach an absolute heading.

        Args:
            heading: Target absolute heading (0-360 or -180 to 180).
            radius: Arc radius in meters.
            time_ms: Duration in milliseconds.
            reverse: If True, drive in reverse.
        """
        time_ms = max(100, min(self.DRIVE_MAX_DURATION_MS, int(time_ms)))
        logger.info(f"DriveArc: heading={heading:.0f}°, radius={radius:.2f}m, duration={time_ms}ms")
        return self.misty_post("/api/drive/arc", {
            "Heading": heading,
            "Radius": radius,
            "TimeMs": time_ms,
            "Reverse": reverse,
        })

    def drive_heading(self, heading: float, distance: float, time_ms: int, reverse: bool = False):
        """Drive in a straight line maintaining an absolute heading.

        Args:
            heading: Absolute heading to maintain (0-360 or -180 to 180).
            distance: Distance in meters (max 1.0m per command for safety).
            time_ms: Duration in milliseconds.
            reverse: If True, drive in reverse.
        """
        distance = max(0.01, min(1.0, distance))
        time_ms = max(100, min(self.DRIVE_MAX_DURATION_MS, int(time_ms)))
        logger.info(f"DriveHeading: heading={heading:.0f}°, distance={distance:.2f}m, duration={time_ms}ms")
        return self.misty_post("/api/drive/hdt", {
            "Heading": heading,
            "Distance": distance,
            "TimeMs": time_ms,
            "Reverse": reverse,
        })

    # --- Movement Lifecycle (#50) ---

    # Settle time after motors stop before resuming audio (#53)
    MOVEMENT_SETTLE_MS = 500  # ms to wait after halt before resuming wake word

    def _safe_halt(self, reason: str = "unknown"):
        """Stop all motors safely. Called before any transition FROM MOVING state.

        This is the single choke point for movement termination — ensures motors
        are always stopped regardless of why movement ended.
        """
        logger.warning(f"Safe halt: reason={reason}")
        self.halt()

    def _pause_wake_word_for_movement(self):
        """Pause wake word listener during movement (motor noise interference, #53)."""
        if self._wake_word_listener:
            self._wake_word_listener.pause()
            logger.info("Wake word paused for movement")

    def _resume_wake_word_after_movement(self):
        """Resume wake word listener after movement + settle time (#53)."""
        time.sleep(self.MOVEMENT_SETTLE_MS / 1000.0)
        if self._wake_word_listener:
            self._wake_word_listener.resume()
            logger.info("Wake word resumed after movement")

    def start_moving(self, reason: str = "command") -> bool:
        """Transition to MOVING state if safe to do so.

        Pre-checks:
        - Must be in IDLE state
        - No active hazards
        - No active bump contacts
        - Battery above movement cutoff (25% or 7.5V) (#52)

        Returns True if transition succeeded, False if blocked.
        """
        # Pre-flight safety checks
        with self.hazard_lock:
            if self.hazard.active_hazards:
                logger.warning(f"Cannot start moving: active hazards ({len(self.hazard.active_hazards)})")
                return False
            if self.hazard.any_bump_active:
                logger.warning("Cannot start moving: bump sensor active")
                return False

        with self.battery_lock:
            if self.battery.last_updated > 0:
                if self.battery.charge_percent < BATTERY_MOVEMENT_CUTOFF:
                    logger.warning(f"Cannot start moving: battery too low for movement "
                                   f"({self.battery.charge_percent*100:.0f}% < {BATTERY_MOVEMENT_CUTOFF*100:.0f}%)")
                    return False
                if self.battery.voltage > 0 and self.battery.voltage < BATTERY_MOVEMENT_VOLTAGE_MIN:
                    logger.warning(f"Cannot start moving: voltage too low "
                                   f"({self.battery.voltage:.1f}V < {BATTERY_MOVEMENT_VOLTAGE_MIN}V)")
                    return False

        # Atomic state transition
        if not self.try_set_state(State.IDLE, State.MOVING):
            current = self.get_state()
            logger.warning(f"Cannot start moving: not IDLE (state={current.value})")
            return False

        # Pause wake word — motor noise interferes with detection (#53)
        self._pause_wake_word_for_movement()

        logger.info(f"MOVING: started (reason={reason})")
        return True

    def stop_moving(self, reason: str = "complete"):
        """Transition from MOVING back to IDLE, halting motors first.

        Args:
            reason: Why movement is stopping (for logging/telemetry).
                    One of PREEMPTION_PRIORITY values or custom string.
        """
        if self.get_state() != State.MOVING:
            return

        self._safe_halt(reason)
        self.set_state(State.IDLE)
        self.last_activity_time = time.time()
        logger.info(f"Movement stopped: reason={reason}")

        # Resume wake word after settle time (non-blocking, #53)
        threading.Thread(
            target=self._resume_wake_word_after_movement,
            name="resume-wake-post-move",
            daemon=True,
        ).start()

    def preempt_movement(self, reason: str):
        """Force-stop movement due to higher-priority event.

        Unlike stop_moving(), this can be called from any thread (e.g., WS event
        handler) and will interrupt in-progress movement immediately.
        """
        with self.state_lock:
            if self.state != State.MOVING:
                return
            self.state = State.IDLE
        logger.warning(f"Movement PREEMPTED: {reason}")
        self._safe_halt(reason)
        self.last_activity_time = time.time()

        # Resume wake word after settle time (non-blocking, #53)
        threading.Thread(
            target=self._resume_wake_word_after_movement,
            name="resume-wake-preempt",
            daemon=True,
        ).start()

    def _execute_voice_movement(self, turn: int, movement: dict):
        """Execute a voice-triggered movement command (#56).

        Called after the acknowledgment audio has already been played.
        Transitions IDLE → MOVING, executes the command, then returns.
        On hazard preemption, generates verbal feedback ("something's in my way!").

        Args:
            turn: Current conversation turn number (for logging).
            movement: Movement command dict from orchestration service
                      (keys: "command", optionally "distance_mm", "speed_pct", "angle_deg").
        """
        command = movement.get("command", "")
        distance_mm = movement.get("distance_mm", 200)
        speed_pct = movement.get("speed_pct", 20)
        angle_deg = movement.get("angle_deg", 90)

        # Clamp to safe bounds (same as teleop endpoint)
        distance_mm = max(50, min(500, distance_mm))
        speed_pct = max(5, min(int(self.DRIVE_MAX_LINEAR_PCT), speed_pct))
        angle_deg = max(10, min(180, angle_deg))

        if command == "stop":
            self.halt()
            logger.info(f"[Turn {turn}] Voice halt executed")
            return

        # Transition to IDLE first (we're in PLAYING after the ack audio)
        self.set_state(State.IDLE)
        time.sleep(0.3)  # brief settle

        # Enter MOVING state (pre-flight checks: hazards, battery, etc.)
        if not self.start_moving(reason=f"voice_{command}"):
            logger.warning(f"[Turn {turn}] Cannot execute voice movement — blocked by safety checks")
            self._speak_movement_failure(turn, "I can't move right now. Something's blocking me.")
            return

        # Visual feedback — orange LED + adventurous face
        self.set_led(255, 165, 0)  # orange = moving
        self.show_face("face_talking_excited.gif")

        try:
            if command in ("forward", "backward"):
                velocity_mms = (speed_pct / 100.0) * 450.0
                duration_ms = int((distance_mm / velocity_mms) * 1000)
                duration_ms = max(100, min(self.DRIVE_MAX_DURATION_MS, duration_ms))
                linear = speed_pct if command == "forward" else -speed_pct
                self.drive_time(linear, 0, duration_ms)

                # Wait for completion, checking for preemption
                wait_s = duration_ms / 1000.0 + 0.5
                preempted = self._wait_for_move_completion(wait_s)
                if preempted:
                    self._speak_movement_failure(turn, "Oops, something's in my way!")
                    return

            elif command in ("rotate_left", "rotate_right"):
                angular_rate = (speed_pct / 100.0) * 150.0
                duration_ms = int((angle_deg / angular_rate) * 1000)
                duration_ms = max(100, min(self.DRIVE_MAX_DURATION_MS, duration_ms))
                angular = speed_pct if command == "rotate_left" else -speed_pct
                self.drive_time(0, angular, duration_ms)

                wait_s = duration_ms / 1000.0 + 0.5
                preempted = self._wait_for_move_completion(wait_s)
                if preempted:
                    self._speak_movement_failure(turn, "Oops, something's in my way!")
                    return
            else:
                logger.warning(f"[Turn {turn}] Unknown voice movement command: {command}")
                return

            logger.info(f"[Turn {turn}] Voice movement complete: {command}")

        except Exception as e:
            logger.error(f"[Turn {turn}] Voice movement error: {e}", exc_info=True)
        finally:
            if self.get_state() == State.MOVING:
                self.stop_moving(reason="voice_move_complete")

    def _wait_for_move_completion(self, timeout_s: float) -> bool:
        """Wait for movement to finish, checking for preemption.

        Returns True if movement was preempted (state changed from MOVING),
        False if movement completed normally.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.get_state() != State.MOVING:
                return True  # preempted
            time.sleep(0.1)
        return False  # completed normally

    def _speak_movement_failure(self, turn: int, text: str):
        """Generate and play a verbal notification about movement failure (#56)."""
        try:
            response = requests.post(
                f"{ORCHESTRATION_URL}/api/tts",
                json={"text": text},
                timeout=10.0,
            )
            if response.status_code == 200 and len(response.content) > 100:
                self.set_state(State.PLAYING)
                self.set_led(255, 255, 0)  # yellow = warning
                self.show_face("face_talking_sad.gif")
                self._talking_head.start("sad")  # emotion-aware talking motion (#116)
                play_duration = self.upload_and_play_audio(response.content, RESPONSE_FILENAME)
                time.sleep(play_duration + 1.0)
                self._talking_head.stop()  # halt + re-center head (#116)
        except Exception as e:
            logger.warning(f"[Turn {turn}] Movement failure speech failed: {e}")

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

    def _laptop_misty_recording_enabled(self) -> bool:
        """Whether laptop wake-word mode should touch Misty's recorder."""
        return LAPTOP_MISTY_RECORDING_MODE in ("fallback", "tally")

    def _laptop_misty_fallback_enabled(self) -> bool:
        """Whether a Misty-side recording is expected to be usable as fallback audio."""
        return LAPTOP_MISTY_RECORDING_MODE == "fallback"

    def _start_misty_recording_window(
        self,
        turn: int,
        phase: str,
        max_duration_s: float | None = None,
    ):
        """Start Misty's recorder and return an idempotent stop callback.

        In laptop wake-word mode this is used either for the full fallback
        recording window or a short tally-light-only pulse.  The returned
        callback returns True only for the call that actually stopped Misty's
        recorder.
        """
        result = self.start_recording(RECORDING_FILENAME)
        if not result or result.get("status") != "Success":
            logger.warning(f"[Turn {turn}] {phase}: Misty recording did not start: {result}")
            return None

        started_at = time.time()
        stopped = False
        lock = threading.Lock()
        timer_holder: dict[str, threading.Timer | None] = {"timer": None}

        def stop_once() -> bool:
            nonlocal stopped
            with lock:
                if stopped:
                    return False
                stopped = True
                timer = timer_holder["timer"]

            if timer:
                timer.cancel()

            stop_result = self.stop_recording()
            duration = time.time() - started_at
            if stop_result and stop_result.get("status") == "Success":
                self._recording_cycles += 1
                logger.info(
                    f"[Turn {turn}] {phase}: Misty recording stopped after "
                    f"{duration:.1f}s (cycle {self._recording_cycles})"
                )
            else:
                logger.warning(f"[Turn {turn}] {phase}: Misty recording stop failed: {stop_result}")
            return True

        if max_duration_s is not None:
            if max_duration_s <= 0:
                stop_once()
            else:
                timer = threading.Timer(max_duration_s, stop_once)
                timer.daemon = True
                timer_holder["timer"] = timer
                timer.start()

        return stop_once

    def _start_configured_laptop_misty_recording(self, turn: int, phase: str):
        """Start Misty recording according to laptop-mode fallback/tally/off config."""
        if not self._laptop_misty_recording_enabled():
            logger.info(
                f"[Turn {turn}] {phase}: Misty recording disabled "
                f"(LAPTOP_MISTY_RECORDING_MODE=off)"
            )
            return None

        if self._laptop_misty_fallback_enabled():
            logger.info(f"[Turn {turn}] {phase}: Misty recording enabled for fallback audio")
            return self._start_misty_recording_window(turn, phase)

        tally_s = max(0.0, LAPTOP_MISTY_TALLY_RECORDING_S)
        logger.info(
            f"[Turn {turn}] {phase}: Misty recording enabled for tally light only "
            f"({tally_s:.1f}s; fallback disabled)"
        )
        return self._start_misty_recording_window(turn, phase, max_duration_s=tally_s)

    def _handle_laptop_capture_failure(
        self,
        turn: int,
        phase: str,
        misty_fallback_available: bool,
    ):
        """Return None to fall back to Misty audio, or raise a clear retryable error."""
        if self._laptop_misty_fallback_enabled() and misty_fallback_available:
            logger.warning(f"[Turn {turn}] {phase}: laptop mic empty, falling back to Misty mic")
            return None

        raise RuntimeError(
            f"{phase}: laptop mic capture returned no usable audio and Misty fallback "
            f"is disabled or unavailable (LAPTOP_MISTY_RECORDING_MODE="
            f"{LAPTOP_MISTY_RECORDING_MODE!r}). Check the laptop microphone or set "
            "LAPTOP_MISTY_RECORDING_MODE=fallback, then retry."
        )

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

    def _upload_greeting(self):
        """Generate TTS phrases and upload to Misty as named audio files."""
        phrases = {
            "greeting_whatsup.wav": "What's up baby?",
            "thinking.wav": "Let me think about that.",
        }
        for filename, text in phrases.items():
            try:
                response = requests.post(
                    f"{ORCHESTRATION_URL}/api/tts",
                    json={"text": text},
                    timeout=15.0,
                )
                if response.status_code != 200:
                    logger.warning(f"TTS for '{filename}' failed: HTTP {response.status_code}")
                    continue

                audio_data = response.content
                if len(audio_data) < 100:
                    logger.warning(f"TTS for '{filename}' too small: {len(audio_data)} bytes")
                    continue

                audio_b64 = base64.b64encode(audio_data).decode("ascii")
                self.misty_post("/api/audio", {
                    "FileName": filename,
                    "Data": audio_b64,
                    "ImmediatelyApply": False,
                    "OverwriteExisting": True,
                })
                logger.info(f"Uploaded '{filename}' to Misty ({len(audio_data)} bytes)")
            except Exception as e:
                logger.warning(f"Failed to upload '{filename}': {e}")

    def _get_misty_image_names(self) -> set[str]:
        """Return the set of image filenames currently stored on Misty.

        Best-effort: returns an empty set if the inventory cannot be read
        (e.g., device unreachable), which causes ensure_face_assets() to
        attempt uploads.
        """
        result = self.misty_get("/api/images")
        if not result:
            logger.warning(
                "Could not list Misty image inventory; required face assets will be "
                "checked by upload path"
            )
            return set()
        images = result.get("result", []) or []
        names: set[str] = set()
        for img in images:
            if isinstance(img, dict):
                name = img.get("name") or img.get("Name")
                if name:
                    names.add(name)
            elif isinstance(img, str):
                names.add(img)
        return names

    def _upload_face_image(self, filename: str, local_path: str) -> bool:
        """Upload a single image asset to Misty via SaveImage. Best-effort."""
        try:
            with open(local_path, "rb") as f:
                data = f.read()
            if len(data) < 100:
                logger.warning(f"Face asset '{filename}' too small: {len(data)} bytes")
                return False
            image_b64 = base64.b64encode(data).decode("ascii")
            result = self.misty_post("/api/images", {
                "FileName": filename,
                "Data": image_b64,
                "ImmediatelyApply": False,
                "OverwriteExisting": True,
            })
            if result is None:
                logger.warning(f"Face asset upload failed (no response): {filename}")
                return False
            logger.info(f"Uploaded face asset '{filename}' to Misty ({len(data)} bytes)")
            return True
        except Exception as e:
            logger.warning(f"Failed to upload face asset '{filename}': {e}")
            return False

    def ensure_face_assets(self) -> bool:
        """Idempotently ensure custom face assets are present on Misty (#110).

        In the default ``missing`` sync mode, assets already on the device
        (checked via the image inventory) are skipped and only missing ones are
        uploaded — safe to run every startup. In ``overwrite`` sync mode
        (``FACE_ASSETS_SYNC_MODE=overwrite`` or ``FACE_ASSETS_FORCE_UPLOAD=true``),
        every required asset is re-uploaded even if a file with the same name
        already exists, so a replacement face reusing the same filenames updates
        Misty's stored assets (#116).

        Informs the FaceAnimator whether all required custom faces are available;
        when some are unavailable the animator falls back to built-in firmware
        faces.

        Returns True if every required custom face is available on the device
        (already present or successfully uploaded); False otherwise.
        """
        overwrite = FACE_ASSETS_SYNC_MODE == "overwrite"
        inventory = set() if overwrite else self._get_misty_image_names()
        uploaded = 0
        skipped = 0
        all_available = True

        for filename in REQUIRED_FACE_ASSETS:
            if not overwrite and filename in inventory:
                skipped += 1
                continue
            local_path = os.path.join(FACE_ASSETS_DIR, filename)
            if not os.path.exists(local_path):
                logger.warning(
                    f"Custom face asset missing locally: {local_path} — "
                    "will rely on built-in fallback"
                )
                all_available = False
                continue
            if self._upload_face_image(filename, local_path):
                uploaded += 1
            else:
                all_available = False

        logger.info(
            f"Face assets [{FACE_ASSETS_SYNC_MODE}]: {skipped} already present, "
            f"{uploaded} uploaded, custom_available={all_available}"
        )

        if self._face_animator:
            self._face_animator.set_custom_faces_available(all_available)

        return all_available

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

    # --- Face recognition (#16) ---

    def get_trained_faces(self) -> list[str]:
        """Get list of trained face IDs from Misty."""
        result = self.misty_get("/api/faces")
        if result and result.get("status") == "Success":
            faces = result.get("result", [])
            self._trained_faces = faces
            return faces
        return []

    def start_face_training(self, face_id: str) -> bool:
        """Start training Misty to recognize a face.

        Stand in front of Misty and slowly turn your head left/right.
        Training takes ~15 seconds. The face_id is the label for this person.
        """
        if not face_id or len(face_id) > 50:
            logger.error(f"Invalid face_id: {face_id}")
            return False
        result = self.misty_post("/api/faces/training/start", {"FaceId": face_id})
        if result and result.get("status") == "Success":
            logger.info(f"Face training started for '{face_id}'")
            return True
        logger.error(f"Face training start failed for '{face_id}'")
        return False

    def cancel_face_training(self) -> bool:
        """Cancel an in-progress face training session."""
        result = self.misty_post("/api/faces/training/cancel")
        return result is not None

    def start_face_recognition(self) -> bool:
        """Start face recognition on Misty's camera.

        Returns True if recognition was started successfully.
        Face events arrive via WebSocket (FaceRecognition).
        """
        result = self.misty_post("/api/faces/recognition/start")
        if result and result.get("status") == "Success":
            logger.debug("Face recognition started")
            return True
        logger.warning("Face recognition start failed")
        return False

    def stop_face_recognition(self) -> bool:
        """Stop face recognition."""
        result = self.misty_post("/api/faces/recognition/stop")
        return result is not None

    def recognize_face_quick(self, timeout_s: float = None) -> str | None:
        """Run face recognition briefly and return the recognized name or None.

        Starts face recognition, waits for a FaceRecognition WebSocket event
        with a known (non-"unknown_person") label, then stops recognition.
        Returns the face ID string or None if no known face was detected in time.
        """
        if timeout_s is None:
            timeout_s = FACE_RECOGNITION_TIMEOUT_S

        # Need trained faces for this to be useful
        if not self._trained_faces:
            self.get_trained_faces()
        if not self._trained_faces:
            logger.debug("No trained faces — skipping recognition")
            return None

        self._recognized_face = None
        self._face_recognition_event.clear()

        if not self.start_face_recognition():
            return None

        try:
            # Wait for a face event (or timeout)
            got_event = self._face_recognition_event.wait(timeout=timeout_s)
            if got_event and self._recognized_face:
                logger.info(f"Face recognized: {self._recognized_face}")
                return self._recognized_face
            else:
                logger.debug(f"No face recognized in {timeout_s}s")
                return None
        finally:
            self.stop_face_recognition()

    # --- Laptop-side face recognition (#125) ------------------------------
    def _build_laptop_face_recognizer(self):
        """Lazily construct the laptop-side FaceRecognizer, or None on failure.

        Imports the recognition module lazily so the heavy/optional face
        dependencies are only touched when USE_LAPTOP_FACE_RECOGNITION is on.
        Caches the constructed recognizer; on a genuine construction error
        (import/params) it caches ``False`` so we do not retry (and spam logs)
        every turn. Profile presence is intentionally NOT checked here — it is
        re-evaluated per turn in ``recognize_face_laptop`` so a profile enrolled
        while the controller is running is picked up without a restart. Fail-open.
        """
        if self._laptop_face_recognizer is not None:
            return self._laptop_face_recognizer or None
        try:
            import face_recognition_service as frs

            store = frs.FaceProfileStore(FACE_PROFILE_DIR)
            embedder = frs.OnnxFaceEmbedder(
                detector_model_path=FACE_DETECTOR_MODEL_PATH,
                embedder_model_path=FACE_EMBEDDER_MODEL_PATH,
            )
            recognizer = frs.FaceRecognizer(
                store=store,
                embedder=embedder,
                threshold=FACE_RECOGNITION_THRESHOLD,
                min_samples=FACE_RECOGNITION_MIN_SAMPLES,
                min_consistent_frames=FACE_RECOGNITION_MIN_CONSISTENT_FRAMES,
            )
            self._laptop_face_recognizer = recognizer
            return recognizer
        except Exception as exc:
            logger.warning("[Face #125] Could not initialize laptop recognizer: %s", exc)
            self._laptop_face_recognizer = False
            return None

    def _build_laptop_frame_source(self):
        """Build the configured frame source for laptop recognition, or None."""
        try:
            import face_recognition_service as frs

            if FACE_RECOGNITION_SOURCE == "webcam":
                return frs.WebcamFrameSource(device_index=0)
            if FACE_RECOGNITION_SOURCE != "misty_camera":
                # Surface misconfiguration (typos, or the CLI-only "image_file")
                # instead of silently using the wrong source.
                logger.warning(
                    "[Face #125] Unsupported FACE_RECOGNITION_SOURCE=%r for live "
                    "conversation; supported values are 'misty_camera' and 'webcam'. "
                    "Falling back to Misty's camera.",
                    FACE_RECOGNITION_SOURCE,
                )
            return frs.MistyCameraFrameSource(MISTY_IP, timeout_s=FACE_RECOGNITION_TIMEOUT_S)
        except Exception as exc:
            logger.warning("[Face #125] Could not build frame source: %s", exc)
            return None

    def recognize_face_laptop(self) -> str | None:
        """Return a recognized speaker name using laptop-side recognition.

        Fully fail-open: any missing model, camera failure, unknown face, or
        low-confidence match returns None so the conversation continues without
        a name. Never raises.
        """
        recognizer = self._build_laptop_face_recognizer()
        if recognizer is None:
            return None
        try:
            import face_recognition_service as frs

            # Refresh profiles each turn so a profile enrolled while the
            # controller is running is picked up without a restart.
            if not recognizer.load_profiles(force=True):
                logger.debug(
                    "[Face #125] No enrolled profiles in %s — skipping (enroll with "
                    "tools/enroll_face.py).",
                    FACE_PROFILE_DIR,
                )
                return None
            source = self._build_laptop_frame_source()
            if source is None:
                return None
            with source:
                name = frs.recognize_speaker(recognizer, source)
            if name:
                logger.info("[Face #125] Laptop recognition identified: %s", name)
            return name
        except Exception as exc:
            logger.debug("[Face #125] Laptop recognition failed (continuing): %s", exc)
            return None

    def _handle_face_recognition_event(self, data: dict):
        """Handle a FaceRecognition WebSocket event."""
        label = data.get("label", data.get("Label", ""))
        # Misty reports "unknown_person" for unrecognized faces
        if label and label != "unknown_person":
            self._recognized_face = label
            self._face_recognition_event.set()
            logger.info(f"FaceRecognition event: {label}")
        else:
            logger.debug(f"FaceRecognition event: unrecognized ({label})")

    def _ws_subscribe_face_recognition(self):
        """Subscribe to FaceRecognition WebSocket events."""
        if self.ws:
            prev = getattr(self, '_face_event_name', None)
            self._face_event_name = f"FaceRec_{time.time_ns()}"
            msg = json.dumps({
                "Operation": "subscribe",
                "Type": "FaceRecognition",
                "DebounceMs": 0,
                "EventName": self._face_event_name,
                "ReturnProperty": None,
                "EventConditions": [],
            })
            self.ws.send(msg)
            logger.info(f"Subscribed to FaceRecognition (name={self._face_event_name})")

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
            # If moving, preempt first
            if self.get_state() == State.MOVING:
                self.preempt_movement("battery_critical")
            if self.try_set_state(State.IDLE, State.CHARGING):
                logger.warning(f"Battery critically low ({b.charge_percent*100:.0f}%) — entering charging mode")
                self._apply_charging_mode()
            return

        # Movement-specific: halt if battery drops below movement threshold (#52)
        if self.get_state() == State.MOVING:
            if b.charge_percent < BATTERY_MOVEMENT_CUTOFF:
                logger.warning(f"Battery below movement cutoff during MOVING "
                               f"({b.charge_percent*100:.0f}% < {BATTERY_MOVEMENT_CUTOFF*100:.0f}%)")
                self.preempt_movement("battery_critical")
                return
            if b.voltage > 0 and b.voltage < BATTERY_MOVEMENT_VOLTAGE_MIN:
                logger.warning(f"Voltage below movement minimum during MOVING "
                               f"({b.voltage:.1f}V < {BATTERY_MOVEMENT_VOLTAGE_MIN}V)")
                self.preempt_movement("battery_critical")
                return

        # Voltage drop detection: halt movement if voltage sags rapidly (#52)
        if self.get_state() == State.MOVING and b.voltage > 0:
            last_voltage = getattr(self, '_last_battery_voltage', 0.0)
            if last_voltage > 0 and (last_voltage - b.voltage) > BATTERY_VOLTAGE_DROP_HALT:
                logger.warning(f"Rapid voltage drop detected during movement: "
                               f"{last_voltage:.2f}V → {b.voltage:.2f}V (drop={last_voltage - b.voltage:.2f}V)")
                self.preempt_movement("battery_critical")
        if b.voltage > 0:
            self._last_battery_voltage = b.voltage

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

    # --- Hazard / Sensor Telemetry Event Handlers (#49) ---

    def _handle_hazard_event(self, data: dict):
        """Handle HazardNotification — firmware-level safety alert.

        The firmware auto-stops motors on hazard, but we need to update our state
        and log the event for safety decisions.
        """
        now = time.time()
        # Extract hazard details from event data
        # HazardNotification contains arrays of sensors that triggered
        bump_hazards = data.get("bumpSensorsHazardState", [])
        tof_hazards = data.get("timeOfFlightSensorsHazardState", [])

        hazards = []
        for sensor in bump_hazards:
            if sensor.get("inHazard", False):
                hazards.append({"type": "bump", "sensor": sensor.get("sensorName", "unknown")})
        for sensor in tof_hazards:
            if sensor.get("inHazard", False):
                hazards.append({
                    "type": "tof",
                    "sensor": sensor.get("sensorName", "unknown"),
                    "distance_mm": sensor.get("distance", 0),
                })

        with self.hazard_lock:
            self.hazard.active_hazards = hazards
            self.hazard.last_hazard_time = now
            if hazards:
                self.hazard.hazard_halt_issued = True

        if hazards:
            logger.warning(f"HAZARD: {len(hazards)} active — {hazards}")
            # Preempt movement if in MOVING state (firmware already halted motors,
            # but we need to update our state machine)
            if self.get_state() == State.MOVING:
                self.preempt_movement("hazard_stop")
        else:
            # Hazard cleared
            with self.hazard_lock:
                self.hazard.hazard_halt_issued = False
            logger.info("Hazard cleared — all sensors nominal")

    def _handle_tof_event(self, data: dict):
        """Handle TimeOfFlight — raw distance reading from a single sensor.

        Updates per-sensor state. Only logs significant changes (DEBUG level for routine).
        """
        sensor_id = data.get("sensorId", "").lower()
        if sensor_id not in TOF_SENSORS:
            return

        distance_mm = data.get("distanceInMeters", 0) * 1000  # API returns meters
        status = data.get("status", 0)
        # Status 0 = valid ranging, 2 = ranging complete. Status >= 100 = reduced confidence.
        is_valid = status in (0, 2)

        now = time.time()
        with self.hazard_lock:
            reading = self.hazard.tof_readings[sensor_id]
            old_valid = reading.is_valid
            old_distance = reading.distance_mm

            reading.distance_mm = distance_mm
            reading.status = status
            reading.is_valid = is_valid
            reading.last_updated = now

        # Log only transitions: sensor going invalid, or close obstacles
        if is_valid and not old_valid:
            logger.debug(f"ToF {sensor_id} recovered (status={status}, dist={distance_mm:.0f}mm)")
        elif not is_valid and old_valid:
            logger.info(f"ToF {sensor_id} degraded (status={status})")
        elif is_valid and distance_mm < 200 and (old_distance >= 200 or not old_valid):
            logger.info(f"ToF {sensor_id} close obstacle: {distance_mm:.0f}mm")

    def _handle_bump_event(self, data: dict):
        """Handle BumpSensor — physical contact detection.

        On bump contact: log and set state. Firmware auto-halts on bump.
        """
        sensor_name = data.get("sensorName", "unknown")
        is_pressed = data.get("isContacted", False)
        now = time.time()

        with self.hazard_lock:
            self.hazard.bump_states[sensor_name] = {
                "is_pressed": is_pressed,
                "last_updated": now,
            }
            self.hazard.last_bump_time = now
            self.hazard.any_bump_active = any(
                s.get("is_pressed", False)
                for s in self.hazard.bump_states.values()
            )

        if is_pressed:
            logger.warning(f"BUMP: {sensor_name} contacted")
            # Preempt movement if in MOVING state
            if self.get_state() == State.MOVING:
                self.preempt_movement("bump_contact")
        else:
            logger.info(f"Bump released: {sensor_name}")

    def get_hazard_snapshot(self) -> dict:
        """Thread-safe snapshot of current hazard/sensor state for decision-making."""
        with self.hazard_lock:
            return {
                "active_hazards": list(self.hazard.active_hazards),
                "last_hazard_time": self.hazard.last_hazard_time,
                "hazard_halt_issued": self.hazard.hazard_halt_issued,
                "any_bump_active": self.hazard.any_bump_active,
                "last_bump_time": self.hazard.last_bump_time,
                "tof_readings": {
                    sid: {
                        "distance_mm": r.distance_mm,
                        "status": r.status,
                        "is_valid": r.is_valid,
                        "last_updated": r.last_updated,
                        "friendly_name": TOF_SENSORS[sid],
                    }
                    for sid, r in self.hazard.tof_readings.items()
                },
                "bump_states": dict(self.hazard.bump_states),
            }

    def check_forward_clear(self, min_distance_mm: float = 200.0) -> bool:
        """Check if forward path is clear based on ToF readings.

        Returns True if all valid forward sensors report distance > min_distance_mm.
        Returns False if any forward sensor reports close obstacle or is stale/invalid.
        """
        now = time.time()
        with self.hazard_lock:
            for sid in TOF_FORWARD_SENSORS:
                reading = self.hazard.tof_readings[sid]
                # Stale data = not clear (fail closed)
                if now - reading.last_updated > TELEMETRY_STALE_TIMEOUT_S:
                    return False
                # Invalid reading = not clear (fail closed)
                if not reading.is_valid:
                    return False
                # Only check horizontal sensors (not edge/downward) for distance
                if sid in ("toffc", "toffr", "toffl"):
                    if reading.distance_mm < min_distance_mm:
                        return False
            return True

    def check_reverse_clear(self, min_distance_mm: float = 200.0) -> bool:
        """Check if reverse path is clear based on rear ToF readings."""
        now = time.time()
        with self.hazard_lock:
            for sid in TOF_REVERSE_SENSORS:
                reading = self.hazard.tof_readings[sid]
                if now - reading.last_updated > TELEMETRY_STALE_TIMEOUT_S:
                    return False
                if not reading.is_valid:
                    return False
                if sid == "tofr":
                    if reading.distance_mm < min_distance_mm:
                        return False
            return True

    def check_sensors_fresh(self, sensor_ids: set = None) -> bool:
        """Check if specified sensors have fresh data (within TELEMETRY_STALE_TIMEOUT_S).

        Args:
            sensor_ids: Set of sensor IDs to check. If None, checks all sensors.
        """
        now = time.time()
        check_ids = sensor_ids or set(TOF_SENSORS.keys())
        with self.hazard_lock:
            for sid in check_ids:
                reading = self.hazard.tof_readings.get(sid)
                if not reading or now - reading.last_updated > TELEMETRY_STALE_TIMEOUT_S:
                    return False
            return True

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
        self.show_face("face_idle.gif")
        logger.info("Charging mode active — keyphrase off, LED off, display sleeping")

    def exit_charging_mode(self):
        """Resume normal operation from charging mode."""
        # In laptop wake word mode, resume the listener instead of Misty's keyphrase
        if self._wake_word_listener:
            self._wake_word_listener.resume()
            self.set_led(0, 255, 0)
            self.show_face("face_idle.gif")
            self.last_activity_time = time.time()
            self._is_dimmed = False
            self.set_state(State.IDLE)
            logger.info("Exited charging mode — resumed laptop wake word listener")
        elif self.start_keyphrase(force_restart=True):
            self.set_led(0, 255, 0)
            self.show_face("face_idle.gif")
            self.last_activity_time = time.time()
            self._is_dimmed = False
            self.set_state(State.IDLE)
            logger.info("Exited charging mode — resumed Misty keyphrase")
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
        # Stop face animator first (§6.3 — before existing cleanup sequence)
        if getattr(self, "_expression_coordinator", None):
            self._expression_coordinator.cancel()  # halt any in-flight gesture (#74)
        if self._talking_head:
            self._talking_head.stop()  # halt + re-center head motion (#116)
        if self._face_animator:
            self._face_animator.stop()
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

    # --- Hazard / Safety Telemetry Subscriptions (#49) ---

    def _ws_subscribe_hazard(self):
        """Subscribe to HazardNotification — firmware-level safety alerts."""
        if self.ws:
            prev = getattr(self, '_hazard_event_name', None)
            self._hazard_event_name = f"Hazard_{time.time_ns()}"

            for old_name in ["Hazard", prev or '']:
                if old_name:
                    unsub = json.dumps({"Operation": "unsubscribe", "EventName": old_name})
                    self.ws.send(unsub)
            time.sleep(0.2)

            msg = json.dumps({
                "Operation": "subscribe",
                "Type": "HazardNotification",
                "DebounceMs": 0,  # safety-critical: every event matters
                "EventName": self._hazard_event_name,
                "ReturnProperty": None,
                "EventConditions": [],
            })
            self.ws.send(msg)
            logger.info(f"Subscribed to HazardNotification (name={self._hazard_event_name})")

    def _ws_subscribe_tof(self):
        """Subscribe to TimeOfFlight — raw distance readings from 8 sensors."""
        if self.ws:
            prev = getattr(self, '_tof_event_name', None)
            self._tof_event_name = f"ToF_{time.time_ns()}"

            for old_name in ["ToF", prev or '']:
                if old_name:
                    unsub = json.dumps({"Operation": "unsubscribe", "EventName": old_name})
                    self.ws.send(unsub)
            time.sleep(0.2)

            msg = json.dumps({
                "Operation": "subscribe",
                "Type": "TimeOfFlight",
                "DebounceMs": 250,  # 4 Hz per sensor — advisory, not primary safety
                "EventName": self._tof_event_name,
                "ReturnProperty": None,
                "EventConditions": [],
            })
            self.ws.send(msg)
            logger.info(f"Subscribed to TimeOfFlight (name={self._tof_event_name})")

    def _ws_subscribe_bump(self):
        """Subscribe to BumpSensor — physical contact detection."""
        if self.ws:
            prev = getattr(self, '_bump_event_name', None)
            self._bump_event_name = f"Bump_{time.time_ns()}"

            for old_name in ["Bump", prev or '']:
                if old_name:
                    unsub = json.dumps({"Operation": "unsubscribe", "EventName": old_name})
                    self.ws.send(unsub)
            time.sleep(0.2)

            msg = json.dumps({
                "Operation": "subscribe",
                "Type": "BumpSensor",
                "DebounceMs": 0,  # safety-critical: immediate notification
                "EventName": self._bump_event_name,
                "ReturnProperty": None,
                "EventConditions": [],
            })
            self.ws.send(msg)
            logger.info(f"Subscribed to BumpSensor (name={self._bump_event_name})")

    def _ws_subscribe_safety_telemetry(self):
        """Subscribe to all safety-related sensor events."""
        self._ws_subscribe_hazard()
        self._ws_subscribe_tof()
        self._ws_subscribe_bump()

    def _on_ws_open(self, ws):
        logger.info("WebSocket connected")
        self.reconnect_attempts = 0
        # Always subscribe to battery and safety telemetry events
        self._ws_subscribe_battery()
        self._ws_subscribe_safety_telemetry()

        # Subscribe to face recognition events if enabled (#16)
        if USE_FACE_RECOGNITION:
            self._ws_subscribe_face_recognition()
            # Cache trained faces list on connect
            self.get_trained_faces()
            if self._trained_faces:
                logger.info(f"Trained faces: {self._trained_faces}")
            else:
                logger.info("No trained faces found — face recognition won't identify anyone")

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
            self.show_face("face_idle.gif")
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
            self.show_face("face_idle.gif")
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

        # Ignore registration status messages
        if isinstance(msg_content, str) and "Registration Status" in msg_content:
            logger.debug(f"WS registration: {msg_content}")
            return

        # Route high-frequency telemetry events FIRST (no per-message INFO log)
        if event_name == getattr(self, '_tof_event_name', ''):
            if isinstance(msg_content, dict):
                self._handle_tof_event(msg_content)
            return

        if event_name == getattr(self, '_hazard_event_name', ''):
            if isinstance(msg_content, dict):
                self._handle_hazard_event(msg_content)
            return

        if event_name == getattr(self, '_bump_event_name', ''):
            if isinstance(msg_content, dict):
                self._handle_bump_event(msg_content)
            return

        if event_name == getattr(self, '_face_event_name', ''):
            if isinstance(msg_content, dict):
                self._handle_face_recognition_event(msg_content)
            return

        # Log non-telemetry WebSocket messages for debugging (#22)
        if event_name:
            msg_preview = str(msg_content)[:200] if msg_content else "(empty)"
            logger.info(f"WS event: {event_name} | msg_type={type(msg_content).__name__} | msg={msg_preview}")
        else:
            logger.info(f"WS raw: {str(message)[:300]}")

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
                self.show_face("face_idle.gif")
                logger.info("Restored from idle-dim on wake word")

            if self.get_state() == State.IDLE and time.time() >= self.ready_time:
                logger.info("[Wake] Wake word detected!")
                self.turn_id += 1
                threading.Thread(
                    target=self._handle_conversation_turn,
                    name=f"turn-{self.turn_id}",
                    daemon=True,
                ).start()
            elif self.get_state() == State.MOVING:
                # Wake word during movement — stop moving, then start conversation
                logger.info("[Wake] Wake word during movement — preempting to converse")
                self.preempt_movement("wake_word")
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

    def _ws_is_connected(self) -> bool:

        """Return True when the current Misty WebSocket still appears usable."""

        sock = getattr(self.ws, "sock", None)

        thread_alive = bool(self.ws_thread and self.ws_thread.is_alive())

        return bool(self.ws and sock and getattr(sock, "connected", False) and thread_alive)



    # --- Laptop wake word listener (issue #44) ---

    def _get_wake_word_status(self) -> dict:
        """Return the active wake-word configuration for logs and status endpoints."""
        if self._wake_word_listener:
            health = self._wake_word_listener.get_health()
            return {
                "source": "laptop_openwakeword",
                "model_name": health.get("model"),
                "model_path": health.get("custom_model_path"),
                "threshold": health.get("threshold"),
                "active": bool(health.get("running")),
                "error": None,
            }

        return {
            "source": self._wake_word_source,
            "model_name": self._wake_word_model_name,
            "model_path": self._wake_word_model_path,
            "threshold": self._wake_word_threshold,
            "active": False,
            "error": self._wake_word_config_error,
        }

    def _start_laptop_wake_word(self):
        """Initialize and start the laptop-based wake word listener."""
        self._wake_word_source = "laptop_openwakeword"
        self._wake_word_model_name = None
        self._wake_word_model_path = None
        self._wake_word_threshold = None
        self._wake_word_config_error = None

        try:
            from wake_word_listener import (
                OWW_CUSTOM_MODEL_PATH,
                OWW_MODEL_NAME,
                OWW_THRESHOLD,
                WakeWordListener,
            )

            self._wake_word_listener = WakeWordListener(
                on_wake_word=self._on_laptop_wake_word,
                model_name=OWW_MODEL_NAME,
                threshold=OWW_THRESHOLD,
                custom_model_path=OWW_CUSTOM_MODEL_PATH,
            )
            if self._wake_word_listener.start():
                health = self._wake_word_listener.get_health()
                self._wake_word_source = "laptop_openwakeword"
                self._wake_word_model_name = health.get("model")
                self._wake_word_model_path = health.get("custom_model_path")
                self._wake_word_threshold = health.get("threshold")
                logger.info(
                    "Laptop wake word listener active "
                    f"(model={self._wake_word_model_name}, path={self._wake_word_model_path or 'default'}, "
                    f"threshold={self._wake_word_threshold})"
                )
            else:
                self._wake_word_listener = None
                self._wake_word_source = "error"
                self._wake_word_config_error = (
                    "Laptop wake-word startup failed. Misty built-in keyphrase is unsupported; "
                    "check the laptop wake-word dependencies, microphone access, and OWW_CUSTOM_MODEL_PATH."
                )
                raise RuntimeError(self._wake_word_config_error)
        except ImportError as e:
            self._wake_word_listener = None
            self._wake_word_source = "error"
            self._wake_word_config_error = (
                f"Laptop wake word dependencies are unavailable ({e}). Install sounddevice and openwakeword, "
                "then retry with a configured OWW_CUSTOM_MODEL_PATH."
            )
            raise RuntimeError(self._wake_word_config_error) from e
        except RuntimeError:
            raise
        except Exception as e:
            self._wake_word_listener = None
            self._wake_word_source = "error"
            self._wake_word_config_error = (
                f"Laptop wake-word startup failed: {e}. Misty built-in keyphrase is unsupported; "
                "check the laptop microphone and OWW_CUSTOM_MODEL_PATH."
            )
            raise RuntimeError(self._wake_word_config_error) from e

    def _on_laptop_wake_word(self):
        """Callback fired by laptop wake word listener on detection."""
        self.last_activity_time = time.time()
        self._last_wake_event_time = time.time()
        self._watchdog_recovery_level = 0

        if self._is_dimmed and self.get_state() == State.IDLE:
            self._is_dimmed = False
            self.set_led(0, 255, 0)
            self.show_face("face_idle.gif")
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
            exchange_result = self._do_conversation_exchange(turn, turn_start)

            # Determine if speech was detected and if movement was requested
            if isinstance(exchange_result, dict):
                # Movement response (#56) — speech was detected, movement command returned
                had_speech = exchange_result.get("had_speech", True)
                movement = exchange_result.get("movement")
            else:
                had_speech = exchange_result
                movement = None

            # Only count cycles with actual speech for proactive reboot tracking.
            # Empty STT (user too far from Misty's mic) shouldn't trigger a reboot.
            if had_speech:
                self._conversation_cycles += 1
                logger.info(f"[Turn {turn}] Conversation cycle {self._conversation_cycles}/{PROACTIVE_REBOOT_AFTER_CYCLES}")
            else:
                logger.info(f"[Turn {turn}] No speech — not counting toward reboot cycles")
                return

            # Execute voice movement if requested (#56) — after ack audio has played
            if movement:
                logger.info(f"[Turn {turn}] Executing voice movement: {movement.get('command')}")
                self._execute_voice_movement(turn, movement)
                # After movement, skip follow-up listening — re-arm and wait for next wake word
                return

            if not FOLLOWUP_ENABLED:
                logger.info(f"[Turn {turn}] Follow-up listening disabled — re-arming wake word")
                return

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
                if isinstance(had_speech, dict):
                    # Follow-up was a movement command (#56) — execute and end conversation
                    movement = had_speech.get("movement")
                    if movement:
                        logger.info(f"[Turn {turn}] Follow-up movement: {movement.get('command')}")
                        self._execute_voice_movement(turn, movement)
                    break
                if not had_speech:
                    logger.info(f"[Turn {turn}] No follow-up speech — ending conversation")
                    break

        except Exception as e:
            logger.error(f"[Turn {turn}] Error: {e}", exc_info=True)
            self.set_led(255, 0, 0)  # red = error
            self.show_face("face_talking_sad.gif")
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
        self.show_face("face_listening.png")  # wide-eyed, attentive
        self.move_head(pitch=-10, roll=0, yaw=0, velocity=60)  # look up slightly — eye contact

        # Start face recognition concurrently with recording.
        face_result = [None]  # mutable container for thread result
        if USE_LAPTOP_FACE_RECOGNITION:
            # Laptop-side recognition (#125) — preferred; replaces Misty's
            # unreliable on-chip pipeline. Fail-open inside recognize_face_laptop.
            def _face_check():
                face_result[0] = self.recognize_face_laptop()
            face_thread = threading.Thread(target=_face_check, daemon=True)
            face_thread.start()
        elif USE_FACE_RECOGNITION and self._trained_faces:
            # Deprecated Misty-native path (#16).
            def _face_check():
                face_result[0] = self.recognize_face_quick()
            face_thread = threading.Thread(target=_face_check, daemon=True)
            face_thread.start()
        else:
            face_thread = None

        # Stop keyphrase before recording — shared mic on Snapdragon 410.
        # In Misty-keyphrase mode, keyphrase auto-stops on detection.
        # In laptop wake word mode, keyphrase is NOT running (we don't start it),
        # so just do a belt-and-suspenders cleanup with minimal delay.
        if self._wake_word_listener:
            logger.info(f"[Turn {turn}] Clearing mic before recording (laptop wake word mode)")
            self.misty_post("/api/audio/record/stop")  # belt-and-suspenders cleanup
            time.sleep(0.5)  # minimal delay — no keyphrase to release

        # Play "What's up baby?" greeting via pre-uploaded TTS audio.
        # Falls back to chime if greeting audio isn't available.
        # Note: misty_post returns None on failure (doesn't raise), so check return value.
        greeting_result = self.misty_post("/api/audio/play", {"FileName": "greeting_whatsup.wav", "Volume": 40})
        if greeting_result:
            time.sleep(1.2)  # let the greeting play before recording starts
        else:
            logger.debug(f"[Turn {turn}] Greeting playback failed, trying chime")
            chime_result = self.misty_post("/api/audio/play", {"FileName": "s_Awe3.wav", "Volume": 30})
            if chime_result:
                time.sleep(0.8)
            else:
                logger.debug(f"[Turn {turn}] Chime fallback also failed; continuing without audio cue")

        # 2. Record audio — bright green LED + tally light = "I'm listening, speak now!"
        self.set_led(0, 255, 0)  # green = recording active, speak now
        
        # Start laptop mic recording (primary audio source for STT)
        use_laptop_mic = self._wake_word_listener and self._wake_word_listener.is_running
        if use_laptop_mic:
            self._wake_word_listener.start_recording()

        # In laptop mode, Misty's recorder is configurable: full fallback audio
        # (default), a short tally-light-only pulse, or fully disabled.
        misty_recording_stop = None
        misty_fallback_available = False
        if use_laptop_mic:
            misty_recording_stop = self._start_configured_laptop_misty_recording(
                turn,
                "initial recording",
            )
            misty_fallback_available = (
                self._laptop_misty_fallback_enabled() and misty_recording_stop is not None
            )
        else:
            self.start_recording(RECORDING_FILENAME)
            misty_fallback_available = True

        record_start = time.time()
        
        if use_laptop_mic:
            # Dynamic recording: laptop mic monitors speech and signals when to stop.
            # Misty fallback mode records for at least RECORDING_DURATION_S.
            # Tally-only mode may stop Misty's recorder earlier via timer.
            speech_ended = threading.Event()
            self._wake_word_listener.start_speech_monitor(
                on_speech_end=lambda: speech_ended.set(),
                min_duration=RECORDING_DURATION_S,  # at least the standard duration
                max_duration=15.0,
            )
            speech_ended.wait(timeout=15.0)
            self._wake_word_listener.stop_speech_monitor()
            # Ensure Misty has recorded at least the standard duration when it
            # is acting as fallback audio.
            elapsed = time.time() - record_start
            if self._laptop_misty_fallback_enabled() and elapsed < RECORDING_DURATION_S:
                time.sleep(RECORDING_DURATION_S - elapsed)
        else:
            # Fallback: fixed duration recording
            time.sleep(RECORDING_DURATION_S)

        if misty_recording_stop:
            misty_recording_stop()
        elif not use_laptop_mic:
            self.stop_recording()
            self._recording_cycles += 1
        record_duration = time.time() - record_start
        
        # Get audio from laptop mic (preferred) or fall back to Misty's mic
        if use_laptop_mic:
            laptop_audio = self._wake_word_listener.stop_recording()
            if len(laptop_audio) > 100:
                logger.info(f"[Turn {turn}] Using LAPTOP mic: {len(laptop_audio)} bytes, {record_duration:.1f}s")
                audio_bytes = laptop_audio
            else:
                audio_bytes = self._handle_laptop_capture_failure(
                    turn,
                    "initial recording",
                    misty_fallback_available,
                )
        else:
            audio_bytes = None

        logger.info(f"[Turn {turn}] Recorded {record_duration:.1f}s (cycle {self._recording_cycles})")

        # Small delay for Misty to finalize the file
        time.sleep(0.5)

        # 3. Retrieve recorded audio — wondering face + thinking sound
        self.set_state(State.PROCESSING)
        self.set_led(0, 0, 255)  # blue = processing
        self.show_face("face_processing.gif")  # one eyebrow raised — "hmm, let me think..."
        self.move_head(pitch=-5, roll=5, yaw=20, velocity=40)  # tilt head — pondering

        # Play thinking phrase so the user knows Misty heard them
        try:
            self.misty_post("/api/audio/play", {"FileName": "thinking.wav", "Volume": 40})
        except Exception as e:
            logger.debug(f"[Turn {turn}] Thinking sound failed: {e}")

        # Fall back to Misty mic if laptop mic wasn't used or was empty
        if audio_bytes is None:
            audio_b64 = self.get_audio_base64(RECORDING_FILENAME)
            if not audio_b64:
                raise RuntimeError("Failed to retrieve recorded audio from Misty")
            audio_bytes = base64.b64decode(audio_b64)
            logger.info(f"[Turn {turn}] Using MISTY mic: {len(audio_bytes)} bytes")

        if len(audio_bytes) < FOLLOWUP_SILENCE_THRESHOLD:
            raise RuntimeError(f"Recording too small ({len(audio_bytes)} bytes) — likely empty")

        # Collect face recognition result (#16) — was running concurrently with recording
        speaker_name = None
        if face_thread is not None:
            face_thread.join(timeout=1.0)  # don't block long — recording is done
            speaker_name = face_result[0]
            if speaker_name:
                logger.info(f"[Turn {turn}] Recognized face: {speaker_name}")
                self._recognized_face = speaker_name  # persist for follow-up turns

        # 4-6. Orchestrate and play response
        return self._do_orchestrate_and_respond(turn, audio_bytes, turn_start, speaker_name=self._recognized_face)

    def _do_orchestrate_and_respond(self, turn: int, audio_bytes: bytes, turn_start: float, speaker_name: str | None = None):
        """Send audio to orchestration service and play response on Misty.
        
        Args:
            turn: Turn ID for logging.
            audio_bytes: WAV audio bytes to transcribe.
            turn_start: Timestamp of when the turn started.
            speaker_name: Optional recognized face name (#16).

        Returns:
            dict — if orchestration returned a movement command.
                   Keys: "movement" (command dict), "had_speech" (True).
            True  — if speech was detected and a conversational response was played.
            False — if no speech was detected (empty STT).
        """
        # Processing state already set by caller — just send to orchestration

        # Send to orchestration service (include speaker_name from face recognition if available)
        # Request inline audio bytes to avoid a second GET round trip (#69)
        form_data = {"return_audio_bytes": "true"}
        if speaker_name:
            form_data["speaker_name"] = speaker_name
        response = requests.post(
            f"{ORCHESTRATION_URL}/api/orchestrate",
            files={"file": (RECORDING_FILENAME, audio_bytes, "audio/wav")},
            data=form_data,
            timeout=60.0,
        )
        result = response.json()

        # Handle empty STT gracefully — not an error, just no speech detected.
        # Only treat as silence if the error is specifically empty_stt (not other 400s
        # like stt_failure or no_file which indicate real problems).
        if result.get("error") == "empty_stt":
            logger.info(f"[Turn {turn}] No speech detected in recording (empty STT) — treating as silence")
            return False

        if response.status_code != 200:
            raise RuntimeError(f"Orchestration HTTP {response.status_code}: {result.get('error', 'unknown')}")

        if result.get("status") != "ok":
            raise RuntimeError(f"Orchestration error: {result.get('error', 'unknown')}")

        # --- Movement response (#56) ---
        if result.get("type") == "movement":
            movement = result.get("movement", {})
            ack_text = result.get("response_text", "")
            pipeline_ms = result.get("pipeline_ms", 0)
            user_text = result.get("user_text", "")
            logger.info(f"[Turn {turn}] MOVEMENT: '{user_text}' -> {movement.get('command')} "
                         f"ack='{ack_text}' ({pipeline_ms}ms)")

            # Retrieve and play acknowledgment audio (speak first, then move)
            # Prefer inline bytes (#69); fall back to GET if not provided.
            audio_file = result.get("audio_file")
            if audio_file:
                response_wav = None
                audio_bytes_b64 = result.get("audioBytes")
                if audio_bytes_b64:
                    try:
                        response_wav = base64.b64decode(audio_bytes_b64)
                        logger.info(f"[Turn {turn}] Inline movement ack audio: {len(response_wav)} bytes")
                    except Exception as e:
                        logger.warning(f"[Turn {turn}] Failed to decode inline movement ack audio: {e}")
                if response_wav is None:
                    audio_url = f"{ORCHESTRATION_URL}/api/audio/{audio_file}"
                    try:
                        audio_resp = requests.get(audio_url, timeout=10.0)
                        audio_resp.raise_for_status()
                        response_wav = audio_resp.content
                        logger.info(f"[Turn {turn}] Downloaded movement ack audio: {len(response_wav)} bytes")
                    except Exception as e:
                        logger.warning(f"[Turn {turn}] Movement ack audio failed: {e}")
                if response_wav:
                    try:
                        self.set_state(State.PLAYING)
                        self.set_led(148, 0, 211)  # purple = speaking
                        self.show_face("face_talking_happy.gif")
                        self._talking_head.start("happy")  # talking motion (#116)
                        play_duration = self.upload_and_play_audio(response_wav, RESPONSE_FILENAME)
                        time.sleep(play_duration + 1.0)
                        self._talking_head.stop()  # halt + re-center head (#116)
                    except Exception as e:
                        logger.warning(f"[Turn {turn}] Movement ack playback failed: {e}")

            return {"movement": movement, "had_speech": True}

        # --- Normal conversational response ---
        transcribed = result.get("transcribedText", "")
        llm_response = result.get("inferenceResponse", "")
        audio_uri = result.get("responseAudio", "")
        latency = result.get("latencyMs", 0)
        logger.info(f"[Turn {turn}] User: '{transcribed}' -> Misty: '{llm_response}' ({latency:.0f}ms)")

        if result.get("ttsFallback"):
            logger.warning(f"[Turn {turn}] WARNING: TTS FALLBACK was used")

        # Retrieve response audio — prefer inline bytes (#69), fall back to GET
        if not audio_uri and not result.get("audioBytes"):
            raise RuntimeError("No response audio URI")

        response_wav = None
        audio_bytes_b64 = result.get("audioBytes")
        if audio_bytes_b64:
            try:
                response_wav = base64.b64decode(audio_bytes_b64, validate=True)

                logger.info(f"[Turn {turn}] Inline response audio: {len(response_wav)} bytes")
            except Exception as e:
                logger.warning(f"[Turn {turn}] Failed to decode inline audio, falling back to GET: {e}")
        if response_wav is None:
            if not audio_uri:
                raise RuntimeError("No response audio URI and inline audio decode failed")
            audio_url = f"{ORCHESTRATION_URL}{audio_uri}"
            audio_resp = requests.get(audio_url, timeout=10.0)
            audio_resp.raise_for_status()
            response_wav = audio_resp.content
            logger.info(f"[Turn {turn}] Downloaded response audio: {len(response_wav)} bytes")

        # Upload to Misty and play — animated, looking at user
        emotion = result.get("emotion", "neutral")
        # Route emotion through the always-available animator so it selects the
        # matching talking face (and applies built-in fallback when custom assets
        # are unavailable) — works whether or not the frame-loop is enabled (#116).
        self._face_animator.set_emotion(emotion)
        self.set_state(State.PLAYING)
        self.set_led(148, 0, 211)  # purple = speaking
        self.move_head(pitch=-10, roll=0, yaw=0, velocity=60)  # face forward — eye contact
        self._express_for_response_emotion(emotion)
        self._talking_head.start(emotion)  # gentle emotion-aware motion while speaking (#116)

        play_duration = self.upload_and_play_audio(response_wav, RESPONSE_FILENAME)

        # Wait for playback to finish (generous buffer — no completion callback from Misty)
        time.sleep(play_duration + 2.0)
        self._talking_head.stop()  # halt + re-center head when playback ends (#116)
        if getattr(self, "_expression_coordinator", None):
            self._expression_coordinator.cancel()  # lower arms/re-center after response gesture

        elapsed = time.time() - turn_start
        logger.info(f"[Turn {turn}] Exchange complete in {elapsed:.1f}s")
        return True

    def _listen_for_followup(self, turn: int) -> bool:
        """Listen briefly for follow-up speech. Returns True if speech was detected and responded to."""
        self.set_state(State.LISTENING)
        self.set_led(0, 200, 200)  # cyan = listening for follow-up
        self.show_face("face_listening.png")  # warm, expectant — "go on..."
        self.move_head(pitch=-10, roll=-3, yaw=-10, velocity=40)  # slight head tilt — attentive

        # Record a short clip — use VAD if available
        use_laptop_mic = self._wake_word_listener and self._wake_word_listener.is_running
        if use_laptop_mic:
            self._wake_word_listener.start_recording()

        misty_recording_stop = None
        if use_laptop_mic:
            misty_recording_stop = self._start_configured_laptop_misty_recording(
                turn,
                "follow-up recording",
            )
        else:
            self.start_recording(RECORDING_FILENAME)

        record_start = time.time()
        if use_laptop_mic:
            speech_ended = threading.Event()
            self._wake_word_listener.start_speech_monitor(
                on_speech_end=lambda: speech_ended.set(),
                min_duration=2.0,   # shorter min for follow-ups
                max_duration=10.0,  # shorter max for follow-ups
            )
            speech_ended.wait(timeout=10.0)
            self._wake_word_listener.stop_speech_monitor()
            # Ensure Misty records at least FOLLOWUP_LISTEN_S when it is acting
            # as fallback audio. Tally-only mode may stop earlier via timer.
            elapsed = time.time() - record_start
            if self._laptop_misty_fallback_enabled() and elapsed < FOLLOWUP_LISTEN_S:
                time.sleep(FOLLOWUP_LISTEN_S - elapsed)
        else:
            time.sleep(FOLLOWUP_LISTEN_S)

        if misty_recording_stop:
            misty_recording_stop()
        elif not use_laptop_mic:
            self.stop_recording()
            self._recording_cycles += 1
        time.sleep(0.5)  # finalize

        # Get audio from laptop mic (preferred) or Misty
        if use_laptop_mic:
            laptop_audio = self._wake_word_listener.stop_recording()
            if len(laptop_audio) > 100:
                logger.info(f"[Turn {turn}] Follow-up using LAPTOP mic: {len(laptop_audio)} bytes")
                audio_bytes = laptop_audio
            else:
                audio_bytes = self._handle_laptop_capture_failure(
                    turn,
                    "follow-up recording",
                    misty_fallback_available,
                )
        else:
            audio_bytes = None

        if audio_bytes is None:
            audio_b64 = self.get_audio_base64(RECORDING_FILENAME)
            if not audio_b64:
                logger.warning(f"[Turn {turn}] Follow-up: failed to retrieve audio")
                return False
            audio_bytes = base64.b64decode(audio_b64)
            logger.info(f"[Turn {turn}] Follow-up using MISTY mic: {len(audio_bytes)} bytes")

        # Very small recordings are certainly silence
        if len(audio_bytes) < FOLLOWUP_SILENCE_THRESHOLD:
            return False

        # Show thinking face while processing follow-up
        self.set_state(State.PROCESSING)
        self.set_led(0, 0, 255)  # blue = processing
        self.show_face("face_processing.gif")  # wondering face
        self.move_head(pitch=-5, roll=5, yaw=20, velocity=40)

        # Send through the full pipeline — orchestration returns empty_stt error
        # if no speech was detected, which we treat as silence
        try:
            form_data = {}
            if self._recognized_face:
                form_data["speaker_name"] = self._recognized_face
            response = requests.post(
                f"{ORCHESTRATION_URL}/api/orchestrate",
                files={"file": (RECORDING_FILENAME, audio_bytes, "audio/wav")},
                data=form_data,
                timeout=60.0,
            )
            result = response.json()

            # empty_stt = silence, not an error
            if result.get("error") == "empty_stt":
                logger.info(f"[Turn {turn}] Follow-up: silence (empty STT)")
                return False

            if response.status_code != 200 or result.get("status") != "ok":
                logger.warning(f"[Turn {turn}] Follow-up: orchestration error: {result.get('error', 'unknown')}")
                return False

            # Movement response in follow-up (#56)
            if result.get("type") == "movement":
                movement = result.get("movement", {})
                ack_text = result.get("response_text", "")
                logger.info(f"[Turn {turn}] Follow-up movement: {movement.get('command')} ack='{ack_text}'")

                audio_file = result.get("audio_file")
                if audio_file:
                    try:
                        audio_url = f"{ORCHESTRATION_URL}/api/audio/{audio_file}"
                        audio_resp = requests.get(audio_url, timeout=10.0)
                        audio_resp.raise_for_status()
                        self.set_state(State.PLAYING)
                        self.set_led(148, 0, 211)
                        self.show_face("face_talking_happy.gif")  # consistent ack face (#116)
                        self._talking_head.start("happy")  # talking motion (#116)
                        play_duration = self.upload_and_play_audio(audio_resp.content, RESPONSE_FILENAME)
                        time.sleep(play_duration + 1.0)
                        self._talking_head.stop()  # halt + re-center head (#116)
                    except Exception as e:
                        logger.warning(f"[Turn {turn}] Follow-up movement ack audio failed: {e}")

                return {"movement": movement, "had_speech": True}

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
            emotion = result.get("emotion", "neutral")
            self._face_animator.set_emotion(emotion)
            self.set_state(State.PLAYING)
            self.set_led(148, 0, 211)  # purple = speaking
            self.move_head(pitch=-10, roll=0, yaw=0, velocity=60)  # face forward
            self._talking_head.start(emotion)  # emotion-aware talking motion (#116)

            play_duration = self.upload_and_play_audio(response_wav, RESPONSE_FILENAME)
            time.sleep(play_duration + 0.5)
            self._talking_head.stop()  # halt + re-center head when playback ends (#116)
            return True

        except Exception as e:
            logger.error(f"[Turn {turn}] Follow-up error: {e}")
            return False

    def _rearm(self):
        self.set_state(State.REARMING)
        self.move_head(pitch=0, roll=0, yaw=0, velocity=40)  # center head
        self._recognized_face = None  # clear face context between conversations (#16)

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
            # Laptop mode — no keyphrase to restart, shorter cooldown needed.

            # Keep the WebSocket open on the normal path; the laptop listener

            # owns wake detection and the existing subscriptions remain valid.

            logger.info("Fast re-arm: audio cleanup (2s) — laptop wake word mode")

            time.sleep(2.0)

            if self._ws_is_connected():

                self.set_led(0, 255, 0)

                self.show_face("face_idle.gif")

                self.last_activity_time = time.time()

                self.set_state(State.IDLE)

                self._wake_word_listener.resume()

                logger.info("Fast re-arm complete — laptop wake word resumed; WebSocket kept open")

                return

            logger.warning(

                "Fast re-arm unavailable — WebSocket disconnected/unhealthy; "

                "falling back to full reconnect"

            )

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
        self.show_face("face_idle.gif")  # calm face
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
            logger.warning(f"Proactive reboot: announcement failed: {e}")

            f"{'laptop_openwakeword (required)' if USE_LAPTOP_WAKE_WORD else 'unsupported (USE_LAPTOP_WAKE_WORD=false)'}"

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
        logger.info(
            f"  Laptop Misty recording: {LAPTOP_MISTY_RECORDING_MODE} "
            f"(tally={max(0.0, LAPTOP_MISTY_TALLY_RECORDING_S):.1f}s)"
        )
        logger.info(f"  Idle timeout:  {IDLE_TIMEOUT_S}s")
        logger.info(f"  Watchdog:      soft={WATCHDOG_IDLE_TIMEOUT_S}s, escalate={WATCHDOG_ESCALATE_TIMEOUT_S}s")
        wake_status = self._get_wake_word_status()
        logger.info(
            "  Wake word:     "
            f"source={wake_status['source']} model={wake_status['model_name'] or 'unconfigured'} "
            f"path={wake_status['model_path'] or 'n/a'} threshold={wake_status['threshold'] or 'n/a'}"
        )
        logger.info(
            f"  Follow-up:     {'enabled' if FOLLOWUP_ENABLED else 'disabled'} "
            f"({FOLLOWUP_TIMEOUT_S}s window, max {FOLLOWUP_MAX_TURNS} turns)"
        )
        logger.info(f"  Proactive reboot: every {PROACTIVE_REBOOT_AFTER_CYCLES} conversations or {PROACTIVE_REBOOT_AFTER_RECORDINGS} recordings")
        logger.info("=" * 60)

        if not USE_LAPTOP_WAKE_WORD:
            raise RuntimeError(
                "USE_LAPTOP_WAKE_WORD=false is not supported. Misty built-in keyphrase is unsupported; "
                "set USE_LAPTOP_WAKE_WORD=true and configure laptop wake-word dependencies."
            )

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

        # Upload "What's up baby?" greeting to Misty via orchestration TTS
        if orch_ok:
            self._upload_greeting()

        # Ensure custom face assets are on Misty (idempotent). If some are
        # unavailable, the animator falls back to built-in firmware faces.
        self.ensure_face_assets()

        # Cancel any lingering skills (e.g., built-in faceDetection)
        self.misty_post("/api/skills/cancel")

        # Start laptop wake word listener before opening the WebSocket so the
        # first _on_ws_open() sees laptop-wake-word mode and does not subscribe
        # to or start Misty keyphrase recognition.
        if USE_LAPTOP_WAKE_WORD:
            self._start_laptop_wake_word()

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
            wake_status = ctrl._get_wake_word_status()
            self._send_json(200, {
                "state": ctrl.get_state().name,
                "turn_id": ctrl.turn_id,
                "conversation_cycles": ctrl._conversation_cycles,
                "proactive_reboot_at": PROACTIVE_REBOOT_AFTER_CYCLES,
                "laptop_wake_word": USE_LAPTOP_WAKE_WORD,
                "wake_source": wake_status["source"],
                "wake_model_name": wake_status["model_name"],
                "wake_model_path": wake_status["model_path"],
                "wake_threshold": wake_status["threshold"],
                "wake_error": wake_status["error"],
                "laptop_misty_recording_mode": LAPTOP_MISTY_RECORDING_MODE,
                "laptop_misty_tally_recording_s": max(0.0, LAPTOP_MISTY_TALLY_RECORDING_S),
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

        elif self.path == "/api/move":
            # Teleop movement endpoint (#51) — strict parameter validation
            ctrl = self.controller
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(content_len)) if content_len > 0 else {}
            except (json.JSONDecodeError, ValueError):
                self._send_json(400, {"error": "invalid_json", "message": "Request body must be valid JSON"})
                return

            command = body.get("command")
            valid_commands = ("forward", "backward", "rotate_left", "rotate_right", "halt")
            if command not in valid_commands:
                self._send_json(400, {
                    "error": "invalid_command",
                    "message": f"command must be one of: {valid_commands}",
                    "received": command,
                })
                return

            # Handle halt immediately (no state transition needed)
            if command == "halt":
                ctrl.halt()
                self._send_json(200, {"status": "ok", "command": "halt", "message": "Emergency halt issued"})
                return

            # Validate parameters
            distance_mm = body.get("distance_mm", 200)
            speed_pct = body.get("speed_pct", 20)
            angle_deg = body.get("angle_deg", 90)

            # Strict bounds
            if not isinstance(distance_mm, (int, float)) or distance_mm < 50 or distance_mm > 500:
                self._send_json(400, {
                    "error": "invalid_distance",
                    "message": "distance_mm must be 50-500",
                    "received": distance_mm,
                })
                return
            if not isinstance(speed_pct, (int, float)) or speed_pct < 5 or speed_pct > 30:
                self._send_json(400, {
                    "error": "invalid_speed",
                    "message": "speed_pct must be 5-30",
                    "received": speed_pct,
                })
                return
            if command in ("rotate_left", "rotate_right"):
                if not isinstance(angle_deg, (int, float)) or angle_deg < 10 or angle_deg > 180:
                    self._send_json(400, {
                        "error": "invalid_angle",
                        "message": "angle_deg must be 10-180",
                        "received": angle_deg,
                    })
                    return

            # Attempt to enter MOVING state
            if not ctrl.start_moving(reason=f"teleop_{command}"):
                state = ctrl.get_state()
                self._send_json(409, {
                    "error": "cannot_move",
                    "state": state.name,
                    "message": "Cannot start moving — check state, hazards, or battery",
                })
                return

            # Execute movement command in background thread
            def _execute_teleop():
                try:
                    if command in ("forward", "backward"):
                        # Calculate duration from distance and speed
                        # At speed_pct=30, max velocity ~135mm/s (30% of 450mm/s)
                        velocity_mms = (speed_pct / 100.0) * 450.0
                        duration_ms = int((distance_mm / velocity_mms) * 1000)
                        duration_ms = max(100, min(ctrl.DRIVE_MAX_DURATION_MS, duration_ms))
                        linear = speed_pct if command == "forward" else -speed_pct
                        ctrl.drive_time(linear, 0, duration_ms)
                        time.sleep(duration_ms / 1000.0 + 0.2)  # wait for completion + buffer
                    elif command in ("rotate_left", "rotate_right"):
                        # Rotate in place — angular velocity only
                        # Approximate: at 20% angular, ~30 deg/s → duration = angle/30 * 1000ms
                        angular_rate = (speed_pct / 100.0) * 150.0  # approx deg/s
                        duration_ms = int((angle_deg / angular_rate) * 1000)
                        duration_ms = max(100, min(ctrl.DRIVE_MAX_DURATION_MS, duration_ms))
                        angular = speed_pct if command == "rotate_left" else -speed_pct
                        ctrl.drive_time(0, angular, duration_ms)
                        time.sleep(duration_ms / 1000.0 + 0.2)
                except Exception as e:
                    logger.error(f"Teleop execution error: {e}")
                finally:
                    if ctrl.get_state() == State.MOVING:
                        ctrl.stop_moving(reason="move_complete")

            threading.Thread(target=_execute_teleop, name="teleop-exec", daemon=True).start()

            self._send_json(200, {
                "status": "ok",
                "command": command,
                "message": f"Movement started: {command}",
            })

        elif self.path == "/api/sensors":
            # Sensor telemetry snapshot (#49)
            ctrl = self.controller
            snapshot = ctrl.get_hazard_snapshot()
            self._send_json(200, {"status": "ok", **snapshot})

        elif self.path == "/api/shutdown":
            ctrl = self.controller
            logger.info("[Test API] Shutdown requested")
            self._send_json(200, {"status": "ok", "message": "Controller shutdown requested"})

            def _shutdown_after_response():
                time.sleep(0.2)
                ctrl._shutdown()
                os._exit(0)

            threading.Thread(
                target=_shutdown_after_response,
                name="api-shutdown",
                daemon=True,
            ).start()

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

    try:
        controller.start()
    except RuntimeError as exc:
        logger.error(str(exc))
        sys.exit(1)

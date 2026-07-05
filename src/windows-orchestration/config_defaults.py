"""
Canonical default values for all environment-configurable settings.

This module is the **single source of truth** for every default used in
``orchestration_service.py``, ``misty_controller.py``, ``.env.example``,
and project documentation.

Maintenance rule
----------------
When you add or change a default:
1. Update the constant here.
2. The two service modules read from this module at import time, so code
   changes are automatic.
3. Manually update ``.env.example`` (comment lines) and
   ``docs/IMPLEMENTATION_GUIDE.md`` to keep prose in sync.

Do **not** hard-code the same value in another file — always import from here.
"""

import os

# ---------------------------------------------------------------------------
# Orchestration service (orchestration_service.py)
# ---------------------------------------------------------------------------

# Foundry Local
FOUNDRY_API_TIMEOUT: float = 10.0
SERVICE_TIMEOUT: float = 15.0

# TTS (Kokoro-ONNX)
KOKORO_VOICE: str = "af_sky"
KOKORO_SPEED: float = 1.2
TTS_CACHE_MAX: int = 200

# LLM prompt-length limits
MAX_USER_CHARS: int = 400
MAX_CONTEXT_CHARS: int = 5000

# ---------------------------------------------------------------------------
# Misty controller (misty_controller.py)
# ---------------------------------------------------------------------------

# Robot connection
MISTY_IP: str = "10.0.0.23"
ORCHESTRATION_URL: str = "http://localhost:5000"

# Audio recording
RECORDING_DURATION_S: float = 6.0

# Follow-up conversation
FOLLOWUP_LISTEN_S: float = 5.0
FOLLOWUP_TIMEOUT_S: float = 90.0
FOLLOWUP_MAX_TURNS: int = 12

# Watchdog / keyphrase recovery
WATCHDOG_IDLE_TIMEOUT_S: float = 90.0
WATCHDOG_ESCALATE_TIMEOUT_S: float = 60.0

# Idle timeout
IDLE_TIMEOUT_S: float = 900.0

# Proactive reboot (after extended use — hardware health maintenance)
PROACTIVE_REBOOT_AFTER_CYCLES: int = 5
PROACTIVE_REBOOT_AFTER_RECORDINGS: int = 15

# Laptop mic recording mode during conversations (issue #44)
LAPTOP_MISTY_RECORDING_MODE: str = "fallback"
LAPTOP_MISTY_TALLY_RECORDING_S: float = 1.0

# Face recognition (issue #16)
FACE_RECOGNITION_TIMEOUT_S: float = 3.0

# Face animation (issue #73, Phase 2)
USE_FACE_ANIMATION: bool = False
FACE_ANIMATION_MAX_FPS: float = 4.0
FACE_ANIMATION_MIN_INTERVAL_S: float = 0.25

# Custom face assets uploaded to Misty at startup (issue #110).
# FACE_ASSETS_DIR is env-configurable (FACE_ASSETS_DIR); it defaults to the
# repo-level assets/ directory (config_defaults.py lives in
# src/windows-orchestration/, so ../../assets is the repo root assets dir).
FACE_ASSETS_DIR: str = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "assets")
)
# Manifest of custom face assets that must exist on the device (not tunable).
REQUIRED_FACE_ASSETS: tuple = (
    "face_idle.gif",
    "face_listening.png",
    "face_processing.gif",
    "face_talking_neutral.gif",
    "face_talking_happy.gif",
    "face_talking_excited.gif",
    "face_talking_sad.gif",
    "face_talking_curious.gif",
)

# Face asset replacement / sync behavior (issue #116).
#   "missing"   = idempotent startup — only upload required assets that are not
#                 already present on the device (default; safe to run every boot).
#   "overwrite" = force re-upload of every required asset even if a file with the
#                 same name already exists on Misty. Use this to replace the
#                 custom face with a new set that reuses the same filenames, then
#                 return to "missing" for normal idempotent startup.
# The FACE_ASSETS_FORCE_UPLOAD=true environment flag is a convenience alias that
# forces "overwrite" mode for a single run.
FACE_ASSETS_SYNC_MODE: str = "missing"

# Emotion-aware subtle talking head motion (issue #116). Off by default and
# gated by config. When enabled, Misty makes small, safe head movements while
# speaking (state PLAYING only) and re-centers when playback ends or the state
# leaves PLAYING. Never runs during MOVING/CHARGING/ERROR/shutdown or drive
# commands, and always stays within the safe head limits below.
USE_TALKING_HEAD_MOTION: bool = False
# Safe head-motion envelope for talking motion (degrees). These stay well inside
# Misty's mechanical limits (pitch -40..26, roll -40..40, yaw -81..81).
TALKING_HEAD_PITCH_CENTER: float = -10.0   # slight up-tilt = eye contact
TALKING_HEAD_PITCH_RANGE: float = 4.0      # +/- pitch wobble
TALKING_HEAD_YAW_RANGE: float = 6.0        # +/- yaw wobble
TALKING_HEAD_ROLL_RANGE: float = 3.0       # +/- roll wobble
TALKING_HEAD_VELOCITY: float = 30.0        # gentle move velocity
TALKING_HEAD_INTERVAL_S: float = 0.8       # seconds between micro-movements

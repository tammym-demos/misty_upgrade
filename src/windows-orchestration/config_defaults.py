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
CHAT_MODEL_ID: str = "Phi-3.5-mini-instruct-generic-cpu:2"

# TTS (Kokoro-ONNX)
KOKORO_VOICE: str = "af_sky"
KOKORO_SPEED: float = 1.2
TTS_CACHE_MAX: int = 200

# STT (faster-whisper)
STT_DEVICE: str = "cpu"
STT_COMPUTE_TYPE: str = "int8"
STT_MIN_RMS: float = 0.0005
STT_MIN_PEAK: float = 0.005
STT_MIN_AVG_LOGPROB: float = -1.0
STT_MAX_NO_SPEECH_PROB: float = 0.6
STT_BEAM_SIZE: int = 1

# LLM prompt-length limits
MAX_USER_CHARS: int = 400
MAX_CONTEXT_CHARS: int = 5000
MAX_CONTEXT_TOKENS: int = 1200

# ---------------------------------------------------------------------------
# Misty controller (misty_controller.py)
# ---------------------------------------------------------------------------

# Robot connection
MISTY_IP: str = "10.0.0.23"
ORCHESTRATION_URL: str = "http://localhost:5000"

# Audio recording
RECORDING_DURATION_S: float = 1.25

# Follow-up conversation
FOLLOWUP_ENABLED: bool = True
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

# Laptop OpenWakeWord wake phrase (issue #72).
# The repository includes the trained "Hey Misty" model, so startup can use it
# by default while still allowing OWW_CUSTOM_MODEL_PATH to override it.
OWW_CUSTOM_MODEL_PATH: str = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "models", "hey_misty.onnx")
)
OWW_MODEL_NAME: str = "hey_misty"
OWW_THRESHOLD: float = 0.85
# Require the wake model to cross threshold on consecutive audio frames before
# firing. This suppresses single-frame background false positives.
OWW_TRIGGER_FRAMES: int = 2
WAKE_WORD_MIN_RMS: int = 100
LAPTOP_MIC_DEVICE: str = ""

# Face recognition (issue #16)
FACE_RECOGNITION_TIMEOUT_S: float = 3.0

# Laptop-side face recognition (issue #125).
# Replaces Misty's unreliable on-chip /api/faces pipeline (documented as
# effectively non-functional on this Snapdragon 410 unit in
# docs/lessons-learned.md) with a laptop-side recognizer that produces a
# speaker_name for the existing orchestration path. Off by default; enable only
# after enrolling a profile (see tools/enroll_face.py). Keep the deprecated
# USE_FACE_RECOGNITION (#16) path disabled when this is enabled.
USE_LAPTOP_FACE_RECOGNITION: bool = False
# Directory holding enrolled face profiles (embeddings + metadata only, never
# photos). Gitignored. Defaults to the repo-level data/face_profiles directory
# (config_defaults.py lives in src/windows-orchestration/, so ../../data/... is
# the repo root).
FACE_PROFILE_DIR: str = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "face_profiles")
)
# Frame source used for recognition during conversations: "misty_camera" or
# "webcam". (The enrollment/recognition CLIs additionally support an "image"
# source for offline use; the live controller uses the robot camera or webcam.)
FACE_RECOGNITION_SOURCE: str = "misty_camera"
# Cosine-distance match threshold (lower = stricter). A probe matches a profile
# only when its distance to the profile centroid is <= this value. Conservative
# defaults reduce false positives (a wrong name is worse than no name).
FACE_RECOGNITION_THRESHOLD: float = 0.4
# Minimum number of frames that must agree on the same name before recognition
# returns it (single-frame false-positive guard).
FACE_RECOGNITION_MIN_CONSISTENT_FRAMES: int = 2
# Minimum number of valid face samples required to enroll a profile.
FACE_RECOGNITION_MIN_SAMPLES: int = 5
# Optional paths to the OpenCV/ONNX face detector and embedding models used by
# OnnxFaceEmbedder. Empty by default; laptop recognition raises a clear
# FaceModelUnavailable error until these are set. Models are not bundled in git
# (*.onnx is gitignored) — see the README for how to obtain them.
FACE_DETECTOR_MODEL_PATH: str = os.environ.get("FACE_DETECTOR_MODEL_PATH", "")
FACE_EMBEDDER_MODEL_PATH: str = os.environ.get("FACE_EMBEDDER_MODEL_PATH", "")

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

# Embodied expression coordinator (issue #74). Off by default and gated by
# config until hardware validation passes. When enabled, an optional
# companion-side coordinator maps constrained expression intents (joy, curious,
# confused, thinking, sassy, annoyed, angry, sad, startled, sleepy, error) to
# bounded face/LED/head/arm choreography. Face rendering is delegated to #73's
# FaceAnimator with a static built-in-face fallback; choreography is cancellable,
# non-blocking and sensor rate-limited. Motor head/arm gestures are safety-gated
# (suppressed during MOVING/CHARGING/ERROR/reboot/re-arm/recording/listening,
# shutdown, and movement preemption); non-motor face/LED cues may still apply in
# those states. It never issues drive/tread commands.
USE_EMBODIED_EXPRESSIONS: bool = False
# Gentle move velocities for expression gestures (percent).
EXPRESSION_HEAD_VELOCITY: float = 40.0
EXPRESSION_ARM_VELOCITY: float = 40.0
# Minimum seconds between repeats of the same sensor-triggered expression
# (rate-limit guard against sensor spam from bump/ToF/hazard streams).
EXPRESSION_SENSOR_MIN_INTERVAL_S: float = 3.0

# Conference Mode for scripted on-stage dialog (issue #128). Off by default and
# gated by config. When enabled, a companion-side conference runner plays
# predetermined Misty WAV cues parsed from a talk script (e.g.
# talks/20260710-2.md) instead of routing scripted lines through the live
# STT -> LLM -> TTS conversation path. Normal wake-word conversation behavior is
# unchanged whenever this flag is off. Runtime never invokes the LLM for a
# scripted Misty cue; optional fallback only re-synthesizes known scripted text.
CONFERENCE_MODE_ENABLED: bool = False
# Default talk script parsed into ordered presenter/Misty cue pairs.
CONFERENCE_SCRIPT_PATH: str = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "talks", "20260710-2.md")
)
# Directory holding the prepared companion-side cue cache and manifest. Defaults
# to the repo-level data/conference directory (config_defaults.py lives in
# src/windows-orchestration/, so ../../data/conference is the repo root path).
CONFERENCE_ASSET_DIR: str = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "conference")
)
# Manifest filename written under CONFERENCE_ASSET_DIR mapping cue IDs to text,
# asset source, local WAV path, duration, and optional Misty filename.
CONFERENCE_MANIFEST_NAME: str = "conference_manifest.json"
# Prefix for the on-Misty audio filename when a cue is pre-uploaded to the robot.
CONFERENCE_MISTY_FILENAME_PREFIX: str = "conf_"
# Auto-advance: listen while the presenter speaks and play the next predetermined
# cue once the presenter finishes speaking. Manual override controls remain
# available at all times regardless of this setting.
CONFERENCE_AUTO_ADVANCE: bool = True
# Seconds of trailing presenter silence that mark end-of-speech for auto-advance.
CONFERENCE_PRESENTER_SILENCE_S: float = 2.5
# Maximum seconds to wait for the presenter to finish before an auto-advance
# attempt gives up and yields to manual control (stage-safety timeout).
CONFERENCE_PRESENTER_MAX_WAIT_S: float = 45.0
# Explicit fallback to live TTS for a scripted cue whose predetermined audio is
# missing. Off by default so showtime uses prepared audio unless enabled.
CONFERENCE_TTS_FALLBACK: bool = False
# Variable substitution for talk scripts (comma-separated key=value pairs).
# Example: CONFERENCE_VARS=customer=Contoso,event=Hackathon
CONFERENCE_VARS: str = ""
# Side where the presenter stands relative to Misty ("left" or "right").
# Used for glance-at-presenter behavior between cues.
CONFERENCE_PRESENTER_SIDE: str = "right"
# Default talking face shown during speech when no [face:...] annotation overrides.
CONFERENCE_TALKING_FACE: str = "face_talking_happy.gif"

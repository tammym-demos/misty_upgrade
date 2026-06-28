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
MISTY_IP: str = "10.0.0.44"
ORCHESTRATION_URL: str = "http://10.0.0.58:5000"

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

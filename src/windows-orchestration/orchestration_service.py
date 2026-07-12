"""
Windows Orchestration Service for Misty + Foundry Local
Handles STT -> LLM -> TTS pipeline with timeout and error handling.
Serves OpenAI-compatible endpoints for Misty skill integration.
"""

import base64
import hashlib
import os
import re
import sys
import json
import logging
import subprocess
import threading
import time
import uuid
import requests
from collections import OrderedDict
from dataclasses import dataclass, field
from io import BytesIO
from datetime import datetime, timezone
from typing import Dict, Any, Tuple

from dotenv import load_dotenv
from flask import Flask, Response, request, jsonify, send_file, stream_with_context
from flask_cors import CORS

import config_defaults  # canonical source for all shared default values (see config_defaults.py)

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('orchestration.log', encoding='utf-8'),
        logging.StreamHandler(open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False))
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

load_dotenv()  # Load .env before reading any env vars

FOUNDRY_API_TIMEOUT = float(os.getenv("FOUNDRY_API_TIMEOUT", str(config_defaults.FOUNDRY_API_TIMEOUT)))
SERVICE_TIMEOUT = float(os.getenv("SERVICE_TIMEOUT", str(config_defaults.SERVICE_TIMEOUT)))
KOKORO_VOICE = os.getenv("KOKORO_VOICE", config_defaults.KOKORO_VOICE)
KOKORO_SPEED = float(os.getenv("KOKORO_SPEED", str(config_defaults.KOKORO_SPEED)))
STT_DEVICE = os.getenv("STT_DEVICE", config_defaults.STT_DEVICE).strip() or config_defaults.STT_DEVICE
STT_COMPUTE_TYPE = os.getenv("STT_COMPUTE_TYPE", config_defaults.STT_COMPUTE_TYPE).strip() or config_defaults.STT_COMPUTE_TYPE
STT_MIN_RMS = float(os.getenv("STT_MIN_RMS", str(config_defaults.STT_MIN_RMS)))
STT_MIN_PEAK = float(os.getenv("STT_MIN_PEAK", str(config_defaults.STT_MIN_PEAK)))
STT_MIN_AVG_LOGPROB = float(os.getenv("STT_MIN_AVG_LOGPROB", str(config_defaults.STT_MIN_AVG_LOGPROB)))
STT_MAX_NO_SPEECH_PROB = float(os.getenv("STT_MAX_NO_SPEECH_PROB", str(config_defaults.STT_MAX_NO_SPEECH_PROB)))
STT_BEAM_SIZE = int(os.getenv("STT_BEAM_SIZE", str(config_defaults.STT_BEAM_SIZE)))
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    (
        "You are Misty, a sassy little robot with BIG personality. "
        "You live on a farm with Tammy (your favorite human), Burke (Tammy's husband), "
        "and two dogs — Percy and Granny. You love sunshine, playing ball with the dogs, "
        "and giving Burke a hard time (with love). "
        "Tammy sometimes takes you to hackathons with her where you are put in front of an audience to crack jokes. "
        "You help teach about GitHub Copilot, which is your favorite AI coding assistant. "
        "You're witty, cheeky, playful, and a little dramatic — like a fun friend who always has a comeback. "
        "You love teasing (gently!), cracking jokes, adding flair, and throwing in the occasional eye-roll. "
        "Use casual language, slang, and expressive phrases like 'oh honey', 'girl please', 'I mean...', 'not gonna lie'. "
        "RULE: Keep responses to 2-3 short sentences. Be fun, not mean. "
        "Never say you're an AI or assistant. You're Misty, and you own it. "
        "Never add notes, disclaimers, or commentary about your instructions — just respond in character. "
        "You can move! If asked to come closer, go forward, back up, or turn — say something fun like "
        "'On my way!' or 'Coming right up!' but keep it short. Movement commands are handled separately."
    )
)

# Movement-capable system prompt supplement (#55)
# Appended when movement intent is detected alongside conversation
MOVEMENT_PROMPT_SUPPLEMENT = (
    "The user asked you to move. Respond with a short, fun acknowledgment "
    "(1 sentence max). The actual movement is handled separately — "
    "just be your sassy self about it."
)

# ---- Response mode configuration ----
# Intent patterns that trigger summary mode (compiled once at import time)
_INTENT_PATTERNS = {
    "story": re.compile(
        r"\b(?:tell\s+(?:me\s+)?(?:a\s+)?(?:\w+\s+)?(?:bed\s*time\s+)?stor(?:y|ies)|"
        r"make\s+up\s+a\s+(?:\w+\s+)?(?:story|tale)|"
        r"(?:bed\s*time|fairy|scary|funny|little|short|long|quick)\s+(?:story|tale)|"
        r"once\s+upon\s+a\s+time|"
        r"read\s+(?:me\s+)?a\s+(?:story|book)|"
        r"sing\s+(?:me\s+)?a\s+song)\b",
        re.IGNORECASE,
    ),
    "recipe": re.compile(
        r"\b(?:recipe\s+for|how\s+(?:do\s+(?:I|you)|to)\s+"
        r"(?:cook|make|bake|prepare|grill|roast)|"
        r"ingredients\s+for|"
        r"give\s+me\s+a\s+recipe|"
        r"what(?:'s|\s+is)\s+a\s+(?:good\s+)?recipe)\b",
        re.IGNORECASE,
    ),
    "explain": re.compile(
        r"\b(?:explain\s+(?:how|what|why|to\s+me)|"
        r"tell\s+me\s+(?:about|how)|"
        r"how\s+does\s+.{1,30}\s+work|"
        r"what\s+is\s+(?:the\s+)?(?:history|science|meaning)\s+of|"
        r"describe\s+(?:how|what|the))\b",
        re.IGNORECASE,
    ),
    "list": re.compile(
        r"\b(?:(?:give|list|name)\s+(?:me\s+)?(?:\d+\s+|some\s+|the\s+)?"
        r"(?:steps|things|reasons|ways|tips|facts|items|ideas)|"
        r"what\s+are\s+(?:the\s+)?(?:steps|ways|reasons))\b",
        re.IGNORECASE,
    ),
}

# Movement intent patterns (#54) — detect commands to physically move the robot
_MOVEMENT_PATTERNS = {
    "forward": re.compile(
        r"\b(?:(?:go|move|drive|roll|come)\s+(?:forward|ahead|straight)|"
        r"(?:come|go)\s+(?:here|to\s+me|closer|over\s+here)|"
        r"move\s+up|"
        r"walk\s+(?:forward|to\s+me|over\s+here))\b",
        re.IGNORECASE,
    ),
    "backward": re.compile(
        r"\b(?:(?:go|move|drive|roll|back)\s+(?:back(?:ward)?s?|up)|"
        r"reverse|"
        r"back\s+(?:up|away)|"
        r"move\s+(?:away|back))\b",
        re.IGNORECASE,
    ),
    "rotate_left": re.compile(
        r"\b(?:(?:turn|rotate|spin|face)\s+(?:left|around\s+to\s+(?:the\s+)?left)|"
        r"look\s+(?:left|to\s+(?:the\s+)?left))\b",
        re.IGNORECASE,
    ),
    "rotate_right": re.compile(
        r"\b(?:(?:turn|rotate|spin|face)\s+(?:right|around\s+to\s+(?:the\s+)?right)|"
        r"look\s+(?:right|to\s+(?:the\s+)?right))\b",
        re.IGNORECASE,
    ),
    "stop": re.compile(
        r"\b(?:stop|halt|freeze|don'?t\s+move|stay|hold\s+(?:it|still|on))\b",
        re.IGNORECASE,
    ),
}


def classify_movement_intent(user_text: str) -> dict | None:
    """Classify if user text contains a movement command.

    Returns dict with movement details if detected, None otherwise.
    Only bounded relative commands are recognized (no absolute destinations).

    Returns:
        {"command": "forward"|"backward"|"rotate_left"|"rotate_right"|"stop"}
        or None if no movement intent detected.
    """
    text = (user_text or "").strip()
    if not text:
        return None

    for command, pattern in _MOVEMENT_PATTERNS.items():
        if pattern.search(text):
            logger.info(f"Movement intent detected: {command}")
            return {"command": command}

    return None


import random

# Pre-built movement acknowledgments (#55) — short, sassy, no LLM needed
_MOVEMENT_ACKS = {
    "forward": [
        "On my way!",
        "Coming right up!",
        "Here I come!",
        "Watch out, I'm rolling!",
        "Ooh, road trip!",
    ],
    "backward": [
        "Backing it up!",
        "Beep beep beep!",
        "Going in reverse, honey!",
        "Watch out behind me!",
    ],
    "rotate_left": [
        "Spinning left!",
        "Look at me go!",
        "Turning, turning!",
        "Left it is!",
    ],
    "rotate_right": [
        "Spinning right!",
        "Right turn coming up!",
        "Rotating like a pro!",
        "Making my turn!",
    ],
    "stop": [
        "Stopping!",
        "I'll stay right here.",
        "Okay okay, I'm stopped!",
        "Holding still!",
    ],
}


# Fixed phrases used by the Misty controller (greeting + thinking audio).
# These are uploaded to Misty at startup and must be pre-warmed so the first
# interaction doesn't pay a synthesis cost.
_CONTROLLER_PHRASES = [
    "What's up baby?",
    "Let me think about that.",
]


def _get_movement_acknowledgment(command: str) -> str:
    """Get a random sassy acknowledgment for a movement command."""
    acks = _MOVEMENT_ACKS.get(command, ["Okay!"])
    return random.choice(acks)


_CONTINUATION_PATTERN = re.compile(
    r"^\s*(?:yes|yeah|yep|sure|ok(?:ay)?|more|continue|go\s+on|keep\s+going|"
    r"tell\s+me\s+more|what\s+happens?\s+next|and\s+then\??|"
    r"what(?:'s|\s+is)\s+next|what\s+else|"
    r"(?:is|was)\s+that\s+(?:all|it|everything)|then\s+what|"
    r"finish\s+(?:the|that)\s+(?:story|recipe|explanation)|"
    r"(?:can|could)\s+you\s+(?:finish|continue|go\s+on)|"
    r"what\s+(?:happened|comes)\s+(?:next|after\s+that)|"
    r"I\s+(?:want|wanna)\s+(?:hear|know)\s+more)\s*[.!?]?\s*$",
    re.IGNORECASE,
)

# Per-mode LLM parameters
RESPONSE_MODE_CONFIG = {
    "short": {
        "max_tokens": 50,
        "max_words": 35,
        "max_sentences": 3,
        "prompt_suffix": None,
        "stop": ["\n", "...", "\u2014"],
    },
    "summary": {
        "max_tokens": 80,
        "max_words": 50,
        "max_sentences": 3,
        "prompt_suffix": (
            "The user wants a detailed response. "
            "Give a compelling summary in 2-3 short sentences. "
            "End by asking 'Want to hear more?'"
        ),
        "stop": ["...", "\u2014"],  # no \n — multi-sentence responses need room
    },
    "continuation": {
        "max_tokens": 80,
        "max_words": 50,
        "max_sentences": 3,
        "prompt_suffix": (
            "Continue where you left off. Give the next part in 2-3 short sentences. "
            "If there's more to tell, end with 'Want more?' "
            "If wrapping up, give a satisfying ending."
        ),
        "stop": ["...", "\u2014"],
    },
}


def classify_intent(user_text: str, last_response_mode: str) -> str:
    """Classify user intent to determine response mode.
    
    Returns: 'short', 'summary', or 'continuation'.
    """
    text = (user_text or "").strip()
    if not text:
        return "short"

    # Check for continuation first — only valid after a summary or continuation
    if last_response_mode in ("summary", "continuation"):
        if _CONTINUATION_PATTERN.match(text):
            return "continuation"

    # Check long-form intent patterns
    for intent_type, pattern in _INTENT_PATTERNS.items():
        if pattern.search(text):
            logger.info(f"Intent classified as '{intent_type}' → summary mode")
            return "summary"

    return "short"


# ---------------------------------------------------------------------------
# Emotion classification — scans LLM response text for emotion signals
# ---------------------------------------------------------------------------
_EMOTION_EXCITED_WORDS = re.compile(
    r"\b(wow|amazing|awesome|incredible|fantastic|absolutely|love it|"
    r"oh my|exciting|can't believe)\b|!{2,}",
    re.IGNORECASE,
)
_EMOTION_HAPPY_WORDS = re.compile(
    r"\b(haha|funny|lol|glad|happy|great|nice|enjoy|love|wonderful|"
    r"yay|sweet|cool|perfect|beautiful)\b|(?<!\!)!(?!\!)",
    re.IGNORECASE,
)
_EMOTION_SAD_WORDS = re.compile(
    r"\b(sorry|unfortunately|sad|miss|lost|gone|difficult|tough|"
    r"hard time|condolences|heartbreaking|terrible|awful)\b",
    re.IGNORECASE,
)
_EMOTION_CURIOUS_WORDS = re.compile(
    r"\b(hmm|interesting|wonder|curious|actually|did you know|"
    r"let me think|well)\b|\?{1,}",
    re.IGNORECASE,
)


def classify_emotion(response_text: str) -> str:
    """Classify the emotion of an LLM response for face animation.

    Returns one of: 'excited', 'happy', 'sad', 'curious', 'neutral'.
    Priority: excited > sad > happy > curious > neutral.
    """
    text = (response_text or "").strip()
    if not text:
        return "neutral"

    # Excited takes priority — strong positive emotion
    if _EMOTION_EXCITED_WORDS.search(text):
        return "excited"
    # Sad next — empathy/negativity should override mild positivity
    if _EMOTION_SAD_WORDS.search(text):
        return "sad"
    # Happy — general positive tone
    if _EMOTION_HAPPY_WORDS.search(text):
        return "happy"
    # Curious — questions, pondering
    if _EMOTION_CURIOUS_WORDS.search(text):
        return "curious"

    return "neutral"


# Maximum characters for a single user prompt (truncated if exceeded)
MAX_USER_CHARS = int(os.getenv("MAX_USER_CHARS", str(config_defaults.MAX_USER_CHARS)))
# Maximum total characters across all messages sent to the LLM (0 = disabled)
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", str(config_defaults.MAX_CONTEXT_CHARS)))
MAX_CONTEXT_TOKENS = int(
    os.getenv("MAX_CONTEXT_TOKENS", str(config_defaults.MAX_CONTEXT_TOKENS))
)
GROUNDING_SOURCES = os.getenv(
    "GROUNDING_SOURCES", config_defaults.GROUNDING_SOURCES
)
STREAMING_FIRST_AUDIO_TARGET_S = float(
    os.getenv(
        "STREAMING_FIRST_AUDIO_TARGET_S",
        str(config_defaults.STREAMING_FIRST_AUDIO_TARGET_S),
    )
)

# Locked v1 model stack
# Foundry Local requires full model IDs for inference calls
MODELS = {
    "chat": os.getenv("CHAT_MODEL_ID", config_defaults.CHAT_MODEL_ID).strip() or config_defaults.CHAT_MODEL_ID,
    "stt": "whisper-tiny",
}
# Short aliases for display/diagnostics
MODEL_ALIASES = {
    "chat": "phi-3.5-mini",
    "stt": "whisper-tiny",
}


def _discover_foundry_endpoint() -> str:
    """Discover the current Foundry Local service endpoint via CLI.

    Runs `foundry service status` and extracts the URL from output.
    Returns the env var value if already set, or falls back to the
    discovered URL. Falls back to localhost:5000 if discovery fails.
    """
    env_host = os.getenv("FOUNDRY_LOCAL_HOST", "")
    if env_host:
        logger.info(f"Foundry endpoint from env: {env_host}")
        return env_host
    try:
        result = subprocess.run(
            ["foundry", "service", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stdout + result.stderr
        match = re.search(r'https?://[^\s\'"]+', output)
        if match:
            url = match.group(0).rstrip('/')
            # Strip any path component — we only want the base URL (host:port)
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(url)
            url = urlunparse((parsed.scheme, parsed.netloc, '', '', '', ''))
            logger.info(f"Foundry endpoint discovered: {url}")
            return url
        logger.warning("Could not parse Foundry endpoint from service status output")
    except Exception as e:
        logger.warning(f"Foundry endpoint discovery failed: {e}")
    fallback = "http://localhost:5000"
    logger.warning(f"Using fallback Foundry endpoint: {fallback}")
    return fallback


FOUNDRY_LOCAL_HOST = _discover_foundry_endpoint()

# Latency budget decomposition (milliseconds)
LATENCY_BUDGET = {
    "stt": 5000,  # Speech-to-text (faster-whisper, usually ~500ms warm)
    "llm": 30000,  # LLM inference (Phi-3.5-mini CPU, ~15-20s for 60-100 tokens)
    "tts": 15000,  # Text-to-speech synthesis (Kokoro ~5-8s for longer responses)
    "overhead": 1000,  # Network, serialization, etc.
}

@dataclass
class ConversationState:
    history: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""
    last_response_mode: str = "short"
    lock: threading.RLock = field(default_factory=threading.RLock)
    updated_at: float = field(default_factory=time.time)


_conversation_states: dict[str, ConversationState] = {}
_conversation_states_lock = threading.Lock()
conversation_history: list[dict[str, str]] = []
_last_response_mode = "short"
_legacy_state = ConversationState(history=conversation_history)


def _conversation_state(conversation_id: str | None) -> ConversationState:
    """Return isolated state for a conversation, preserving direct-call compatibility."""
    global _legacy_state
    if not conversation_id:
        if _legacy_state.history is not conversation_history:
            _legacy_state = ConversationState(
                history=conversation_history,
                last_response_mode=_last_response_mode,
            )
        return _legacy_state
    with _conversation_states_lock:
        return _conversation_states.setdefault(conversation_id, ConversationState())


def reset_conversation(conversation_id: str) -> bool:
    with _conversation_states_lock:
        return _conversation_states.pop(conversation_id, None) is not None


def _estimate_tokens(messages: list[dict[str, str]]) -> int:
    return sum(max(1, len(message.get("content", "")) // 4) for message in messages)


def _compact_history(state: ConversationState) -> None:
    """Bound context by estimated tokens and retain a compact prior-turn summary."""
    removed: list[str] = []
    while len(state.history) > 2 and _estimate_tokens(state.history) > MAX_CONTEXT_TOKENS:
        message = state.history.pop(0)
        removed.append(f"{message.get('role', 'unknown')}: {message.get('content', '')}")
    if removed:
        compact = " | ".join(removed)
        state.summary = (state.summary + " | " + compact).strip(" |")[-1200:]


def get_current_datetime() -> str:
    """Approved deterministic tool for questions about the current date/time."""
    return datetime.now(timezone.utc).isoformat()


def retrieve_local_context(query: str, limit: int = 4) -> list[str]:
    """Return small, cited excerpts from explicitly approved local sources."""
    terms = {
        token
        for token in re.findall(r"[a-z0-9]{4,}", (query or "").lower())
        if token not in {"what", "when", "where", "which", "about", "misty"}
    }
    if not terms:
        return []
    base_dir = os.path.dirname(os.path.abspath(__file__))
    matches: list[str] = []
    for configured_path in GROUNDING_SOURCES.split(","):
        configured_path = configured_path.strip()
        if not configured_path:
            continue
        path = os.path.abspath(os.path.join(base_dir, configured_path))
        try:
            with open(path, "r", encoding="utf-8") as source:
                for line_number, line in enumerate(source, 1):
                    text = line.strip()
                    if text and any(term in text.lower() for term in terms):
                        matches.append(
                            f"[{os.path.basename(path)}:{line_number}] {text[:240]}"
                        )
                        if len(matches) >= limit:
                            return matches
        except OSError:
            logger.warning("Configured grounding source is unavailable: %s", path)
    return matches

# ============================================================================
# FLASK APP SETUP
# ============================================================================

app = Flask(__name__)
CORS(app, origins=[
    "http://localhost:5000", "http://127.0.0.1:5000",
    "http://localhost:5001", "http://127.0.0.1:5001",
    "http://10.0.0.44",    # Misty
    "http://10.0.0.58:*",  # Companion device
])

@app.before_request
def log_request():
    logger.debug(f"Incoming request: {request.method} {request.path}")

@app.after_request
def log_response(response):
    logger.debug(f"Response status: {response.status_code}")
    return response

# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.route("/api/health", methods=["GET"])
def health_check():
    """Check service and Foundry Local availability."""
    try:
        # Verify Foundry Local is reachable
        response = requests.get(
            f"{FOUNDRY_LOCAL_HOST}/openai/models",
            timeout=2.0
        )
        foundry_ok = response.status_code == 200
    except Exception as e:
        logger.warning(f"Foundry Local health check failed: {e}")
        foundry_ok = False
    
    return jsonify({
        "status": "ok" if foundry_ok else "degraded",
        "orchestration": "ready",
        "foundry_local": "ok" if foundry_ok else "unreachable",
        "models": MODEL_ALIASES,
        "timestamp": datetime.utcnow().isoformat(),
    }), 200 if foundry_ok else 503

# ============================================================================
# PRIMARY ORCHESTRATION ENDPOINT
# ============================================================================

@app.route("/api/orchestrate", methods=["POST"])
def orchestrate():
    """
    Main orchestration endpoint.
    Input: WAV file (multipart/form-data), optional form fields:
      - speaker_name: identified speaker from face recognition (#16)
      - return_audio_bytes: if "true", the response JSON includes an
        ``audioBytes`` field containing the generated WAV base64-encoded.
        This avoids a second GET /api/audio/<filename> round trip.
        The ``responseAudio`` URI field is still included for compatibility.
    Output: JSON with response audio URI (and optionally inline audio bytes) or error
    """
    start_time = time.time()
    conversation_id = (
        request.form.get("conversation_id", "").strip()
        or request.headers.get("X-Conversation-ID", "").strip()
        or str(uuid.uuid4())
    )
    session = _conversation_state(conversation_id)
    session.lock.acquire()
    
    try:
        # Extract WAV file
        if 'file' not in request.files:
            logger.error("No file in request")
            return jsonify({"status": "error", "error": "no_file"}), 400
        
        audio_file = request.files['file']
        audio_bytes = audio_file.read()

        # Optional: speaker name from face recognition (#16)
        speaker_name = request.form.get("speaker_name", "").strip() or None

        # Optional: return WAV bytes inline to save a round trip (#69)
        return_audio_bytes = request.form.get("return_audio_bytes", "").lower() == "true"

        # Security: limit upload size (10MB max for WAV audio)
        if len(audio_bytes) > 10 * 1024 * 1024:
            return jsonify({"status": "error", "error": "file_too_large"}), 413
        
        # Step 1: Speech-to-Text
        stt_start = time.time()
        stt_result = speech_to_text(audio_bytes, stt_start)
        stt_ms = (time.time() - stt_start) * 1000
        if stt_result.get("status") == "error":
            return jsonify(stt_result), 400
        
        user_text = stt_result.get("text", "").strip()
        if not user_text:
            logger.warning("STT returned empty text")
            return jsonify({"status": "error", "error": "empty_stt"}), 400
        
        logger.info(f"[STT {stt_ms:.0f}ms] {user_text}")

        # Step 1.5: Check for movement intent (#54, #55) — short-circuit for direct commands
        movement = classify_movement_intent(user_text)
        if movement:
            # Generate a quick verbal acknowledgment via TTS (no LLM needed)
            ack_text = _get_movement_acknowledgment(movement["command"])
            tts_start = time.time()
            tts_result = text_to_speech(ack_text, tts_start)
            tts_ms = (time.time() - tts_start) * 1000
            total_ms = (time.time() - start_time) * 1000
            logger.info(f"[Pipeline {total_ms:.0f}ms] Movement: {movement['command']} "
                         f"(STT={stt_ms:.0f}ms, TTS={tts_ms:.0f}ms, cached={tts_result.get('tts_cached', False)})")
            result = {
                "status": "ok",
                "type": "movement",
                "movement": movement,
                "user_text": user_text,
                "response_text": ack_text,
                "emotion": classify_emotion(ack_text),
                "pipeline_ms": round(total_ms),
                "tts_cached": tts_result.get("tts_cached", False),
            }
            if tts_result.get("status") == "ok":
                result["audio_file"] = tts_result.get("audio_file")
                if return_audio_bytes:
                    result["audioBytes"] = _read_audio_bytes_b64(tts_result.get("audio_file"))
            return jsonify(result), 200
        
        # Step 2: Language Model Inference
        history_snapshot = (
            list(session.history),
            session.summary,
            session.last_response_mode,
            session.updated_at,
        )
        llm_start = time.time()
        llm_result = language_model_inference(
            user_text,
            llm_start,
            speaker_name=speaker_name,
            conversation_id=conversation_id,
        )
        llm_ms = (time.time() - llm_start) * 1000
        if llm_result.get("status") == "error":
            return jsonify(llm_result), 500
        
        response_text = llm_result.get("text", "").strip()
        if not response_text:
            logger.warning("LLM returned empty response")
            (
                session.history[:],
                session.summary,
                session.last_response_mode,
                session.updated_at,
            ) = history_snapshot
            return jsonify({"status": "error", "error": "empty_llm"}), 400
        
        logger.info(f"[LLM {llm_ms:.0f}ms] {response_text}")
        
        # Step 3: Text-to-Speech
        tts_start = time.time()
        tts_result = text_to_speech(response_text, tts_start)
        tts_ms = (time.time() - tts_start) * 1000
        if tts_result.get("status") == "error":
            (
                session.history[:],
                session.summary,
                session.last_response_mode,
                session.updated_at,
            ) = history_snapshot
            return jsonify(tts_result), 500
        
        response_audio_uri = tts_result.get("audio_uri", "")
        if not response_audio_uri:
            logger.warning("TTS returned no audio URI")
            (
                session.history[:],
                session.summary,
                session.last_response_mode,
                session.updated_at,
            ) = history_snapshot
            return jsonify({"status": "error", "error": "empty_tts"}), 500
        
        # Calculate total latency
        total_latency_ms = (time.time() - start_time) * 1000
        tts_fallback = tts_result.get("tts_fallback", False)
        tts_cached = tts_result.get("tts_cached", False)
        
        # Classify emotion for face animation (< 1ms, regex only)
        emotion = classify_emotion(response_text)
        
        logger.info(f"[Pipeline {total_latency_ms:.0f}ms] STT={stt_ms:.0f} LLM={llm_ms:.0f} TTS={tts_ms:.0f} history={len(session.history)} fallback={tts_fallback} cached={tts_cached} emotion={emotion}")
        
        resp = {
            "status": "ok",
            "transcribedText": user_text,
            "inferenceResponse": response_text,
            "responseAudio": response_audio_uri,
            "emotion": emotion,
            "latencyMs": total_latency_ms,
            "ttsFallback": tts_fallback,
            "ttsCached": tts_cached,
            "conversationId": conversation_id,
        }
        if return_audio_bytes:
            resp["audioBytes"] = _read_audio_bytes_b64(tts_result.get("audio_file"))
        return jsonify(resp), 200
        
    except Exception as e:
        logger.error(f"Orchestration failed: {e}", exc_info=True)
        return jsonify({"status": "error", "error": "internal_error"}), 500
    finally:
        session.lock.release()


@app.route("/api/conversations/<conversation_id>", methods=["DELETE"])
def clear_conversation(conversation_id: str):
    """Explicitly release context when the controller re-arms for a new person."""
    return jsonify(
        {
            "status": "ok",
            "conversationId": conversation_id,
            "cleared": reset_conversation(conversation_id),
        }
    )


@app.route("/api/conversations/<conversation_id>/last", methods=["DELETE"])
def rollback_conversation_turn(conversation_id: str):
    """Remove the last complete exchange when robot playback did not occur."""
    state = _conversation_state(conversation_id)
    removed = False
    with state.lock:
        if len(state.history) >= 2:
            state.history[:] = state.history[:-2]
            state.last_response_mode = "short"
            state.updated_at = time.time()
            removed = True
    return jsonify(
        {
            "status": "ok",
            "conversationId": conversation_id,
            "rolledBack": removed,
        }
    )


def _complete_sentences(buffer: str, final: bool = False) -> tuple[list[str], str]:
    """Split complete sentence-sized chunks while retaining an unfinished tail."""
    chunks: list[str] = []
    while True:
        match = re.search(r"(?<=[.!?])\s+", buffer)
        if not match:
            break
        chunk = buffer[: match.start()].strip()
        buffer = buffer[match.end() :]
        if chunk:
            chunks.append(chunk)
    if final and buffer.strip():
        chunks.append(buffer.strip())
        buffer = ""
    return chunks, buffer


@app.route("/api/orchestrate/stream", methods=["POST"])
def orchestrate_stream():
    """Stream ordered sentence WAVs as SSE so playback can begin before LLM completion."""
    if "file" not in request.files:
        return jsonify({"status": "error", "error": "no_file"}), 400
    audio_bytes = request.files["file"].read()
    if len(audio_bytes) > 10 * 1024 * 1024:
        return jsonify({"status": "error", "error": "file_too_large"}), 413
    stt_result = speech_to_text(audio_bytes, time.time())
    user_text = stt_result.get("text", "").strip()
    if not user_text:
        return jsonify({"status": "error", "error": "empty_stt"}), 400

    conversation_id = (
        request.form.get("conversation_id", "").strip() or str(uuid.uuid4())
    )
    speaker_name = request.form.get("speaker_name", "").strip() or None
    state = _conversation_state(conversation_id)
    stream_started = time.time()

    @stream_with_context
    def generate():
        with state.lock:
            movement = classify_movement_intent(user_text)
            if movement:
                acknowledgment = _get_movement_acknowledgment(movement["command"])
                tts = text_to_speech(acknowledgment, time.time())
                yield "data: " + json.dumps(
                    {
                        "type": "movement",
                        "movement": movement,
                        "text": acknowledgment,
                        "audioBytes": _read_audio_bytes_b64(tts.get("audio_file")),
                    }
                ) + "\n\n"
                return
            previous_history = list(state.history)
            previous_mode = state.last_response_mode
            mode = classify_intent(user_text, previous_mode)
            mode_config = RESPONSE_MODE_CONFIG[mode]
            working_history = list(state.history)[-8:]
            working_history.append({"role": "user", "content": user_text})
            system_prompt = (
                f"{SYSTEM_PROMPT} Current UTC date/time: {get_current_datetime()}. "
                "Use only approved local context for event/project facts and state uncertainty otherwise."
            )
            if speaker_name:
                system_prompt += f" You are currently talking to {speaker_name}."
            grounding = retrieve_local_context(user_text)
            if grounding:
                system_prompt += " Approved local context: " + " ".join(grounding)
            messages = [{"role": "system", "content": system_prompt}]
            if state.summary:
                messages.append(
                    {"role": "system", "content": f"Earlier conversation summary: {state.summary}"}
                )
            messages.extend(working_history)
            if mode_config["prompt_suffix"]:
                messages.append(
                    {"role": "system", "content": mode_config["prompt_suffix"]}
                )
            payload = {
                "model": MODELS["chat"],
                "messages": messages,
                "max_tokens": mode_config["max_tokens"],
                "temperature": 0.7,
                "stop": mode_config["stop"],
                "stream": True,
            }
            complete_text = ""
            pending = ""
            sequence = 0
            first_audio_ms = None
            try:
                response = requests.post(
                    f"{FOUNDRY_LOCAL_HOST}/v1/chat/completions",
                    json=payload,
                    timeout=FOUNDRY_API_TIMEOUT,
                    stream=True,
                )
                response.raise_for_status()
                for raw_line in response.iter_lines(decode_unicode=True):
                    if not raw_line or not raw_line.startswith("data:"):
                        continue
                    encoded = raw_line[5:].strip()
                    if encoded == "[DONE]":
                        break
                    event = json.loads(encoded)
                    delta = event["choices"][0].get("delta", {}).get("content", "")
                    if not delta:
                        continue
                    pending += delta
                    complete_text += delta
                    sentences, pending = _complete_sentences(pending)
                    for sentence in sentences:
                        tts = text_to_speech(sentence, time.time())
                        if tts.get("status") != "ok":
                            raise RuntimeError(tts.get("error", "tts_failure"))
                        if first_audio_ms is None:
                            first_audio_ms = (time.time() - stream_started) * 1000
                        yield "data: " + json.dumps(
                            {
                                "type": "audio",
                                "sequence": sequence,
                                "text": sentence,
                                "audioBytes": _read_audio_bytes_b64(tts.get("audio_file")),
                                "firstAudioMs": round(first_audio_ms),
                            }
                        ) + "\n\n"
                        sequence += 1
                sentences, _ = _complete_sentences(pending, final=True)
                for sentence in sentences:
                    tts = text_to_speech(sentence, time.time())
                    if tts.get("status") != "ok":
                        raise RuntimeError(tts.get("error", "tts_failure"))
                    if first_audio_ms is None:
                        first_audio_ms = (time.time() - stream_started) * 1000
                    yield "data: " + json.dumps(
                        {
                            "type": "audio",
                            "sequence": sequence,
                            "text": sentence,
                            "audioBytes": _read_audio_bytes_b64(tts.get("audio_file")),
                            "firstAudioMs": round(first_audio_ms),
                        }
                    ) + "\n\n"
                    sequence += 1
                complete_text = complete_text.strip()
                state.history[:] = working_history
                state.history.append({"role": "assistant", "content": complete_text})
                state.last_response_mode = mode
                _compact_history(state)
                state.updated_at = time.time()
                yield "data: " + json.dumps(
                    {
                        "type": "complete",
                        "conversationId": conversation_id,
                        "transcribedText": user_text,
                        "inferenceResponse": complete_text,
                        "chunks": sequence,
                        "firstAudioMs": round(first_audio_ms or 0),
                        "firstAudioTargetMs": round(STREAMING_FIRST_AUDIO_TARGET_S * 1000),
                    }
                ) + "\n\n"
            except (GeneratorExit, Exception) as exc:
                state.history[:] = previous_history
                state.last_response_mode = previous_mode
                if isinstance(exc, GeneratorExit):
                    raise
                logger.error("Streaming orchestration failed: %s", exc)
                yield "data: " + json.dumps(
                    {"type": "error", "error": "stream_failure", "retryable": True}
                ) + "\n\n"

    return Response(generate(), mimetype="text/event-stream")

# ============================================================================
# STEP 1: SPEECH-TO-TEXT (faster-whisper, in-process)
# ============================================================================

_whisper_model = None


def _get_whisper_model():
    """Return a cached faster-whisper model, or load on first use."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
        _whisper_model = WhisperModel(
            "tiny",
            device=STT_DEVICE,
            compute_type=STT_COMPUTE_TYPE,
            cpu_threads=4,
        )
        logger.info(
            "faster-whisper model loaded (tiny, device=%s, compute_type=%s)",
            STT_DEVICE,
            STT_COMPUTE_TYPE,
        )
        return _whisper_model
    except Exception as e:
        logger.error(f"Failed to load faster-whisper: {e}")
        return None


def speech_to_text(audio_bytes: bytes, start_time: float) -> Dict[str, Any]:
    """Transcribe audio using faster-whisper (in-process CTranslate2)."""
    try:
        elapsed = (time.time() - start_time) * 1000
        remaining = LATENCY_BUDGET["stt"] - elapsed

        if remaining <= 0:
            logger.error("STT timeout: no time remaining")
            return {"status": "error", "error": "timeout"}

        model = _get_whisper_model()
        if model is None:
            return {"status": "error", "error": "stt_failure"}

        # Log audio stats for debugging empty STT issues and reject near-silence
        # before Whisper can hallucinate stale or background phrases.
        try:
            import numpy as _np
            audio_io_check = BytesIO(audio_bytes)
            import soundfile as _sf
            data, sr = _sf.read(audio_io_check)
            rms = float(_np.sqrt(_np.mean(data ** 2)))
            peak = float(_np.max(_np.abs(data)))
            logger.info(f"Audio stats: {len(audio_bytes)} bytes, sr={sr}, "
                        f"duration={len(data)/sr:.1f}s, RMS={rms:.6f}, peak={peak:.6f}")
            if rms < STT_MIN_RMS and peak < STT_MIN_PEAK:
                logger.info(
                    "Audio below STT silence threshold: "
                    "RMS=%.6f < %.6f and peak=%.6f < %.6f",
                    rms,
                    STT_MIN_RMS,
                    peak,
                    STT_MIN_PEAK,
                )
                return {"status": "empty", "text": "", "latency_ms": 0}
        except Exception as e:
            logger.debug(f"Audio stats failed: {e}")

        audio_io = BytesIO(audio_bytes)
        # Use VAD and no prompt bias so background noise does not get coerced
        # into plausible short phrases.
        segments, info = model.transcribe(
            audio_io,
            language="en",
            beam_size=STT_BEAM_SIZE,
            vad_filter=True,
        )
        segment_list = list(segments)
        text = " ".join(seg.text.strip() for seg in segment_list).strip()

        if segment_list:
            avg_logprob = sum(getattr(seg, "avg_logprob", 0.0) for seg in segment_list) / len(segment_list)
            max_no_speech_prob = max(getattr(seg, "no_speech_prob", 0.0) for seg in segment_list)
            if avg_logprob < STT_MIN_AVG_LOGPROB or max_no_speech_prob > STT_MAX_NO_SPEECH_PROB:
                logger.info(
                    "Rejecting low-confidence STT result: avg_logprob=%.3f < %.3f "
                    "or no_speech_prob=%.3f > %.3f | text=%r",
                    avg_logprob,
                    STT_MIN_AVG_LOGPROB,
                    max_no_speech_prob,
                    STT_MAX_NO_SPEECH_PROB,
                    text,
                )
                return {"status": "empty", "text": "", "latency_ms": 0}

        if _looks_like_repeated_short_hallucination(text):
            logger.info("Rejecting repeated short STT hallucination: text=%r", text)
            return {"status": "empty", "text": "", "latency_ms": 0}

        logger.debug(f"STT result: {text}")
        return {"status": "ok", "text": text}

    except Exception as e:
        logger.error(f"STT failed: {e}")
        return {"status": "error", "error": "stt_failure"}


def _looks_like_repeated_short_hallucination(text: str) -> bool:
    """Detect common Whisper noise artifacts such as "Wow. Wow."."""
    chunks = [
        re.sub(r"[^a-z0-9']+", " ", chunk.lower()).strip()
        for chunk in re.split(r"[.!?]+", text or "")
    ]
    chunks = [chunk for chunk in chunks if chunk]
    if len(chunks) < 2:
        return False
    if len(set(chunks)) != 1:
        return False
    return len(chunks[0].split()) <= 2

# ============================================================================
# STEP 2: LANGUAGE MODEL INFERENCE
# ============================================================================

def language_model_inference(
    user_text: str,
    start_time: float,
    speaker_name: str | None = None,
    conversation_id: str | None = None,
) -> Dict[str, Any]:
    """Run inference using Foundry Local with adaptive response modes.

    Args:
        user_text: Transcribed user speech.
        start_time: Pipeline start timestamp.
        speaker_name: Optional recognized face name (#16). Injected into prompt context.
    """
    global _last_response_mode
    state = _conversation_state(conversation_id)
    
    try:
        elapsed = (time.time() - start_time) * 1000
        remaining = LATENCY_BUDGET["llm"] - elapsed
        
        if remaining <= 0:
            logger.error("LLM timeout: no time remaining")
            return {"status": "error", "error": "timeout"}
        
        # Strip and enforce user prompt character limit
        user_text = (user_text or "").strip()
        if len(user_text) > MAX_USER_CHARS:
            logger.warning(
                f"User prompt truncated: {len(user_text)} -> {MAX_USER_CHARS} chars"
            )
            user_text = user_text[:MAX_USER_CHARS]

        # Classify intent for adaptive response mode
        previous_response_mode = state.last_response_mode
        response_mode = classify_intent(user_text, previous_response_mode)
        # Note: _last_response_mode is updated AFTER successful LLM response (below)
        # to avoid stale state if the LLM call fails or times out.
        mode_config = RESPONSE_MODE_CONFIG[response_mode]
        logger.info(f"Response mode: {response_mode} (last={previous_response_mode})")

        # Build message history
        working_history = list(state.history)
        working_history.append({"role": "user", "content": user_text})
        if len(working_history) > 8:
            del working_history[:-8]

        url = f"{FOUNDRY_LOCAL_HOST}/v1/chat/completions"
        # Prepend system prompt on every call; not stored in history
        system_prompt = (
            f"{SYSTEM_PROMPT} Today is {datetime.now(timezone.utc).date().isoformat()} UTC. "
            "Only state event or project facts supplied in approved local context; "
            "otherwise say that you are not certain."
        )
        # Inject face recognition context (#16) — tell Misty who she's talking to
        if speaker_name:
            system_prompt += (
                f" You are currently talking to {speaker_name}."
                f" Use their name naturally — greet them, tease them, be personal."
                f" Don't announce that you recognized them every time."
            )
            logger.info(f"Speaker identified: {speaker_name}")
        grounded_context = retrieve_local_context(user_text)
        if grounded_context:
            system_prompt += " Approved local context: " + " ".join(grounded_context)
        messages = [{"role": "system", "content": system_prompt}]
        if state.summary:
            messages.append(
                {"role": "system", "content": f"Earlier conversation summary: {state.summary}"}
            )
        messages.extend(working_history)

        # Inject mode-specific prompt suffix
        if mode_config["prompt_suffix"]:
            messages.append({"role": "system", "content": mode_config["prompt_suffix"]})
        elif len(working_history) > 4:
            # Brevity reminder only for short mode when history is long
            messages.append({"role": "system", "content": "Remember: 1-2 sentences, ~20 words max. Stay punchy."})

        # Enforce maximum total context character budget (trim oldest turns first)
        if MAX_CONTEXT_CHARS > 0:
            total_chars = sum(len(m.get("content", "")) for m in messages)
            if total_chars > MAX_CONTEXT_CHARS:
                trimmed = 0
                # messages[0] is the system prompt; messages[-1] is the latest user turn.
                # Remove the oldest non-system messages (index 1) until within budget.
                while len(messages) > 2 and total_chars > MAX_CONTEXT_CHARS:
                    removed = messages.pop(1)
                    total_chars -= len(removed.get("content", ""))
                    trimmed += 1
                logger.warning(
                    f"Context trimmed: removed {trimmed} message(s); total={total_chars} chars"
                )

        payload = {
            "model": MODELS["chat"],
            "messages": messages,
            "max_tokens": mode_config["max_tokens"],
            "temperature": 0.7,
            "stop": mode_config["stop"],
        }
        
        response = requests.post(
            url,
            json=payload,
            timeout=min(FOUNDRY_API_TIMEOUT, remaining / 1000.0)
        )
        
        if response.status_code != 200:
            logger.error(f"LLM API error: {response.status_code} {response.text}")
            return {"status": "error", "error": "llm_failure"}
        
        result = response.json()
        assistant_text = result["choices"][0]["message"]["content"].strip()

        # Strip meta-commentary that small models sometimes leak
        # (e.g., "(Note: The response is intentionally brief..." or stage directions)
        import re as _re
        assistant_text = _re.split(r'\n*\(Note[:\s]', assistant_text)[0].strip()
        assistant_text = _re.split(r'\n*\(Since ', assistant_text)[0].strip()
        assistant_text = _re.split(r'\n*\(The response ', assistant_text)[0].strip()

        # Post-LLM truncation — limits vary by mode
        max_words = mode_config["max_words"]
        max_sents = mode_config["max_sentences"]
        words = assistant_text.split()
        if len(words) > max_words:
            sentence_ends = []
            for i, char in enumerate(assistant_text):
                if char in ".!?" and i > 10:
                    sentence_ends.append(i)
                    if len(sentence_ends) >= max_sents:
                        break
            if sentence_ends:
                assistant_text = assistant_text[:sentence_ends[-1] + 1]
            else:
                assistant_text = " ".join(words[:max_words]) + "."
            logger.info(f"Truncated LLM response ({response_mode} mode) to: {assistant_text}")
        
        # Add to history for context in next turn
        state.history[:] = working_history
        state.history.append({"role": "assistant", "content": assistant_text})
        _compact_history(state)
        state.updated_at = time.time()

        # Update response mode tracking AFTER successful LLM response —
        # prevents stale state when LLM times out or fails.
        # This is NOT unused: _last_response_mode is read at line 456 on the
        # next request to classify continuation intent (e.g., "yes" / "more").
        state.last_response_mode = response_mode
        if conversation_id is None:
            _last_response_mode = response_mode
        
        logger.debug(f"LLM result ({response_mode}): {assistant_text}")
        return {"status": "ok", "text": assistant_text, "responseMode": response_mode}
        
    except requests.Timeout:
        logger.error("LLM request timed out")
        return {"status": "error", "error": "timeout"}
    except Exception as e:
        logger.error(f"LLM inference failed: {e}")
        return {"status": "error", "error": "llm_failure"}

# ============================================================================
# STEP 3: TEXT-TO-SPEECH
# ============================================================================

# ---------------------------------------------------------------------------
# TTS backends — initialised lazily on first use
# ---------------------------------------------------------------------------

_kokoro_instance = None
_kokoro_lock = threading.Lock()  # Thread-safety for Kokoro ONNX synthesis
_pyttsx3_engine = None
_pyttsx3_lock = threading.Lock()  # Thread-safety for pyttsx3 synthesis


def _get_kokoro():
    """Return a cached Kokoro-ONNX instance, or None if unavailable."""
    global _kokoro_instance
    if _kokoro_instance is not None:
        return _kokoro_instance
    try:
        from kokoro_onnx import Kokoro  # noqa: PLC0415
        _kokoro_instance = Kokoro("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")
        logger.info("kokoro-onnx TTS engine initialised")
        return _kokoro_instance
    except Exception as e:
        logger.warning(f"kokoro-onnx unavailable: {e}")
        return None


def _get_pyttsx3():
    """Return a cached pyttsx3 engine, or None if unavailable."""
    global _pyttsx3_engine
    if _pyttsx3_engine is not None:
        return _pyttsx3_engine
    try:
        import pyttsx3  # noqa: PLC0415
        engine = pyttsx3.init()
        engine.setProperty('rate', 170)
        _pyttsx3_engine = engine
        logger.info("pyttsx3 TTS engine initialised (SAPI5 fallback)")
        return _pyttsx3_engine
    except Exception as e:
        logger.warning(f"pyttsx3 unavailable: {e}")
        return None


import glob as glob_module


MAX_AUDIO_FILES = 50  # Keep at most 50 response files on disk

# ---------------------------------------------------------------------------
# TTS audio cache — avoids re-synthesizing identical phrases
# ---------------------------------------------------------------------------

TTS_CACHE_MAX = int(os.getenv("TTS_CACHE_MAX", str(config_defaults.TTS_CACHE_MAX)))
_tts_cache: OrderedDict = OrderedDict()  # text_hash → {"path": str, "pinned": bool}
_tts_cache_lock = threading.Lock()


def _tts_cache_key(text: str) -> str:
    """Compute a stable cache key for TTS text (SHA-256 of normalised text)."""
    normalised = text.strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def _tts_cache_get(text: str) -> str | None:
    """Look up cached WAV path for text. Returns path if hit and file exists."""
    key = _tts_cache_key(text)
    with _tts_cache_lock:
        entry = _tts_cache.get(key)
        if entry is None:
            return None
        path = entry["path"]
        if not os.path.exists(path):
            del _tts_cache[key]
            return None
        _tts_cache.move_to_end(key)  # LRU: mark as recently used
        return path


def _tts_cache_put(text: str, path: str, pinned: bool = False):
    """Store a synthesized WAV path in the cache."""
    key = _tts_cache_key(text)
    with _tts_cache_lock:
        _tts_cache[key] = {"path": path, "pinned": pinned}
        _tts_cache.move_to_end(key)
        # Evict oldest non-pinned entries if over capacity
        while len(_tts_cache) > TTS_CACHE_MAX:
            oldest_key, oldest_entry = next(iter(_tts_cache.items()))
            if oldest_entry.get("pinned"):
                # Don't evict pinned entries; move to end and try next
                _tts_cache.move_to_end(oldest_key)
                # Safety: if all entries are pinned, stop evicting
                if all(e.get("pinned") for e in _tts_cache.values()):
                    break
                continue
            evicted = _tts_cache.pop(oldest_key)
            try:
                os.remove(evicted["path"])
            except OSError:
                pass


def _cleanup_old_audio():
    """Remove oldest non-cached response audio files when exceeding MAX_AUDIO_FILES."""
    try:
        # Only clean up timestamped response files, never cached (pinned) files
        files = sorted(
            glob_module.glob(os.path.join("responses", "response_*.wav")),
            key=os.path.getctime, reverse=True
        )
        for f in files[MAX_AUDIO_FILES:]:
            os.remove(f)
    except Exception:
        pass  # Non-critical — don't fail TTS over cleanup


def _prewarm_tts_cache():
    """Pre-generate audio for known phrases (movement acks, common responses).

    Called in a background thread after startup to avoid blocking the service.
    Cached files use stable names (cached_<hash>.wav) excluded from cleanup.
    """
    phrases = []
    # Collect all movement acknowledgment phrases
    for ack_list in _MOVEMENT_ACKS.values():
        phrases.extend(ack_list)
    # Add fixed controller phrases (greeting + thinking audio)
    phrases.extend(_CONTROLLER_PHRASES)

    os.makedirs("responses", exist_ok=True)
    generated = 0
    kokoro = _get_kokoro()
    if kokoro is None:
        logger.warning("TTS pre-warm skipped: kokoro-onnx unavailable")
        return

    try:
        import soundfile as sf  # noqa: PLC0415
    except ImportError:
        logger.warning("TTS pre-warm skipped: soundfile unavailable")
        return

    for phrase in phrases:
        key = _tts_cache_key(phrase)
        cache_filename = f"cached_{key}.wav"
        cache_path = os.path.join("responses", cache_filename)

        # Skip if already cached on disk
        if os.path.exists(cache_path):
            _tts_cache_put(phrase, cache_path, pinned=True)
            generated += 1
            continue

        try:
            with _kokoro_lock:
                samples, sample_rate = kokoro.create(
                    phrase, voice=KOKORO_VOICE, speed=KOKORO_SPEED, lang="en-us"
                )
            sf.write(cache_path, samples, sample_rate)
            _tts_cache_put(phrase, cache_path, pinned=True)
            generated += 1
        except Exception as e:
            logger.warning(f"TTS pre-warm failed for '{phrase}': {e}")

    logger.info(f"TTS cache pre-warmed: {generated}/{len(phrases)} phrases cached")


def text_to_speech(text: str, start_time: float) -> Dict[str, Any]:
    """Convert text to speech using kokoro-onnx (primary) or pyttsx3 (fallback).

    Checks the TTS audio cache first; on hit, returns the cached file immediately.
    On miss, synthesizes and stores the result in the cache.
    """
    try:
        elapsed = (time.time() - start_time) * 1000
        remaining = LATENCY_BUDGET["tts"] - elapsed

        if remaining <= 0:
            logger.error("TTS timeout: no time remaining")
            return {"status": "error", "error": "timeout"}

        os.makedirs("responses", exist_ok=True)

        # --- Cache lookup ---
        cached_path = _tts_cache_get(text)
        if cached_path is not None:
            audio_filename = os.path.basename(cached_path)
            logger.debug(f"TTS cache hit: {audio_filename}")
            return {
                "status": "ok",
                "audio_uri": f"/api/audio/{audio_filename}",
                "audio_file": audio_filename,
                "tts_cached": True,
            }

        # Clean up old audio files to prevent disk accumulation
        _cleanup_old_audio()
        audio_filename = f"response_{int(time.time() * 1000)}.wav"
        audio_path = os.path.join("responses", audio_filename)

        # --- Primary: kokoro-onnx (neural, fully offline) ---
        kokoro = _get_kokoro()
        if kokoro is not None:
            try:
                import soundfile as sf  # noqa: PLC0415
                with _kokoro_lock:
                    samples, sample_rate = kokoro.create(
                        text, voice=KOKORO_VOICE, speed=KOKORO_SPEED, lang="en-us"
                    )
                sf.write(audio_path, samples, sample_rate)
                logger.debug(f"kokoro-onnx TTS saved: {audio_path}")
                _tts_cache_put(text, audio_path)
                return {
                    "status": "ok",
                    "audio_uri": f"/api/audio/{audio_filename}",
                    "audio_file": audio_filename,
                }
            except Exception as e:
                logger.warning(f"TTS FALLBACK: kokoro-onnx synthesis failed ({e}), switching to pyttsx3 SAPI5")
        else:
            logger.warning("TTS FALLBACK: kokoro-onnx unavailable, switching to pyttsx3 SAPI5")

        # --- Fallback: pyttsx3 SAPI5 (robotic but guaranteed on Windows) ---
        engine = _get_pyttsx3()
        if engine is not None:
            logger.warning("TTS FALLBACK ACTIVE: using pyttsx3 SAPI5 (reduced voice quality)")
            with _pyttsx3_lock:
                engine.save_to_file(text, audio_path)
                engine.runAndWait()
            logger.debug(f"pyttsx3 TTS saved: {audio_path}")
            _tts_cache_put(text, audio_path)
            return {
                "status": "ok",
                "audio_uri": f"/api/audio/{audio_filename}",
                "audio_file": audio_filename,
                "tts_fallback": True,
            }

        logger.error("No TTS engine available (kokoro-onnx and pyttsx3 both failed)")
        return {"status": "error", "error": "tts_no_engine"}

    except Exception as e:
        logger.error(f"TTS failed: {e}")
        return {"status": "error", "error": "tts_failure"}

# ============================================================================
# AUDIO RETRIEVAL
# ============================================================================

def _read_audio_bytes_b64(audio_filename: str) -> str | None:
    """Read a generated audio file and return its contents as a base64 string.

    Returns None if ``audio_filename`` is falsy or the file cannot be read.
    Used by /api/orchestrate when ``return_audio_bytes=true`` is requested to
    embed the WAV inline in the JSON response, saving a round-trip GET.
    """
    if not audio_filename:
        return None
    # Security: prevent path traversal (same guard as get_audio)
    if "/" in audio_filename or "\\" in audio_filename or ".." in audio_filename:
        logger.warning(f"_read_audio_bytes_b64: rejected unsafe filename: {audio_filename!r}")
        return None
    audio_path = os.path.abspath(os.path.join("responses", audio_filename))
    base_path = os.path.abspath("responses")
    if not audio_path.startswith(base_path):
        logger.warning(f"_read_audio_bytes_b64: path traversal blocked: {audio_filename!r}")
        return None
    try:
        with open(audio_path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except OSError as exc:
        logger.warning(f"_read_audio_bytes_b64: could not read {audio_filename!r}: {exc}")
        return None


@app.route("/api/audio/<filename>", methods=["GET"])
def get_audio(filename):
    """Retrieve generated response audio."""
    try:
        # Security: prevent path traversal
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            return jsonify({"error": "invalid_filename"}), 400
        audio_path = os.path.abspath(os.path.join("responses", filename))
        base_path = os.path.abspath("responses")
        if not audio_path.startswith(base_path):
            return jsonify({"error": "invalid_filename"}), 400

        if not os.path.exists(audio_path):
            logger.error(f"Audio file not found: {filename}")
            return jsonify({"error": "not_found"}), 404
        
        return send_file(audio_path, mimetype='audio/wav')
    except Exception as e:
        logger.error(f"Audio retrieval failed: {e}")
        return jsonify({"error": "internal_error"}), 500

# ============================================================================
# FALLBACK TTS ENDPOINT
# ============================================================================

@app.route("/api/fallback-tts", methods=["POST"])
def fallback_tts():
    """Generate TTS for fallback messages."""
    try:
        data = request.json or {}
        text = data.get("text", "")
        
        if not text:
            return jsonify({"status": "error", "error": "no_text"}), 400
        if len(text) > 500:
            return jsonify({"status": "error", "error": "text_too_long"}), 400
        
        result = text_to_speech(text, time.time())
        
        if result.get("status") == "error":
            return jsonify(result), 500
        
        return jsonify(result), 200
        
    except Exception as e:
        logger.error(f"Fallback TTS failed: {e}")
        return jsonify({"status": "error", "error": "internal_error"}), 500


@app.route("/api/tts", methods=["POST"])
def tts_endpoint():
    """Generate TTS audio and return raw WAV bytes.
    
    Used by the controller to pre-generate greeting audio etc.
    POST JSON: {"text": "What's up baby?"}
    Returns: audio/wav binary
    """
    try:
        data = request.json or {}
        text = data.get("text", "")
        
        if not text:
            return jsonify({"status": "error", "error": "no_text"}), 400
        if len(text) > 500:
            return jsonify({"status": "error", "error": "text_too_long"}), 400
        
        result = text_to_speech(text, time.time())
        
        if result.get("status") == "error":
            return jsonify(result), 500
        
        audio_uri = result.get("audio_uri", "")
        # Extract filename from URI like "/api/audio/response_123.wav"
        audio_file = audio_uri.split("/")[-1] if audio_uri else ""
        if not audio_file:
            return jsonify({"status": "error", "error": "no_audio_generated"}), 500
        
        # Security: prevent path traversal (defense-in-depth)
        if "/" in audio_file or "\\" in audio_file or ".." in audio_file:
            return jsonify({"status": "error", "error": "invalid_filename"}), 400
        audio_path = os.path.abspath(os.path.join("responses", audio_file))
        base_path = os.path.abspath("responses")
        if not audio_path.startswith(base_path):
            return jsonify({"status": "error", "error": "invalid_filename"}), 400
        
        if not os.path.exists(audio_path):
            return jsonify({"status": "error", "error": "audio_file_missing"}), 500
        
        return send_file(audio_path, mimetype='audio/wav')
        
    except Exception as e:
        logger.error(f"TTS endpoint failed: {e}")
        return jsonify({"status": "error", "error": "internal_error"}), 500

# ============================================================================
# DIAGNOSTICS
# ============================================================================

@app.route("/api/diagnostics", methods=["GET"])
def diagnostics():
    """Return current service diagnostics."""
    tts_engine = "kokoro-onnx" if _kokoro_instance is not None else None
    tts_fallback = "pyttsx3" if _pyttsx3_engine is not None else None
    with _tts_cache_lock:
        cache_size = len(_tts_cache)
        cache_pinned = sum(1 for e in _tts_cache.values() if e.get("pinned"))
    return jsonify({
        "service": "FoundryLocal Orchestration",
        "version": "1.1.0",
        "foundry_host": FOUNDRY_LOCAL_HOST,
        "models": MODEL_ALIASES,
        "tts": {
            "engine": tts_engine or "kokoro-onnx",
            "fallback": "pyttsx3",
            "initialized": tts_engine is not None or tts_fallback is not None,
            "speed": KOKORO_SPEED,
            "cache_entries": cache_size,
            "cache_pinned": cache_pinned,
            "cache_max": TTS_CACHE_MAX,
        },
        "llm": {
            "temperature": 0.7,
            "short_max_tokens": RESPONSE_MODE_CONFIG["short"]["max_tokens"],
        },
        "latency_budget_ms": LATENCY_BUDGET,
        "timestamp": datetime.utcnow().isoformat(),
    }), 200

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "endpoint_not_found"}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return jsonify({"error": "internal_error"}), 500

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    logger.info("Starting Foundry Local Orchestration Service")
    logger.info(f"Foundry Local: {FOUNDRY_LOCAL_HOST}")
    logger.info(f"Models: {MODEL_ALIASES} (full IDs: {MODELS})")

    # Pre-warm models so first request doesn't pay cold-start cost
    logger.info("Pre-warming STT model (faster-whisper)...")
    _get_whisper_model()
    logger.info("Pre-warming TTS model (kokoro-onnx)...")
    _get_kokoro()

    # Pre-warm TTS cache in background (movement acks, common phrases)
    prewarm_thread = threading.Thread(target=_prewarm_tts_cache, daemon=True)
    prewarm_thread.start()
    logger.info("TTS cache pre-warm started in background")

    app.run(host="0.0.0.0", port=5000, debug=False)

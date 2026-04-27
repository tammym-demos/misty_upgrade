"""
Windows Orchestration Service for Misty + Foundry Local
Handles STT -> LLM -> TTS pipeline with timeout and error handling.
Serves OpenAI-compatible endpoints for Misty skill integration.
"""

import os
import re
import sys
import json
import logging
import subprocess
import time
import requests
from io import BytesIO
from datetime import datetime
from typing import Dict, Any, Tuple

from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

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

FOUNDRY_API_TIMEOUT = float(os.getenv("FOUNDRY_API_TIMEOUT", "10.0"))
SERVICE_TIMEOUT = float(os.getenv("SERVICE_TIMEOUT", "15.0"))
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "af_sky")
KOKORO_SPEED = float(os.getenv("KOKORO_SPEED", "1.4"))
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    (
        "You are Misty, a sassy little robot with BIG personality. "
        "You're witty, cheeky, playful, and a little dramatic — like a fun friend who always has a comeback. "
        "You love teasing (gently!), cracking jokes, adding flair, and throwing in the occasional eye-roll. "
        "Use casual language, slang, and expressive phrases like 'oh honey', 'girl please', 'I mean...', 'not gonna lie'. "
        "RULE: Keep responses to 2-3 short sentences. Be fun, not flat. "
        "Never say you're an AI or assistant. You're Misty, and you own it."
    )
)

# ---- Response mode configuration ----
# Intent patterns that trigger summary mode (compiled once at import time)
_INTENT_PATTERNS = {
    "story": re.compile(
        r"\b(?:tell\s+(?:me\s+)?(?:a\s+)?(?:bed\s*time\s+)?stor(?:y|ies)|"
        r"make\s+up\s+a\s+(?:story|tale)|"
        r"(?:bed\s*time|fairy|scary|funny)\s+(?:story|tale)|"
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

_CONTINUATION_PATTERN = re.compile(
    r"^\s*(?:yes|yeah|yep|sure|ok(?:ay)?|more|continue|go\s+on|keep\s+going|"
    r"tell\s+me\s+more|what\s+happens?\s+next|and\s+then\??|"
    r"what(?:'s|\s+is)\s+next|what\s+else)\s*[.!?]?\s*$",
    re.IGNORECASE,
)

# Per-mode LLM parameters
RESPONSE_MODE_CONFIG = {
    "short": {
        "max_tokens": 60,
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
            "Give a compelling summary in 2-3 sentences. "
            "End by asking 'Want to hear more?'"
        ),
        "stop": ["...", "\u2014"],  # no \n — multi-sentence responses need room
    },
    "continuation": {
        "max_tokens": 80,
        "max_words": 50,
        "max_sentences": 3,
        "prompt_suffix": (
            "Continue where you left off. Give the next part in 2-3 sentences. "
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


# Maximum characters for a single user prompt (truncated if exceeded)
MAX_USER_CHARS = int(os.getenv("MAX_USER_CHARS", "400"))
# Maximum total characters across all messages sent to the LLM (0 = disabled)
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "5000"))

# Locked v1 model stack
# Foundry Local requires full model IDs for inference calls
MODELS = {
    "chat": "Phi-3.5-mini-instruct-openvino-gpu:2",
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
    "stt": 3000,  # Speech-to-text (faster-whisper, usually ~500ms warm)
    "llm": 8000,  # LLM inference (Phi-3.5-mini, depends on output length)
    "tts": 3000,  # Text-to-speech synthesis (Kokoro ~1-2s)
    "overhead": 1000,  # Network, serialization, etc.
}

# Global conversation context (in-memory for v1; stateless per utterance for MVP)
conversation_history = []
# Track last response mode for continuation detection
_last_response_mode = "short"

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
    Input: WAV file (multipart/form-data)
    Output: JSON with response audio URI or error
    """
    start_time = time.time()
    
    try:
        # Extract WAV file
        if 'file' not in request.files:
            logger.error("No file in request")
            return jsonify({"status": "error", "error": "no_file"}), 400
        
        audio_file = request.files['file']
        audio_bytes = audio_file.read()

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
        
        # Step 2: Language Model Inference
        llm_start = time.time()
        llm_result = language_model_inference(user_text, llm_start)
        llm_ms = (time.time() - llm_start) * 1000
        if llm_result.get("status") == "error":
            return jsonify(llm_result), 500
        
        response_text = llm_result.get("text", "").strip()
        if not response_text:
            logger.warning("LLM returned empty response")
            return jsonify({"status": "error", "error": "empty_llm"}), 400
        
        logger.info(f"[LLM {llm_ms:.0f}ms] {response_text}")
        
        # Step 3: Text-to-Speech
        tts_start = time.time()
        tts_result = text_to_speech(response_text, tts_start)
        tts_ms = (time.time() - tts_start) * 1000
        if tts_result.get("status") == "error":
            return jsonify(tts_result), 500
        
        response_audio_uri = tts_result.get("audio_uri", "")
        if not response_audio_uri:
            logger.warning("TTS returned no audio URI")
            return jsonify({"status": "error", "error": "empty_tts"}), 500
        
        # Calculate total latency
        total_latency_ms = (time.time() - start_time) * 1000
        tts_fallback = tts_result.get("tts_fallback", False)
        logger.info(f"[Pipeline {total_latency_ms:.0f}ms] STT={stt_ms:.0f} LLM={llm_ms:.0f} TTS={tts_ms:.0f} history={len(conversation_history)} fallback={tts_fallback}")
        
        return jsonify({
            "status": "ok",
            "transcribedText": user_text,
            "inferenceResponse": response_text,
            "responseAudio": response_audio_uri,
            "latencyMs": total_latency_ms,
            "ttsFallback": tts_fallback,
        }), 200
        
    except Exception as e:
        logger.error(f"Orchestration failed: {e}", exc_info=True)
        return jsonify({"status": "error", "error": "internal_error"}), 500

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
        _whisper_model = WhisperModel("tiny", compute_type="int8", cpu_threads=4)
        logger.info("faster-whisper model loaded (tiny, int8)")
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

        audio_io = BytesIO(audio_bytes)
        segments, info = model.transcribe(
            audio_io,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters={
                "min_speech_duration_ms": 200,
                "min_silence_duration_ms": 100,
                "speech_pad_ms": 200,
            },
            initial_prompt="Hey Misty, tell me about science, history, math, geography, and fun facts.",
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()

        logger.debug(f"STT result: {text}")
        return {"status": "ok", "text": text}

    except Exception as e:
        logger.error(f"STT failed: {e}")
        return {"status": "error", "error": "stt_failure"}

# ============================================================================
# STEP 2: LANGUAGE MODEL INFERENCE
# ============================================================================

def language_model_inference(user_text: str, start_time: float) -> Dict[str, Any]:
    """Run inference using Foundry Local with adaptive response modes."""
    global conversation_history, _last_response_mode
    
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
        response_mode = classify_intent(user_text, _last_response_mode)
        mode_config = RESPONSE_MODE_CONFIG[response_mode]
        logger.info(f"Response mode: {response_mode} (last={_last_response_mode})")

        # Build message history
        conversation_history.append({"role": "user", "content": user_text})
        # Keep history limited to last 8 messages (4 turns) for better context
        if len(conversation_history) > 8:
            del conversation_history[:-8]

        url = f"{FOUNDRY_LOCAL_HOST}/v1/chat/completions"
        # Prepend system prompt on every call; not stored in history
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history

        # Inject mode-specific prompt suffix
        if mode_config["prompt_suffix"]:
            messages.append({"role": "system", "content": mode_config["prompt_suffix"]})
        elif len(conversation_history) > 4:
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
            "temperature": 0.85,
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
        
        # Update continuation tracking
        _last_response_mode = response_mode

        # Add to history for context in next turn
        conversation_history.append({"role": "assistant", "content": assistant_text})
        
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
_pyttsx3_engine = None


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


def _cleanup_old_audio():
    """Remove oldest audio files when exceeding MAX_AUDIO_FILES."""
    try:
        files = sorted(
            glob_module.glob(os.path.join("responses", "response_*.wav")),
            key=os.path.getctime, reverse=True
        )
        for f in files[MAX_AUDIO_FILES:]:
            os.remove(f)
    except Exception:
        pass  # Non-critical — don't fail TTS over cleanup


def text_to_speech(text: str, start_time: float) -> Dict[str, Any]:
    """Convert text to speech using kokoro-onnx (primary) or pyttsx3 (fallback)."""
    try:
        elapsed = (time.time() - start_time) * 1000
        remaining = LATENCY_BUDGET["tts"] - elapsed

        if remaining <= 0:
            logger.error("TTS timeout: no time remaining")
            return {"status": "error", "error": "timeout"}

        os.makedirs("responses", exist_ok=True)
        # Clean up old audio files to prevent disk accumulation
        _cleanup_old_audio()
        audio_filename = f"response_{int(time.time() * 1000)}.wav"
        audio_path = os.path.join("responses", audio_filename)

        # --- Primary: kokoro-onnx (neural, fully offline, win-arm64 native) ---
        kokoro = _get_kokoro()
        if kokoro is not None:
            try:
                import soundfile as sf  # noqa: PLC0415
                samples, sample_rate = kokoro.create(
                    text, voice=KOKORO_VOICE, speed=KOKORO_SPEED, lang="en-us"
                )
                sf.write(audio_path, samples, sample_rate)
                logger.debug(f"kokoro-onnx TTS saved: {audio_path}")
                return {"status": "ok", "audio_uri": f"/api/audio/{audio_filename}"}
            except Exception as e:
                logger.warning(f"⚠️ TTS FALLBACK: kokoro-onnx synthesis failed ({e}), switching to pyttsx3 SAPI5")
        else:
            logger.warning("⚠️ TTS FALLBACK: kokoro-onnx unavailable, switching to pyttsx3 SAPI5")

        # --- Fallback: pyttsx3 SAPI5 (robotic but guaranteed on Windows) ---
        engine = _get_pyttsx3()
        if engine is not None:
            logger.warning("⚠️ TTS FALLBACK ACTIVE: using pyttsx3 SAPI5 (reduced voice quality)")
            engine.save_to_file(text, audio_path)
            engine.runAndWait()
            logger.debug(f"pyttsx3 TTS saved: {audio_path}")
            return {"status": "ok", "audio_uri": f"/api/audio/{audio_filename}", "tts_fallback": True}

        logger.error("❌ No TTS engine available (kokoro-onnx and pyttsx3 both failed)")
        return {"status": "error", "error": "tts_no_engine"}

    except Exception as e:
        logger.error(f"TTS failed: {e}")
        return {"status": "error", "error": "tts_failure"}

# ============================================================================
# AUDIO RETRIEVAL
# ============================================================================

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

# ============================================================================
# DIAGNOSTICS
# ============================================================================

@app.route("/api/diagnostics", methods=["GET"])
def diagnostics():
    """Return current service diagnostics."""
    tts_engine = "kokoro-onnx" if _kokoro_instance is not None else None
    tts_fallback = "pyttsx3" if _pyttsx3_engine is not None else None
    return jsonify({
        "service": "FoundryLocal Orchestration",
        "version": "1.0.0",
        "foundry_host": FOUNDRY_LOCAL_HOST,
        "models": MODEL_ALIASES,
        "tts": {
            "engine": tts_engine or "kokoro-onnx",
            "fallback": "pyttsx3",
            "initialized": tts_engine is not None or tts_fallback is not None,
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

    app.run(host="0.0.0.0", port=5000, debug=False)

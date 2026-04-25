"""
Windows Orchestration Service for Misty + Foundry Local
Handles STT -> LLM -> TTS pipeline with timeout and error handling.
Serves OpenAI-compatible endpoints for Misty skill integration.
"""

import os
import re
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
        logging.FileHandler('orchestration.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

load_dotenv()  # Load .env before reading any env vars

FOUNDRY_API_TIMEOUT = float(os.getenv("FOUNDRY_API_TIMEOUT", "5.0"))
SERVICE_TIMEOUT = float(os.getenv("SERVICE_TIMEOUT", "6.0"))
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "af_heart")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are Misty, a helpful and friendly robot assistant. Keep answers concise and conversational — no more than 2-3 sentences. Be warm and engaging. "
    "When the user message or conversation context is long, internally summarize it into 2-3 bullet points and answer only from that summary. "
    "Never quote the user verbatim; paraphrase and quote at most 80 characters. "
    "If a request is too long or unclear, ask a single clarifying question instead of guessing."
)

# Maximum characters for a single user prompt (truncated if exceeded)
MAX_USER_CHARS = int(os.getenv("MAX_USER_CHARS", "400"))
# Maximum total characters across all messages sent to the LLM (0 = disabled)
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "3000"))

# Locked v1 model stack
MODELS = {
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
    "stt": 1500,  # Speech-to-text
    "llm": 2000,  # LLM inference
    "tts": 1500,  # Text-to-speech synthesis
    "overhead": 500,  # Network, serialization, etc.
}

# Global conversation context (in-memory for v1; stateless per utterance for MVP)
conversation_history = []

# ============================================================================
# FLASK APP SETUP
# ============================================================================

app = Flask(__name__)
CORS(app)

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
        "models": MODELS,
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
        
        # Step 1: Speech-to-Text
        stt_result = speech_to_text(audio_bytes, start_time)
        if stt_result.get("status") == "error":
            return jsonify(stt_result), 400
        
        user_text = stt_result.get("text", "").strip()
        if not user_text:
            logger.warning("STT returned empty text")
            return jsonify({"status": "error", "error": "empty_stt"}), 400
        
        logger.info(f"Transcribed text: {user_text}")
        
        # Step 2: Language Model Inference
        llm_result = language_model_inference(user_text, start_time)
        if llm_result.get("status") == "error":
            return jsonify(llm_result), 500
        
        response_text = llm_result.get("text", "").strip()
        if not response_text:
            logger.warning("LLM returned empty response")
            return jsonify({"status": "error", "error": "empty_llm"}), 400
        
        logger.info(f"LLM response: {response_text}")
        
        # Step 3: Text-to-Speech
        tts_result = text_to_speech(response_text, start_time)
        if tts_result.get("status") == "error":
            return jsonify(tts_result), 500
        
        response_audio_uri = tts_result.get("audio_uri", "")
        if not response_audio_uri:
            logger.warning("TTS returned no audio URI")
            return jsonify({"status": "error", "error": "empty_tts"}), 500
        
        # Calculate total latency
        total_latency_ms = (time.time() - start_time) * 1000
        tts_fallback = tts_result.get("tts_fallback", False)
        if tts_fallback:
            logger.warning(f"⚠️ Orchestration completed in {total_latency_ms:.0f}ms (TTS FALLBACK active)")
        else:
            logger.info(f"Orchestration completed in {total_latency_ms:.0f}ms")
        
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
# STEP 1: SPEECH-TO-TEXT
# ============================================================================

def speech_to_text(audio_bytes: bytes, start_time: float) -> Dict[str, Any]:
    """Transcribe audio using Foundry Local."""
    try:
        elapsed = (time.time() - start_time) * 1000
        remaining = LATENCY_BUDGET["stt"] - elapsed
        
        if remaining <= 0:
            logger.error("STT timeout: no time remaining")
            return {"status": "error", "error": "timeout"}
        
        url = f"{FOUNDRY_LOCAL_HOST}/v1/audio/transcriptions"
        files = {
            'file': ('audio.wav', BytesIO(audio_bytes), 'audio/wav'),
        }
        data = {
            'model': MODELS["stt"],
            'language': 'en',
        }
        
        response = requests.post(
            url,
            files=files,
            data=data,
            timeout=min(FOUNDRY_API_TIMEOUT, remaining / 1000.0)
        )
        
        if response.status_code != 200:
            logger.error(f"STT API error: {response.status_code} {response.text}")
            return {"status": "error", "error": "stt_failure"}
        
        result = response.json()
        text = result.get("text", "")
        
        logger.debug(f"STT result: {text}")
        return {"status": "ok", "text": text}
        
    except requests.Timeout:
        logger.error("STT request timed out")
        return {"status": "error", "error": "timeout"}
    except Exception as e:
        logger.error(f"STT failed: {e}")
        return {"status": "error", "error": "stt_failure"}

# ============================================================================
# STEP 2: LANGUAGE MODEL INFERENCE
# ============================================================================

def language_model_inference(user_text: str, start_time: float) -> Dict[str, Any]:
    """Run inference using Foundry Local."""
    global conversation_history
    
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
                f"User prompt truncated: {len(user_text)} → {MAX_USER_CHARS} chars"
            )
            user_text = user_text[:MAX_USER_CHARS]

        # Build message history
        conversation_history.append({"role": "user", "content": user_text})

        # Keep history limited to last 10 messages to control context size
        if len(conversation_history) > 10:
            del conversation_history[:-10]

        url = f"{FOUNDRY_LOCAL_HOST}/v1/chat/completions"
        # Prepend system prompt on every call; not stored in history
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history

        # Enforce maximum total context character budget (trim oldest turns first)
        if MAX_CONTEXT_CHARS > 0:
            total_chars = sum(len(m.get("content", "")) for m in messages)
            if total_chars > MAX_CONTEXT_CHARS:
                trimmed = 0
                # messages[0] is the system prompt; messages[-1] is the latest user turn.
                # Remove the oldest non-system messages (index 1) until within budget.
                while len(messages) > 2:
                    total_chars = sum(len(m.get("content", "")) for m in messages)
                    if total_chars <= MAX_CONTEXT_CHARS:
                        break
                    messages.pop(1)
                    trimmed += 1
                final_chars = sum(len(m.get("content", "")) for m in messages)
                logger.warning(
                    f"Context trimmed: removed {trimmed} message(s); total={final_chars} chars"
                )

        payload = {
            "model": MODELS["chat"],
            "messages": messages,
            "max_tokens": 150,  # Keep output short for latency
            "temperature": 0.7,
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
        
        # Add to history for context in next turn
        conversation_history.append({"role": "assistant", "content": assistant_text})
        
        logger.debug(f"LLM result: {assistant_text}")
        return {"status": "ok", "text": assistant_text}
        
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


def text_to_speech(text: str, start_time: float) -> Dict[str, Any]:
    """Convert text to speech using kokoro-onnx (primary) or pyttsx3 (fallback)."""
    try:
        elapsed = (time.time() - start_time) * 1000
        remaining = LATENCY_BUDGET["tts"] - elapsed

        if remaining <= 0:
            logger.error("TTS timeout: no time remaining")
            return {"status": "error", "error": "timeout"}

        os.makedirs("responses", exist_ok=True)
        audio_filename = f"response_{int(time.time() * 1000)}.wav"
        audio_path = os.path.join("responses", audio_filename)

        # --- Primary: kokoro-onnx (neural, fully offline, win-arm64 native) ---
        kokoro = _get_kokoro()
        if kokoro is not None:
            try:
                import soundfile as sf  # noqa: PLC0415
                samples, sample_rate = kokoro.create(
                    text, voice=KOKORO_VOICE, speed=1.0, lang="en-us"
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
        audio_path = os.path.join("responses", filename)
        
        if not os.path.exists(audio_path):
            logger.error(f"Audio file not found: {audio_path}")
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
        "models": MODELS,
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
    logger.info(f"Models: {MODELS}")
    app.run(host="0.0.0.0", port=5000, debug=False)

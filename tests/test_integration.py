"""
Integration Test Suite for Misty + Foundry Local
Tests communication between components and validates latency SLO.
"""

import sys
import unittest
import unittest.mock
import re
import subprocess
import requests
import json
import time
import os
import threading
from io import BytesIO
from urllib.parse import urlparse, urlunparse

# ---------------------------------------------------------------------------
# Path setup for importing orchestration_service in unit tests.
# Must be done before the module is imported so that FOUNDRY_LOCAL_HOST is
# already in the environment when _discover_foundry_endpoint() runs.
# ---------------------------------------------------------------------------
os.environ.setdefault("FOUNDRY_LOCAL_HOST", "http://localhost:9999")
_ORCHESTRATION_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration")
)
if _ORCHESTRATION_PATH not in sys.path:
    sys.path.insert(0, _ORCHESTRATION_PATH)


def _discover_foundry_endpoint() -> str:
    """Discover the Foundry Local endpoint, same logic as orchestration_service.py."""
    env_host = os.getenv("FOUNDRY_LOCAL_HOST", "")
    if env_host:
        return env_host
    try:
        result = subprocess.run(
            ["foundry", "service", "status"],
            capture_output=True, text=True, timeout=10,
        )
        match = re.search(r'https?://[^\s\'"]+', result.stdout + result.stderr)
        if match:
            parsed = urlparse(match.group(0).rstrip('/'))
            return urlunparse((parsed.scheme, parsed.netloc, '', '', '', ''))
    except Exception as e:
        print(f"Failed to discover Foundry endpoint via CLI: {e}")
    return ""


# Configuration for testing
MISTY_HOST = os.getenv("MISTY_HOST", "http://10.0.0.44")
WINDOWS_HOST = os.getenv("WINDOWS_HOST", "http://localhost:5000")
FOUNDRY_HOST = _discover_foundry_endpoint()

class TestWindowsOrchestration(unittest.TestCase):
    """Test Windows orchestration service."""
    
    def test_health_check(self):
        """Verify orchestration service is running."""
        response = requests.get(f"{WINDOWS_HOST}/api/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("status", data)
        self.assertIn("models", data)
    
    def test_diagnostics_endpoint(self):
        """Verify diagnostics endpoint."""
        response = requests.get(f"{WINDOWS_HOST}/api/diagnostics")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["service"], "FoundryLocal Orchestration")
        self.assertIn("chat", data["models"])
        self.assertIn("stt", data["models"])
        # TTS is not a Foundry model — it's reported separately
        self.assertIn("tts", data)
        self.assertIn("engine", data["tts"])

    def test_tts_endpoint_generates_audio(self):
        """Verify /api/tts generates WAV audio from text."""
        response = requests.post(
            f"{WINDOWS_HOST}/api/tts",
            json={"text": "Hello"},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Type", ""), "audio/wav")
        # WAV files start with RIFF header and must be > 44 bytes (header only)
        self.assertGreater(len(response.content), 44, "TTS returned empty or header-only WAV")
        self.assertTrue(response.content[:4] == b"RIFF", "Response is not a valid WAV file")

    def test_tts_endpoint_rejects_empty_text(self):
        """Verify /api/tts returns 400 for empty text."""
        response = requests.post(
            f"{WINDOWS_HOST}/api/tts",
            json={"text": ""},
            timeout=10,
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data.get("error"), "no_text")

    def test_tts_endpoint_rejects_overlong_text(self):
        """Verify /api/tts returns 400 for text over 500 chars."""
        response = requests.post(
            f"{WINDOWS_HOST}/api/tts",
            json={"text": "a" * 501},
            timeout=10,
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data.get("error"), "text_too_long")

class TestMistyConnectivity(unittest.TestCase):
    """Test Misty robot connectivity."""
    
    def test_misty_rest_endpoint(self):
        """Confirm Misty responds over REST."""
        try:
            response = requests.get(f"{MISTY_HOST}/api/device", timeout=2)
            self.assertEqual(response.status_code, 200)
        except requests.exceptions.RequestException as e:
            self.fail(f"Misty REST endpoint unreachable: {e}")
    
    def test_misty_skill_deployment_capability(self):
        """Verify skill deployment endpoint is available."""
        try:
            # Endpoint for checking if robot can accept skills
            response = requests.get(f"{MISTY_HOST}/api/skills", timeout=2)
            # 200 or 401 (auth) means endpoint exists
            self.assertIn(response.status_code, [200, 401, 403])
        except requests.exceptions.RequestException as e:
            self.fail(f"Misty skill endpoint unreachable: {e}")

class TestFoundryLocalIntegration(unittest.TestCase):
    """Test Foundry Local endpoints."""

    def setUp(self):
        if not FOUNDRY_HOST:
            self.skipTest(
                "Foundry Local endpoint not discovered. "
                "Set FOUNDRY_LOCAL_HOST or ensure `foundry service status` works."
            )

    def test_foundry_models_endpoint(self):
        """Verify Foundry Local models API."""
        try:
            response = requests.get(f"{FOUNDRY_HOST}/openai/models", timeout=2)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            # Foundry returns a plain array of model ID strings
            self.assertIsInstance(data, list)
            self.assertGreater(len(data), 0)
        except requests.exceptions.RequestException as e:
            self.fail(f"Foundry Local models endpoint unreachable: {e}")

    def test_foundry_chat_completions(self):
        """Test basic LLM inference."""
        payload = {
            "model": "Phi-3.5-mini-instruct-openvino-gpu:2",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 50,
        }
        try:
            response = requests.post(
                f"{FOUNDRY_HOST}/v1/chat/completions",
                json=payload,
                timeout=10
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("choices", data)
            self.assertGreater(len(data["choices"]), 0)
        except requests.exceptions.RequestException as e:
            self.fail(f"Foundry Local chat endpoint failed: {e}")

class TestLatencySLO(unittest.TestCase):
    """Validate latency SLO compliance."""
    
    def test_orchestration_latency_p50(self):
        """Test p50 latency < 3 seconds (requires mock WAV)."""
        # This requires a valid WAV file; for now, test endpoint availability
        response = requests.get(f"{WINDOWS_HOST}/api/health")
        self.assertEqual(response.status_code, 200)
        
        # TODO: Once service is running, generate test WAV and measure actual latency
        # Expected: median latency < 3000ms
    
    def test_orchestration_latency_p95(self):
        """Test p95 latency < 6 seconds."""
        # TODO: Once service is running, measure across multiple requests
        # Expected: 95th percentile latency < 6000ms

class TestFallbackBehavior(unittest.TestCase):
    """Test fallback error handling."""
    
    def test_service_unavailable_fallback(self):
        """Verify graceful handling when service is down."""
        # This test should run independently and not require live service
        pass
    
    def test_timeout_fallback(self):
        """Verify timeout handling."""
        pass
    
    def test_model_load_failure_fallback(self):
        """Verify recovery from model load failures."""
        pass

class TestVerificationChecklist(unittest.TestCase):
    """Map to verification items from plan."""
    
    def test_item_1_misty_rest_access(self):
        """1. Confirm Misty responds over REST on the local network."""
        try:
            response = requests.get(f"{MISTY_HOST}/api/device", timeout=2)
            self.assertIsNotNone(response)
        except:
            self.skipTest("Misty not available; skipping physical validation")
    
    def test_item_2_wake_word_detection(self):
        """2. Confirm wake word detection triggers reliably."""
        # Manual validation; would need to interact with running Misty
        self.skipTest("Manual validation required on live robot")
    
    def test_item_3_audio_recording(self):
        """3. Confirm Misty records and exposes short WAV clips."""
        self.skipTest("Manual validation required on live robot")
    
    def test_item_4_foundry_cold_start(self):
        """4. Confirm first-run model download and cold start."""
        self.skipTest("One-time validation; deferred to deployment")
    
    def test_item_5_warm_cache_latency(self):
        """5. Confirm warm-cache runs meet latency SLO (p50<3s, p95<6s)."""
        self.skipTest("Requires benchmarking on live system")
    
    def test_item_6_offline_capability(self):
        """6. Confirm offline-after-download behavior."""
        self.skipTest("Manual validation after first-run setup")
    
    def test_item_7_companion_service_output(self):
        """7. Confirm service returns valid text and WAV for known inputs."""
        # Placeholder for integration test once service is deployed
        pass
    
    def test_item_8_fallback_availability(self):
        """8. Confirm deterministic fallback behavior when service unavailable."""
        pass
    
    def test_item_9_end_to_end_interaction(self):
        """9. Confirm full Misty interaction under normal and degraded conditions."""
        self.skipTest("End-to-end validation on live robot")

class TestPromptLimiting(unittest.TestCase):
    """
    Unit tests for prompt truncation and context capping.
    These tests do NOT require Foundry Local or any live service —
    all HTTP calls are mocked.
    """

    _svc = None  # orchestration_service module, lazily imported once

    @classmethod
    def setUpClass(cls):
        """Import orchestration_service, skipping the class if unavailable."""
        try:
            import orchestration_service  # noqa: PLC0415
            cls._svc = orchestration_service
        except Exception as exc:  # pragma: no cover
            cls._svc = None
            print(f"[TestPromptLimiting] Could not import orchestration_service: {exc}")

    def setUp(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported; skipping unit tests")
        # Reset conversation history and response mode before every test
        self._svc.conversation_history = []
        self._svc._last_response_mode = "short"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _mock_llm_response(self, text="OK"):
        """Return a mock requests.Response that looks like a successful LLM reply."""
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": text}}]}
        return mock_resp

    def _call_llm_and_capture_payload(self, user_text, mock_response_text="OK"):
        """
        Call language_model_inference with a mocked requests.post and return
        (result_dict, payload_sent_to_foundry).
        """
        with unittest.mock.patch.object(
            self._svc.requests, "post", return_value=self._mock_llm_response(mock_response_text)
        ) as mock_post:
            result = self._svc.language_model_inference(user_text, time.time())
            payload = mock_post.call_args[1]["json"]  # keyword arg 'json'
        return result, payload

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_overlong_user_text_is_truncated(self):
        """User text longer than MAX_USER_CHARS must be truncated before the LLM call."""
        max_chars = self._svc.MAX_USER_CHARS
        long_text = "a" * (max_chars + 500)

        _, payload = self._call_llm_and_capture_payload(long_text)

        user_msgs = [m for m in payload["messages"] if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1, "Expected exactly one user message in payload")
        self.assertLessEqual(
            len(user_msgs[0]["content"]),
            max_chars,
            f"Content length {len(user_msgs[0]['content'])} exceeds MAX_USER_CHARS={max_chars}",
        )

    def test_short_user_text_is_not_altered(self):
        """User text within MAX_USER_CHARS must reach the LLM unchanged."""
        short_text = "What time is it?"

        _, payload = self._call_llm_and_capture_payload(short_text)

        user_msgs = [m for m in payload["messages"] if m["role"] == "user"]
        self.assertEqual(len(user_msgs), 1)
        self.assertEqual(user_msgs[0]["content"], short_text)

    def test_empty_user_text_is_handled_safely(self):
        """Empty string user_text must not raise an exception."""
        with unittest.mock.patch.object(
            self._svc.requests, "post", return_value=self._mock_llm_response()
        ):
            try:
                self._svc.language_model_inference("", time.time())
            except Exception as exc:  # pragma: no cover
                self.fail(f"language_model_inference raised on empty input: {exc}")

    def test_context_trimming_respects_max_context_chars(self):
        """
        When total message content exceeds MAX_CONTEXT_CHARS, the payload sent to
        Foundry must be trimmed so that total chars <= MAX_CONTEXT_CHARS.
        The system prompt and the most recent user message must always be preserved.
        """
        max_ctx = self._svc.MAX_CONTEXT_CHARS
        if max_ctx <= 0:
            self.skipTest("MAX_CONTEXT_CHARS is disabled (0)")

        # Stuff conversation_history with large old turns to exceed the budget
        large_content = "x" * (max_ctx // 2 + 1)
        self._svc.conversation_history = [
            {"role": "user", "content": large_content},
            {"role": "assistant", "content": large_content},
        ]

        latest_user_text = "short question"
        _, payload = self._call_llm_and_capture_payload(latest_user_text)

        total_chars = sum(len(m.get("content", "")) for m in payload["messages"])
        self.assertLessEqual(
            total_chars,
            max_ctx,
            f"Payload total chars {total_chars} exceeds MAX_CONTEXT_CHARS={max_ctx}",
        )

        # The most recent user message must always survive trimming
        user_msgs = [m for m in payload["messages"] if m["role"] == "user"]
        self.assertTrue(
            any(m["content"] == latest_user_text for m in user_msgs),
            "Most recent user message was removed during context trimming",
        )

        # The system prompt must always be the first message
        self.assertEqual(payload["messages"][0]["role"], "system")

    def test_history_cap_at_eight_messages(self):
        """Conversation history must be capped at 8 messages (4 turns)."""
        # Fill history with 10 messages (5 turns)
        self._svc.conversation_history = [
            {"role": "user", "content": f"msg {i}"}
            if i % 2 == 0
            else {"role": "assistant", "content": f"reply {i}"}
            for i in range(10)
        ]

        self._call_llm_and_capture_payload("latest question")

        # After the call, history should have been trimmed to 8 + the new user msg
        # The function appends user msg, trims to 8, then appends assistant response
        # So conversation_history should be 8 (trim happened) + 1 (assistant) = at most 9
        # But actually: append user -> trim to 8 -> call LLM -> append assistant = 9
        # Next call will trim to 8 again. Just check it's <= 9.
        self.assertLessEqual(
            len(self._svc.conversation_history),
            9,
            f"History should be capped near 8, got {len(self._svc.conversation_history)}",
        )

    def test_response_truncation_at_35_words(self):
        """Responses over 35 words must be truncated to 35 words or 3 sentences."""
        long_response = " ".join([f"word{i}" for i in range(50)])
        result, _ = self._call_llm_and_capture_payload(
            "test", mock_response_text=long_response
        )
        # The result should be truncated
        words = result["text"].split()
        self.assertLessEqual(
            len(words),
            36,  # 35 words + possible trailing period word
            f"Response should be truncated to ~35 words, got {len(words)}",
        )

    def test_two_sentence_truncation(self):
        """A response with 4+ sentences over 35 words should be truncated at 3 sentence boundaries."""
        four_sentences = (
            "This is the very first long sentence about robotics and AI technology. "
            "This is the equally long second sentence with more interesting details. "
            "This is the third sentence that adds even more juicy context here. "
            "This is the fourth sentence that definitely should be cut out entirely."
        )
        result, _ = self._call_llm_and_capture_payload(
            "test", mock_response_text=four_sentences
        )
        # Should keep at most 3 sentence-ending punctuation marks
        text = result["text"]
        sentence_count = text.count(".") + text.count("!") + text.count("?")
        self.assertLessEqual(sentence_count, 3, f"Expected at most 3 sentences in: {text}")

    def test_max_tokens_is_60(self):
        """The LLM payload must use max_tokens=60 for short mode."""
        _, payload = self._call_llm_and_capture_payload("test question")
        self.assertEqual(
            payload["max_tokens"],
            60,
            f"Expected max_tokens=60 for short mode, got {payload['max_tokens']}",
        )

    # ------------------------------------------------------------------
    # Intent classification tests
    # ------------------------------------------------------------------

    def test_classify_intent_short_default(self):
        """Normal questions should classify as 'short'."""
        for text in ["What's your name?", "How are you?", "Tell me a joke.", "What's 2+2?"]:
            mode = self._svc.classify_intent(text, "short")
            self.assertEqual(mode, "short", f"Expected 'short' for: {text}")

    def test_classify_intent_story(self):
        """Story requests should classify as 'summary'."""
        for text in ["Tell me a bedtime story", "Make up a story about a robot",
                      "Tell me a fairy tale", "Can you tell me a scary story?"]:
            mode = self._svc.classify_intent(text, "short")
            self.assertEqual(mode, "summary", f"Expected 'summary' for: {text}")

    def test_classify_intent_recipe(self):
        """Recipe requests should classify as 'summary'."""
        for text in ["Give me a recipe for chicken pot pie",
                      "How do I make chocolate chip cookies?",
                      "How to cook a steak?"]:
            mode = self._svc.classify_intent(text, "short")
            self.assertEqual(mode, "summary", f"Expected 'summary' for: {text}")

    def test_classify_intent_explain(self):
        """Explanation requests should classify as 'summary'."""
        for text in ["Explain how gravity works", "Tell me about the solar system",
                      "How does a computer work?"]:
            mode = self._svc.classify_intent(text, "short")
            self.assertEqual(mode, "summary", f"Expected 'summary' for: {text}")

    def test_classify_intent_continuation(self):
        """Continuation phrases after a summary should classify as 'continuation'."""
        for text in ["yes", "more", "continue", "go on", "tell me more",
                      "what happens next", "keep going"]:
            mode = self._svc.classify_intent(text, "summary")
            self.assertEqual(mode, "continuation", f"Expected 'continuation' for: {text}")

    def test_classify_intent_continuation_requires_prior_summary(self):
        """Continuation phrases should NOT trigger if last mode was 'short'."""
        mode = self._svc.classify_intent("yes", "short")
        self.assertEqual(mode, "short", "'yes' after short mode should stay 'short'")

    def test_classify_intent_continuation_chain(self):
        """Continuation should chain — 'more' after continuation stays continuation."""
        mode = self._svc.classify_intent("more", "continuation")
        self.assertEqual(mode, "continuation", "'more' after continuation should stay 'continuation'")

    def test_classify_intent_empty_input(self):
        """Empty input should default to 'short'."""
        self.assertEqual(self._svc.classify_intent("", "short"), "short")
        self.assertEqual(self._svc.classify_intent("  ", "summary"), "short")

    # ------------------------------------------------------------------
    # Adaptive response mode tests
    # ------------------------------------------------------------------

    def test_summary_mode_max_tokens_80(self):
        """Summary mode requests should use max_tokens=80."""
        _, payload = self._call_llm_and_capture_payload("Tell me a bedtime story")
        self.assertEqual(
            payload["max_tokens"],
            80,
            f"Expected max_tokens=80 for summary mode, got {payload['max_tokens']}",
        )

    def test_summary_mode_includes_prompt_suffix(self):
        """Summary mode should inject a mode-specific system prompt."""
        _, payload = self._call_llm_and_capture_payload("Tell me a bedtime story")
        system_msgs = [m for m in payload["messages"] if m["role"] == "system"]
        system_text = " ".join(m["content"] for m in system_msgs)
        self.assertIn("Want to hear more", system_text,
                       "Summary mode should include 'Want to hear more?' in system prompt")

    def test_summary_mode_truncation_50_words(self):
        """Summary mode should truncate at 50 words, not 25."""
        long_response = " ".join([f"word{i}" for i in range(60)])
        result, _ = self._call_llm_and_capture_payload(
            "Tell me a bedtime story", mock_response_text=long_response
        )
        words = result["text"].split()
        self.assertLessEqual(len(words), 51)  # 50 + possible trailing period
        self.assertGreater(len(words), 25, "Summary mode should allow more than 25 words")

    def test_response_includes_mode_field(self):
        """API response should include responseMode field."""
        result, _ = self._call_llm_and_capture_payload("What's your name?")
        self.assertIn("responseMode", result)
        self.assertEqual(result["responseMode"], "short")

    def test_response_mode_summary_in_result(self):
        """Summary mode should be reflected in the response."""
        result, _ = self._call_llm_and_capture_payload("Tell me a bedtime story")
        self.assertEqual(result["responseMode"], "summary")

    def test_continuation_mode_after_summary(self):
        """After a summary response, 'yes' should trigger continuation mode."""
        # First call: summary
        self._call_llm_and_capture_payload("Tell me a bedtime story")
        # Second call: continuation
        result, payload = self._call_llm_and_capture_payload("yes")
        self.assertEqual(result["responseMode"], "continuation")
        self.assertEqual(payload["max_tokens"], 80)

    def test_topic_change_resets_to_short(self):
        """Changing topic after a summary should reset to short mode."""
        # First call: summary
        self._call_llm_and_capture_payload("Tell me a bedtime story")
        # Second call: different topic
        result, payload = self._call_llm_and_capture_payload("What's 2 plus 2?")
        self.assertEqual(result["responseMode"], "short")
        self.assertEqual(payload["max_tokens"], 60)

    def test_brevity_reminder_suppressed_in_summary_mode(self):
        """Brevity reminder should not appear in summary/continuation modes."""
        # Fill history to trigger brevity reminder threshold (>4 messages)
        self._svc.conversation_history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "hi again"},
            {"role": "assistant", "content": "hello again"},
            {"role": "user", "content": "one more"},
        ]
        _, payload = self._call_llm_and_capture_payload("Tell me a bedtime story")
        system_msgs = [m for m in payload["messages"] if m["role"] == "system"]
        system_text = " ".join(m["content"] for m in system_msgs)
        self.assertNotIn("Stay punchy", system_text,
                          "Brevity reminder should be suppressed in summary mode")


class TestMovementIntentClassification(unittest.TestCase):
    """Unit tests for movement intent detection (#54).

    Tests the regex-based movement command classification in orchestration_service.
    """

    _svc = None

    @classmethod
    def setUpClass(cls):
        try:
            import orchestration_service
            cls._svc = orchestration_service
        except Exception as exc:
            cls._svc = None
            print(f"[TestMovementIntentClassification] Could not import: {exc}")

    def setUp(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")

    # --- Forward commands ---

    def test_go_forward(self):
        result = self._svc.classify_movement_intent("go forward")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "forward")

    def test_move_ahead(self):
        result = self._svc.classify_movement_intent("move ahead")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "forward")

    def test_come_here(self):
        result = self._svc.classify_movement_intent("come here")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "forward")

    def test_come_to_me(self):
        result = self._svc.classify_movement_intent("come to me")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "forward")

    def test_drive_straight(self):
        result = self._svc.classify_movement_intent("drive straight")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "forward")

    # --- Backward commands ---

    def test_go_back(self):
        result = self._svc.classify_movement_intent("go back")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "backward")

    def test_move_backward(self):
        result = self._svc.classify_movement_intent("move backward")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "backward")

    def test_back_up(self):
        result = self._svc.classify_movement_intent("back up")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "backward")

    def test_reverse(self):
        result = self._svc.classify_movement_intent("reverse")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "backward")

    # --- Rotate commands ---

    def test_turn_left(self):
        result = self._svc.classify_movement_intent("turn left")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "rotate_left")

    def test_rotate_right(self):
        result = self._svc.classify_movement_intent("rotate right")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "rotate_right")

    def test_spin_left(self):
        result = self._svc.classify_movement_intent("spin left")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "rotate_left")

    def test_look_right(self):
        result = self._svc.classify_movement_intent("look right")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "rotate_right")

    # --- Stop commands ---

    def test_stop(self):
        result = self._svc.classify_movement_intent("stop")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "stop")

    def test_halt(self):
        result = self._svc.classify_movement_intent("halt")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "stop")

    def test_freeze(self):
        result = self._svc.classify_movement_intent("freeze")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "stop")

    def test_dont_move(self):
        result = self._svc.classify_movement_intent("don't move")
        self.assertIsNotNone(result)
        self.assertEqual(result["command"], "stop")

    # --- Non-movement commands (should return None) ---

    def test_hello(self):
        result = self._svc.classify_movement_intent("hello misty")
        self.assertIsNone(result)

    def test_whats_the_weather(self):
        result = self._svc.classify_movement_intent("what's the weather today")
        self.assertIsNone(result)

    def test_tell_me_a_story(self):
        result = self._svc.classify_movement_intent("tell me a story")
        self.assertIsNone(result)

    def test_empty_string(self):
        result = self._svc.classify_movement_intent("")
        self.assertIsNone(result)

    def test_none_input(self):
        result = self._svc.classify_movement_intent(None)
        self.assertIsNone(result)

    # --- Movement acknowledgments (#55) ---

    def test_get_movement_ack_forward(self):
        """Movement acknowledgment for forward should be a non-empty string."""
        ack = self._svc._get_movement_acknowledgment("forward")
        self.assertIsInstance(ack, str)
        self.assertTrue(len(ack) > 0)

    def test_get_movement_ack_stop(self):
        ack = self._svc._get_movement_acknowledgment("stop")
        self.assertIsInstance(ack, str)
        self.assertTrue(len(ack) > 0)

    def test_get_movement_ack_unknown_command(self):
        """Unknown command should return fallback 'Okay!'."""
        ack = self._svc._get_movement_acknowledgment("unknown_cmd")
        self.assertEqual(ack, "Okay!")

    def test_movement_prompt_supplement_exists(self):
        """MOVEMENT_PROMPT_SUPPLEMENT should be defined."""
        self.assertTrue(hasattr(self._svc, 'MOVEMENT_PROMPT_SUPPLEMENT'))
        self.assertIn("move", self._svc.MOVEMENT_PROMPT_SUPPLEMENT.lower())

    def test_system_prompt_mentions_movement(self):
        """System prompt should mention Misty can move."""
        self.assertIn("move", self._svc.SYSTEM_PROMPT.lower())


class TestDrivePrimitives(unittest.TestCase):
    """Unit tests for drive/locomotion methods (#48).

    These tests mock HTTP calls — no live robot required.
    """

    _ctrl = None

    @classmethod
    def setUpClass(cls):
        try:
            _ctrl_path = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration")
            )
            if _ctrl_path not in sys.path:
                sys.path.insert(0, _ctrl_path)
            from misty_controller import MistyController
            cls._MistyController = MistyController
        except Exception as exc:
            cls._MistyController = None
            print(f"[TestDrivePrimitives] Could not import misty_controller: {exc}")

    def setUp(self):
        if self._MistyController is None:
            self.skipTest("misty_controller could not be imported")
        self.ctrl = self._MistyController()
        # Mock misty_post to capture calls without network
        self._post_calls = []
        self.ctrl.misty_post = lambda endpoint, body=None, timeout=5.0: (
            self._post_calls.append((endpoint, body)) or {"status": "Success", "result": True}
        )

    # --- Parameter clamping tests ---

    def test_drive_clamps_linear_velocity(self):
        """Linear velocity should be clamped to ±DRIVE_MAX_LINEAR_PCT."""
        self.ctrl.drive(200, 0)
        endpoint, body = self._post_calls[-1]
        self.assertEqual(endpoint, "/api/drive")
        self.assertEqual(body["LinearVelocity"], self.ctrl.DRIVE_MAX_LINEAR_PCT)

    def test_drive_clamps_negative_linear_velocity(self):
        """Negative linear velocity should be clamped."""
        self.ctrl.drive(-200, 0)
        _, body = self._post_calls[-1]
        self.assertEqual(body["LinearVelocity"], -self.ctrl.DRIVE_MAX_LINEAR_PCT)

    def test_drive_clamps_angular_velocity(self):
        """Angular velocity should be clamped to ±DRIVE_MAX_ANGULAR_PCT."""
        self.ctrl.drive(0, 150)
        _, body = self._post_calls[-1]
        self.assertEqual(body["AngularVelocity"], self.ctrl.DRIVE_MAX_ANGULAR_PCT)

    def test_drive_passes_valid_values_unchanged(self):
        """Values within bounds pass through unchanged."""
        self.ctrl.drive(15, -10)
        _, body = self._post_calls[-1]
        self.assertEqual(body["LinearVelocity"], 15)
        self.assertEqual(body["AngularVelocity"], -10)

    # --- DriveTime tests ---

    def test_drive_time_clamps_duration(self):
        """Duration should be clamped to DRIVE_MAX_DURATION_MS."""
        self.ctrl.drive_time(10, 0, 99999)
        _, body = self._post_calls[-1]
        self.assertEqual(body["TimeMs"], self.ctrl.DRIVE_MAX_DURATION_MS)

    def test_drive_time_enforces_minimum_duration(self):
        """Duration below 100ms should be raised to 100."""
        self.ctrl.drive_time(10, 0, 50)
        _, body = self._post_calls[-1]
        self.assertEqual(body["TimeMs"], 100)

    def test_drive_time_valid_params(self):
        """Valid parameters pass through correctly."""
        self.ctrl.drive_time(20, -15, 2000)
        endpoint, body = self._post_calls[-1]
        self.assertEqual(endpoint, "/api/drive/time")
        self.assertEqual(body["LinearVelocity"], 20)
        self.assertEqual(body["AngularVelocity"], -15)
        self.assertEqual(body["TimeMs"], 2000)

    # --- DriveTrack tests ---

    def test_drive_track_clamps_speeds(self):
        """Track speeds should be clamped to ±DRIVE_MAX_LINEAR_PCT."""
        self.ctrl.drive_track(100, -100)
        endpoint, body = self._post_calls[-1]
        self.assertEqual(endpoint, "/api/drive/track")
        self.assertEqual(body["LeftTrackSpeed"], self.ctrl.DRIVE_MAX_LINEAR_PCT)
        self.assertEqual(body["RightTrackSpeed"], -self.ctrl.DRIVE_MAX_LINEAR_PCT)

    def test_drive_track_valid_params(self):
        """Valid track speeds pass through."""
        self.ctrl.drive_track(20, 25)
        _, body = self._post_calls[-1]
        self.assertEqual(body["LeftTrackSpeed"], 20)
        self.assertEqual(body["RightTrackSpeed"], 25)

    # --- Halt / Stop tests ---

    def test_halt_calls_correct_endpoint(self):
        """halt() should call POST /api/halt."""
        self.ctrl.halt()
        endpoint, body = self._post_calls[-1]
        self.assertEqual(endpoint, "/api/halt")

    def test_stop_driving_calls_correct_endpoint(self):
        """stop_driving() should call POST /api/drive/stop."""
        self.ctrl.stop_driving()
        endpoint, body = self._post_calls[-1]
        self.assertEqual(endpoint, "/api/drive/stop")

    # --- DriveArc tests ---

    def test_drive_arc_clamps_duration(self):
        """DriveArc duration should be clamped."""
        self.ctrl.drive_arc(90, 0.5, 99999)
        endpoint, body = self._post_calls[-1]
        self.assertEqual(endpoint, "/api/drive/arc")
        self.assertEqual(body["TimeMs"], self.ctrl.DRIVE_MAX_DURATION_MS)
        self.assertEqual(body["Heading"], 90)
        self.assertEqual(body["Radius"], 0.5)
        self.assertFalse(body["Reverse"])

    def test_drive_arc_reverse(self):
        """DriveArc with reverse=True."""
        self.ctrl.drive_arc(180, 1.0, 3000, reverse=True)
        _, body = self._post_calls[-1]
        self.assertTrue(body["Reverse"])

    # --- DriveHeading tests ---

    def test_drive_heading_clamps_distance(self):
        """Distance should be clamped to max 1.0m."""
        self.ctrl.drive_heading(0, 5.0, 3000)
        endpoint, body = self._post_calls[-1]
        self.assertEqual(endpoint, "/api/drive/hdt")
        self.assertEqual(body["Distance"], 1.0)

    def test_drive_heading_clamps_min_distance(self):
        """Distance below 0.01m should be raised."""
        self.ctrl.drive_heading(0, 0.001, 1000)
        _, body = self._post_calls[-1]
        self.assertEqual(body["Distance"], 0.01)

    def test_drive_heading_valid_params(self):
        """Valid heading parameters pass through."""
        self.ctrl.drive_heading(45, 0.5, 2000, reverse=True)
        _, body = self._post_calls[-1]
        self.assertEqual(body["Heading"], 45)
        self.assertEqual(body["Distance"], 0.5)
        self.assertEqual(body["TimeMs"], 2000)
        self.assertTrue(body["Reverse"])


class TestHazardTelemetry(unittest.TestCase):
    """Unit tests for hazard/sensor telemetry subscription and handling (#49).

    These tests mock WebSocket — no live robot required.
    """

    _ctrl = None

    @classmethod
    def setUpClass(cls):
        try:
            _ctrl_path = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration")
            )
            if _ctrl_path not in sys.path:
                sys.path.insert(0, _ctrl_path)
            from misty_controller import (
                MistyController, HazardState, ToFReading,
                TOF_SENSORS, TOF_FORWARD_SENSORS, TOF_REVERSE_SENSORS,
                TELEMETRY_STALE_TIMEOUT_S,
            )
            cls._MistyController = MistyController
            cls._HazardState = HazardState
            cls._ToFReading = ToFReading
            cls._TOF_SENSORS = TOF_SENSORS
            cls._TOF_FORWARD_SENSORS = TOF_FORWARD_SENSORS
            cls._TOF_REVERSE_SENSORS = TOF_REVERSE_SENSORS
            cls._TELEMETRY_STALE_TIMEOUT_S = TELEMETRY_STALE_TIMEOUT_S
        except Exception as exc:
            cls._MistyController = None
            print(f"[TestHazardTelemetry] Could not import misty_controller: {exc}")

    def setUp(self):
        if self._MistyController is None:
            self.skipTest("misty_controller could not be imported")
        self.ctrl = self._MistyController()
        # Mock misty_post to capture calls
        self._post_calls = []
        self.ctrl.misty_post = lambda endpoint, body=None, timeout=5.0: (
            self._post_calls.append((endpoint, body)) or {"status": "Success", "result": True}
        )

    # --- HazardState initialization ---

    def test_hazard_state_initialized(self):
        """Controller should initialize with empty HazardState."""
        self.assertIsNotNone(self.ctrl.hazard)
        self.assertEqual(self.ctrl.hazard.active_hazards, [])
        self.assertEqual(self.ctrl.hazard.last_hazard_time, 0.0)
        self.assertFalse(self.ctrl.hazard.hazard_halt_issued)
        self.assertFalse(self.ctrl.hazard.any_bump_active)

    def test_tof_readings_initialized_for_all_sensors(self):
        """ToF readings dict should have entries for all 8 sensors."""
        self.assertEqual(len(self.ctrl.hazard.tof_readings), 8)
        for sid in self._TOF_SENSORS:
            self.assertIn(sid, self.ctrl.hazard.tof_readings)
            reading = self.ctrl.hazard.tof_readings[sid]
            self.assertEqual(reading.sensor_id, sid)
            self.assertFalse(reading.is_valid)

    # --- ToF event handling ---

    def test_handle_tof_event_updates_reading(self):
        """ToF event should update the per-sensor reading."""
        self.ctrl._handle_tof_event({
            "sensorId": "toffc",
            "distanceInMeters": 0.350,
            "status": 0,
        })
        reading = self.ctrl.hazard.tof_readings["toffc"]
        self.assertAlmostEqual(reading.distance_mm, 350.0, places=1)
        self.assertEqual(reading.status, 0)
        self.assertTrue(reading.is_valid)
        self.assertGreater(reading.last_updated, 0)

    def test_handle_tof_event_invalid_status(self):
        """ToF event with high status should mark reading as invalid."""
        self.ctrl._handle_tof_event({
            "sensorId": "toffr",
            "distanceInMeters": 0.100,
            "status": 255,
        })
        reading = self.ctrl.hazard.tof_readings["toffr"]
        self.assertFalse(reading.is_valid)
        self.assertEqual(reading.status, 255)

    def test_handle_tof_event_status_2_is_valid(self):
        """Status 2 (ranging complete) should be treated as valid."""
        self.ctrl._handle_tof_event({
            "sensorId": "toffl",
            "distanceInMeters": 0.500,
            "status": 2,
        })
        self.assertTrue(self.ctrl.hazard.tof_readings["toffl"].is_valid)

    def test_handle_tof_ignores_unknown_sensor(self):
        """Unknown sensor ID should be silently ignored."""
        self.ctrl._handle_tof_event({
            "sensorId": "unknown_sensor",
            "distanceInMeters": 0.100,
            "status": 0,
        })
        # No crash, no new entry
        self.assertNotIn("unknown_sensor", self.ctrl.hazard.tof_readings)

    # --- Hazard event handling ---

    def test_handle_hazard_event_with_bump(self):
        """HazardNotification with bump hazard should record active hazards."""
        self.ctrl._handle_hazard_event({
            "bumpSensorsHazardState": [
                {"sensorName": "Bump_FrontRight", "inHazard": True},
                {"sensorName": "Bump_FrontLeft", "inHazard": False},
            ],
            "timeOfFlightSensorsHazardState": [],
        })
        self.assertEqual(len(self.ctrl.hazard.active_hazards), 1)
        self.assertEqual(self.ctrl.hazard.active_hazards[0]["type"], "bump")
        self.assertTrue(self.ctrl.hazard.hazard_halt_issued)

    def test_handle_hazard_event_with_tof(self):
        """HazardNotification with ToF hazard should record active hazards."""
        self.ctrl._handle_hazard_event({
            "bumpSensorsHazardState": [],
            "timeOfFlightSensorsHazardState": [
                {"sensorName": "TOF_FrontCenter", "inHazard": True, "distance": 80},
            ],
        })
        self.assertEqual(len(self.ctrl.hazard.active_hazards), 1)
        self.assertEqual(self.ctrl.hazard.active_hazards[0]["type"], "tof")
        self.assertEqual(self.ctrl.hazard.active_hazards[0]["distance_mm"], 80)

    def test_handle_hazard_cleared(self):
        """HazardNotification with no active hazards should clear state."""
        # First, set a hazard
        self.ctrl._handle_hazard_event({
            "bumpSensorsHazardState": [{"sensorName": "Bump_FR", "inHazard": True}],
            "timeOfFlightSensorsHazardState": [],
        })
        self.assertTrue(self.ctrl.hazard.hazard_halt_issued)
        # Now clear it
        self.ctrl._handle_hazard_event({
            "bumpSensorsHazardState": [{"sensorName": "Bump_FR", "inHazard": False}],
            "timeOfFlightSensorsHazardState": [],
        })
        self.assertEqual(self.ctrl.hazard.active_hazards, [])
        self.assertFalse(self.ctrl.hazard.hazard_halt_issued)

    # --- Bump event handling ---

    def test_handle_bump_event_pressed(self):
        """Bump contact should be recorded and any_bump_active set."""
        self.ctrl._handle_bump_event({
            "sensorName": "Bump_FrontRight",
            "isContacted": True,
        })
        self.assertTrue(self.ctrl.hazard.any_bump_active)
        self.assertIn("Bump_FrontRight", self.ctrl.hazard.bump_states)
        self.assertTrue(self.ctrl.hazard.bump_states["Bump_FrontRight"]["is_pressed"])

    def test_handle_bump_event_released(self):
        """Bump release should clear that sensor."""
        # Press first
        self.ctrl._handle_bump_event({"sensorName": "Bump_FrontLeft", "isContacted": True})
        self.assertTrue(self.ctrl.hazard.any_bump_active)
        # Release
        self.ctrl._handle_bump_event({"sensorName": "Bump_FrontLeft", "isContacted": False})
        self.assertFalse(self.ctrl.hazard.any_bump_active)

    def test_multiple_bumps_any_active(self):
        """any_bump_active should be True if ANY bump is still pressed."""
        self.ctrl._handle_bump_event({"sensorName": "Bump_FR", "isContacted": True})
        self.ctrl._handle_bump_event({"sensorName": "Bump_FL", "isContacted": True})
        # Release one
        self.ctrl._handle_bump_event({"sensorName": "Bump_FR", "isContacted": False})
        self.assertTrue(self.ctrl.hazard.any_bump_active)  # FL still pressed

    # --- check_forward_clear ---

    def test_check_forward_clear_all_good(self):
        """Forward clear when all forward sensors have valid, distant readings."""
        now = time.time()
        for sid in self._TOF_FORWARD_SENSORS:
            with self.ctrl.hazard_lock:
                r = self.ctrl.hazard.tof_readings[sid]
                r.distance_mm = 500.0
                r.status = 0
                r.is_valid = True
                r.last_updated = now
        self.assertTrue(self.ctrl.check_forward_clear())

    def test_check_forward_clear_close_obstacle(self):
        """Forward NOT clear when a front sensor detects close obstacle."""
        now = time.time()
        for sid in self._TOF_FORWARD_SENSORS:
            with self.ctrl.hazard_lock:
                r = self.ctrl.hazard.tof_readings[sid]
                r.distance_mm = 500.0
                r.status = 0
                r.is_valid = True
                r.last_updated = now
        # Place obstacle in front center
        with self.ctrl.hazard_lock:
            self.ctrl.hazard.tof_readings["toffc"].distance_mm = 100.0
        self.assertFalse(self.ctrl.check_forward_clear())

    def test_check_forward_clear_stale_data(self):
        """Forward NOT clear when sensor data is stale (fail closed)."""
        # Leave last_updated at 0 (stale)
        self.assertFalse(self.ctrl.check_forward_clear())

    def test_check_forward_clear_invalid_sensor(self):
        """Forward NOT clear when a sensor has invalid status."""
        now = time.time()
        for sid in self._TOF_FORWARD_SENSORS:
            with self.ctrl.hazard_lock:
                r = self.ctrl.hazard.tof_readings[sid]
                r.distance_mm = 500.0
                r.status = 0
                r.is_valid = True
                r.last_updated = now
        # Invalidate one sensor
        with self.ctrl.hazard_lock:
            self.ctrl.hazard.tof_readings["toffl"].is_valid = False
        self.assertFalse(self.ctrl.check_forward_clear())

    # --- check_reverse_clear ---

    def test_check_reverse_clear_all_good(self):
        """Reverse clear when rear sensors have valid, distant readings."""
        now = time.time()
        for sid in self._TOF_REVERSE_SENSORS:
            with self.ctrl.hazard_lock:
                r = self.ctrl.hazard.tof_readings[sid]
                r.distance_mm = 400.0
                r.status = 0
                r.is_valid = True
                r.last_updated = now
        self.assertTrue(self.ctrl.check_reverse_clear())

    def test_check_reverse_clear_obstacle_behind(self):
        """Reverse NOT clear when rear sensor detects obstacle."""
        now = time.time()
        for sid in self._TOF_REVERSE_SENSORS:
            with self.ctrl.hazard_lock:
                r = self.ctrl.hazard.tof_readings[sid]
                r.distance_mm = 400.0
                r.status = 0
                r.is_valid = True
                r.last_updated = now
        with self.ctrl.hazard_lock:
            self.ctrl.hazard.tof_readings["tofr"].distance_mm = 50.0
        self.assertFalse(self.ctrl.check_reverse_clear())

    # --- check_sensors_fresh ---

    def test_check_sensors_fresh_all_recent(self):
        """All sensors fresh when recently updated."""
        now = time.time()
        for sid in self._TOF_SENSORS:
            with self.ctrl.hazard_lock:
                self.ctrl.hazard.tof_readings[sid].last_updated = now
        self.assertTrue(self.ctrl.check_sensors_fresh())

    def test_check_sensors_fresh_one_stale(self):
        """Not fresh if any sensor is stale."""
        now = time.time()
        for sid in self._TOF_SENSORS:
            with self.ctrl.hazard_lock:
                self.ctrl.hazard.tof_readings[sid].last_updated = now
        # Make one stale
        with self.ctrl.hazard_lock:
            self.ctrl.hazard.tof_readings["tofr"].last_updated = now - 10.0
        self.assertFalse(self.ctrl.check_sensors_fresh())

    def test_check_sensors_fresh_subset(self):
        """Can check freshness of specific sensor subset."""
        now = time.time()
        with self.ctrl.hazard_lock:
            self.ctrl.hazard.tof_readings["toffc"].last_updated = now
            self.ctrl.hazard.tof_readings["toffr"].last_updated = now
        self.assertTrue(self.ctrl.check_sensors_fresh({"toffc", "toffr"}))
        # But full set would fail
        self.assertFalse(self.ctrl.check_sensors_fresh())

    # --- get_hazard_snapshot ---

    def test_get_hazard_snapshot_returns_dict(self):
        """Snapshot should return a dict with expected keys."""
        snapshot = self.ctrl.get_hazard_snapshot()
        self.assertIsInstance(snapshot, dict)
        self.assertIn("active_hazards", snapshot)
        self.assertIn("tof_readings", snapshot)
        self.assertIn("bump_states", snapshot)
        self.assertIn("any_bump_active", snapshot)
        self.assertEqual(len(snapshot["tof_readings"]), 8)

    def test_get_hazard_snapshot_includes_friendly_names(self):
        """Snapshot ToF readings should include friendly sensor names."""
        snapshot = self.ctrl.get_hazard_snapshot()
        self.assertEqual(snapshot["tof_readings"]["toffc"]["friendly_name"], "front_center")
        self.assertEqual(snapshot["tof_readings"]["tofr"]["friendly_name"], "rear")

    # --- WebSocket subscription methods ---

    def test_ws_subscribe_hazard_sends_message(self):
        """_ws_subscribe_hazard should send subscribe JSON via WebSocket."""
        sent_messages = []
        mock_ws = unittest.mock.MagicMock()
        mock_ws.send = lambda msg: sent_messages.append(msg)
        self.ctrl.ws = mock_ws

        self.ctrl._ws_subscribe_hazard()

        # Should have sent unsubscribe(s) + subscribe
        subscribe_msgs = [m for m in sent_messages if "subscribe" in m.lower()]
        self.assertTrue(len(subscribe_msgs) >= 1)
        # Last message should be the subscribe
        last = json.loads(subscribe_msgs[-1])
        self.assertEqual(last["Operation"], "subscribe")
        self.assertEqual(last["Type"], "HazardNotification")
        self.assertEqual(last["DebounceMs"], 0)

    def test_ws_subscribe_tof_sends_message(self):
        """_ws_subscribe_tof should send subscribe JSON with 250ms debounce."""
        sent_messages = []
        mock_ws = unittest.mock.MagicMock()
        mock_ws.send = lambda msg: sent_messages.append(msg)
        self.ctrl.ws = mock_ws

        self.ctrl._ws_subscribe_tof()

        subscribe_msgs = [m for m in sent_messages if '"subscribe"' in m.lower()]
        last = json.loads(subscribe_msgs[-1])
        self.assertEqual(last["Type"], "TimeOfFlight")
        self.assertEqual(last["DebounceMs"], 250)

    def test_ws_subscribe_bump_sends_message(self):
        """_ws_subscribe_bump should send subscribe JSON with 0 debounce."""
        sent_messages = []
        mock_ws = unittest.mock.MagicMock()
        mock_ws.send = lambda msg: sent_messages.append(msg)
        self.ctrl.ws = mock_ws

        self.ctrl._ws_subscribe_bump()

        subscribe_msgs = [m for m in sent_messages if '"subscribe"' in m.lower()]
        last = json.loads(subscribe_msgs[-1])
        self.assertEqual(last["Type"], "BumpSensor")
        self.assertEqual(last["DebounceMs"], 0)

    def test_ws_subscribe_uses_unique_names(self):
        """Consecutive subscriptions should use different event names."""
        mock_ws = unittest.mock.MagicMock()
        self.ctrl.ws = mock_ws

        self.ctrl._ws_subscribe_hazard()
        name1 = self.ctrl._hazard_event_name

        time.sleep(0.001)  # ensure time_ns differs
        self.ctrl._ws_subscribe_hazard()
        name2 = self.ctrl._hazard_event_name

        self.assertNotEqual(name1, name2)


class TestMovingState(unittest.TestCase):
    """Unit tests for MOVING state and preemption logic (#50).

    These tests mock HTTP calls — no live robot required.
    """

    @classmethod
    def setUpClass(cls):
        try:
            _ctrl_path = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration")
            )
            if _ctrl_path not in sys.path:
                sys.path.insert(0, _ctrl_path)
            from misty_controller import (
                MistyController, State, PREEMPTION_PRIORITY,
                BATTERY_MOVEMENT_CUTOFF, BATTERY_MOVEMENT_VOLTAGE_MIN,
                BATTERY_VOLTAGE_DROP_HALT,
            )
            cls._MistyController = MistyController
            cls._State = State
            cls._PREEMPTION_PRIORITY = PREEMPTION_PRIORITY
            cls._BATTERY_MOVEMENT_CUTOFF = BATTERY_MOVEMENT_CUTOFF
            cls._BATTERY_MOVEMENT_VOLTAGE_MIN = BATTERY_MOVEMENT_VOLTAGE_MIN
            cls._BATTERY_VOLTAGE_DROP_HALT = BATTERY_VOLTAGE_DROP_HALT
        except Exception as exc:
            cls._MistyController = None
            print(f"[TestMovingState] Could not import misty_controller: {exc}")

    def setUp(self):
        if self._MistyController is None:
            self.skipTest("misty_controller could not be imported")
        self.ctrl = self._MistyController()
        self._post_calls = []
        self.ctrl.misty_post = lambda endpoint, body=None, timeout=5.0: (
            self._post_calls.append((endpoint, body)) or {"status": "Success", "result": True}
        )

    # --- State enum ---

    def test_moving_state_exists(self):
        """MOVING should be a valid state."""
        self.assertEqual(self._State.MOVING.value, "MOVING")

    def test_preemption_priority_defined(self):
        """Preemption priority list should be defined."""
        self.assertIn("hazard_stop", self._PREEMPTION_PRIORITY)
        self.assertIn("bump_contact", self._PREEMPTION_PRIORITY)
        self.assertIn("wake_word", self._PREEMPTION_PRIORITY)
        self.assertIn("move_complete", self._PREEMPTION_PRIORITY)

    # --- start_moving ---

    def test_start_moving_from_idle(self):
        """start_moving should succeed from IDLE state."""
        self.ctrl.set_state(self._State.IDLE)
        result = self.ctrl.start_moving(reason="test")
        self.assertTrue(result)
        self.assertEqual(self.ctrl.get_state(), self._State.MOVING)

    def test_start_moving_blocked_not_idle(self):
        """start_moving should fail if not in IDLE state."""
        self.ctrl.set_state(self._State.RECORDING)
        result = self.ctrl.start_moving(reason="test")
        self.assertFalse(result)
        self.assertEqual(self.ctrl.get_state(), self._State.RECORDING)

    def test_start_moving_blocked_active_hazard(self):
        """start_moving should fail if hazards are active."""
        self.ctrl.set_state(self._State.IDLE)
        with self.ctrl.hazard_lock:
            self.ctrl.hazard.active_hazards = [{"type": "tof", "sensor": "front"}]
        result = self.ctrl.start_moving(reason="test")
        self.assertFalse(result)
        self.assertEqual(self.ctrl.get_state(), self._State.IDLE)

    def test_start_moving_blocked_bump_active(self):
        """start_moving should fail if any bump sensor is active."""
        self.ctrl.set_state(self._State.IDLE)
        with self.ctrl.hazard_lock:
            self.ctrl.hazard.any_bump_active = True
        result = self.ctrl.start_moving(reason="test")
        self.assertFalse(result)

    def test_start_moving_blocked_battery_critical(self):
        """start_moving should fail if battery is below movement cutoff (25%)."""
        self.ctrl.set_state(self._State.IDLE)
        with self.ctrl.battery_lock:
            self.ctrl.battery.charge_percent = 0.20  # below 25% movement cutoff
            self.ctrl.battery.last_updated = time.time()
        result = self.ctrl.start_moving(reason="test")
        self.assertFalse(result)

    def test_start_moving_blocked_low_voltage(self):
        """start_moving should fail if voltage is below movement minimum (#52)."""
        self.ctrl.set_state(self._State.IDLE)
        with self.ctrl.battery_lock:
            self.ctrl.battery.charge_percent = 0.30  # above percentage cutoff
            self.ctrl.battery.voltage = 7.2  # below 7.5V voltage minimum
            self.ctrl.battery.last_updated = time.time()
        result = self.ctrl.start_moving(reason="test")
        self.assertFalse(result)

    def test_start_moving_ok_with_good_battery(self):
        """start_moving should succeed with good battery levels."""
        self.ctrl.set_state(self._State.IDLE)
        with self.ctrl.battery_lock:
            self.ctrl.battery.charge_percent = 0.50
            self.ctrl.battery.voltage = 8.0
            self.ctrl.battery.last_updated = time.time()
        result = self.ctrl.start_moving(reason="test")
        self.assertTrue(result)

    # --- stop_moving ---

    def test_stop_moving_halts_and_returns_to_idle(self):
        """stop_moving should halt motors and return to IDLE."""
        self.ctrl.set_state(self._State.MOVING)
        self.ctrl.stop_moving(reason="move_complete")
        self.assertEqual(self.ctrl.get_state(), self._State.IDLE)
        # Should have called /api/halt
        halt_calls = [c for c in self._post_calls if c[0] == "/api/halt"]
        self.assertEqual(len(halt_calls), 1)

    def test_stop_moving_noop_if_not_moving(self):
        """stop_moving should do nothing if not in MOVING state."""
        self.ctrl.set_state(self._State.IDLE)
        self.ctrl.stop_moving(reason="test")
        # No halt call expected
        halt_calls = [c for c in self._post_calls if c[0] == "/api/halt"]
        self.assertEqual(len(halt_calls), 0)

    # --- preempt_movement ---

    def test_preempt_halts_and_returns_to_idle(self):
        """preempt_movement should halt motors and return to IDLE."""
        self.ctrl.set_state(self._State.MOVING)
        self.ctrl.preempt_movement("hazard_stop")
        self.assertEqual(self.ctrl.get_state(), self._State.IDLE)
        halt_calls = [c for c in self._post_calls if c[0] == "/api/halt"]
        self.assertEqual(len(halt_calls), 1)

    def test_preempt_noop_if_not_moving(self):
        """preempt_movement should do nothing if not in MOVING state."""
        self.ctrl.set_state(self._State.IDLE)
        self.ctrl.preempt_movement("hazard_stop")
        halt_calls = [c for c in self._post_calls if c[0] == "/api/halt"]
        self.assertEqual(len(halt_calls), 0)

    # --- Hazard preemption integration ---

    def test_hazard_event_preempts_movement(self):
        """Active hazard during MOVING should preempt movement."""
        self.ctrl.set_state(self._State.MOVING)
        self.ctrl._handle_hazard_event({
            "bumpSensorsHazardState": [],
            "timeOfFlightSensorsHazardState": [
                {"sensorName": "TOF_FC", "inHazard": True, "distance": 50},
            ],
        })
        self.assertEqual(self.ctrl.get_state(), self._State.IDLE)

    def test_hazard_cleared_no_preempt(self):
        """Hazard cleared event should NOT affect MOVING state."""
        self.ctrl.set_state(self._State.MOVING)
        self.ctrl._handle_hazard_event({
            "bumpSensorsHazardState": [],
            "timeOfFlightSensorsHazardState": [
                {"sensorName": "TOF_FC", "inHazard": False},
            ],
        })
        self.assertEqual(self.ctrl.get_state(), self._State.MOVING)

    # --- Bump preemption integration ---

    def test_bump_event_preempts_movement(self):
        """Bump contact during MOVING should preempt movement."""
        self.ctrl.set_state(self._State.MOVING)
        self.ctrl._handle_bump_event({
            "sensorName": "Bump_FrontRight",
            "isContacted": True,
        })
        self.assertEqual(self.ctrl.get_state(), self._State.IDLE)

    def test_bump_release_no_preempt(self):
        """Bump release should NOT preempt movement."""
        self.ctrl.set_state(self._State.MOVING)
        self.ctrl._handle_bump_event({
            "sensorName": "Bump_FrontRight",
            "isContacted": False,
        })
        self.assertEqual(self.ctrl.get_state(), self._State.MOVING)

    # --- Battery preemption during movement (#52) ---

    def test_battery_low_preempts_movement(self):
        """Battery dropping below movement cutoff during MOVING should preempt."""
        self.ctrl.set_state(self._State.MOVING)
        from misty_controller import BatteryState
        b = BatteryState(charge_percent=0.20, voltage=7.8, last_updated=time.time())
        self.ctrl._evaluate_battery_thresholds(b)
        self.assertEqual(self.ctrl.get_state(), self._State.IDLE)

    def test_battery_voltage_low_preempts_movement(self):
        """Voltage below minimum during MOVING should preempt."""
        self.ctrl.set_state(self._State.MOVING)
        from misty_controller import BatteryState
        b = BatteryState(charge_percent=0.30, voltage=7.2, last_updated=time.time())
        self.ctrl._evaluate_battery_thresholds(b)
        self.assertEqual(self.ctrl.get_state(), self._State.IDLE)

    def test_battery_voltage_drop_preempts_movement(self):
        """Rapid voltage drop during MOVING should preempt."""
        self.ctrl.set_state(self._State.MOVING)
        self.ctrl._last_battery_voltage = 8.0
        from misty_controller import BatteryState
        # Drop of 0.4V (> 0.3V threshold)
        b = BatteryState(charge_percent=0.40, voltage=7.6, last_updated=time.time())
        self.ctrl._evaluate_battery_thresholds(b)
        self.assertEqual(self.ctrl.get_state(), self._State.IDLE)

    def test_battery_ok_no_preempt_during_movement(self):
        """Normal battery during MOVING should not preempt."""
        self.ctrl.set_state(self._State.MOVING)
        self.ctrl._last_battery_voltage = 8.0
        from misty_controller import BatteryState
        b = BatteryState(charge_percent=0.50, voltage=7.9, last_updated=time.time())
        self.ctrl._evaluate_battery_thresholds(b)
        self.assertEqual(self.ctrl.get_state(), self._State.MOVING)

    # --- Wake word pause/resume (#53) ---

    def test_start_moving_pauses_wake_word(self):
        """start_moving should pause the wake word listener."""
        self.ctrl.set_state(self._State.IDLE)
        pause_called = []
        mock_listener = unittest.mock.MagicMock()
        mock_listener.pause = lambda: pause_called.append(True)
        self.ctrl._wake_word_listener = mock_listener

        self.ctrl.start_moving(reason="test")
        self.assertTrue(pause_called)

    def test_stop_moving_resumes_wake_word(self):
        """stop_moving should resume the wake word listener after settle."""
        self.ctrl.set_state(self._State.MOVING)
        resume_called = []
        mock_listener = unittest.mock.MagicMock()
        mock_listener.resume = lambda: resume_called.append(True)
        self.ctrl._wake_word_listener = mock_listener
        # Override settle time for test speed
        self.ctrl.MOVEMENT_SETTLE_MS = 10

        self.ctrl.stop_moving(reason="test")
        time.sleep(0.1)  # allow thread to complete
        self.assertTrue(resume_called)


class TestTeleopEndpoint(unittest.TestCase):
    """Unit tests for the HTTP teleop endpoint (#51).

    Tests the ControllerAPIHandler's /api/move endpoint logic.
    These tests call the controller methods directly (not via HTTP),
    verifying parameter validation and movement commands.
    """

    @classmethod
    def setUpClass(cls):
        try:
            _ctrl_path = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration")
            )
            if _ctrl_path not in sys.path:
                sys.path.insert(0, _ctrl_path)
            from misty_controller import MistyController, State
            cls._MistyController = MistyController
            cls._State = State
        except Exception as exc:
            cls._MistyController = None
            print(f"[TestTeleopEndpoint] Could not import misty_controller: {exc}")

    def setUp(self):
        if self._MistyController is None:
            self.skipTest("misty_controller could not be imported")
        self.ctrl = self._MistyController()
        self._post_calls = []
        self.ctrl.misty_post = lambda endpoint, body=None, timeout=5.0: (
            self._post_calls.append((endpoint, body)) or {"status": "Success", "result": True}
        )

    def test_halt_command_immediate(self):
        """Halt command should issue halt regardless of state."""
        self.ctrl.halt()
        halt_calls = [c for c in self._post_calls if c[0] == "/api/halt"]
        self.assertEqual(len(halt_calls), 1)

    def test_forward_movement_via_drive_time(self):
        """Forward command should use drive_time with positive velocity."""
        self.ctrl.set_state(self._State.IDLE)
        self.ctrl.start_moving(reason="teleop_forward")
        self.assertEqual(self.ctrl.get_state(), self._State.MOVING)
        # Execute forward drive
        self.ctrl.drive_time(20, 0, 1000)
        drive_calls = [c for c in self._post_calls if c[0] == "/api/drive/time"]
        self.assertEqual(len(drive_calls), 1)
        _, body = drive_calls[0]
        self.assertEqual(body["LinearVelocity"], 20)
        self.assertEqual(body["AngularVelocity"], 0)
        self.assertEqual(body["TimeMs"], 1000)

    def test_backward_movement_via_drive_time(self):
        """Backward command should use drive_time with negative velocity."""
        self.ctrl.set_state(self._State.IDLE)
        self.ctrl.start_moving(reason="teleop_backward")
        self.ctrl.drive_time(-15, 0, 800)
        drive_calls = [c for c in self._post_calls if c[0] == "/api/drive/time"]
        self.assertEqual(len(drive_calls), 1)
        _, body = drive_calls[0]
        self.assertEqual(body["LinearVelocity"], -15)

    def test_rotate_movement_via_drive_time(self):
        """Rotate command should use drive_time with angular only."""
        self.ctrl.set_state(self._State.IDLE)
        self.ctrl.start_moving(reason="teleop_rotate")
        self.ctrl.drive_time(0, 20, 1500)
        drive_calls = [c for c in self._post_calls if c[0] == "/api/drive/time"]
        _, body = drive_calls[0]
        self.assertEqual(body["LinearVelocity"], 0)
        self.assertEqual(body["AngularVelocity"], 20)

    def test_cannot_move_when_not_idle(self):
        """Movement should be rejected when not in IDLE state."""
        self.ctrl.set_state(self._State.RECORDING)
        result = self.ctrl.start_moving(reason="teleop")
        self.assertFalse(result)

    def test_speed_clamped_to_max(self):
        """Speed above max should be clamped by drive methods."""
        self.ctrl.set_state(self._State.IDLE)
        self.ctrl.start_moving(reason="test")
        self.ctrl.drive_time(50, 0, 1000)  # 50 > max 30
        drive_calls = [c for c in self._post_calls if c[0] == "/api/drive/time"]
        _, body = drive_calls[0]
        self.assertEqual(body["LinearVelocity"], self.ctrl.DRIVE_MAX_LINEAR_PCT)

    def test_sensors_endpoint_returns_snapshot(self):
        """get_hazard_snapshot should return usable data for /api/sensors."""
        snapshot = self.ctrl.get_hazard_snapshot()
        self.assertIn("tof_readings", snapshot)
        self.assertIn("bump_states", snapshot)
        self.assertIn("active_hazards", snapshot)
        self.assertEqual(len(snapshot["tof_readings"]), 8)


class TestSpeakMoveIntegration(unittest.TestCase):
    """Unit tests for combined speak + move responses (#56).

    Validates that movement responses from orchestration are correctly
    detected, acknowledgment audio is played, and movement is executed.
    Uses mocked HTTP and state machine.
    """

    _ctrl = None
    _ctrl_mod = None
    _State = None

    @classmethod
    def setUpClass(cls):
        try:
            _ctrl_path = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration")
            )
            if _ctrl_path not in sys.path:
                sys.path.insert(0, _ctrl_path)
            import misty_controller as _ctrl_mod
            cls._ctrl_mod = _ctrl_mod
            cls._State = _ctrl_mod.State
        except Exception as e:
            raise unittest.SkipTest(f"Cannot import misty_controller: {e}")

    def setUp(self):
        """Create controller with mocked HTTP and WebSocket."""
        self._post_calls = []

        def mock_post(path, body=None, timeout=None):
            self._post_calls.append((path, body))
            return {"status": "Success"}

        self.ctrl = self._ctrl_mod.MistyController.__new__(self._ctrl_mod.MistyController)
        self.ctrl.misty_ip = "10.0.0.99"
        self.ctrl.state = self._State.IDLE
        self.ctrl.state_lock = threading.Lock()
        self.ctrl.battery = self._ctrl_mod.BatteryState()
        self.ctrl.battery_lock = threading.Lock()
        self.ctrl.battery.charge_percent = 0.50
        self.ctrl.battery.voltage = 8.0
        self.ctrl.battery.last_updated = time.time()
        self.ctrl.hazard = self._ctrl_mod.HazardState()
        self.ctrl.hazard_lock = threading.Lock()
        self.ctrl.last_activity_time = time.time()
        self.ctrl._wake_word_listener = None
        self.ctrl.misty_post = mock_post
        self.ctrl.DRIVE_MAX_DURATION_MS = 3000
        self.ctrl.MOVEMENT_SETTLE_MS = 100  # fast for tests

    # --- _do_orchestrate_and_respond movement detection ---

    def test_movement_response_detected(self):
        """When orchestrate returns type=movement, return dict with movement info."""
        movement_result = {
            "status": "ok",
            "type": "movement",
            "movement": {"command": "forward"},
            "user_text": "come here",
            "response_text": "On my way!",
            "pipeline_ms": 150,
        }
        # Mock requests.post and requests.get
        import unittest.mock as mock
        mock_resp = mock.MagicMock()
        mock_resp.json.return_value = movement_result
        mock_resp.status_code = 200

        with mock.patch("requests.post", return_value=mock_resp):
            result = self.ctrl._do_orchestrate_and_respond(1, b"fake_audio", time.time())

        self.assertIsInstance(result, dict)
        self.assertTrue(result["had_speech"])
        self.assertEqual(result["movement"]["command"], "forward")

    def test_normal_response_returns_true(self):
        """Normal conversational response should return True (not a dict)."""
        normal_result = {
            "status": "ok",
            "transcribedText": "hello",
            "inferenceResponse": "Hi there!",
            "responseAudio": "/api/audio/resp.wav",
            "latencyMs": 500,
        }
        import unittest.mock as mock
        mock_post_resp = mock.MagicMock()
        mock_post_resp.json.return_value = normal_result
        mock_post_resp.status_code = 200

        mock_get_resp = mock.MagicMock()
        mock_get_resp.content = b"\x00" * 1000  # fake WAV data
        mock_get_resp.raise_for_status = mock.MagicMock()

        # Mock upload_and_play_audio to return short duration
        self.ctrl.upload_and_play_audio = mock.MagicMock(return_value=0.1)
        self.ctrl.set_led = mock.MagicMock()
        self.ctrl.display_image = mock.MagicMock()
        self.ctrl.move_head = mock.MagicMock()

        with mock.patch("requests.post", return_value=mock_post_resp), \
             mock.patch("requests.get", return_value=mock_get_resp), \
             mock.patch("time.sleep"):
            result = self.ctrl._do_orchestrate_and_respond(1, b"fake", time.time())

        self.assertTrue(result)
        self.assertNotIsInstance(result, dict)

    def test_empty_stt_returns_false(self):
        """Empty STT should return False."""
        import unittest.mock as mock
        mock_resp = mock.MagicMock()
        mock_resp.json.return_value = {"status": "error", "error": "empty_stt"}
        mock_resp.status_code = 400

        with mock.patch("requests.post", return_value=mock_resp):
            result = self.ctrl._do_orchestrate_and_respond(1, b"fake", time.time())

        self.assertFalse(result)

    # --- _execute_voice_movement ---

    def test_voice_movement_forward(self):
        """Voice forward command should call drive_time with positive linear."""
        import unittest.mock as mock

        self.ctrl.set_led = mock.MagicMock()
        self.ctrl.display_image = mock.MagicMock()
        self.ctrl.halt = mock.MagicMock()
        self.ctrl.drive_time = mock.MagicMock()

        movement = {"command": "forward", "distance_mm": 200, "speed_pct": 20}
        self.ctrl._execute_voice_movement(1, movement)

        # Should have called drive_time with positive linear velocity
        self.ctrl.drive_time.assert_called_once()
        args = self.ctrl.drive_time.call_args[0]
        self.assertGreater(args[0], 0)  # positive linear
        self.assertEqual(args[1], 0)     # no angular

    def test_voice_movement_stop_halts_immediately(self):
        """Voice stop command should halt immediately without state transition."""
        import unittest.mock as mock
        self.ctrl.halt = mock.MagicMock()
        self.ctrl._execute_voice_movement(1, {"command": "stop"})
        self.ctrl.halt.assert_called_once()

    def test_voice_movement_blocked_by_hazard(self):
        """Voice movement should be blocked when hazards are active."""
        import unittest.mock as mock

        self.ctrl.set_led = mock.MagicMock()
        self.ctrl.display_image = mock.MagicMock()

        # Set active hazard
        with self.ctrl.hazard_lock:
            self.ctrl.hazard.active_hazards = ["TOF_FrontCenter"]

        self.ctrl._speak_movement_failure = mock.MagicMock()
        self.ctrl._execute_voice_movement(1, {"command": "forward"})

        # Should have called failure speech, not drive
        self.ctrl._speak_movement_failure.assert_called_once()

    def test_voice_movement_clamps_speed(self):
        """Voice movement should clamp speed to DRIVE_MAX_LINEAR_PCT."""
        import unittest.mock as mock

        self.ctrl.set_led = mock.MagicMock()
        self.ctrl.display_image = mock.MagicMock()
        self.ctrl.halt = mock.MagicMock()
        self.ctrl.drive_time = mock.MagicMock()

        # Request 50% speed — should be clamped to DRIVE_MAX_LINEAR_PCT (30)
        movement = {"command": "forward", "distance_mm": 200, "speed_pct": 50}
        self.ctrl._execute_voice_movement(1, movement)

        # The speed_pct clamped internally, but drive_time gets the clamped value
        self.ctrl.drive_time.assert_called_once()
        args = self.ctrl.drive_time.call_args[0]
        self.assertLessEqual(args[0], self._ctrl_mod.MistyController.DRIVE_MAX_LINEAR_PCT)

    def test_voice_movement_backward(self):
        """Voice backward command should use negative linear velocity."""
        import unittest.mock as mock

        self.ctrl.set_led = mock.MagicMock()
        self.ctrl.display_image = mock.MagicMock()
        self.ctrl.halt = mock.MagicMock()
        self.ctrl.drive_time = mock.MagicMock()

        self.ctrl._execute_voice_movement(1, {"command": "backward", "distance_mm": 150})

        self.ctrl.drive_time.assert_called_once()
        args = self.ctrl.drive_time.call_args[0]
        self.assertLess(args[0], 0)  # negative linear

    def test_voice_movement_rotate(self):
        """Voice rotate command should use angular velocity, zero linear."""
        import unittest.mock as mock

        self.ctrl.set_led = mock.MagicMock()
        self.ctrl.display_image = mock.MagicMock()
        self.ctrl.halt = mock.MagicMock()
        self.ctrl.drive_time = mock.MagicMock()

        self.ctrl._execute_voice_movement(1, {"command": "rotate_left", "angle_deg": 90})

        self.ctrl.drive_time.assert_called_once()
        args = self.ctrl.drive_time.call_args[0]
        self.assertEqual(args[0], 0)     # no linear
        self.assertGreater(args[1], 0)   # positive angular

    # --- _wait_for_move_completion ---

    def test_wait_for_move_returns_false_on_normal_completion(self):
        """Should return False (not preempted) if state stays MOVING until timeout."""
        self.ctrl.set_state(self._State.MOVING)
        result = self.ctrl._wait_for_move_completion(0.3)
        self.assertFalse(result)  # completed normally

    def test_wait_for_move_returns_true_on_preemption(self):
        """Should return True if state changes from MOVING (preempted)."""
        self.ctrl.set_state(self._State.MOVING)

        def preempt_after_delay():
            time.sleep(0.1)
            self.ctrl.set_state(self._State.IDLE)

        threading.Thread(target=preempt_after_delay, daemon=True).start()
        result = self.ctrl._wait_for_move_completion(2.0)
        self.assertTrue(result)  # preempted

    # --- State machine transition tests (Plan 3.3 from #60) ---

    def test_move_rejected_during_recording(self):
        """Move should be rejected when state is RECORDING (Plan 3.3)."""
        self.ctrl.set_state(self._State.RECORDING)
        result = self.ctrl.start_moving(reason="test")
        self.assertFalse(result)
        self.assertEqual(self.ctrl.get_state(), self._State.RECORDING)

    def test_move_rejected_during_playing(self):
        """Move should be rejected when state is PLAYING (Plan 3.3)."""
        self.ctrl.set_state(self._State.PLAYING)
        result = self.ctrl.start_moving(reason="test")
        self.assertFalse(result)
        self.assertEqual(self.ctrl.get_state(), self._State.PLAYING)

    def test_move_rejected_during_processing(self):
        """Move should be rejected when state is PROCESSING."""
        self.ctrl.set_state(self._State.PROCESSING)
        result = self.ctrl.start_moving(reason="test")
        self.assertFalse(result)

    def test_move_rejected_during_rearming(self):
        """Move should be rejected when state is REARMING."""
        self.ctrl.set_state(self._State.REARMING)
        result = self.ctrl.start_moving(reason="test")
        self.assertFalse(result)

    def test_idle_to_moving_to_idle_clean_cycle(self):
        """Full IDLE -> MOVING -> IDLE cycle (Plan 3.3)."""
        self.assertEqual(self.ctrl.get_state(), self._State.IDLE)
        ok = self.ctrl.start_moving(reason="test")
        self.assertTrue(ok)
        self.assertEqual(self.ctrl.get_state(), self._State.MOVING)
        self.ctrl.stop_moving(reason="complete")
        self.assertEqual(self.ctrl.get_state(), self._State.IDLE)

    def test_sequential_move_cycles(self):
        """Multiple IDLE -> MOVING -> IDLE cycles should all succeed."""
        for i in range(3):
            self.ctrl.set_state(self._State.IDLE)
            ok = self.ctrl.start_moving(reason=f"cycle_{i}")
            self.assertTrue(ok, f"Cycle {i} failed to start")
            self.ctrl.stop_moving(reason="complete")
            self.assertEqual(self.ctrl.get_state(), self._State.IDLE)


if __name__ == "__main__":
    unittest.main()

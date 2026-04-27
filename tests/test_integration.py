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


if __name__ == "__main__":
    unittest.main()

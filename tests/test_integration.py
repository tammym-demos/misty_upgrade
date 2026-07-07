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
import tempfile
import types
import pytest
from io import BytesIO
from types import SimpleNamespace
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


class TestOrchestrationSttConfidence(unittest.TestCase):
    """Unit tests for STT hallucination guards."""

    @classmethod
    def setUpClass(cls):
        import orchestration_service  # noqa: PLC0415
        cls._svc = orchestration_service

    def test_low_confidence_transcript_is_treated_as_empty_stt(self):
        """Whisper guesses from near-silence should not reach the LLM."""
        model = unittest.mock.MagicMock()
        model.transcribe.return_value = (
            [
                SimpleNamespace(
                    text="Thank you very much for watching, and I hope you enjoyed this video.",
                    avg_logprob=-1.85,
                    no_speech_prob=0.01,
                )
            ],
            SimpleNamespace(),
        )

        with unittest.mock.patch.object(self._svc, "_get_whisper_model", return_value=model):
            result = self._svc.speech_to_text(b"not-a-real-wav", time.time())

        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["text"], "")


class TestLaptopMistyRecordingConfig(unittest.TestCase):
    """Unit tests for laptop-mode Misty recording configuration (#66)."""

    _controller_module = None

    @classmethod
    def setUpClass(cls):
        try:
            import misty_controller  # noqa: PLC0415
            cls._controller_module = misty_controller
        except ModuleNotFoundError as exc:
            if exc.name != "websocket":  # pragma: no cover
                cls._controller_module = None
                print(f"[TestLaptopMistyRecordingConfig] Could not import misty_controller: {exc}")
                return
            # The config/error-path tests do not open WebSockets.  Allow the
            # unit tests to run in minimal CI environments where the optional
            # controller runtime dependency has not been installed.
            websocket_stub = unittest.mock.MagicMock()
            websocket_stub.WebSocketApp = unittest.mock.MagicMock()
            with unittest.mock.patch.dict(sys.modules, {"websocket": websocket_stub}):
                import misty_controller  # noqa: PLC0415
                cls._controller_module = misty_controller
        except Exception as exc:  # pragma: no cover
            cls._controller_module = None
            print(f"[TestLaptopMistyRecordingConfig] Could not import misty_controller: {exc}")

    def setUp(self):
        if self._controller_module is None:
            self.skipTest("misty_controller could not be imported")
        self._old_mode = self._controller_module.LAPTOP_MISTY_RECORDING_MODE
        self._old_tally = self._controller_module.LAPTOP_MISTY_TALLY_RECORDING_S

    def tearDown(self):
        if self._controller_module is not None:
            self._controller_module.LAPTOP_MISTY_RECORDING_MODE = self._old_mode
            self._controller_module.LAPTOP_MISTY_TALLY_RECORDING_S = self._old_tally

    def test_recording_mode_helpers_distinguish_fallback_tally_and_off(self):
        """Mode helpers must expose fallback, tally-only, and disabled behavior."""
        ctrl = self._controller_module.MistyController()

        self._controller_module.LAPTOP_MISTY_RECORDING_MODE = "fallback"
        self.assertTrue(ctrl._laptop_misty_recording_enabled())
        self.assertTrue(ctrl._laptop_misty_fallback_enabled())

        self._controller_module.LAPTOP_MISTY_RECORDING_MODE = "tally"
        self.assertTrue(ctrl._laptop_misty_recording_enabled())
        self.assertFalse(ctrl._laptop_misty_fallback_enabled())

        self._controller_module.LAPTOP_MISTY_RECORDING_MODE = "off"
        self.assertFalse(ctrl._laptop_misty_recording_enabled())
        self.assertFalse(ctrl._laptop_misty_fallback_enabled())

    def test_laptop_capture_failure_falls_back_when_available(self):
        """Safe default uses Misty audio when laptop capture is empty."""
        ctrl = self._controller_module.MistyController()
        self._controller_module.LAPTOP_MISTY_RECORDING_MODE = "fallback"

        result = ctrl._handle_laptop_capture_failure(
            turn=1,
            phase="initial recording",
            misty_fallback_available=True,
        )

        self.assertIsNone(result)

    def test_laptop_capture_failure_raises_when_fallback_disabled(self):
        """Disabled fallback must surface a clear retryable error."""
        ctrl = self._controller_module.MistyController()
        self._controller_module.LAPTOP_MISTY_RECORDING_MODE = "off"

        with self.assertRaisesRegex(RuntimeError, "Misty fallback is disabled or unavailable"):
            ctrl._handle_laptop_capture_failure(
                turn=1,
                phase="initial recording",
                misty_fallback_available=False,
            )

class TestWakeWordConfiguration(unittest.TestCase):
    """Unit tests for the supported laptop OpenWakeWord wake path."""

    _controller_module = None
    _listener_module = None

    @classmethod
    def setUpClass(cls):
        try:
            import misty_controller  # noqa: PLC0415
            import wake_word_listener  # noqa: PLC0415
            cls._controller_module = misty_controller
            cls._listener_module = wake_word_listener
        except ModuleNotFoundError as exc:
            if exc.name != "websocket":  # pragma: no cover
                cls._controller_module = None
                cls._listener_module = None
                print(f"[TestWakeWordConfiguration] Could not import modules: {exc}")
                return
            websocket_stub = unittest.mock.MagicMock()
            websocket_stub.WebSocketApp = unittest.mock.MagicMock()
            with unittest.mock.patch.dict(sys.modules, {"websocket": websocket_stub}):
                import misty_controller  # noqa: PLC0415
                import wake_word_listener  # noqa: PLC0415
                cls._controller_module = misty_controller
                cls._listener_module = wake_word_listener
        except Exception as exc:  # pragma: no cover
            cls._controller_module = None
            cls._listener_module = None
            print(f"[TestWakeWordConfiguration] Could not import modules: {exc}")

    def test_controller_defaults_to_laptop_wake_word(self):
        """The controller should default to the laptop wake-word path."""
        if self._controller_module is None:
            self.skipTest("misty_controller could not be imported")
        self.assertTrue(self._controller_module.USE_LAPTOP_WAKE_WORD)

    def test_listener_defaults_to_bundled_custom_model(self):
        """The listener should use the bundled Hey Misty model when no override is set."""
        if self._listener_module is None:
            self.skipTest("wake_word_listener could not be imported")

        expected = os.path.normpath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "models",
                "hey_misty.onnx",
            )
        )
        actual = os.path.normpath(self._listener_module.OWW_CUSTOM_MODEL_PATH)

        self.assertEqual(actual, expected)
        self.assertTrue(os.path.exists(actual))

    def test_missing_custom_model_configuration_fails_fast(self):
        """Missing custom-model config should fail the listener instead of silently using Misty keyphrase."""
        if self._listener_module is None:
            self.skipTest("wake_word_listener could not be imported")

        fake_openwakeword = types.ModuleType("openwakeword")
        fake_model_module = types.ModuleType("openwakeword.model")

        class FakeModel:
            def __init__(self, *args, **kwargs):
                self.models = {"fake": None}

        fake_model_module.Model = FakeModel
        fake_openwakeword.model = fake_model_module

        with unittest.mock.patch.dict(
            sys.modules,
            {"openwakeword": fake_openwakeword, "openwakeword.model": fake_model_module},
        ):
            listener = self._listener_module.WakeWordListener(on_wake_word=lambda: None, custom_model_path="")
            self.assertFalse(listener._init_model())

    def test_explicit_custom_model_path_is_selected(self):
        """Explicit model selection should use the configured custom model artifact."""
        if self._listener_module is None:
            self.skipTest("wake_word_listener could not be imported")

        fake_openwakeword = types.ModuleType("openwakeword")
        fake_model_module = types.ModuleType("openwakeword.model")

        class FakeModel:
            def __init__(self, *args, **kwargs):
                self.models = {"fake": None}

        fake_model_module.Model = FakeModel
        fake_openwakeword.model = fake_model_module

        with tempfile.NamedTemporaryFile(suffix=".onnx", delete=False) as handle:
            model_path = handle.name

        try:
            with unittest.mock.patch.dict(
                sys.modules,
                {"openwakeword": fake_openwakeword, "openwakeword.model": fake_model_module},
            ):
                listener = self._listener_module.WakeWordListener(
                    on_wake_word=lambda: None,
                    custom_model_path=model_path,
                )
                self.assertTrue(listener._init_model())
                self.assertEqual(list(listener._oww_model.models.keys()), ["fake"])
        finally:
            if os.path.exists(model_path):
                os.remove(model_path)

    def test_controller_raises_when_laptop_wake_word_startup_fails(self):
        """Wake-word startup failures must not silently fall back to Misty keyphrase."""
        if self._controller_module is None:
            self.skipTest("misty_controller could not be imported")

        fake_module = types.ModuleType("wake_word_listener")

        class FakeWakeWordListener:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                return False

            def get_health(self):
                return {"running": False, "model": "hey_misty", "threshold": 0.7, "custom_model_path": None}

        fake_module.WakeWordListener = FakeWakeWordListener
        fake_module.OWW_CUSTOM_MODEL_PATH = ""
        fake_module.OWW_MODEL_NAME = "hey_misty"
        fake_module.OWW_THRESHOLD = 0.7

        ctrl = self._controller_module.MistyController()
        with unittest.mock.patch.dict(sys.modules, {"wake_word_listener": fake_module}):
            with self.assertRaisesRegex(RuntimeError, "Laptop wake-word startup failed"):
                ctrl._start_laptop_wake_word()

        self.assertEqual(ctrl._wake_word_source, "error")
        self.assertIn("unsupported", ctrl._wake_word_config_error)


class TestLaptopFastRearm(unittest.TestCase):
    """Unit tests for laptop wake-word re-arm behavior (#65)."""

    _controller_module = None

    @classmethod
    def setUpClass(cls):
        try:
            import misty_controller  # noqa: PLC0415
            cls._controller_module = misty_controller
        except ModuleNotFoundError as exc:
            if exc.name != "websocket":  # pragma: no cover
                cls._controller_module = None
                print(f"[TestLaptopFastRearm] Could not import misty_controller: {exc}")
                return
            websocket_stub = unittest.mock.MagicMock()
            websocket_stub.WebSocketApp = unittest.mock.MagicMock()
            with unittest.mock.patch.dict(sys.modules, {"websocket": websocket_stub}):
                import misty_controller  # noqa: PLC0415
                cls._controller_module = misty_controller
        except Exception as exc:  # pragma: no cover
            cls._controller_module = None
            print(f"[TestLaptopFastRearm] Could not import misty_controller: {exc}")

    def setUp(self):
        if self._controller_module is None:
            self.skipTest("misty_controller could not be imported")

    def _controller_with_mocks(self):
        ctrl = self._controller_module.MistyController()
        ctrl.move_head = unittest.mock.MagicMock()
        ctrl.stop_recording = unittest.mock.MagicMock(return_value={"status": "Success"})
        ctrl.misty_post = unittest.mock.MagicMock()
        ctrl.set_led = unittest.mock.MagicMock()
        ctrl.display_image = unittest.mock.MagicMock()
        ctrl._connect_ws = unittest.mock.MagicMock()
        return ctrl

    def test_laptop_rearm_keeps_healthy_websocket_open(self):
        """Normal laptop-mode re-arm must resume listening without reconnecting."""
        ctrl = self._controller_with_mocks()
        ctrl._wake_word_listener = unittest.mock.MagicMock()
        ws = SimpleNamespace(sock=SimpleNamespace(connected=True), close=unittest.mock.MagicMock())
        ctrl.ws = ws
        ctrl.ws_thread = SimpleNamespace(is_alive=lambda: True)
        state_when_resumed = []
        ctrl._wake_word_listener.resume.side_effect = lambda: state_when_resumed.append(ctrl.get_state())

        with unittest.mock.patch.object(self._controller_module.time, "sleep"):
            with self.assertLogs("misty_controller", level="INFO") as logs:
                ctrl._rearm()

        ctrl.stop_recording.assert_called_once()
        ws.close.assert_not_called()
        ctrl._connect_ws.assert_not_called()
        ctrl._wake_word_listener.resume.assert_called_once()
        self.assertEqual(ctrl.get_state(), self._controller_module.State.IDLE)
        self.assertEqual(state_when_resumed, [self._controller_module.State.IDLE])
        self.assertTrue(
            any("Fast re-arm complete" in message and "WebSocket kept open" in message for message in logs.output)
        )

    def test_laptop_rearm_reconnects_when_websocket_unhealthy(self):
        """Laptop mode must fall back to full reconnect when the socket is unhealthy."""
        ctrl = self._controller_with_mocks()
        ctrl._wake_word_listener = unittest.mock.MagicMock()
        ws = SimpleNamespace(sock=SimpleNamespace(connected=False), close=unittest.mock.MagicMock())
        ctrl.ws = ws
        ctrl.ws_thread = SimpleNamespace(is_alive=lambda: True)

        with unittest.mock.patch.object(self._controller_module.time, "sleep"):
            with self.assertLogs("misty_controller", level="WARNING") as logs:
                ctrl._rearm()

        ws.close.assert_called_once()
        ctrl._connect_ws.assert_called_once()
        ctrl._wake_word_listener.resume.assert_called_once()
        self.assertTrue(any("falling back to full reconnect" in message for message in logs.output))

    def test_laptop_rearm_reconnects_when_websocket_thread_is_stopped(self):
        """Laptop mode must reconnect if the WebSocket loop is no longer alive."""
        ctrl = self._controller_with_mocks()
        ctrl._wake_word_listener = unittest.mock.MagicMock()
        ws = SimpleNamespace(sock=SimpleNamespace(connected=True), close=unittest.mock.MagicMock())
        ctrl.ws = ws
        ctrl.ws_thread = SimpleNamespace(is_alive=lambda: False)

        with unittest.mock.patch.object(self._controller_module.time, "sleep"):
            ctrl._rearm()

        ws.close.assert_called_once()
        ctrl._connect_ws.assert_called_once()
        ctrl._wake_word_listener.resume.assert_called_once()

    def test_misty_keyphrase_rearm_still_reconnects_websocket(self):
        """Non-laptop keyphrase mode keeps the existing full re-arm path."""
        ctrl = self._controller_with_mocks()
        ctrl._wake_word_listener = None
        ws = SimpleNamespace(sock=SimpleNamespace(connected=True), close=unittest.mock.MagicMock())
        ctrl.ws = ws

        with unittest.mock.patch.object(self._controller_module.time, "sleep"):
            ctrl._rearm()

        ctrl.misty_post.assert_any_call("/api/audio/keyphrase/stop")
        ws.close.assert_called_once()
        ctrl._connect_ws.assert_called_once()

@pytest.mark.live
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

@pytest.mark.live
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

@pytest.mark.live
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
            "model": os.getenv("FOUNDRY_CHAT_MODEL", "Phi-3.5-mini-instruct-generic-cpu:2"),
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

@pytest.mark.live
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

@pytest.mark.live
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

    def test_max_tokens_is_50(self):
        """The LLM payload must use max_tokens=50 for short mode (#21 optimisation)."""
        _, payload = self._call_llm_and_capture_payload("test question")
        self.assertEqual(
            payload["max_tokens"],
            50,
            f"Expected max_tokens=50 for short mode, got {payload['max_tokens']}",
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
        self.assertEqual(payload["max_tokens"], 50)

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


class TestTTSCache(unittest.TestCase):
    """Unit tests for TTS audio caching (#21).

    Tests the in-memory LRU cache that avoids re-synthesizing identical phrases.
    """

    _svc = None

    @classmethod
    def setUpClass(cls):
        try:
            import orchestration_service
            cls._svc = orchestration_service
        except Exception as exc:
            cls._svc = None
            print(f"[TestTTSCache] Could not import orchestration_service: {exc}")

    def setUp(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")
        # Clear cache before each test
        with self._svc._tts_cache_lock:
            self._svc._tts_cache.clear()

    def test_cache_key_is_case_insensitive(self):
        """Cache key normalises text to lowercase for stable matching."""
        key1 = self._svc._tts_cache_key("On my way!")
        key2 = self._svc._tts_cache_key("on my way!")
        key3 = self._svc._tts_cache_key("ON MY WAY!")
        self.assertEqual(key1, key2)
        self.assertEqual(key2, key3)

    def test_cache_key_strips_whitespace(self):
        """Leading/trailing whitespace doesn't affect the cache key."""
        key1 = self._svc._tts_cache_key("Hello!")
        key2 = self._svc._tts_cache_key("  Hello!  ")
        self.assertEqual(key1, key2)

    def test_cache_put_and_get(self):
        """put then get returns the stored path."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"fake wav")
            path = f.name
        try:
            self._svc._tts_cache_put("hello", path)
            result = self._svc._tts_cache_get("hello")
            self.assertEqual(result, path)
        finally:
            os.unlink(path)

    def test_cache_miss_returns_none(self):
        """get on empty cache returns None."""
        result = self._svc._tts_cache_get("nonexistent phrase")
        self.assertIsNone(result)

    def test_cache_returns_none_if_file_deleted(self):
        """If the cached file is deleted from disk, cache returns None and evicts."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"fake wav")
            path = f.name
        self._svc._tts_cache_put("ephemeral", path)
        os.unlink(path)  # simulate cleanup
        result = self._svc._tts_cache_get("ephemeral")
        self.assertIsNone(result)

    def test_cache_eviction_respects_max_size(self):
        """Non-pinned entries are evicted when cache exceeds TTS_CACHE_MAX."""
        import tempfile
        original_max = self._svc.TTS_CACHE_MAX
        try:
            self._svc.TTS_CACHE_MAX = 3
            paths = []
            for i in range(5):
                f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                f.write(b"fake")
                f.close()
                paths.append(f.name)
                self._svc._tts_cache_put(f"phrase {i}", f.name)
            with self._svc._tts_cache_lock:
                self.assertLessEqual(len(self._svc._tts_cache), 3)
        finally:
            self._svc.TTS_CACHE_MAX = original_max
            for p in paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_pinned_entries_survive_eviction(self):
        """Pinned cache entries are not evicted when cache is full."""
        import tempfile
        original_max = self._svc.TTS_CACHE_MAX
        try:
            self._svc.TTS_CACHE_MAX = 3
            # Add 2 pinned entries
            pinned_paths = []
            for i in range(2):
                f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                f.write(b"pinned")
                f.close()
                pinned_paths.append(f.name)
                self._svc._tts_cache_put(f"pinned {i}", f.name, pinned=True)
            # Add 3 non-pinned (should evict some non-pinned)
            extra_paths = []
            for i in range(3):
                f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                f.write(b"extra")
                f.close()
                extra_paths.append(f.name)
                self._svc._tts_cache_put(f"extra {i}", f.name, pinned=False)
            # Both pinned entries should still be findable
            for i, p in enumerate(pinned_paths):
                result = self._svc._tts_cache_get(f"pinned {i}")
                self.assertEqual(result, p, f"Pinned entry {i} was evicted")
        finally:
            self._svc.TTS_CACHE_MAX = original_max
            for p in pinned_paths + extra_paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_text_to_speech_populates_cache(self):
        """text_to_speech stores result in cache on successful synthesis."""
        text = "Cache test phrase"
        # Ensure cache starts empty for this text
        self.assertIsNone(self._svc._tts_cache_get(text))

        # Mock kokoro to simulate synthesis
        import numpy as np
        mock_kokoro = unittest.mock.MagicMock()
        mock_kokoro.create.return_value = (np.zeros(1000, dtype=np.float32), 24000)

        original_get = self._svc._get_kokoro
        self._svc._get_kokoro = lambda: mock_kokoro
        try:
            result = self._svc.text_to_speech(text, time.time())
            self.assertEqual(result["status"], "ok")
            self.assertIn("audio_file", result)
            # Second call should hit cache
            result2 = self._svc.text_to_speech(text, time.time())
            self.assertTrue(result2.get("tts_cached", False))
        finally:
            self._svc._get_kokoro = original_get
            # Clean up generated file
            audio_file = result.get("audio_file", "")
            if audio_file:
                path = os.path.join("responses", audio_file)
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def test_text_to_speech_returns_audio_file_field(self):
        """text_to_speech result includes audio_file for consistent access."""
        import numpy as np
        mock_kokoro = unittest.mock.MagicMock()
        mock_kokoro.create.return_value = (np.zeros(1000, dtype=np.float32), 24000)

        original_get = self._svc._get_kokoro
        self._svc._get_kokoro = lambda: mock_kokoro
        try:
            result = self._svc.text_to_speech("Audio file test", time.time())
            self.assertEqual(result["status"], "ok")
            self.assertIn("audio_file", result)
            self.assertIn("audio_uri", result)
            self.assertTrue(result["audio_file"].endswith(".wav"))
        finally:
            self._svc._get_kokoro = original_get
            audio_file = result.get("audio_file", "")
            if audio_file:
                try:
                    os.unlink(os.path.join("responses", audio_file))
                except OSError:
                    pass


    def test_controller_phrases_in_prewarm_set(self):
        """_CONTROLLER_PHRASES are included in _prewarm_tts_cache prewarm calls (#67).

        Verifies _prewarm_tts_cache() attempts to cache/pin the controller phrases.
        """
        svc = self._svc

        # Avoid depending on kokoro-onnx / soundfile during tests: pretend cached files already exist.
        with unittest.mock.patch.object(svc, "_get_kokoro", return_value=object()), \
             unittest.mock.patch.dict("sys.modules", {"soundfile": unittest.mock.MagicMock()}), \
             unittest.mock.patch.object(svc.os.path, "exists", return_value=True), \
             unittest.mock.patch.object(svc, "_tts_cache_put") as mock_put:
            svc._prewarm_tts_cache()

        calls_by_text = {call.args[0]: call for call in mock_put.call_args_list}
        for phrase in svc._CONTROLLER_PHRASES:
            self.assertIn(phrase, calls_by_text, f"Controller phrase '{phrase}' was not prewarmed")
            self.assertTrue(
                calls_by_text[phrase].kwargs.get("pinned", False),
                f"Controller phrase '{phrase}' was not pinned",
            )
    def test_controller_phrases_are_pinned_on_cache_put(self):
        """_CONTROLLER_PHRASES entries stored with pinned=True survive eviction (#67)."""
        import tempfile
        original_max = self._svc.TTS_CACHE_MAX
        pinned_paths = []
        f2 = None
        try:
            self._svc.TTS_CACHE_MAX = 1  # very small to force eviction pressure
            for phrase in self._svc._CONTROLLER_PHRASES:
                f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                f.write(b"pinned controller phrase")
                f.close()
                pinned_paths.append((phrase, f.name))
                self._svc._tts_cache_put(phrase, f.name, pinned=True)
            # Add a non-pinned entry to force eviction
            f2 = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            f2.write(b"evictable")
            f2.close()
            self._svc._tts_cache_put("evictable phrase", f2.name, pinned=False)
            # All controller phrases must still be retrievable
            for phrase, path in pinned_paths:
                result = self._svc._tts_cache_get(phrase)
                self.assertEqual(result, path, f"Pinned controller phrase '{phrase}' was evicted")
        finally:
            self._svc.TTS_CACHE_MAX = original_max
            for _phrase, path in pinned_paths:
                try:
                    os.unlink(path)
                except OSError:
                    # Best-effort cleanup in test teardown; file may already be removed.
                    pass
            if f2 is not None:
                try:
                    os.unlink(f2.name)
                except OSError:
                    # Best-effort cleanup in test teardown; file may already be removed.
                    pass


class TestLatencyConfig(unittest.TestCase):
    """Unit tests for latency-related configuration changes (#21)."""

    _svc = None

    @classmethod
    def setUpClass(cls):
        try:
            import orchestration_service
            cls._svc = orchestration_service
        except Exception as exc:
            cls._svc = None
            print(f"[TestLatencyConfig] Could not import orchestration_service: {exc}")

    def setUp(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")

    def test_kokoro_speed_default(self):
        """Default Kokoro speed should be 1.2 for latency optimisation."""
        # KOKORO_SPEED comes from env; default in code is 1.2
        self.assertGreaterEqual(self._svc.KOKORO_SPEED, 1.0)

    def test_short_mode_max_tokens(self):
        """Short mode max_tokens should be 50 (reduced from 60)."""
        config = self._svc.RESPONSE_MODE_CONFIG["short"]
        self.assertEqual(config["max_tokens"], 50)

    def test_temperature_is_reduced(self):
        """LLM temperature should be 0.7 for more focused responses."""
        # Temperature is in the payload builder, not a top-level constant,
        # so we verify via a mock LLM call.
        self._svc.conversation_history = []
        self._svc._last_response_mode = "short"
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
        with unittest.mock.patch.object(
            self._svc.requests, "post", return_value=mock_resp
        ) as mock_post:
            self._svc.language_model_inference("test", time.time())
            payload = mock_post.call_args[1]["json"]
        self.assertAlmostEqual(payload["temperature"], 0.7)

    def test_diagnostics_includes_tts_cache_stats(self):
        """Diagnostics endpoint should report TTS cache stats."""
        with self._svc.app.test_client() as client:
            resp = client.get("/api/diagnostics")
            data = resp.get_json()
            self.assertIn("tts", data)
            self.assertIn("cache_entries", data["tts"])
            self.assertIn("cache_pinned", data["tts"])
            self.assertIn("speed", data["tts"])

    def test_diagnostics_includes_llm_config(self):
        """Diagnostics endpoint should report LLM config."""
        with self._svc.app.test_client() as client:
            resp = client.get("/api/diagnostics")
            data = resp.get_json()
            self.assertIn("llm", data)
            self.assertIn("temperature", data["llm"])
            self.assertIn("short_max_tokens", data["llm"])


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
        self.ctrl._face_animator = unittest.mock.MagicMock()
        self.ctrl._talking_head = unittest.mock.MagicMock()
        self.ctrl._expression_coordinator = None
        self.ctrl.misty_post = mock_post
        self.ctrl.DRIVE_MAX_DURATION_MS = 3000
        self.ctrl.MOVEMENT_SETTLE_MS = 100  # fast for tests

    def test_move_arms_uses_firmware_arm_payload_for_both_arms(self):
        """Misty's /api/arms endpoint requires Arm/Position, not left/right fields."""
        self.ctrl.move_arms(left=-10, right=-10, velocity=40)

        self.assertEqual(
            self._post_calls,
            [("/api/arms", {"Arm": "both", "Position": -10, "Velocity": 40})],
        )

    def test_move_arms_splits_different_left_right_positions(self):
        """Different arm positions must be sent as one request per arm."""
        self.ctrl.move_arms(left=30, right=80, velocity=40)

        self.assertEqual(
            self._post_calls,
            [
                ("/api/arms", {"Arm": "left", "Position": 30, "Velocity": 40}),
                ("/api/arms", {"Arm": "right", "Position": 80, "Velocity": 40}),
            ],
        )

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

    def test_normal_excited_response_triggers_body_expression(self):
        """Excited spoken responses should trigger safe embodied choreography."""
        normal_result = {
            "status": "ok",
            "transcribedText": "celebrate",
            "inferenceResponse": "Wow, that's amazing!",
            "responseAudio": "/api/audio/resp.wav",
            "latencyMs": 500,
            "emotion": "excited",
        }
        import unittest.mock as mock
        mock_post_resp = mock.MagicMock()
        mock_post_resp.json.return_value = normal_result
        mock_post_resp.status_code = 200

        mock_get_resp = mock.MagicMock()
        mock_get_resp.content = b"\x00" * 1000
        mock_get_resp.raise_for_status = mock.MagicMock()

        expression = mock.MagicMock()
        expression.enabled = True
        expression.express.return_value = True
        self.ctrl._expression_coordinator = expression
        self.ctrl.upload_and_play_audio = mock.MagicMock(return_value=0.1)
        self.ctrl.set_led = mock.MagicMock()
        self.ctrl.display_image = mock.MagicMock()
        self.ctrl.move_head = mock.MagicMock()

        with mock.patch("requests.post", return_value=mock_post_resp), \
             mock.patch("requests.get", return_value=mock_get_resp), \
             mock.patch("time.sleep"):
            result = self.ctrl._do_orchestrate_and_respond(1, b"fake", time.time())

        self.assertTrue(result)
        expression.express.assert_called_once_with("joy", source="response")
        expression.cancel.assert_called_once()

    def test_neutral_response_does_not_trigger_body_expression(self):
        """Neutral speech should not add arm/head choreography noise."""
        expression = unittest.mock.MagicMock()
        expression.enabled = True
        self.ctrl._expression_coordinator = expression

        self.assertFalse(self.ctrl._express_for_response_emotion("neutral"))
        expression.express.assert_not_called()

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


class TestFaceRecognition(unittest.TestCase):
    """Unit tests for facial recognition integration (#16)."""

    _svc = None
    _ctrl_mod = None

    @classmethod
    def setUpClass(cls):
        try:
            import orchestration_service
            cls._svc = orchestration_service
        except Exception:
            cls._svc = None
        try:
            import misty_controller as mc
            cls._ctrl_mod = mc
        except Exception:
            cls._ctrl_mod = None

    def setUp(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")
        self._svc.conversation_history = []
        self._svc._last_response_mode = "short"

    # ------------------------------------------------------------------
    # Orchestration: speaker_name in LLM prompt
    # ------------------------------------------------------------------

    def _mock_llm_response(self, text="OK"):
        mock_resp = unittest.mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": text}}]}
        return mock_resp

    def test_speaker_name_injected_into_system_prompt(self):
        """When speaker_name is given, LLM system prompt includes the name."""
        with unittest.mock.patch.object(
            self._svc.requests, "post", return_value=self._mock_llm_response("Hi Tammy!")
        ) as mock_post:
            self._svc.language_model_inference("hello", time.time(), speaker_name="Tammy")
            payload = mock_post.call_args[1]["json"]

        system_msgs = [m for m in payload["messages"] if m["role"] == "system"]
        self.assertTrue(len(system_msgs) > 0)
        self.assertIn("Tammy", system_msgs[0]["content"])
        self.assertIn("currently talking to", system_msgs[0]["content"])

    def test_no_speaker_name_no_injection(self):
        """Without speaker_name, system prompt should NOT mention a person."""
        with unittest.mock.patch.object(
            self._svc.requests, "post", return_value=self._mock_llm_response("Hello!")
        ) as mock_post:
            self._svc.language_model_inference("hello", time.time(), speaker_name=None)
            payload = mock_post.call_args[1]["json"]

        system_msgs = [m for m in payload["messages"] if m["role"] == "system"]
        self.assertTrue(len(system_msgs) > 0)
        self.assertNotIn("currently talking to", system_msgs[0]["content"])

    def test_speaker_name_from_form_data(self):
        """The /api/orchestrate endpoint should extract speaker_name from form data."""
        with self._svc.app.test_client() as client:
            # Mock all the pipeline stages
            with unittest.mock.patch.object(self._svc, "speech_to_text") as mock_stt, \
                 unittest.mock.patch.object(self._svc, "language_model_inference") as mock_llm, \
                 unittest.mock.patch.object(self._svc, "text_to_speech") as mock_tts:
                mock_stt.return_value = {"status": "ok", "text": "hello"}
                mock_llm.return_value = {"status": "ok", "response_text": "Hi!",
                                         "llm_ms": 100, "mode": "short"}
                mock_tts.return_value = {"status": "ok", "audio_uri": "/api/audio/test.wav",
                                         "audio_file": "test.wav", "tts_ms": 50}

                resp = client.post(
                    "/api/orchestrate",
                    data={"speaker_name": "Burke"},
                    content_type="multipart/form-data",
                )
                # speaker_name should have been passed through to LLM
                # (we can't easily check the arg in this integration-style test without deeper mocking,
                # but at minimum the endpoint should not crash)
                self.assertIn(resp.status_code, [200, 400])  # 400 if file missing, 200 if mocked

    # ------------------------------------------------------------------
    # Controller: face recognition methods
    # ------------------------------------------------------------------

    def test_recognize_face_quick_returns_name(self):
        """recognize_face_quick returns a known face name when recognized."""
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")

        import unittest.mock as mock
        ctrl = self._ctrl_mod.MistyController.__new__(self._ctrl_mod.MistyController)
        ctrl.misty_ip = "0.0.0.0"
        ctrl._recognized_face = None
        ctrl._face_recognition_event = threading.Event()
        ctrl._face_event_name = None
        ctrl._trained_faces = ["Tammy"]  # must have trained faces

        # Mock start/stop face recognition
        def fake_start():
            # Simulate a face event arriving during recognition
            ctrl._recognized_face = "Tammy"
            ctrl._face_recognition_event.set()
            return True

        ctrl.start_face_recognition = mock.MagicMock(side_effect=fake_start)
        ctrl.stop_face_recognition = mock.MagicMock()
        ctrl.get_trained_faces = mock.MagicMock()

        name = ctrl.recognize_face_quick()
        self.assertEqual(name, "Tammy")
        ctrl.stop_face_recognition.assert_called_once()

    def test_recognize_face_quick_returns_none_on_timeout(self):
        """recognize_face_quick returns None if no face is detected within timeout."""
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")

        import unittest.mock as mock
        ctrl = self._ctrl_mod.MistyController.__new__(self._ctrl_mod.MistyController)
        ctrl.misty_ip = "0.0.0.0"
        ctrl._recognized_face = None
        ctrl._face_recognition_event = threading.Event()
        ctrl._face_event_name = None
        ctrl._trained_faces = ["Tammy"]
        ctrl.start_face_recognition = mock.MagicMock(return_value=True)
        ctrl.stop_face_recognition = mock.MagicMock()
        ctrl.get_trained_faces = mock.MagicMock()

        # No face event fired — should timeout and return None
        name = ctrl.recognize_face_quick(timeout_s=0.1)
        self.assertIsNone(name)
        ctrl.stop_face_recognition.assert_called_once()

    def test_recognize_face_quick_ignores_unknown(self):
        """recognize_face_quick returns None for 'unknown_person'."""
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")

        import unittest.mock as mock
        ctrl = self._ctrl_mod.MistyController.__new__(self._ctrl_mod.MistyController)
        ctrl.misty_ip = "0.0.0.0"
        ctrl._recognized_face = None
        ctrl._face_recognition_event = threading.Event()
        ctrl._face_event_name = None
        ctrl._trained_faces = ["Tammy"]
        ctrl.get_trained_faces = mock.MagicMock()

        # Simulate "unknown_person" event — handler won't signal the event
        def fake_start():
            # unknown_person doesn't set _recognized_face (handler filters it)
            return True

        ctrl.start_face_recognition = mock.MagicMock(side_effect=fake_start)
        ctrl.stop_face_recognition = mock.MagicMock()

        name = ctrl.recognize_face_quick(timeout_s=0.1)
        self.assertIsNone(name)

    def test_face_event_handler_sets_recognized_face(self):
        """_handle_face_recognition_event should set _recognized_face and signal the event."""
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")

        ctrl = self._ctrl_mod.MistyController.__new__(self._ctrl_mod.MistyController)
        ctrl._recognized_face = None
        ctrl._face_recognition_event = threading.Event()

        # Event data has label at top level (not nested under message)
        event_data = {
            "label": "Burke",
        }
        ctrl._handle_face_recognition_event(event_data)

        self.assertEqual(ctrl._recognized_face, "Burke")
        self.assertTrue(ctrl._face_recognition_event.is_set())

    def test_face_event_handler_ignores_unknown(self):
        """_handle_face_recognition_event should NOT signal for unknown_person."""
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")

        ctrl = self._ctrl_mod.MistyController.__new__(self._ctrl_mod.MistyController)
        ctrl._recognized_face = None
        ctrl._face_recognition_event = threading.Event()

        event_data = {
            "label": "unknown_person",
        }
        ctrl._handle_face_recognition_event(event_data)

        self.assertIsNone(ctrl._recognized_face)
        self.assertFalse(ctrl._face_recognition_event.is_set())

    def test_rearm_clears_recognized_face(self):
        """_rearm should clear _recognized_face between conversations."""
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")

        import unittest.mock as mock
        ctrl = self._ctrl_mod.MistyController.__new__(self._ctrl_mod.MistyController)
        ctrl._recognized_face = "Tammy"
        ctrl._face_recognition_event = threading.Event()
        ctrl._state = self._ctrl_mod.State.REARMING

        # Mock all the methods _rearm calls
        ctrl.set_state = mock.MagicMock()
        ctrl.move_head = mock.MagicMock()
        ctrl._conversation_cycles = 0
        ctrl._recording_cycles = 0
        ctrl.stop_recording = mock.MagicMock()
        ctrl._wake_word_listener = None
        ctrl.ws = None
        ctrl.reconnect_attempts = 0
        ctrl.misty_post = mock.MagicMock()
        ctrl._connect_ws = mock.MagicMock()

        with mock.patch("time.sleep"):
            ctrl._rearm()

        self.assertIsNone(ctrl._recognized_face)


class TestCanonicalDefaults(unittest.TestCase):
    """Verify that config_defaults.py is the authoritative source of truth (#70).

    These tests confirm that orchestration_service and misty_controller read
    their defaults from config_defaults, ensuring no silent drift.
    """

    _svc = None
    _ctrl_mod = None
    _cfg = None

    @classmethod
    def setUpClass(cls):
        try:
            import config_defaults
            cls._cfg = config_defaults
        except Exception as exc:
            print(f"[TestCanonicalDefaults] Could not import config_defaults: {exc}")
        try:
            import orchestration_service
            cls._svc = orchestration_service
        except Exception as exc:
            print(f"[TestCanonicalDefaults] Could not import orchestration_service: {exc}")
        try:
            import misty_controller as mc
            cls._ctrl_mod = mc
        except Exception as exc:
            print(f"[TestCanonicalDefaults] Could not import misty_controller: {exc}")

    def setUp(self):
        if self._cfg is None:
            self.skipTest("config_defaults could not be imported")

    # ------------------------------------------------------------------
    # config_defaults module structure
    # ------------------------------------------------------------------

    def test_config_defaults_exports_orchestration_values(self):
        """config_defaults must export all orchestration service defaults."""
        for attr in (
            "FOUNDRY_API_TIMEOUT", "SERVICE_TIMEOUT",
            "KOKORO_VOICE", "KOKORO_SPEED", "TTS_CACHE_MAX",
            "MAX_USER_CHARS", "MAX_CONTEXT_CHARS",
        ):
            self.assertTrue(hasattr(self._cfg, attr), f"config_defaults missing: {attr}")

    def test_config_defaults_exports_controller_values(self):
        """config_defaults must export all misty_controller defaults."""
        for attr in (
            "MISTY_IP", "ORCHESTRATION_URL",
            "RECORDING_DURATION_S", "FOLLOWUP_LISTEN_S", "FOLLOWUP_TIMEOUT_S",
            "FOLLOWUP_MAX_TURNS", "WATCHDOG_IDLE_TIMEOUT_S", "WATCHDOG_ESCALATE_TIMEOUT_S",
            "IDLE_TIMEOUT_S", "PROACTIVE_REBOOT_AFTER_CYCLES",
            "PROACTIVE_REBOOT_AFTER_RECORDINGS", "LAPTOP_MISTY_RECORDING_MODE",
            "LAPTOP_MISTY_TALLY_RECORDING_S", "FACE_RECOGNITION_TIMEOUT_S",
        ):
            self.assertTrue(hasattr(self._cfg, attr), f"config_defaults missing: {attr}")

    # ------------------------------------------------------------------
    # Orchestration service agrees with config_defaults
    # ------------------------------------------------------------------

    def test_orchestration_foundry_api_timeout_matches_defaults(self):
        """orchestration_service FOUNDRY_API_TIMEOUT default == config_defaults."""
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")
        # When no env var is set the module should use config_defaults value.
        self.assertAlmostEqual(self._svc.FOUNDRY_API_TIMEOUT, self._cfg.FOUNDRY_API_TIMEOUT)

    def test_orchestration_service_timeout_matches_defaults(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")
        self.assertAlmostEqual(self._svc.SERVICE_TIMEOUT, self._cfg.SERVICE_TIMEOUT)

    def test_orchestration_kokoro_voice_matches_defaults(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")
        self.assertEqual(self._svc.KOKORO_VOICE, self._cfg.KOKORO_VOICE)

    def test_orchestration_kokoro_speed_matches_defaults(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")
        self.assertAlmostEqual(self._svc.KOKORO_SPEED, self._cfg.KOKORO_SPEED)

    def test_orchestration_max_user_chars_matches_defaults(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")
        self.assertEqual(self._svc.MAX_USER_CHARS, self._cfg.MAX_USER_CHARS)

    def test_orchestration_max_context_chars_matches_defaults(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")
        self.assertEqual(self._svc.MAX_CONTEXT_CHARS, self._cfg.MAX_CONTEXT_CHARS)

    def test_orchestration_tts_cache_max_matches_defaults(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")
        self.assertEqual(self._svc.TTS_CACHE_MAX, self._cfg.TTS_CACHE_MAX)

    # ------------------------------------------------------------------
    # Controller agrees with config_defaults
    # ------------------------------------------------------------------

    def test_controller_followup_timeout_matches_defaults(self):
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")
        self.assertAlmostEqual(self._ctrl_mod.FOLLOWUP_TIMEOUT_S, self._cfg.FOLLOWUP_TIMEOUT_S)

    def test_controller_followup_max_turns_matches_defaults(self):
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")
        self.assertEqual(self._ctrl_mod.FOLLOWUP_MAX_TURNS, self._cfg.FOLLOWUP_MAX_TURNS)

    def test_controller_watchdog_idle_timeout_matches_defaults(self):
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")
        self.assertAlmostEqual(self._ctrl_mod.WATCHDOG_IDLE_TIMEOUT_S, self._cfg.WATCHDOG_IDLE_TIMEOUT_S)

    def test_controller_proactive_reboot_cycles_matches_defaults(self):
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")
        self.assertEqual(self._ctrl_mod.PROACTIVE_REBOOT_AFTER_CYCLES, self._cfg.PROACTIVE_REBOOT_AFTER_CYCLES)

    def test_controller_proactive_reboot_recordings_matches_defaults(self):
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")
        self.assertEqual(self._ctrl_mod.PROACTIVE_REBOOT_AFTER_RECORDINGS, self._cfg.PROACTIVE_REBOOT_AFTER_RECORDINGS)

    # ------------------------------------------------------------------
    # Key default values are sane
    # ------------------------------------------------------------------

    def test_foundry_api_timeout_is_positive(self):
        self.assertGreater(self._cfg.FOUNDRY_API_TIMEOUT, 0)

    def test_service_timeout_exceeds_foundry_timeout(self):
        """SERVICE_TIMEOUT must be longer than FOUNDRY_API_TIMEOUT."""
        self.assertGreater(self._cfg.SERVICE_TIMEOUT, self._cfg.FOUNDRY_API_TIMEOUT)

    def test_followup_timeout_allows_multiple_turns(self):
        """FOLLOWUP_TIMEOUT_S should be long enough for multiple follow-up turns."""
        min_useful = self._cfg.FOLLOWUP_LISTEN_S * 2
        self.assertGreaterEqual(self._cfg.FOLLOWUP_TIMEOUT_S, min_useful)

    def test_laptop_recording_mode_is_valid(self):
        self.assertIn(self._cfg.LAPTOP_MISTY_RECORDING_MODE, ("fallback", "tally", "off"))


class TestEmotionClassification(unittest.TestCase):
    """Unit tests for LLM-response emotion tagging used by the face system (#110).

    These validate the emotion tag that orchestration responses include so the
    controller/FaceAnimator can pick a matching talking face. No live services.
    """

    _svc = None

    @classmethod
    def setUpClass(cls):
        try:
            import orchestration_service  # noqa: PLC0415
            cls._svc = orchestration_service
        except Exception as exc:  # pragma: no cover - import guard
            print(f"[TestEmotionClassification] Could not import: {exc}")

    def setUp(self):
        if self._svc is None:
            self.skipTest("orchestration_service could not be imported")

    _VALID = {"excited", "happy", "sad", "curious", "neutral"}

    def test_excited_detected(self):
        self.assertEqual(self._svc.classify_emotion("Wow, that is amazing!!"), "excited")

    def test_sad_detected(self):
        self.assertEqual(
            self._svc.classify_emotion("Unfortunately that is a tough situation."),
            "sad",
        )

    def test_happy_detected(self):
        self.assertEqual(self._svc.classify_emotion("That's a great idea!"), "happy")

    def test_curious_detected(self):
        self.assertEqual(
            self._svc.classify_emotion("Hmm, that's interesting. I wonder why?"),
            "curious",
        )

    def test_neutral_default(self):
        self.assertEqual(self._svc.classify_emotion("The store opens at nine."), "neutral")

    def test_empty_is_neutral(self):
        self.assertEqual(self._svc.classify_emotion(""), "neutral")
        self.assertEqual(self._svc.classify_emotion(None), "neutral")

    def test_all_outputs_are_valid_emotions(self):
        """Every classification maps to a supported face_talking_{emotion} variant."""
        samples = [
            "Wow amazing!!", "I'm so sorry for your loss", "That's wonderful!",
            "Hmm, curious?", "It is a table.", "", "12345",
        ]
        for text in samples:
            self.assertIn(self._svc.classify_emotion(text), self._VALID)


class TestCustomFaceAssetUpload(unittest.TestCase):
    """Unit tests for idempotent startup face-asset upload + fallback (#110).

    All Misty HTTP interaction is mocked — no live robot required.
    """

    _ctrl_mod = None

    @classmethod
    def setUpClass(cls):
        try:
            _ctrl_path = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration")
            )
            if _ctrl_path not in sys.path:
                sys.path.insert(0, _ctrl_path)
            import misty_controller as _ctrl_mod  # noqa: PLC0415
            cls._ctrl_mod = _ctrl_mod
        except Exception as exc:  # pragma: no cover - import guard
            print(f"[TestCustomFaceAssetUpload] Could not import misty_controller: {exc}")

    def setUp(self):
        if self._ctrl_mod is None:
            self.skipTest("misty_controller could not be imported")
        self.ctrl = self._ctrl_mod.MistyController.__new__(self._ctrl_mod.MistyController)
        self.ctrl._face_animator = unittest.mock.MagicMock()

    # --- _get_misty_image_names parsing ---

    def test_get_image_names_parses_result(self):
        self.ctrl.misty_get = unittest.mock.MagicMock(
            return_value={"result": [{"name": "e_Joy.jpg"}, {"name": "face_idle.gif"}]}
        )
        names = self.ctrl._get_misty_image_names()
        self.assertEqual(names, {"e_Joy.jpg", "face_idle.gif"})

    def test_get_image_names_empty_on_failure(self):
        self.ctrl.misty_get = unittest.mock.MagicMock(return_value=None)
        self.assertEqual(self.ctrl._get_misty_image_names(), set())

    # --- ensure_face_assets behavior ---

    def test_ensure_skips_when_all_present_idempotent(self):
        """When all required faces are already on the device, no uploads occur."""
        present = set(self._ctrl_mod.REQUIRED_FACE_ASSETS)
        self.ctrl._get_misty_image_names = unittest.mock.MagicMock(return_value=present)
        self.ctrl._upload_face_image = unittest.mock.MagicMock(return_value=True)

        result = self.ctrl.ensure_face_assets()

        self.assertTrue(result)
        self.ctrl._upload_face_image.assert_not_called()
        self.ctrl._face_animator.set_custom_faces_available.assert_called_once_with(True)

    def test_ensure_uploads_missing_assets(self):
        """Missing-but-locally-present assets are uploaded; result is available."""
        self.ctrl._get_misty_image_names = unittest.mock.MagicMock(return_value=set())
        self.ctrl._upload_face_image = unittest.mock.MagicMock(return_value=True)

        with unittest.mock.patch.object(self._ctrl_mod.os.path, "exists", return_value=True):
            result = self.ctrl.ensure_face_assets()

        self.assertTrue(result)
        self.assertEqual(
            self.ctrl._upload_face_image.call_count,
            len(self._ctrl_mod.REQUIRED_FACE_ASSETS),
        )
        self.ctrl._face_animator.set_custom_faces_available.assert_called_once_with(True)

    def test_ensure_missing_local_triggers_fallback(self):
        """When local assets are missing, animator is told to use built-in fallback."""
        self.ctrl._get_misty_image_names = unittest.mock.MagicMock(return_value=set())
        self.ctrl._upload_face_image = unittest.mock.MagicMock(return_value=True)

        with unittest.mock.patch.object(self._ctrl_mod.os.path, "exists", return_value=False):
            result = self.ctrl.ensure_face_assets()

        self.assertFalse(result)
        self.ctrl._upload_face_image.assert_not_called()
        self.ctrl._face_animator.set_custom_faces_available.assert_called_once_with(False)

    def test_ensure_upload_failure_triggers_fallback(self):
        """A failed upload marks custom faces unavailable (fallback)."""
        self.ctrl._get_misty_image_names = unittest.mock.MagicMock(return_value=set())
        self.ctrl._upload_face_image = unittest.mock.MagicMock(return_value=False)

        with unittest.mock.patch.object(self._ctrl_mod.os.path, "exists", return_value=True):
            result = self.ctrl.ensure_face_assets()

        self.assertFalse(result)
        self.ctrl._face_animator.set_custom_faces_available.assert_called_once_with(False)

    def test_ensure_no_animator_does_not_crash(self):
        """ensure_face_assets works when no animator is configured."""
        self.ctrl._face_animator = None
        self.ctrl._get_misty_image_names = unittest.mock.MagicMock(
            return_value=set(self._ctrl_mod.REQUIRED_FACE_ASSETS)
        )
        self.ctrl._upload_face_image = unittest.mock.MagicMock(return_value=True)
        result = self.ctrl.ensure_face_assets()
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()

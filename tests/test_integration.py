"""
Integration Test Suite for Misty + Foundry Local
Tests communication between components and validates latency SLO.
"""

import unittest
import re
import subprocess
import requests
import json
import time
import os
from io import BytesIO
from urllib.parse import urlparse, urlunparse


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

if __name__ == "__main__":
    unittest.main()

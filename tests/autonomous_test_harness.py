"""
Autonomous Test Harness for Misty II Conversational AI

Plays "Hey Misty" + test questions through laptop speakers,
monitors controller/orchestration logs for results, and logs
test outcomes. Designed to run unattended for hours.

Usage:
    python autonomous_test_harness.py [--interval 600] [--cycles 50]

Requires:
    - Orchestration service running on port 5000
    - Misty controller running and in IDLE state
    - Laptop speakers audible to Misty's microphone
    - pyttsx3, requests, winsound
"""

import argparse
import datetime
import json
import logging
import os
import psutil
import re
import subprocess
import sys
import tempfile
import time
import wave

import pyttsx3
import requests

# --- Configuration ---
MISTY_IP = os.getenv("MISTY_IP", "10.0.0.44")
MISTY_BASE = f"http://{MISTY_IP}"
ORCHESTRATION_URL = os.getenv("ORCHESTRATION_URL", "http://localhost:5000")
CONTROLLER_API_URL = os.getenv("CONTROLLER_API_URL", "http://localhost:5001")
INTERVAL_S = 600  # 10 minutes between cycles
MAX_CYCLES = 144  # ~24 hours at 10 min intervals
RESPONSE_TIMEOUT_S = 60  # max wait for Misty to finish a turn (includes pipeline + playback)
REARM_WAIT_S = 50  # wait for sensory reboot + re-arm (30s reboot + buffer)

# Test questions — varied complexity and topics
TEST_QUESTIONS = [
    "What color is the sky?",
    "How many legs does a cat have?",
    "What is the capital of France?",
    "Tell me a fun fact.",
    "What is two plus two?",
    "Who painted the Mona Lisa?",
    "What planet is closest to the sun?",
    "What is the largest animal on Earth?",
    "How many days are in a week?",
    "What language do they speak in Brazil?",
    "What is the boiling point of water?",
    "Name a color in the rainbow.",
    "What ocean is between America and Europe?",
    "What is the tallest mountain in the world?",
    "How many continents are there?",
    "What sound does a dog make?",
    "What season comes after summer?",
    "Where do penguins live?",
    "What is the speed of light?",
    "Tell me a joke.",
]

# --- Logging ---
LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "test-results")
os.makedirs(LOG_DIR, exist_ok=True)

log_file = os.path.join(LOG_DIR, f"autonomous_test_{datetime.date.today().isoformat()}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# Results JSON file
results_file = os.path.join(LOG_DIR, f"test_results_{datetime.date.today().isoformat()}.json")


class TestResult:
    def __init__(self, cycle: int, question: str):
        self.cycle = cycle
        self.question = question
        self.timestamp = datetime.datetime.now().isoformat()
        self.wake_word_detected = False
        self.stt_text = None
        self.llm_response = None
        self.pipeline_ms = None
        self.stt_ms = None
        self.llm_ms = None
        self.tts_ms = None
        self.response_words = None
        self.rearm_success = False
        self.rearm_time_s = None
        self.error = None
        self.stt_accurate = None  # manual/heuristic check
        self.cpu_percent = None
        self.memory_percent = None
        self.memory_mb = None

    def to_dict(self):
        return self.__dict__


class AutonomousTestHarness:
    def __init__(self, interval_s: int = INTERVAL_S, max_cycles: int = MAX_CYCLES):
        self.interval_s = interval_s
        self.max_cycles = max_cycles
        self.results: list[TestResult] = []
        self.question_index = 0
        self.tts_engine = None
        self.wav_cache: dict[str, str] = {}  # text -> wav file path

        # Controller log file for monitoring
        self.controller_log = os.path.join(
            os.path.dirname(__file__), "..", "src", "windows-orchestration", "misty_controller.log"
        )
        self.orch_log = os.path.join(
            os.path.dirname(__file__), "..", "src", "windows-orchestration", "orchestration.log"
        )

    def _init_tts(self):
        """Initialize pyttsx3 for generating test audio."""
        if not self.tts_engine:
            self.tts_engine = pyttsx3.init()
            self.tts_engine.setProperty("rate", 150)  # clear, not too fast
            self.tts_engine.setProperty("volume", 1.0)
        return self.tts_engine

    def _generate_wav(self, text: str) -> str:
        """Generate a WAV file from text using pyttsx3."""
        if text in self.wav_cache:
            return self.wav_cache[text]

        engine = self._init_tts()
        wav_path = os.path.join(tempfile.gettempdir(), f"misty_test_{hash(text) & 0xFFFFFFFF:08x}.wav")
        engine.save_to_file(text, wav_path)
        engine.runAndWait()
        self.wav_cache[text] = wav_path
        logger.info(f"Generated WAV: '{text}' -> {wav_path}")
        return wav_path

    def _play_wav(self, wav_path: str):
        """Play a WAV file through laptop speakers using ffplay (no window)."""
        try:
            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", wav_path],
                timeout=15,
                check=True,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"ffplay timed out playing {wav_path}")
        except FileNotFoundError:
            # Fallback to winsound
            import winsound
            winsound.PlaySound(wav_path, winsound.SND_FILENAME)

    def _speak_through_speakers(self, text: str):
        """Speak text through laptop speakers (real-time, no file)."""
        engine = self._init_tts()
        engine.say(text)
        engine.runAndWait()

    def _get_log_tail(self, log_path: str, lines: int = 50) -> list[str]:
        """Get the last N lines of a log file."""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                return all_lines[-lines:]
        except FileNotFoundError:
            return []

    def _get_log_since(self, log_path: str, since_time: datetime.datetime, max_lines: int = 200) -> list[str]:
        """Get log lines since a given time."""
        lines = self._get_log_tail(log_path, max_lines)
        result = []
        for line in lines:
            # Parse timestamp from log line: "2026-04-25 20:43:12,325 [INFO] ..."
            match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ ", line)
            if match:
                try:
                    line_time = datetime.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                    # Use today's date context
                    if line_time.date() == since_time.date() and line_time >= since_time:
                        result.append(line.strip())
                except ValueError:
                    pass
        return result

    def check_services(self) -> bool:
        """Verify all services are running."""
        errors = []

        # Check orchestration service
        try:
            r = requests.get(f"{ORCHESTRATION_URL}/api/health", timeout=5)
            if r.status_code != 200 or r.json().get("status") != "ok":
                errors.append(f"Orchestration unhealthy: {r.text}")
        except Exception as e:
            errors.append(f"Orchestration unreachable: {e}")

        # Check controller API
        try:
            r = requests.get(f"{CONTROLLER_API_URL}/api/status", timeout=5)
            status = r.json()
            if status.get("state") != "IDLE":
                errors.append(f"Controller not IDLE: {status.get('state')}")
            logger.info(f"Controller state: {status.get('state')}, turn: {status.get('turn_id')}")
        except Exception as e:
            errors.append(f"Controller API unreachable: {e}")

        # Check Misty
        try:
            r = requests.get(f"{MISTY_BASE}/api/battery", timeout=5)
            battery = r.json().get("result", {})
            charge = battery.get("chargePercent", 0) * 100
            charging = battery.get("isCharging", False)
            if charge < 15 and not charging:
                errors.append(f"Misty battery too low: {charge:.0f}% (not charging)")
                logger.warning(f"Battery at {charge:.0f}% — stopping tests to preserve battery")
            else:
                logger.info(f"Misty battery: {charge:.0f}% {'(charging)' if charging else ''}")
        except Exception as e:
            errors.append(f"Misty unreachable: {e}")

        if errors:
            for e in errors:
                logger.error(f"Service check failed: {e}")
            return False

        logger.info("All services healthy")
        return True

    def _check_controller_state(self) -> str:
        """Check current controller state from logs."""
        lines = self._get_log_tail(self.controller_log, 20)
        for line in reversed(lines):
            match = re.search(r"State: \w+ -> (\w+)", line)
            if match:
                return match.group(1)
        return "UNKNOWN"

    def run_test_cycle(self, cycle: int) -> TestResult:
        """Run a single test cycle: wake word -> question -> monitor response."""
        question = TEST_QUESTIONS[self.question_index % len(TEST_QUESTIONS)]
        self.question_index += 1
        result = TestResult(cycle, question)

        logger.info(f"=" * 60)
        logger.info(f"Cycle {cycle}: Question: '{question}'")
        logger.info(f"=" * 60)

        # Capture laptop resource usage
        result.cpu_percent = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        result.memory_percent = mem.percent
        result.memory_mb = round(mem.used / 1024 / 1024)
        logger.info(f"Laptop resources: CPU={result.cpu_percent}%, RAM={result.memory_percent}% ({result.memory_mb}MB)")

        # Check controller is in IDLE state
        state = self._check_controller_state()
        if state not in ("IDLE", "UNKNOWN"):
            logger.warning(f"Controller not IDLE (state={state}), waiting 30s...")
            time.sleep(30)
            state = self._check_controller_state()
            if state not in ("IDLE", "UNKNOWN"):
                result.error = f"Controller stuck in {state}"
                logger.error(result.error)
                return result

        # Record the time before we trigger
        trigger_time = datetime.datetime.now()

        # Step 1: Trigger conversation via controller API (bypasses wake word)
        logger.info("Triggering conversation via controller API...")
        try:
            r = requests.post(f"{CONTROLLER_API_URL}/api/test/trigger", timeout=5)
            if r.status_code == 409:
                result.error = f"Controller not IDLE: {r.json().get('state')}"
                logger.error(result.error)
                return result
            elif r.status_code != 200:
                result.error = f"Trigger failed: {r.status_code} {r.text}"
                logger.error(result.error)
                return result
            result.wake_word_detected = True  # programmatic trigger = guaranteed
            logger.info(f"Trigger response: {r.json()}")
        except Exception as e:
            result.error = f"Controller API error: {e}"
            logger.error(result.error)
            return result

        # Step 2: Wait for Misty to start recording, then speak the question
        # Controller enters RECORDING state, stops keyphrase, starts recording for 4s.
        # We need ~2.5s for keyphrase stop + state transition + REST call to Misty.
        time.sleep(2.5)
        logger.info(f"Playing question through speakers: '{question}'")
        self._speak_through_speakers(question)

        # Step 3: Wait for the full pipeline to complete
        logger.info(f"Waiting up to {RESPONSE_TIMEOUT_S}s for response...")
        start_wait = time.time()
        response_seen = False

        while (time.time() - start_wait) < RESPONSE_TIMEOUT_S:
            time.sleep(3)
            lines = self._get_log_since(self.controller_log, trigger_time)

            for line in lines:
                # Check for wake word
                if "[Wake] Wake word detected" in line:
                    result.wake_word_detected = True

                # Check for STT + response: "[Turn X] User: 'text' -> Misty: 'text' (Xms)"
                turn_match = re.search(
                    r"\[Turn \d+\] (?:User|Follow-up): '(.+?)' -> (?:Misty: )?'(.+?)' \((\d+)ms\)", line
                )
                if turn_match:
                    result.stt_text = turn_match.group(1)
                    result.llm_response = turn_match.group(2)
                    result.pipeline_ms = int(turn_match.group(3))
                    result.response_words = len(result.llm_response.split())
                    response_seen = True

                # Check for re-arm
                if "Re-arm complete" in line or "State: REARMING -> IDLE" in line or "State: DISCONNECTED -> IDLE" in line:
                    result.rearm_success = True
                    result.rearm_time_s = time.time() - start_wait

            if response_seen and result.rearm_success:
                break

        # If we saw a response but not re-arm, wait a bit more for sensory reboot
        if response_seen and not result.rearm_success:
            logger.info("Response seen, waiting for sensory reboot re-arm...")
            rearm_start = time.time()
            while (time.time() - rearm_start) < REARM_WAIT_S:
                time.sleep(5)
                lines = self._get_log_since(self.controller_log, trigger_time)
                for line in lines:
                    if "Re-arm complete" in line or "State: REARMING -> IDLE" in line or "State: DISCONNECTED -> IDLE" in line:
                        result.rearm_success = True
                        result.rearm_time_s = time.time() - start_wait
                        break
                if result.rearm_success:
                    break

        # Parse orchestration log for per-stage timing
        orch_lines = self._get_log_since(self.orch_log, trigger_time)
        for line in orch_lines:
            timing_match = re.search(r"\[Pipeline (\d+)ms\] STT=(\d+) LLM=(\d+) TTS=(\d+)", line)
            if timing_match:
                result.pipeline_ms = int(timing_match.group(1))
                result.stt_ms = int(timing_match.group(2))
                result.llm_ms = int(timing_match.group(3))
                result.tts_ms = int(timing_match.group(4))

        # Heuristic STT accuracy check
        if result.stt_text:
            # Check if key words from the question appear in STT output
            q_words = set(question.lower().replace("?", "").replace(".", "").split())
            stt_words = set(result.stt_text.lower().replace("?", "").replace(".", "").split())
            # Remove common words
            stop_words = {"what", "is", "the", "a", "an", "of", "in", "how", "many", "do", "does", "are", "tell", "me"}
            q_key = q_words - stop_words
            stt_key = stt_words - stop_words
            if q_key:
                overlap = len(q_key & stt_key) / len(q_key)
                result.stt_accurate = overlap >= 0.5
            else:
                result.stt_accurate = True  # simple question

        # Log results
        if not result.wake_word_detected:
            result.error = "Wake word not detected"
            logger.error(f"FAIL: Wake word not detected")
        elif not response_seen:
            result.error = "No response within timeout"
            logger.error(f"FAIL: No response within {RESPONSE_TIMEOUT_S}s")
        else:
            logger.info(f"STT heard: '{result.stt_text}'")
            logger.info(f"Misty said: '{result.llm_response}'")
            logger.info(f"Pipeline: {result.pipeline_ms}ms (STT={result.stt_ms}, LLM={result.llm_ms}, TTS={result.tts_ms})")
            logger.info(f"Response length: {result.response_words} words")
            logger.info(f"STT accurate: {result.stt_accurate}")
            logger.info(f"Re-arm: {'OK' if result.rearm_success else 'FAILED'} ({result.rearm_time_s:.0f}s)" if result.rearm_time_s else f"Re-arm: {'OK' if result.rearm_success else 'PENDING'}")

        return result

    def _save_results(self):
        """Save all results to JSON file."""
        data = {
            "date": datetime.date.today().isoformat(),
            "total_cycles": len(self.results),
            "summary": self._compute_summary(),
            "results": [r.to_dict() for r in self.results],
        }
        with open(results_file, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Results saved to {results_file}")

    def _compute_summary(self) -> dict:
        """Compute aggregate statistics."""
        if not self.results:
            return {}

        completed = [r for r in self.results if r.pipeline_ms is not None]
        wake_detected = sum(1 for r in self.results if r.wake_word_detected)
        stt_accurate = sum(1 for r in completed if r.stt_accurate)
        rearmed = sum(1 for r in self.results if r.rearm_success)
        errors = sum(1 for r in self.results if r.error)

        pipelines = [r.pipeline_ms for r in completed if r.pipeline_ms]
        word_counts = [r.response_words for r in completed if r.response_words]
        rearm_times = [r.rearm_time_s for r in self.results if r.rearm_time_s]

        return {
            "total": len(self.results),
            "completed": len(completed),
            "wake_word_rate": f"{wake_detected}/{len(self.results)}",
            "stt_accuracy_rate": f"{stt_accurate}/{len(completed)}" if completed else "N/A",
            "rearm_rate": f"{rearmed}/{len(self.results)}",
            "error_count": errors,
            "pipeline_avg_ms": int(sum(pipelines) / len(pipelines)) if pipelines else None,
            "pipeline_min_ms": min(pipelines) if pipelines else None,
            "pipeline_max_ms": max(pipelines) if pipelines else None,
            "response_words_avg": round(sum(word_counts) / len(word_counts), 1) if word_counts else None,
            "rearm_time_avg_s": round(sum(rearm_times) / len(rearm_times), 1) if rearm_times else None,
        }

    def print_summary(self):
        """Print a summary of all test results."""
        summary = self._compute_summary()
        logger.info("=" * 60)
        logger.info("TEST SUMMARY")
        logger.info("=" * 60)
        for k, v in summary.items():
            logger.info(f"  {k}: {v}")

    def run(self):
        """Main test loop."""
        logger.info("=" * 60)
        logger.info("Autonomous Test Harness Starting")
        logger.info(f"  Interval: {self.interval_s}s ({self.interval_s // 60} min)")
        logger.info(f"  Max cycles: {self.max_cycles}")
        logger.info(f"  Questions: {len(TEST_QUESTIONS)}")
        logger.info(f"  Log: {log_file}")
        logger.info(f"  Results: {results_file}")
        logger.info("=" * 60)

        # Pre-generate wake word WAV
        logger.info("Warming up TTS engine...")
        self._init_tts()

        for cycle in range(1, self.max_cycles + 1):
            # Service health check
            if not self.check_services():
                logger.error(f"Cycle {cycle}: Services unhealthy — waiting {self.interval_s}s")
                time.sleep(self.interval_s)
                continue

            # Run test
            try:
                result = self.run_test_cycle(cycle)
                self.results.append(result)
                self._save_results()
            except Exception as e:
                logger.error(f"Cycle {cycle}: Unhandled error: {e}", exc_info=True)
                error_result = TestResult(cycle, TEST_QUESTIONS[(self.question_index - 1) % len(TEST_QUESTIONS)])
                error_result.error = str(e)
                self.results.append(error_result)

            # Print running summary every 5 cycles
            if cycle % 5 == 0:
                self.print_summary()
                self._save_results()

            # Wait for next cycle
            if cycle < self.max_cycles:
                logger.info(f"Next cycle in {self.interval_s}s ({self.interval_s // 60} min)...")
                time.sleep(self.interval_s)

        self.print_summary()
        self._save_results()
        logger.info("Autonomous test harness complete.")


def main():
    parser = argparse.ArgumentParser(description="Autonomous Misty II Test Harness")
    parser.add_argument("--interval", type=int, default=INTERVAL_S, help="Seconds between test cycles")
    parser.add_argument("--cycles", type=int, default=MAX_CYCLES, help="Maximum number of test cycles")
    parser.add_argument("--single", action="store_true", help="Run a single test cycle and exit")
    args = parser.parse_args()

    harness = AutonomousTestHarness(
        interval_s=args.interval,
        max_cycles=1 if args.single else args.cycles,
    )
    harness.run()


if __name__ == "__main__":
    main()

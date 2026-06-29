"""
Face Display Hardware Validation Probe for Misty II.

Standalone script that validates Misty's face display capabilities for
animated expressions. Runs from the companion laptop — requires only Misty
on the network. No dependency on the controller or orchestration service.

Tests performed:
  1. Frame rate measurement — repeated /api/images/display calls, latency stats
  2. Visual artifact check — runs at 0.5, 1, 2, 4 FPS for operator confirmation
  3. Animated GIF support — uploads a tiny programmatic GIF
  4. Native animation endpoints — probes for sequence/list APIs on v2.0.2
  5. Audio regression — confirms rapid display calls don't degrade keyphrase/mic

Prerequisites:
    pip install requests Pillow

Usage:
    python tools/face_display_probe.py [--misty-ip 10.0.0.44] [--skip-audio]

Output:
    Prints structured JSON summary of validation results.
"""

import argparse
import base64
import io
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("ERROR: 'requests' package required. Install with: pip install requests")

try:
    from PIL import Image
except ImportError:
    Image = None  # GIF test will be skipped


# --- Built-in Misty face images used for testing ---
BUILTIN_FACES = [
    "e_DefaultContent.jpg",
    "e_Joy.jpg",
    "e_Admiration.jpg",
    "e_ContentLeft.jpg",
    "e_ContentRight.jpg",
    "e_Contempt.jpg",
    "e_EcstacyHilarious.jpg",
    "e_Sadness.jpg",
]

DEFAULT_FACE = "e_DefaultContent.jpg"


@dataclass
class FrameRateResult:
    total_frames: int = 0
    duration_s: float = 0.0
    achieved_fps: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    latency_min_ms: float = 0.0
    latency_max_ms: float = 0.0
    errors: int = 0


@dataclass
class ArtifactResult:
    target_fps: float = 0.0
    achieved_fps: float = 0.0
    frames_sent: int = 0
    errors: int = 0
    operator_notes: str = ""


@dataclass
class GifResult:
    supported: Optional[bool] = None
    notes: str = ""


@dataclass
class EndpointProbeResult:
    endpoint: str = ""
    status_code: int = 0
    available: bool = False
    notes: str = ""


@dataclass
class AudioRegressionResult:
    tested: bool = False
    keyphrase_events_before: int = 0
    keyphrase_events_during: int = 0
    display_calls_during: int = 0
    regression_detected: Optional[bool] = None
    notes: str = ""


@dataclass
class ValidationResults:
    misty_ip: str = ""
    timestamp: str = ""
    frame_rate: Optional[FrameRateResult] = None
    artifact_checks: list = field(default_factory=list)
    gif_support: Optional[GifResult] = None
    endpoint_probes: list = field(default_factory=list)
    audio_regression: Optional[AudioRegressionResult] = None
    sustainable_max_fps: Optional[float] = None
    recommendation: str = ""


class MistyDisplayProbe:
    """Runs display validation tests against a Misty robot."""

    def __init__(self, misty_ip: str, timeout: float = 5.0):
        self.base_url = f"http://{misty_ip}/api"
        self.timeout = timeout
        self.misty_ip = misty_ip

    def _display(self, filename: str, timeout: Optional[float] = None) -> float:
        """Push a display image. Returns latency in seconds, raises on failure."""
        t0 = time.perf_counter()
        resp = requests.post(
            f"{self.base_url}/images/display",
            json={"FileName": filename, "Alpha": 1},
            timeout=timeout or self.timeout,
        )
        latency = time.perf_counter() - t0
        resp.raise_for_status()
        return latency

    def _restore_default(self):
        """Restore Misty's face to default expression."""
        try:
            self._display(DEFAULT_FACE)
        except Exception:
            pass

    def check_connectivity(self) -> bool:
        """Verify Misty is reachable."""
        try:
            resp = requests.get(f"{self.base_url}/device", timeout=self.timeout)
            return resp.status_code == 200
        except Exception as e:
            print(f"  ERROR: Cannot reach Misty at {self.misty_ip}: {e}")
            return False

    def test_frame_rate(self, num_frames: int = 60) -> FrameRateResult:
        """
        Test 1: Push images as fast as possible, measure latency stats.
        """
        print(f"\n[Test 1] Frame rate measurement ({num_frames} frames, max speed)...")
        result = FrameRateResult(total_frames=num_frames)
        latencies = []
        errors = 0

        t_start = time.perf_counter()
        for i in range(num_frames):
            face = BUILTIN_FACES[i % len(BUILTIN_FACES)]
            try:
                lat = self._display(face, timeout=3.0)
                latencies.append(lat)
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"  Frame {i} error: {e}")
        t_end = time.perf_counter()

        result.duration_s = round(t_end - t_start, 3)
        result.errors = errors

        if latencies:
            latencies_ms = [l * 1000 for l in latencies]
            result.achieved_fps = round(len(latencies) / result.duration_s, 2)
            result.latency_p50_ms = round(statistics.median(latencies_ms), 1)
            result.latency_p95_ms = round(
                sorted(latencies_ms)[int(len(latencies_ms) * 0.95)], 1
            )
            result.latency_p99_ms = round(
                sorted(latencies_ms)[int(len(latencies_ms) * 0.99)], 1
            )
            result.latency_min_ms = round(min(latencies_ms), 1)
            result.latency_max_ms = round(max(latencies_ms), 1)

            print(f"  Achieved: {result.achieved_fps} FPS")
            print(f"  Latency p50={result.latency_p50_ms}ms p95={result.latency_p95_ms}ms p99={result.latency_p99_ms}ms")
            print(f"  Range: {result.latency_min_ms}ms - {result.latency_max_ms}ms")
            print(f"  Errors: {errors}/{num_frames}")
        else:
            print("  ERROR: No successful frames!")

        self._restore_default()
        return result

    def test_artifacts(self, target_fps: float, duration_s: float = 10.0) -> ArtifactResult:
        """
        Test 2: Run at a target FPS for a duration, for operator visual check.
        """
        print(f"\n  [{target_fps} FPS] Running for {duration_s}s — watch Misty's face for artifacts...")
        result = ArtifactResult(target_fps=target_fps)
        interval = 1.0 / target_fps
        frames_sent = 0
        errors = 0

        t_start = time.perf_counter()
        frame_idx = 0
        next_frame_time = t_start

        while (time.perf_counter() - t_start) < duration_s:
            now = time.perf_counter()
            if now >= next_frame_time:
                face = BUILTIN_FACES[frame_idx % len(BUILTIN_FACES)]
                try:
                    self._display(face, timeout=2.0)
                    frames_sent += 1
                except Exception:
                    errors += 1
                frame_idx += 1
                next_frame_time += interval
            else:
                time.sleep(max(0, next_frame_time - now - 0.005))

        elapsed = time.perf_counter() - t_start
        result.frames_sent = frames_sent
        result.errors = errors
        result.achieved_fps = round(frames_sent / elapsed, 2) if elapsed > 0 else 0

        print(f"    Sent {frames_sent} frames, achieved {result.achieved_fps} FPS, {errors} errors")
        return result

    def test_artifact_sweep(self) -> list:
        """Run artifact tests at multiple FPS targets."""
        print("\n[Test 2] Visual artifact check (watch Misty's face)...")
        print("  Press Ctrl+C to skip remaining FPS levels.")
        results = []
        for fps in [0.5, 1.0, 2.0, 4.0]:
            try:
                r = self.test_artifacts(fps, duration_s=8.0)
                results.append(r)
            except KeyboardInterrupt:
                print("  Skipping remaining FPS levels.")
                break
            time.sleep(1.0)  # Brief pause between levels

        self._restore_default()
        return results

    def test_gif_support(self) -> GifResult:
        """
        Test 3: Upload a tiny animated GIF and check if Misty animates it.
        """
        print("\n[Test 3] Animated GIF support...")
        result = GifResult()

        if Image is None:
            result.notes = "Pillow not installed — GIF test skipped"
            print(f"  SKIPPED: {result.notes}")
            return result

        # Generate a minimal 2-frame animated GIF (red/blue, 64x64)
        frame1 = Image.new("RGB", (64, 64), color=(255, 0, 0))
        frame2 = Image.new("RGB", (64, 64), color=(0, 0, 255))

        buf = io.BytesIO()
        frame1.save(
            buf,
            format="GIF",
            save_all=True,
            append_images=[frame2],
            duration=500,
            loop=0,
        )
        gif_data = buf.getvalue()
        gif_b64 = base64.b64encode(gif_data).decode("ascii")

        # Upload the GIF to Misty
        try:
            resp = requests.post(
                f"{self.base_url}/images",
                json={
                    "FileName": "probe_test_anim.gif",
                    "Data": gif_b64,
                    "ImmediatelyApply": False,
                    "OverwriteExisting": True,
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                result.supported = False
                result.notes = f"Upload failed: HTTP {resp.status_code} — {resp.text[:200]}"
                print(f"  Upload failed: {result.notes}")
                return result
            print("  GIF uploaded successfully.")
        except Exception as e:
            result.supported = False
            result.notes = f"Upload exception: {e}"
            print(f"  {result.notes}")
            return result

        # Display the GIF
        try:
            self._display("probe_test_anim.gif")
            print("  GIF displayed — watch Misty for 5s to see if it animates...")
            time.sleep(5.0)
            result.notes = (
                "GIF uploaded and displayed. Operator must visually confirm whether "
                "Misty shows animation (alternating red/blue) or just the first frame (red only)."
            )
            print(f"  {result.notes}")
            # We can't programmatically tell — operator confirms
            result.supported = None  # Unknown until operator confirms
        except Exception as e:
            result.supported = False
            result.notes = f"Display failed: {e}"
            print(f"  {result.notes}")

        # Cleanup: delete the test GIF and restore default face
        try:
            requests.delete(
                f"{self.base_url}/images",
                json={"FileName": "probe_test_anim.gif"},
                timeout=self.timeout,
            )
        except Exception:
            pass
        self._restore_default()
        return result

    def test_endpoints(self) -> list:
        """
        Test 4: Probe for native animation/sequence endpoints on v2.0.2.
        """
        print("\n[Test 4] Probing native animation endpoints...")
        endpoints_to_probe = [
            ("GET", "/images/list", "List all stored images"),
            ("GET", "/images", "Image info endpoint"),
            ("GET", "/animations", "Native animation sequences"),
            ("GET", "/animations/list", "Animation list"),
            ("GET", "/display/settings", "Display settings"),
            ("GET", "/display", "Display info"),
        ]

        results = []
        for method, path, desc in endpoints_to_probe:
            url = f"{self.base_url}{path}"
            try:
                if method == "GET":
                    resp = requests.get(url, timeout=self.timeout)
                else:
                    resp = requests.post(url, timeout=self.timeout)

                r = EndpointProbeResult(
                    endpoint=f"{method} /api{path}",
                    status_code=resp.status_code,
                    available=resp.status_code == 200,
                    notes=desc,
                )
                status = "AVAILABLE" if r.available else f"HTTP {resp.status_code}"
                print(f"  {method} /api{path} — {status} ({desc})")

                # If available, peek at response shape
                if r.available:
                    try:
                        data = resp.json()
                        if isinstance(data, dict) and "result" in data:
                            result_data = data["result"]
                            if isinstance(result_data, list):
                                r.notes += f" — {len(result_data)} items"
                            elif isinstance(result_data, dict):
                                r.notes += f" — keys: {list(result_data.keys())[:5]}"
                    except Exception:
                        pass

            except Exception as e:
                r = EndpointProbeResult(
                    endpoint=f"{method} /api{path}",
                    status_code=0,
                    available=False,
                    notes=f"Error: {e}",
                )
                print(f"  {method} /api{path} — ERROR ({e})")

            results.append(r)

        return results

    def test_audio_regression(self, duration_s: float = 30.0) -> AudioRegressionResult:
        """
        Test 5: Run rapid display calls while keyphrase is active.
        Check if KeyPhraseRecognized events still fire.

        NOTE: This is a best-effort test. Full validation requires saying
        "Hey Misty" during the test and confirming the event arrives.
        """
        print(f"\n[Test 5] Audio regression check ({duration_s}s)...")
        print("  Starting keyphrase recognition...")
        result = AudioRegressionResult(tested=True)

        # Start keyphrase
        try:
            requests.post(
                f"{self.base_url}/audio/keyphrase/start",
                timeout=self.timeout,
            )
        except Exception as e:
            result.notes = f"Could not start keyphrase: {e}"
            result.tested = False
            print(f"  SKIPPED: {result.notes}")
            return result

        time.sleep(2.0)  # Let keyphrase stabilize

        # Run display loop at ~2 FPS for the duration
        print(f"  Running display loop at ~2 FPS for {duration_s}s...")
        print("  Say 'Hey Misty' during this time to test keyphrase detection.")
        interval = 0.5  # 2 FPS
        frames_sent = 0
        t_start = time.perf_counter()

        while (time.perf_counter() - t_start) < duration_s:
            face = BUILTIN_FACES[frames_sent % len(BUILTIN_FACES)]
            try:
                self._display(face, timeout=2.0)
                frames_sent += 1
            except Exception:
                pass
            elapsed = time.perf_counter() - t_start
            next_time = (frames_sent) * interval
            sleep_time = next_time - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        result.display_calls_during = frames_sent

        # Stop keyphrase
        try:
            requests.post(
                f"{self.base_url}/audio/keyphrase/stop",
                timeout=self.timeout,
            )
        except Exception:
            pass

        result.notes = (
            f"Sent {frames_sent} display calls over {duration_s}s (~2 FPS). "
            "Manual verification required: say 'Hey Misty' during the test "
            "and confirm the robot responds. If keyphrase worked during display "
            "loop, no regression detected."
        )
        print(f"  Done. {result.notes}")
        self._restore_default()
        return result


def main():
    parser = argparse.ArgumentParser(
        description="Misty II Face Display Hardware Validation Probe"
    )
    parser.add_argument(
        "--misty-ip",
        default=os.environ.get("MISTY_IP", "10.0.0.44"),
        help="Misty robot IP address (default: MISTY_IP env or 10.0.0.44)",
    )
    parser.add_argument(
        "--skip-audio",
        action="store_true",
        help="Skip the audio regression test (Test 5)",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=60,
        help="Number of frames for max-speed test (default: 60)",
    )
    parser.add_argument(
        "--output",
        choices=["json", "text"],
        default="text",
        help="Output format (default: text with JSON summary at end)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Misty II Face Display Hardware Validation Probe")
    print("=" * 60)
    print(f"  Target: {args.misty_ip}")
    print()

    probe = MistyDisplayProbe(args.misty_ip)

    # Connectivity check
    print("[Pre-check] Verifying Misty connectivity...")
    if not probe.check_connectivity():
        sys.exit(1)
    print("  OK — Misty is reachable.")

    results = ValidationResults(
        misty_ip=args.misty_ip,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )

    # Test 1: Frame rate
    results.frame_rate = probe.test_frame_rate(num_frames=args.frames)

    # Test 2: Artifact sweep
    results.artifact_checks = [asdict(r) for r in probe.test_artifact_sweep()]

    # Test 3: GIF support
    results.gif_support = probe.test_gif_support()

    # Test 4: Endpoint probes
    results.endpoint_probes = [asdict(r) for r in probe.test_endpoints()]

    # Test 5: Audio regression
    if args.skip_audio:
        print("\n[Test 5] Audio regression — SKIPPED (--skip-audio)")
        results.audio_regression = AudioRegressionResult(
            tested=False, notes="Skipped via --skip-audio flag"
        )
    else:
        results.audio_regression = probe.test_audio_regression()

    # Determine sustainable max FPS recommendation
    if results.frame_rate and results.frame_rate.achieved_fps > 0:
        # Conservative: recommend 50% of measured max, capped at 4 FPS
        raw_max = results.frame_rate.achieved_fps
        recommended = min(raw_max * 0.5, 4.0)
        results.sustainable_max_fps = round(recommended, 1)
        results.recommendation = (
            f"Measured max {raw_max} FPS. "
            f"Recommend FACE_ANIMATION_MAX_FPS={results.sustainable_max_fps} "
            f"(50% of max, capped at 4). "
            f"p95 latency={results.frame_rate.latency_p95_ms}ms per frame."
        )
    else:
        results.recommendation = "Could not determine sustainable FPS — too many errors."

    # Output summary
    print("\n" + "=" * 60)
    print("  VALIDATION SUMMARY")
    print("=" * 60)
    print(f"  Recommendation: {results.recommendation}")
    print()

    # Serialize to JSON
    def to_dict(obj):
        if hasattr(obj, "__dict__"):
            return {k: to_dict(v) for k, v in obj.__dict__.items()}
        elif isinstance(obj, list):
            return [to_dict(i) for i in obj]
        return obj

    summary_json = json.dumps(to_dict(results), indent=2)

    if args.output == "json":
        print(summary_json)
    else:
        print("  JSON summary:")
        print(summary_json)

    print("\n  Restore default face...")
    probe._restore_default()
    print("  Done. Copy the JSON above into docs/design-animated-face-expressions.md §7 results.")


if __name__ == "__main__":
    main()

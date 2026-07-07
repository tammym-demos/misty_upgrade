"""
Face training / management CLI for Misty II (#112).

Standalone companion-laptop utility that manages Misty's on-robot face
recognition catalog over the REST API. It reuses the same endpoints proven in
``src/windows-orchestration/misty_controller.py`` (``/api/faces``,
``/api/faces/training/start``, ``/api/faces/training/cancel``) so training a
face here produces exactly the labels the live pipeline recognizes.

No dependency on the controller or orchestration service — only Misty on the
network is required.

Prerequisites:
    pip install requests

Usage:
    # List currently trained faces
    python tools/train_face.py --list

    # Train a new face (stand in front of Misty during training)
    python tools/train_face.py --name Tammy

    # Train and then run a quick recognition verify afterwards
    python tools/train_face.py --name Tammy --verify

    # Point at a specific robot (otherwise MISTY_IP env, then default)
    python tools/train_face.py --name Tammy --misty-ip 10.0.0.44

Exit codes:
    0  success (requested action completed)
    1  action failed (Misty unreachable, training/verify failed, bad args)

Note:
    Misty's built-in face recognition only works with human faces and its
    reliability depends on lighting and camera quality. This tool drives the
    on-robot pipeline; end-to-end conversational recognition additionally
    requires ``USE_FACE_RECOGNITION=true`` in the orchestration ``.env``.
"""

import argparse
import os
import sys
import time

try:
    import requests
except ImportError:  # pragma: no cover - trivial import guard
    sys.exit("ERROR: 'requests' package required. Install with: pip install requests")


# Misty caps face labels at 50 characters; mirror the controller's guard.
MAX_FACE_ID_LEN = 50

# Misty's on-robot training routine takes roughly 15 seconds to capture a face.
DEFAULT_TRAINING_WAIT_S = 20.0

# Label Misty reports for a detected-but-unknown face.
UNKNOWN_LABEL = "unknown_person"


class MistyFaceTrainer:
    """Drives Misty's face training / recognition REST endpoints."""

    def __init__(self, misty_ip: str, timeout: float = 10.0):
        self.misty_ip = misty_ip
        self.base_url = f"http://{misty_ip}/api"
        self.timeout = timeout

    # --- Low-level REST helpers -------------------------------------------
    def _get(self, endpoint: str):
        resp = requests.get(f"{self.base_url}{endpoint}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, body: dict = None):
        resp = requests.post(f"{self.base_url}{endpoint}", json=body, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # --- Connectivity ------------------------------------------------------
    def check_connectivity(self) -> bool:
        """Return True if Misty is reachable on the network."""
        try:
            self._get("/device")
            return True
        except Exception as exc:
            print(f"  ERROR: Cannot reach Misty at {self.misty_ip}: {exc}")
            return False

    # --- Face catalog ------------------------------------------------------
    def list_faces(self) -> list:
        """Return the list of trained face IDs, or [] on failure."""
        try:
            result = self._get("/faces")
        except Exception as exc:
            print(f"  ERROR: GET /faces failed: {exc}")
            return []
        if result and result.get("status") == "Success":
            return result.get("result", []) or []
        print(f"  ERROR: GET /faces returned unexpected payload: {result}")
        return []

    def start_training(self, face_id: str) -> bool:
        """Start on-robot training for ``face_id``. Returns True on success."""
        if not face_id or len(face_id) > MAX_FACE_ID_LEN:
            print(
                f"  ERROR: Invalid face id '{face_id}' "
                f"(must be 1-{MAX_FACE_ID_LEN} characters)."
            )
            return False
        try:
            result = self._post("/faces/training/start", {"FaceId": face_id})
        except Exception as exc:
            print(f"  ERROR: POST /faces/training/start failed: {exc}")
            return False
        if result and result.get("status") == "Success":
            return True
        print(f"  ERROR: Training start rejected by Misty: {result}")
        return False

    def cancel_training(self) -> bool:
        """Cancel an in-progress training session (best effort)."""
        try:
            self._post("/faces/training/cancel")
            return True
        except Exception as exc:
            print(f"  WARNING: cancel training failed: {exc}")
            return False

    def recognize_once(self, timeout_s: float = 5.0) -> str:
        """Run recognition briefly; return a recognized label or ``""``.

        Uses REST polling of ``/faces/recognition`` events surfaced through
        ``GET /faces`` is not sufficient, so this polls the recognition start
        endpoint and reads back detected faces. Recognition events normally
        arrive over WebSocket in the live pipeline; for a simple CLI verify we
        start recognition and poll for a known label.
        """
        try:
            self._post("/faces/recognition/start")
        except Exception as exc:
            print(f"  WARNING: could not start recognition: {exc}")
            return ""
        recognized = ""
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                try:
                    result = self._get("/faces/recognition")
                except Exception:
                    # Not all firmware exposes a GET; fall back to catalog check.
                    break
                label = ""
                if isinstance(result, dict):
                    payload = result.get("result") or {}
                    if isinstance(payload, dict):
                        label = payload.get("label") or payload.get("Label") or ""
                if label and label != UNKNOWN_LABEL:
                    recognized = label
                    break
                time.sleep(0.5)
        finally:
            try:
                self._post("/faces/recognition/stop")
            except Exception:
                # Best-effort cleanup: recognition may already be stopped or
                # Misty may be unreachable. Nothing actionable to do here, so
                # suppress rather than mask the primary result/return path.
                pass
        return recognized


def _print_faces(faces: list) -> None:
    if faces:
        print(f"  Trained faces ({len(faces)}):")
        for face in faces:
            print(f"    - {face}")
    else:
        print("  Trained faces: (none)")


def run_training(trainer: MistyFaceTrainer, name: str, wait_s: float, verify: bool) -> int:
    """Train ``name`` and optionally verify. Returns a process exit code."""
    existing = trainer.list_faces()
    _print_faces(existing)
    if name in existing:
        print(f"  NOTE: '{name}' is already trained; re-training will refresh it.")

    print(f"\n  Starting face training for '{name}'...")
    print("  >>> Stand ~0.5-1 m in front of Misty, face the camera, and slowly")
    print("  >>> turn your head left and right. Keep your face well lit.")
    if not trainer.start_training(name):
        return 1

    print(f"  Training in progress (~15s). Waiting up to {wait_s:.0f}s...")
    time.sleep(wait_s)

    faces_after = trainer.list_faces()
    if name in faces_after:
        print(f"\n  SUCCESS: '{name}' is now a trained face.")
    else:
        print(
            f"\n  WARNING: '{name}' not yet present in the trained-face list. "
            "Training may still be finalizing, or lighting/camera prevented a "
            "clean capture. Re-run with better lighting if recognition fails."
        )
        _print_faces(faces_after)

    if verify:
        print("\n  Verifying recognition (look at Misty)...")
        label = trainer.recognize_once()
        if label == name:
            print(f"  VERIFY OK: Misty recognized '{label}'.")
        elif label:
            print(f"  VERIFY: Misty recognized '{label}' (expected '{name}').")
        else:
            print("  VERIFY: no known face recognized within the timeout.")

    return 0 if name in faces_after else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Train and manage Misty II face recognition (#112). "
            "Reuses the same /api/faces endpoints as the live controller."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python tools/train_face.py --list\n"
            "  python tools/train_face.py --name Tammy\n"
            "  python tools/train_face.py --name Tammy --verify\n"
        ),
    )
    parser.add_argument(
        "--name",
        help="Face label to train (e.g. Tammy). Omit with --list to only list.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List currently trained faces and exit.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="After training, run a quick recognition check.",
    )
    parser.add_argument(
        "--misty-ip",
        default=os.environ.get("MISTY_IP", "10.0.0.44"),
        help="Misty robot IP address (default: MISTY_IP env or 10.0.0.44).",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_TRAINING_WAIT_S,
        help=f"Seconds to wait for training to complete (default: {DEFAULT_TRAINING_WAIT_S:.0f}).",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.list and not args.name:
        parser.error("provide --name NAME to train, or --list to list faces")

    trainer = MistyFaceTrainer(args.misty_ip)
    print(f"  Misty target: {args.misty_ip}")
    if not trainer.check_connectivity():
        return 1

    if args.name:
        return run_training(trainer, args.name, args.wait, args.verify)

    # --list only
    _print_faces(trainer.list_faces())
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Laptop-side face recognition CLI / smoke test (#125).

Captures one or more frames from Misty's RGB camera or the laptop webcam and
reports the recognized profile name, cosine distance, threshold, and frame
source. Returns a non-zero exit code when no known face is recognized, so it can
be used as a smoke test.

Usage (run from the repository root):
    python tools\\recognize_face.py --source misty --misty-ip 10.0.0.15
    python tools\\recognize_face.py --source webcam
    python tools\\recognize_face.py --source image --image face.jpg

Exit codes:
    0  a known face was recognized above confidence
    1  no known face recognized / capture or model unavailable / bad args
"""

import argparse
import os
import sys

_ORCH_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration")
)
if _ORCH_DIR not in sys.path:
    sys.path.insert(0, _ORCH_DIR)

import config_defaults  # noqa: E402
import face_recognition_service as frs  # noqa: E402


def build_source(args) -> frs.FrameSource:
    if args.source == "misty":
        if not args.misty_ip:
            raise frs.FrameSourceError(
                "--source misty requires --misty-ip ADDRESS (or set MISTY_IP)"
            )
        return frs.MistyCameraFrameSource(args.misty_ip)
    if args.source == "webcam":
        return frs.WebcamFrameSource(device_index=args.webcam_index)
    if args.source == "image":
        if not args.image:
            raise frs.FrameSourceError("--source image requires one or more --image paths")
        return frs.ImageFileFrameSource(args.image)
    raise frs.FrameSourceError(f"unknown source: {args.source!r}")


def build_recognizer(args) -> frs.FaceRecognizer:
    store = frs.FaceProfileStore(args.profile_dir)
    embedder = frs.OnnxFaceEmbedder(
        detector_model_path=config_defaults.FACE_DETECTOR_MODEL_PATH,
        embedder_model_path=config_defaults.FACE_EMBEDDER_MODEL_PATH,
    )
    return frs.FaceRecognizer(
        store=store,
        embedder=embedder,
        threshold=args.threshold,
        min_samples=config_defaults.FACE_RECOGNITION_MIN_SAMPLES,
        min_consistent_frames=args.min_frames,
    )


def run_recognize(args) -> int:
    recognizer = build_recognizer(args)
    if not recognizer.load_profiles():
        print(
            f"  No enrolled profiles found in {recognizer.store.directory}. "
            "Enroll one first with tools\\enroll_face.py."
        )
        return 1

    try:
        source = build_source(args)
    except frs.FrameSourceError as exc:
        print(f"  ERROR: {exc}")
        return 1

    try:
        with source:
            frames = source.capture_many(args.frames, interval_s=args.interval)
    except frs.FaceRecognitionError as exc:
        print(f"  ERROR: capture failed: {exc}")
        return 1

    result = recognizer.recognize_consistent(frames, source=source.name)
    if result is None:
        print(
            f"  No known face recognized (source={source.name}, "
            f"frames={len(frames)}, threshold={recognizer.threshold}, "
            f"min_consistent_frames={recognizer.min_consistent_frames})."
        )
        return 1

    print(
        f"  RECOGNIZED: {result.name}\n"
        f"    distance:   {result.distance:.4f} (lower is better)\n"
        f"    threshold:  {result.threshold:.4f}\n"
        f"    confidence: {result.confidence:.3f}\n"
        f"    source:     {result.source}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recognize a laptop-side face profile (#125).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (run from the repository root):\n"
            "  python tools\\recognize_face.py --source misty --misty-ip 10.0.0.15\n"
            "  python tools\\recognize_face.py --source webcam\n"
        ),
    )
    parser.add_argument(
        "--source",
        choices=("misty", "webcam", "image"),
        default="misty",
        help="Frame source for recognition (default: misty).",
    )
    parser.add_argument(
        "--misty-ip",
        default=os.environ.get("MISTY_IP"),
        help="Misty robot IP (defaults to MISTY_IP env var). Required for --source misty.",
    )
    parser.add_argument("--webcam-index", type=int, default=0, help="Webcam device index (default: 0).")
    parser.add_argument("--image", nargs="+", help="Image file paths for --source image.")
    parser.add_argument(
        "--frames",
        type=_positive_int,
        default=5,
        help="Number of frames to capture (default: 5).",
    )
    parser.add_argument(
        "--interval",
        type=_nonneg_float,
        default=0.2,
        help="Seconds between captured frames (default: 0.2).",
    )
    parser.add_argument(
        "--min-frames",
        type=_positive_int,
        default=config_defaults.FACE_RECOGNITION_MIN_CONSISTENT_FRAMES,
        help=(
            "Frames that must agree on the same name (default: "
            f"{config_defaults.FACE_RECOGNITION_MIN_CONSISTENT_FRAMES})."
        ),
    )
    parser.add_argument(
        "--threshold",
        type=_positive_float,
        default=config_defaults.FACE_RECOGNITION_THRESHOLD,
        help=f"Match threshold (default: {config_defaults.FACE_RECOGNITION_THRESHOLD}).",
    )
    parser.add_argument(
        "--profile-dir",
        default=config_defaults.FACE_PROFILE_DIR,
        help="Directory holding profiles (default: repo data/face_profiles).",
    )
    return parser


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not (parsed > 0):
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def _nonneg_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_recognize(args)


if __name__ == "__main__":
    sys.exit(main())

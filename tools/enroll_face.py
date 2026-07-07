"""Laptop-side face enrollment CLI (#125).

Enrolls a named local face profile from Misty's RGB camera or the laptop webcam
and stores it in a gitignored local profile directory. This replaces Misty's
unreliable on-chip ``/api/faces`` training (see docs/lessons-learned.md).

Only numeric face embeddings and metadata are stored — never photos.

Usage (run from the repository root):
    python tools\\enroll_face.py --name Tammy --source misty --misty-ip 10.0.0.15 --samples 10
    python tools\\enroll_face.py --name Tammy --source webcam --samples 10
    python tools\\enroll_face.py --name Tammy --source image --image face1.jpg face2.jpg
    python tools\\enroll_face.py --list
    python tools\\enroll_face.py --delete Tammy

Exit codes:
    0  success (requested action completed)
    1  failure (too few samples, unavailable model/camera, bad args)
"""

import argparse
import os
import sys

# Make the windows-orchestration package importable.
_ORCH_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration")
)
if _ORCH_DIR not in sys.path:
    sys.path.insert(0, _ORCH_DIR)

import config_defaults  # noqa: E402
import face_recognition_service as frs  # noqa: E402


def build_source(args) -> frs.FrameSource:
    """Construct the requested frame source or raise FrameSourceError."""
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
        min_samples=args.min_samples,
        min_consistent_frames=config_defaults.FACE_RECOGNITION_MIN_CONSISTENT_FRAMES,
    )


def run_list(args) -> int:
    store = frs.FaceProfileStore(args.profile_dir)
    names = store.list_names()
    if names:
        print(f"  Enrolled profiles ({len(names)}) in {store.directory}:")
        for name in names:
            try:
                profile = store.load(name)
                print(
                    f"    - {name}  (samples={profile.sample_count}, "
                    f"dim={profile.embedding_dim}, model={profile.model_name}"
                    f"@{profile.model_version}, created={profile.created_at})"
                )
            except Exception as exc:
                print(f"    - {name}  (unreadable: {exc})")
    else:
        print(f"  Enrolled profiles: (none) in {store.directory}")
    return 0


def run_delete(args) -> int:
    store = frs.FaceProfileStore(args.profile_dir)
    try:
        deleted = store.delete(args.delete)
    except frs.ProfileNameError as exc:
        print(f"  ERROR: {exc}")
        return 1
    if deleted:
        print(f"  Deleted profile '{args.delete}'.")
        return 0
    print(f"  No profile named '{args.delete}' to delete.")
    return 1


def run_enroll(args) -> int:
    try:
        frs.validate_profile_name(args.name)
    except frs.ProfileNameError as exc:
        print(f"  ERROR: {exc}")
        return 1

    try:
        source = build_source(args)
    except frs.FrameSourceError as exc:
        print(f"  ERROR: {exc}")
        return 1

    recognizer = build_recognizer(args)

    print(f"  Enrolling '{args.name}' from {args.source} source.")
    print("  >>> Face the camera in good, even lighting.")
    print("  >>> Slowly turn your head left and right during capture.")
    if args.preview_only:
        print("  (--preview-only) No profile will be saved.")

    try:
        with source:
            frames = source.capture_many(args.samples, interval_s=args.interval)
    except frs.FaceRecognitionError as exc:
        print(f"  ERROR: capture failed: {exc}")
        return 1

    print(f"  Captured {len(frames)} frame(s); extracting face embeddings...")

    if args.preview_only:
        # Dry run: count valid single-face frames without persisting.
        valid = 0
        rejected = 0
        for frame in frames:
            try:
                recognizer._single_face_embedding(frame)
                valid += 1
            except frs.FaceRecognitionError:
                rejected += 1
        print(f"  PREVIEW: {valid} valid sample(s), {rejected} rejected. Nothing saved.")
        return 0 if valid >= recognizer.min_samples else 1

    try:
        profile = recognizer.enroll(args.name, frames)
    except frs.FaceRecognitionError as exc:
        print(f"  ERROR: enrollment failed: {exc}")
        return 1

    path = recognizer.store.path_for(profile.name)
    print(
        f"\n  SUCCESS: enrolled '{profile.name}' "
        f"({profile.sample_count} samples, dim={profile.embedding_dim})."
    )
    print(f"  Saved profile: {path}")
    print("  Reminder: this file contains biometric embeddings — keep it out of git.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enroll a laptop-side face profile (#125).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (run from the repository root):\n"
            "  python tools\\enroll_face.py --name Tammy --source misty --misty-ip 10.0.0.15 --samples 10\n"
            "  python tools\\enroll_face.py --name Tammy --source webcam --samples 10\n"
            "  python tools\\enroll_face.py --list\n"
            "  python tools\\enroll_face.py --delete Tammy\n"
        ),
    )
    parser.add_argument("--name", help="Profile name to enroll (e.g. Tammy).")
    parser.add_argument("--list", action="store_true", help="List enrolled profiles and exit.")
    parser.add_argument("--delete", metavar="NAME", help="Delete an enrolled profile and exit.")
    parser.add_argument(
        "--source",
        choices=("misty", "webcam", "image"),
        default="misty",
        help="Frame source for enrollment (default: misty).",
    )
    parser.add_argument(
        "--misty-ip",
        default=os.environ.get("MISTY_IP"),
        help="Misty robot IP (defaults to MISTY_IP env var). Required for --source misty.",
    )
    parser.add_argument("--webcam-index", type=int, default=0, help="Webcam device index (default: 0).")
    parser.add_argument("--image", nargs="+", help="Image file paths for --source image.")
    parser.add_argument(
        "--samples",
        type=_positive_int,
        default=10,
        help="Number of frames to CAPTURE (default: 10).",
    )
    parser.add_argument(
        "--min-samples",
        type=_positive_int,
        default=config_defaults.FACE_RECOGNITION_MIN_SAMPLES,
        help=(
            "Minimum number of VALID single-face samples required to enroll "
            f"(default: {config_defaults.FACE_RECOGNITION_MIN_SAMPLES}). Frames with "
            "no face or multiple faces are skipped, so this is independent of --samples."
        ),
    )
    parser.add_argument(
        "--interval",
        type=_nonneg_float,
        default=0.3,
        help="Seconds between captured frames (default: 0.3).",
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
        help="Directory to store profiles (default: repo data/face_profiles).",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="Dry run: capture and count valid samples without saving a profile.",
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

    actions = [bool(args.list), bool(args.delete), bool(args.name)]
    if sum(actions) != 1:
        parser.error("choose exactly one of --name NAME, --list, or --delete NAME")

    if args.list:
        return run_list(args)
    if args.delete:
        return run_delete(args)
    return run_enroll(args)


if __name__ == "__main__":
    sys.exit(main())

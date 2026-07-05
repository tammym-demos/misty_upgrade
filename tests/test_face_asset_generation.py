"""Regression tests for generated custom Misty face assets."""

import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageStat


REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION_PATH = REPO_ROOT / "src" / "windows-orchestration"
if str(ORCHESTRATION_PATH) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATION_PATH))

import config_defaults  # noqa: E402


def _load_generator():
    path = REPO_ROOT / "tools" / "generate_face_assets.py"
    spec = importlib.util.spec_from_file_location("generate_face_assets", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mean_luma(path: Path) -> float:
    with Image.open(path).convert("RGB") as image:
        stat = ImageStat.Stat(image)
    return sum(stat.mean) / 3


def test_sprite_frame_crops_match_misty_display_aspect():
    generator = _load_generator()
    target = generator.WIDTH / generator.HEIGHT

    for name, box in generator.SPRITE_FRAMES.items():
        x1, y1, x2, y2 = box
        aspect = (x2 - x1) / (y2 - y1)
        assert aspect == pytest.approx(target, abs=0.01), name


def test_required_face_assets_are_misty_display_sized():
    assets_dir = REPO_ROOT / "assets"

    for filename in config_defaults.REQUIRED_FACE_ASSETS:
        with Image.open(assets_dir / filename) as image:
            assert image.size == (480, 272), filename


def test_generated_talking_gifs_have_expected_frame_count():
    assets_dir = REPO_ROOT / "assets"

    for filename in config_defaults.REQUIRED_FACE_ASSETS:
        if filename.startswith("face_talking_") and filename.endswith(".gif"):
            with Image.open(assets_dir / filename) as image:
                assert image.n_frames == 4, filename


def test_extracted_frames_are_bright_enough_for_misty_display():
    frames_dir = REPO_ROOT / "assets" / "frames"

    for frame in frames_dir.glob("frame_*.png"):
        assert _mean_luma(frame) >= 105, frame.name

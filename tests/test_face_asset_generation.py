"""Regression tests for generated custom Misty face assets."""

import importlib.util
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageStat


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


def test_vector_frame_renders_at_misty_display_size():
    generator = _load_generator()

    frame = generator.render_face_frame(emotion="happy", openness=0.5, pulse=0.8)

    assert frame.size == (generator.WIDTH, generator.HEIGHT)
    assert generator.TARGET_ASPECT == generator.WIDTH / generator.HEIGHT


def test_single_mouth_anchor_is_centered_and_bounded():
    generator = _load_generator()

    closed = generator.mouth_bounds(0.0)
    open_mouth = generator.mouth_bounds(1.0)

    assert closed[0] == open_mouth[0]
    assert closed[2] == open_mouth[2]
    assert open_mouth[1] < generator.MOUTH_CENTER[1] < open_mouth[3]
    assert open_mouth[2] - open_mouth[0] == generator.MOUTH_MAX_WIDTH
    assert open_mouth[3] - open_mouth[1] <= generator.MOUTH_MAX_HEIGHT


def test_mouth_uses_magenta_crescent_lower_region():
    generator = _load_generator()
    frame = generator.render_face_frame(emotion="happy", openness=0.7, pulse=0.8).convert("RGB")
    x1, y1, x2, y2 = generator.mouth_bounds(0.7)
    lower_mouth = frame.crop((x1 + 20, y1 + 24, x2 - 20, y2 + 16))
    stat = ImageStat.Stat(lower_mouth)

    assert stat.mean[0] > stat.mean[1] + 30
    assert stat.mean[2] > stat.mean[1] + 15


def test_happy_talking_accent_is_blue_not_yellow():
    generator = _load_generator()

    accent = generator.EMOTION_ACCENT["happy"]
    eye = generator.EMOTION_EYE["happy"]

    assert accent[2] > accent[0]
    assert eye[2] > eye[0]
    assert accent[0] < 120


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


def test_generated_faces_are_readable_without_washing_out():
    assets_dir = REPO_ROOT / "assets"

    for filename in config_defaults.REQUIRED_FACE_ASSETS:
        luma = _mean_luma(assets_dir / filename)
        assert 25 <= luma <= 180, filename


def test_talking_emotions_remain_distinct_in_grayscale():
    assets_dir = REPO_ROOT / "assets"
    generator = _load_generator()

    with Image.open(assets_dir / "face_talking_neutral.png").convert("L") as neutral:
        for emotion in generator.EMOTIONS:
            if emotion == "neutral":
                continue
            with Image.open(
                assets_dir / f"face_talking_{emotion}.png"
            ).convert("L") as expression:
                difference = ImageChops.difference(neutral, expression)
                assert ImageStat.Stat(difference).mean[0] > 3.0, emotion


def test_preview_contact_sheet_is_generated():
    assets_dir = REPO_ROOT / "assets"
    contact_sheet = assets_dir / _load_generator().PREVIEW_CONTACT_SHEET

    assert contact_sheet.exists()
    with Image.open(contact_sheet) as image:
        expected_rows = (3 + len(_load_generator().EMOTIONS) + 1) // 2
        assert image.size == (480, 136 * expected_rows)

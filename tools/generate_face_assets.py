"""
Generate custom face assets for Misty II.

Creates a consistent face identity with emotion variants and talking mouth
animations. All faces use the same base eye style with variations in eyebrows,
eye size, and mouth shape to express different emotions naturally.

Design: GitHub issue #110
Display: 480x272 pixels, white/purple on black (matching Misty's built-in style)

Usage:
    python tools/generate_face_assets.py [--misty-ip 10.0.0.23] [--upload]

Output:
    assets/face_*.gif and assets/face_*.png uploaded to Misty
"""

import argparse
import base64
import io
import math
import os
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

from PIL import Image, ImageDraw


# --- Constants ---
WIDTH, HEIGHT = 480, 272
BG_COLOR = (0, 0, 0)
EYE_WHITE = (255, 255, 255)
EYE_OUTLINE = (200, 200, 200)
IRIS_COLOR = (148, 0, 211)  # purple, matching Misty's built-in style
PUPIL_COLOR = (20, 20, 20)
MOUTH_WHITE = (255, 255, 255)
MOUTH_INNER = (30, 30, 30)


def draw_eye(draw, cx, cy, size=50, openness=1.0, brow_angle=0, look_x=0, look_y=0):
    """Draw a single eye with iris, pupil, and eyebrow.

    Args:
        cx, cy: center of eye
        size: radius of eye
        openness: 0.0 (closed) to 1.0 (fully open)
        brow_angle: degrees rotation of eyebrow (-15 to 15)
        look_x, look_y: iris offset from center (-10 to 10)
    """
    # Eye white (outer circle, vertically squished by openness)
    h = int(size * openness)
    if h < 5:
        # Eye closed — just a line
        draw.line([cx - size, cy, cx + size, cy], fill=EYE_WHITE, width=3)
        return

    # Outer white ring
    draw.ellipse(
        [cx - size, cy - h, cx + size, cy + h],
        fill=None, outline=EYE_WHITE, width=4,
    )
    # Fill white
    draw.ellipse(
        [cx - size + 2, cy - h + 2, cx + size - 2, cy + h - 2],
        fill=EYE_WHITE,
    )

    # Iris (inner colored circle)
    iris_r = int(size * 0.55)
    iris_cx = cx + look_x
    iris_cy = cy + look_y
    draw.ellipse(
        [iris_cx - iris_r, iris_cy - iris_r, iris_cx + iris_r, iris_cy + iris_r],
        fill=IRIS_COLOR, outline=IRIS_COLOR,
    )

    # Iris ring (darker purple border)
    draw.ellipse(
        [iris_cx - iris_r, iris_cy - iris_r, iris_cx + iris_r, iris_cy + iris_r],
        fill=None, outline=(100, 0, 160), width=3,
    )

    # Pupil
    pupil_r = int(size * 0.22)
    draw.ellipse(
        [iris_cx - pupil_r, iris_cy - pupil_r, iris_cx + pupil_r, iris_cy + pupil_r],
        fill=PUPIL_COLOR,
    )

    # Light reflection spot
    ref_x = iris_cx + int(size * 0.15)
    ref_y = iris_cy - int(size * 0.15)
    ref_r = int(size * 0.1)
    draw.ellipse(
        [ref_x - ref_r, ref_y - ref_r, ref_x + ref_r, ref_y + ref_r],
        fill=(255, 255, 255),
    )

    # Eyebrow
    brow_y = cy - h - 12
    brow_half = int(size * 0.8)
    # Apply angle
    offset = int(math.sin(math.radians(brow_angle)) * 10)
    draw.line(
        [cx - brow_half, brow_y + offset, cx + brow_half, brow_y - offset],
        fill=EYE_WHITE, width=4,
    )


def draw_mouth(draw, cx, cy, mouth_type="smile", size=1.0):
    """Draw mouth at given position.

    mouth_type: 'smile', 'open', 'wide', 'frown', 'neutral', 'tiny_open'
    size: multiplier for mouth dimensions
    """
    s = size
    if mouth_type == "smile":
        draw.arc(
            [int(cx - 40 * s), int(cy - 12 * s), int(cx + 40 * s), int(cy + 20 * s)],
            start=0, end=180, fill=MOUTH_WHITE, width=4,
        )
    elif mouth_type == "open":
        draw.ellipse(
            [int(cx - 30 * s), int(cy - 16 * s), int(cx + 30 * s), int(cy + 16 * s)],
            fill=MOUTH_WHITE,
        )
        draw.ellipse(
            [int(cx - 22 * s), int(cy - 10 * s), int(cx + 22 * s), int(cy + 10 * s)],
            fill=MOUTH_INNER,
        )
    elif mouth_type == "wide":
        draw.ellipse(
            [int(cx - 40 * s), int(cy - 20 * s), int(cx + 40 * s), int(cy + 20 * s)],
            fill=MOUTH_WHITE,
        )
        draw.ellipse(
            [int(cx - 30 * s), int(cy - 13 * s), int(cx + 30 * s), int(cy + 13 * s)],
            fill=MOUTH_INNER,
        )
    elif mouth_type == "frown":
        draw.arc(
            [int(cx - 35 * s), int(cy - 5 * s), int(cx + 35 * s), int(cy + 25 * s)],
            start=180, end=360, fill=MOUTH_WHITE, width=4,
        )
    elif mouth_type == "neutral":
        draw.line(
            [int(cx - 25 * s), cy, int(cx + 25 * s), cy],
            fill=MOUTH_WHITE, width=3,
        )
    elif mouth_type == "tiny_open":
        draw.ellipse(
            [int(cx - 18 * s), int(cy - 10 * s), int(cx + 18 * s), int(cy + 10 * s)],
            fill=MOUTH_WHITE,
        )
        draw.ellipse(
            [int(cx - 12 * s), int(cy - 6 * s), int(cx + 12 * s), int(cy + 6 * s)],
            fill=MOUTH_INNER,
        )


def make_face(
    openness=1.0, brow_angle=0, look_x=0, look_y=0,
    mouth_type="smile", mouth_size=1.0, eye_size=50,
):
    """Generate a complete face image."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Eye positions (centered, slightly above middle)
    left_eye_cx, left_eye_cy = 160, 110
    right_eye_cx, right_eye_cy = 320, 110

    # Draw eyes
    draw_eye(draw, left_eye_cx, left_eye_cy, size=eye_size,
             openness=openness, brow_angle=brow_angle,
             look_x=look_x, look_y=look_y)
    draw_eye(draw, right_eye_cx, right_eye_cy, size=eye_size,
             openness=openness, brow_angle=-brow_angle,
             look_x=look_x, look_y=look_y)

    # Mouth position
    mouth_cx, mouth_cy = 240, 220
    draw_mouth(draw, mouth_cx, mouth_cy, mouth_type=mouth_type, size=mouth_size)

    return img


def make_talking_gif(emotion="neutral", duration_ms=150):
    """Generate a talking mouth animation GIF for a given emotion.

    Returns (gif_bytes, static_frame) tuple.
    """
    # Emotion presets
    presets = {
        "neutral": dict(openness=1.0, brow_angle=0, eye_size=50, mouth_size=1.0),
        "happy": dict(openness=1.0, brow_angle=5, eye_size=50, mouth_size=1.1),
        "excited": dict(openness=1.0, brow_angle=10, eye_size=55, mouth_size=1.2),
        "sad": dict(openness=0.8, brow_angle=-8, eye_size=48, mouth_size=0.9),
        "curious": dict(openness=1.0, brow_angle=8, eye_size=52, mouth_size=1.0),
    }
    p = presets.get(emotion, presets["neutral"])

    # Talking frames: smile → open → wide → open
    mouth_sequence = ["smile", "open", "wide", "open"]
    frames = []
    for mouth in mouth_sequence:
        frame = make_face(
            openness=p["openness"], brow_angle=p["brow_angle"],
            eye_size=p["eye_size"], mouth_type=mouth, mouth_size=p["mouth_size"],
        )
        frames.append(frame)

    # Create GIF
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:],
        duration=duration_ms, loop=0,
    )
    return buf.getvalue(), frames[0]


def make_idle_gif():
    """Generate idle face with slow blink and eye shift."""
    frames = []
    # Normal → normal → blink → normal → look left → normal → look right → normal
    configs = [
        dict(openness=1.0, look_x=0, mouth_type="smile"),
        dict(openness=1.0, look_x=0, mouth_type="smile"),
        dict(openness=0.0, look_x=0, mouth_type="smile"),  # blink
        dict(openness=1.0, look_x=0, mouth_type="smile"),
        dict(openness=1.0, look_x=-8, mouth_type="smile"),  # look left
        dict(openness=1.0, look_x=0, mouth_type="smile"),
        dict(openness=1.0, look_x=8, mouth_type="smile"),  # look right
        dict(openness=1.0, look_x=0, mouth_type="smile"),
    ]
    for cfg in configs:
        frame = make_face(brow_angle=0, eye_size=50, mouth_size=1.0, **cfg)
        frames.append(frame)

    buf = io.BytesIO()
    # 500ms per frame = slow natural idle (4s full cycle)
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:],
        duration=500, loop=0,
    )
    return buf.getvalue(), frames[0]


def make_listening_face():
    """Attentive listening face — wide eyes, slight smile."""
    return make_face(openness=1.0, brow_angle=5, eye_size=52, mouth_type="smile", mouth_size=1.0)


def make_processing_gif():
    """Thinking animation — eyes shift side to side."""
    frames = [
        make_face(openness=1.0, brow_angle=-3, look_x=0, mouth_type="neutral", eye_size=50),
        make_face(openness=1.0, brow_angle=-3, look_x=-8, mouth_type="neutral", eye_size=50),
        make_face(openness=1.0, brow_angle=-3, look_x=-8, mouth_type="neutral", eye_size=50),
        make_face(openness=1.0, brow_angle=-3, look_x=8, mouth_type="neutral", eye_size=50),
        make_face(openness=1.0, brow_angle=-3, look_x=8, mouth_type="neutral", eye_size=50),
        make_face(openness=1.0, brow_angle=-3, look_x=0, mouth_type="neutral", eye_size=50),
    ]
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:],
        duration=400, loop=0,
    )
    return buf.getvalue(), frames[0]


def upload_to_misty(misty_ip, filename, data, is_gif=False):
    """Upload an image/GIF to Misty."""
    if requests is None:
        print(f"  [skip] requests not available, cannot upload {filename}")
        return False
    b64 = base64.b64encode(data).decode("ascii")
    try:
        resp = requests.post(
            f"http://{misty_ip}/api/images",
            json={
                "FileName": filename,
                "Data": b64,
                "ImmediatelyApply": False,
                "OverwriteExisting": True,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            print(f"  [OK] Uploaded {filename} ({len(data)} bytes)")
            return True
        else:
            print(f"  [FAIL] Upload {filename} failed: HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"  [FAIL] Upload {filename} error: {e}")
        return False


def generate_all(assets_dir: Path, misty_ip: str = None, upload: bool = False):
    """Generate all face assets and optionally upload to Misty."""
    assets_dir.mkdir(parents=True, exist_ok=True)

    print("Generating face assets...")

    # 1. Idle GIF (blink + eye shift)
    idle_gif, idle_static = make_idle_gif()
    (assets_dir / "face_idle.gif").write_bytes(idle_gif)
    idle_static.save(assets_dir / "face_idle.png")
    print(f"  face_idle.gif: {len(idle_gif)} bytes (8 frames @ 500ms)")

    # 2. Listening face (static)
    listening = make_listening_face()
    buf = io.BytesIO()
    listening.save(buf, format="PNG")
    listening_data = buf.getvalue()
    (assets_dir / "face_listening.png").write_bytes(listening_data)
    print(f"  face_listening.png: {len(listening_data)} bytes")

    # 3. Processing GIF (thinking eyes)
    proc_gif, proc_static = make_processing_gif()
    (assets_dir / "face_processing.gif").write_bytes(proc_gif)
    proc_static.save(assets_dir / "face_processing.png")
    print(f"  face_processing.gif: {len(proc_gif)} bytes (6 frames @ 400ms)")

    # 4. Talking GIFs per emotion
    emotions = ["neutral", "happy", "excited", "sad", "curious"]
    for emotion in emotions:
        gif_data, static = make_talking_gif(emotion, duration_ms=150)
        (assets_dir / f"face_talking_{emotion}.gif").write_bytes(gif_data)
        static.save(assets_dir / f"face_talking_{emotion}.png")
        print(f"  face_talking_{emotion}.gif: {len(gif_data)} bytes (4 frames @ 150ms)")

    # 5. Upload to Misty
    if upload and misty_ip:
        print(f"\nUploading to Misty at {misty_ip}...")
        upload_to_misty(misty_ip, "face_idle.gif", idle_gif, is_gif=True)
        upload_to_misty(misty_ip, "face_listening.png", listening_data)
        upload_to_misty(misty_ip, "face_processing.gif", proc_gif, is_gif=True)
        for emotion in emotions:
            gif_data = (assets_dir / f"face_talking_{emotion}.gif").read_bytes()
            upload_to_misty(misty_ip, f"face_talking_{emotion}.gif", gif_data, is_gif=True)

    print("\nDone!")


def main():
    parser = argparse.ArgumentParser(description="Generate Misty face assets")
    parser.add_argument("--misty-ip", default=os.environ.get("MISTY_IP", "10.0.0.23"))
    parser.add_argument("--upload", action="store_true", help="Upload assets to Misty")
    parser.add_argument("--assets-dir", default="assets", help="Output directory")
    args = parser.parse_args()

    assets_dir = Path(args.assets_dir)
    generate_all(assets_dir, misty_ip=args.misty_ip, upload=args.upload)


if __name__ == "__main__":
    main()


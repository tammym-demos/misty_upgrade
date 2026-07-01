"""
Generate custom face assets for Misty II.

Uses extracted frames from the robot face sprite sheet (assets/frames/) to
create emotion variants, talking animations, idle blink cycles, and processing
states. Each frame is a distinct face pose with real mouth movement, eye
states, and cheek LED intensities.

Source frames (from assets/new_robot_pics_v2.png):
  - frame_normal: eyes open, mouth closed, cheeks low
  - frame_excited: max open eyes/mouth, max cheeks, max expression
  - frame_talk_closed: mouth closed, eyes wide, cheeks low
  - frame_talk_puckered: mouth 'u' shape, wink, asymmetric cheeks
  - frame_talk_pause: eyes & mouth closed, cheeks off
  - frame_talk_open: max open mouth, max open eyes, max cheeks

Design: GitHub issue #110
Display: 480x272 pixels

Usage:
    python tools/generate_face_assets.py [--misty-ip 10.0.0.23] [--upload]
    python tools/generate_face_assets.py --extract  # re-extract frames from sprite sheet

Output:
    assets/face_*.gif and assets/face_*.png uploaded to Misty
"""

import argparse
import base64
import io
import os
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

from PIL import Image, ImageEnhance


# --- Constants ---
WIDTH, HEIGHT = 480, 272

# Sprite sheet frame coordinates (x1, y1, x2, y2) in new_robot_pics_v2.png
SPRITE_FRAMES = {
    "frame_normal": (33, 40, 671, 370),
    "frame_excited": (690, 40, 1327, 370),
    "frame_talk_closed": (41, 410, 352, 630),
    "frame_talk_puckered": (362, 410, 673, 630),
    "frame_talk_pause": (688, 410, 999, 630),
    "frame_talk_open": (1008, 410, 1319, 630),
}

# Emotion color tints (R, G, B, alpha) blended over frames
EMOTION_TINTS = {
    "neutral": None,
    "happy": (255, 200, 50, 20),
    "excited": (255, 100, 255, 20),
    "sad": (80, 80, 180, 30),
    "curious": (100, 255, 200, 15),
}

EMOTION_BRIGHTNESS = {
    "neutral": 1.0,
    "happy": 1.05,
    "excited": 1.1,
    "sad": 0.85,
    "curious": 1.03,
}


def extract_frames(assets_dir: Path):
    """Extract individual frames from the sprite sheet."""
    sprite_path = assets_dir / "new_robot_pics_v2.png"
    if not sprite_path.exists():
        raise FileNotFoundError(f"Sprite sheet not found: {sprite_path}")

    img = Image.open(sprite_path).convert("RGBA")
    frames_dir = assets_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    for name, box in SPRITE_FRAMES.items():
        frame = img.crop(box).resize((WIDTH, HEIGHT), Image.LANCZOS)
        frame.save(frames_dir / f"{name}.png")
        print(f"  Extracted {name}.png from {box}")

    print(f"  All frames saved to {frames_dir}")


def load_frames(assets_dir: Path) -> dict:
    """Load all pre-extracted frame images."""
    frames_dir = assets_dir / "frames"
    if not frames_dir.exists():
        print("  Frames not found, extracting from sprite sheet...")
        extract_frames(assets_dir)

    frames = {}
    for name in SPRITE_FRAMES:
        path = frames_dir / f"{name}.png"
        if not path.exists():
            raise FileNotFoundError(f"Frame not found: {path}")
        frames[name] = Image.open(path).convert("RGBA")

    return frames


def apply_tint(img: Image.Image, tint_rgba: tuple) -> Image.Image:
    """Apply a color tint overlay."""
    if tint_rgba is None:
        return img.copy()
    overlay = Image.new("RGBA", img.size, tint_rgba)
    return Image.alpha_composite(img, overlay)


def apply_brightness(img: Image.Image, factor: float) -> Image.Image:
    """Adjust image brightness."""
    if factor == 1.0:
        return img
    rgb = img.convert("RGB")
    enhancer = ImageEnhance.Brightness(rgb)
    result = enhancer.enhance(factor).convert("RGBA")
    result.putalpha(img.split()[3])
    return result


def to_rgb(img: Image.Image) -> Image.Image:
    """Convert RGBA to RGB with black background."""
    background = Image.new("RGB", img.size, (0, 0, 0))
    background.paste(img, mask=img.split()[3])
    return background


def apply_emotion(img: Image.Image, emotion: str) -> Image.Image:
    """Apply emotion-specific color tint and brightness."""
    result = apply_tint(img, EMOTION_TINTS.get(emotion))
    result = apply_brightness(result, EMOTION_BRIGHTNESS.get(emotion, 1.0))
    return result


def make_talking_gif(frames: dict, emotion: str = "neutral", duration_ms: int = 150):
    """Generate talking animation using real mouth-position frames.

    Cycle: closed -> puckered -> open -> puckered (natural speech pattern)
    Returns (gif_bytes, static_frame) tuple.
    """
    # Use the actual mouth-position frames for animation
    mouth_sequence = [
        frames["frame_talk_closed"],
        frames["frame_talk_puckered"],
        frames["frame_talk_open"],
        frames["frame_talk_puckered"],
    ]

    gif_frames = []
    for frame in mouth_sequence:
        styled = apply_emotion(frame, emotion)
        gif_frames.append(to_rgb(styled))

    buf = io.BytesIO()
    gif_frames[0].save(
        buf, format="GIF", save_all=True, append_images=gif_frames[1:],
        duration=duration_ms, loop=0,
    )
    return buf.getvalue(), gif_frames[0]


def make_idle_gif(frames: dict):
    """Generate idle animation with natural blink cycle.

    Cycle: normal -> normal -> pause(blink) -> normal -> normal -> puckered(cute) -> normal -> normal
    """
    sequence = [
        frames["frame_normal"],
        frames["frame_normal"],
        frames["frame_talk_pause"],   # blink (eyes closed)
        frames["frame_normal"],
        frames["frame_normal"],
        frames["frame_normal"],
        frames["frame_talk_puckered"],  # cute expression
        frames["frame_normal"],
    ]

    gif_frames = [to_rgb(f) for f in sequence]

    buf = io.BytesIO()
    gif_frames[0].save(
        buf, format="GIF", save_all=True, append_images=gif_frames[1:],
        duration=500, loop=0,
    )
    return buf.getvalue(), gif_frames[0]


def make_listening_face(frames: dict) -> Image.Image:
    """Attentive listening face - wide eyes, alert."""
    # Use normal frame with slight brightness boost
    img = apply_brightness(frames["frame_normal"], 1.05)
    return to_rgb(img)


def make_processing_gif(frames: dict):
    """Thinking animation - pulse between normal and pause states."""
    sequence = [
        frames["frame_normal"],
        frames["frame_talk_pause"],     # eyes close (thinking)
        frames["frame_talk_pause"],
        frames["frame_normal"],
        frames["frame_talk_puckered"],  # slight expression
        frames["frame_normal"],
    ]

    # Apply blue thinking tint
    gif_frames = []
    for frame in sequence:
        tinted = apply_tint(frame, (80, 120, 255, 15))
        gif_frames.append(to_rgb(tinted))

    buf = io.BytesIO()
    gif_frames[0].save(
        buf, format="GIF", save_all=True, append_images=gif_frames[1:],
        duration=400, loop=0,
    )
    return buf.getvalue(), gif_frames[0]


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

    print("Generating face assets from sprite frames...")
    frames = load_frames(assets_dir)
    print(f"  Loaded {len(frames)} source frames")

    # 1. Idle GIF (blink animation using real closed-eye frame)
    idle_gif, idle_static = make_idle_gif(frames)
    (assets_dir / "face_idle.gif").write_bytes(idle_gif)
    idle_static.save(assets_dir / "face_idle.png")
    print(f"  face_idle.gif: {len(idle_gif)} bytes (8 frames @ 500ms)")

    # 1b. Idle smile (static - normal frame)
    idle_smile = to_rgb(frames["frame_normal"])
    buf = io.BytesIO()
    idle_smile.save(buf, format="PNG")
    (assets_dir / "face_idle_smile.png").write_bytes(buf.getvalue())
    print(f"  face_idle_smile.png: {len(buf.getvalue())} bytes")

    # 2. Listening face (static)
    listening = make_listening_face(frames)
    buf = io.BytesIO()
    listening.save(buf, format="PNG")
    listening_data = buf.getvalue()
    (assets_dir / "face_listening.png").write_bytes(listening_data)
    print(f"  face_listening.png: {len(listening_data)} bytes")

    # 3. Processing GIF (thinking animation)
    proc_gif, proc_static = make_processing_gif(frames)
    (assets_dir / "face_processing.gif").write_bytes(proc_gif)
    proc_static.save(assets_dir / "face_processing.png")
    print(f"  face_processing.gif: {len(proc_gif)} bytes (6 frames @ 400ms)")

    # 4. Talking GIFs per emotion (real mouth movement!)
    emotions = ["neutral", "happy", "excited", "sad", "curious"]
    for emotion in emotions:
        gif_data, static = make_talking_gif(frames, emotion, duration_ms=150)
        (assets_dir / f"face_talking_{emotion}.gif").write_bytes(gif_data)
        static.save(assets_dir / f"face_talking_{emotion}.png")
        print(f"  face_talking_{emotion}.gif: {len(gif_data)} bytes (4 frames @ 150ms)")

    # 5. Generic talking GIF (neutral)
    talking_gif, _ = make_talking_gif(frames, "neutral", duration_ms=150)
    (assets_dir / "face_talking.gif").write_bytes(talking_gif)
    print(f"  face_talking.gif: {len(talking_gif)} bytes (4 frames @ 150ms)")

    # 6. Upload to Misty
    if upload and misty_ip:
        print(f"\nUploading to Misty at {misty_ip}...")
        upload_to_misty(misty_ip, "face_idle.gif", idle_gif, is_gif=True)
        upload_to_misty(misty_ip, "face_listening.png", listening_data)
        upload_to_misty(misty_ip, "face_processing.gif", proc_gif, is_gif=True)
        for emotion in emotions:
            gif_data = (assets_dir / f"face_talking_{emotion}.gif").read_bytes()
            upload_to_misty(misty_ip, f"face_talking_{emotion}.gif", gif_data, is_gif=True)
        upload_to_misty(misty_ip, "face_talking.gif", talking_gif, is_gif=True)

    print("\nDone!")


def main():
    parser = argparse.ArgumentParser(description="Generate Misty face assets")
    parser.add_argument("--misty-ip", default=os.environ.get("MISTY_IP", "10.0.0.23"))
    parser.add_argument("--upload", action="store_true", help="Upload assets to Misty")
    parser.add_argument("--extract", action="store_true",
                        help="Re-extract frames from sprite sheet")
    parser.add_argument("--assets-dir", default="assets", help="Output directory")
    args = parser.parse_args()

    assets_dir = Path(args.assets_dir)

    if args.extract:
        print("Extracting frames from sprite sheet...")
        extract_frames(assets_dir)
        print("Done!")
        return

    generate_all(assets_dir, misty_ip=args.misty_ip, upload=args.upload)


if __name__ == "__main__":
    main()


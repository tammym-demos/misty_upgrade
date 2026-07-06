"""
Generate custom face assets for Misty II.

Draws a clean, Misty-inspired vector face directly with Pillow. The assets keep
the existing runtime filenames used by FaceAnimator and startup sync logic.

Display: 480x272 pixels

Usage:
    python tools/generate_face_assets.py [--misty-ip 10.0.0.23] [--upload]
    python tools/generate_face_assets.py --preview

Output:
    assets/face_*.gif and assets/face_*.png
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

from PIL import Image, ImageDraw, ImageFilter, ImageOps


WIDTH, HEIGHT = 480, 272
TARGET_ASPECT = WIDTH / HEIGHT
SCALE = 3

EMOTIONS = ("neutral", "happy", "excited", "sad", "curious")
PREVIEW_CONTACT_SHEET = "face_preview_contact_sheet.png"

LEFT_EYE = (132, 104)
RIGHT_EYE = (348, 104)
MOUTH_CENTER = (240, 224)
MOUTH_MAX_WIDTH = 180
MOUTH_MAX_HEIGHT = 48

PALETTE = {
    "bg": (4, 5, 12),
    "panel": (18, 20, 36),
    "panel_2": (28, 31, 52),
    "line": (100, 105, 150),
    "eye_outer": (222, 226, 244),
    "eye_inner": (34, 19, 58),
    "mouth": (18, 8, 24),
    "mouth_shadow": (7, 4, 12),
    "tongue": (255, 94, 164),
    "tongue_hi": (255, 160, 202),
}

EMOTION_ACCENT = {
    "neutral": (104, 145, 255),
    "happy": (74, 176, 255),
    "excited": (118, 105, 255),
    "sad": (74, 118, 235),
    "curious": (74, 220, 255),
}

EMOTION_EYE = {
    "neutral": (118, 150, 255),
    "happy": (86, 190, 255),
    "excited": (132, 112, 255),
    "sad": (90, 125, 255),
    "curious": (80, 225, 255),
}


def _hi_canvas() -> Image.Image:
    return Image.new("RGBA", (WIDTH * SCALE, HEIGHT * SCALE), (*PALETTE["bg"], 255))


def _sc(value: int | float) -> int:
    return int(round(value * SCALE))


def _box(box: tuple[int | float, int | float, int | float, int | float]) -> tuple[int, int, int, int]:
    return tuple(_sc(v) for v in box)


def _pt(point: tuple[int | float, int | float]) -> tuple[int, int]:
    return (_sc(point[0]), _sc(point[1]))


def _rgba(color: tuple[int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return (*color, alpha)


def _downsample(img: Image.Image) -> Image.Image:
    return img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def _glow(base: Image.Image, draw_func, radius: int = 10) -> Image.Image:
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw_func(ImageDraw.Draw(overlay))
    return Image.alpha_composite(base, overlay.filter(ImageFilter.GaussianBlur(_sc(radius))))


def draw_background(img: Image.Image, accent: tuple[int, int, int], pulse: float) -> Image.Image:
    """Draw an edge-to-edge faceplate and soft breathing glow."""
    alpha = int(70 + 95 * pulse)

    def glow(draw: ImageDraw.ImageDraw):
        draw.ellipse(_box((34, 174, 156, 288)), fill=_rgba(accent, int(alpha * 0.38)))
        draw.ellipse(_box((324, 174, 446, 288)), fill=_rgba(accent, int(alpha * 0.38)))
        draw.rectangle(_box((0, 0, WIDTH, 20)), fill=_rgba(accent, int(alpha * 0.24)))

    img = _glow(img, glow, radius=12)
    draw = ImageDraw.Draw(img)
    draw.rectangle(_box((0, 0, WIDTH, HEIGHT)), fill=(*PALETTE["panel"], 245))
    draw.rounded_rectangle(_box((10, 22, WIDTH - 10, HEIGHT + 20)), radius=_sc(42), fill=(*PALETTE["panel_2"], 238))
    draw.line(_box((224, 34, 256, 34)), fill=_rgba(PALETTE["line"], 80), width=_sc(2))
    draw.line(_box((224, 264, 256, 264)), fill=_rgba(PALETTE["line"], 80), width=_sc(2))
    for side in (1, -1):
        start_x = 240 + side * 84
        draw.line(_box((start_x, 158, start_x + side * 38, 182, start_x + side * 38, 212)), fill=_rgba(accent, 78), width=_sc(2))
        draw.ellipse(_box((start_x + side * 40 - 3, 210, start_x + side * 40 + 3, 216)), fill=_rgba(accent, 105))
    return img


def draw_cheeks(img: Image.Image, accent: tuple[int, int, int], pulse: float) -> Image.Image:
    """Draw low-resolution LED cheek clusters."""
    draw = ImageDraw.Draw(img)
    alpha = int(100 + 110 * pulse)
    for origin_x in (68, 374):
        for row in range(3):
            for col in range(4):
                x = origin_x + col * 13
                y = 178 + row * 13
                draw.ellipse(_box((x - 5, y - 5, x + 5, y + 5)), fill=_rgba(accent, alpha))
                draw.ellipse(_box((x - 2, y - 2, x + 2, y + 2)), fill=(255, 235, 250, min(245, alpha + 30)))
    return img


def draw_eye(
    img: Image.Image,
    center: tuple[int, int],
    accent: tuple[int, int, int],
    *,
    blink: float = 0.0,
    gaze: int = 0,
    happy_arc: bool = False,
    sad: bool = False,
) -> Image.Image:
    """Draw one expressive robotic eye."""
    draw = ImageDraw.Draw(img)
    cx, cy = center
    rx, ry = 66, max(10, int(52 * (1.0 - blink)))
    if blink >= 0.82:
        y = cy + (7 if sad else 0)
        draw.arc(_box((cx - 64, y - 19, cx + 64, y + 22)), 15, 165, fill=_rgba(accent, 235), width=_sc(7))
        return img

    draw.ellipse(_box((cx - 78, cy - 65, cx + 78, cy + 65)), fill=(5, 6, 14, 255), outline=_rgba(PALETTE["eye_outer"], 210), width=_sc(4))
    draw.ellipse(_box((cx - 70, cy - 57, cx + 70, cy + 57)), outline=_rgba(accent, 145), width=_sc(3))
    draw.arc(_box((cx - 74, cy - 61, cx + 74, cy + 61)), 205, 335, fill=(255, 255, 255, 120), width=_sc(3))
    for angle_box in ((cx - 64, cy - 51, cx + 64, cy + 51), (cx - 52, cy - 41, cx + 52, cy + 41)):
        draw.ellipse(_box(angle_box), outline=_rgba(PALETTE["line"], 70), width=_sc(1))
    draw.ellipse(_box((cx - rx, cy - ry, cx + rx, cy + ry)), fill=(236, 240, 255, 248))
    draw.ellipse(_box((cx - 52, cy - max(8, ry - 7), cx + 52, cy + max(8, ry - 7))), fill=_rgba(accent, 190))
    pupil_x = cx + gaze
    pupil_y = cy + (5 if sad else 0)
    draw.ellipse(_box((pupil_x - 29, pupil_y - 31, pupil_x + 29, pupil_y + 31)), fill=(16, 7, 28, 255))
    draw.ellipse(_box((pupil_x - 17, pupil_y - 18, pupil_x + 17, pupil_y + 18)), outline=_rgba(accent, 125), width=_sc(2))
    draw.pieslice(_box((pupil_x - 38, pupil_y - 39, pupil_x + 38, pupil_y + 39)), 225, 300, fill=(255, 255, 255, 68))
    draw.ellipse(_box((pupil_x + 11, pupil_y - 22, pupil_x + 28, pupil_y - 5)), fill=(255, 255, 255, 242))
    draw.ellipse(_box((pupil_x + 28, pupil_y + 10, pupil_x + 37, pupil_y + 19)), fill=(255, 220, 255, 190))
    for dx in (-58, 58):
        draw.line(_box((cx + dx, cy - 10, cx + dx, cy + 10)), fill=_rgba(accent, 110), width=_sc(2))

    if happy_arc:
        draw.arc(_box((cx - 66, cy - 70, cx + 66, cy + 34)), 200, 340, fill=(255, 255, 255, 145), width=_sc(3))
    if sad:
        draw.line(_box((cx - 65, cy - 62, cx + 65, cy - 42)), fill=_rgba(accent, 155), width=_sc(5))
    return img


def mouth_bounds(openness: float, width: int = MOUTH_MAX_WIDTH) -> tuple[int, int, int, int]:
    """Return the single animated mouth bounding box for a talking frame."""
    openness = max(0.0, min(1.0, openness))
    mouth_h = max(8, int(12 + (MOUTH_MAX_HEIGHT - 12) * openness))
    x1 = MOUTH_CENTER[0] - width // 2
    y1 = MOUTH_CENTER[1] - mouth_h // 2
    return (x1, y1, x1 + width, y1 + mouth_h)


def draw_mouth(
    img: Image.Image,
    accent: tuple[int, int, int],
    *,
    openness: float = 0.2,
    smile: float = 0.0,
    sad: bool = False,
) -> Image.Image:
    """Draw one coherent mouth; all talking frames use this same anchor."""
    draw = ImageDraw.Draw(img)
    x1, y1, x2, y2 = mouth_bounds(openness)
    draw.rounded_rectangle(_box((x1 - 9, y1 - 8, x2 + 9, y2 + 8)), radius=_sc(28), fill=(*PALETTE["mouth_shadow"], 255))

    lip_top = y1 + 2
    lip_bottom = y2 + 10
    draw.ellipse(_box((x1 - 2, lip_top - 4, x2 + 2, lip_bottom + 8)), fill=(*PALETTE["tongue"], 245))
    draw.ellipse(_box((x1 + 18, lip_top + 4, x2 - 18, lip_bottom + 4)), fill=(*PALETTE["tongue_hi"], 180))

    opening_h = max(8, int((y2 - y1) * (0.55 + openness * 0.15)))
    opening_top = y1 - 1
    opening_bottom = opening_top + opening_h
    draw.rounded_rectangle(
        _box((x1 + 8, opening_top, x2 - 8, opening_bottom)),
        radius=_sc(18),
        fill=(*PALETTE["mouth"], 255),
    )
    draw.arc(_box((x1 + 4, y1 - 8, x2 - 4, y2 + 18)), 12, 168, fill=_rgba(accent, 170), width=_sc(2))

    if openness > 0.28:
        tongue_w = int(48 + 20 * openness)
        draw.ellipse(
            _box((MOUTH_CENTER[0] - tongue_w // 2, y2 - 14, MOUTH_CENTER[0] + tongue_w // 2, y2 + 8)),
            fill=(*PALETTE["tongue_hi"], 190),
        )
    else:
        curve_y = MOUTH_CENTER[1] + int(5 * smile) + (6 if sad else 0)
        if sad:
            draw.arc(_box((x1 + 28, curve_y - 3, x2 - 28, curve_y + 26)), 200, 340, fill=(*PALETTE["tongue_hi"], 235), width=_sc(4))
        else:
            draw.arc(_box((x1 + 24, curve_y - 18, x2 - 24, curve_y + 12)), 20, 160, fill=(*PALETTE["tongue_hi"], 235), width=_sc(4))
    return img


def draw_sparkle(img: Image.Image, accent: tuple[int, int, int], offset: int) -> Image.Image:
    draw = ImageDraw.Draw(img)
    for x, y in ((240 + offset, 56), (288 - offset // 2, 198)):
        draw.line(_box((x - 8, y, x + 8, y)), fill=(255, 255, 255, 210), width=_sc(2))
        draw.line(_box((x, y - 8, x, y + 8)), fill=_rgba(accent, 210), width=_sc(2))
    return img


def draw_processing_scan(img: Image.Image, accent: tuple[int, int, int], y: int) -> Image.Image:
    def scan(draw: ImageDraw.ImageDraw):
        draw.rectangle(_box((78, y - 3, WIDTH - 78, y + 3)), fill=_rgba(accent, 170))
        draw.rectangle(_box((78, y - 1, WIDTH - 78, y + 1)), fill=(255, 255, 255, 215))

    return _glow(img, scan, radius=4)


def render_face_frame(
    *,
    emotion: str = "neutral",
    openness: float = 0.18,
    blink: float = 0.0,
    gaze: int = 0,
    pulse: float = 0.5,
    processing_y: int | None = None,
    listening: bool = False,
    sparkle: int = 0,
) -> Image.Image:
    """Render a single vector face frame."""
    emotion = emotion if emotion in EMOTIONS else "neutral"
    accent = EMOTION_ACCENT[emotion]
    eye_accent = EMOTION_EYE[emotion]
    sad = emotion == "sad"
    happy = emotion in {"happy", "excited"}

    img = draw_background(_hi_canvas(), accent, pulse)
    img = draw_eye(img, LEFT_EYE, eye_accent, blink=blink, gaze=gaze, happy_arc=happy, sad=sad)
    img = draw_eye(img, RIGHT_EYE, eye_accent, blink=blink, gaze=gaze, happy_arc=happy, sad=sad)
    img = draw_cheeks(img, accent, pulse)
    if listening:
        draw = ImageDraw.Draw(img)
        for radius, alpha in ((22, 180), (34, 135), (46, 95)):
            draw.arc(_box((240 - radius, 42 - radius, 240 + radius, 42 + radius)), 205, 335, fill=_rgba(accent, alpha), width=_sc(3))
    if processing_y is not None:
        img = draw_processing_scan(img, accent, processing_y)
    if sparkle:
        img = draw_sparkle(img, accent, sparkle)
    img = draw_mouth(img, accent, openness=openness, smile=0.8 if happy else 0.0, sad=sad)
    return _downsample(img)


def save_gif(frames: list[Image.Image], duration_ms: int) -> bytes:
    """Encode frames as a looping RGB GIF and return the bytes."""
    rgb_frames = [frame.convert("RGB") for frame in frames]
    buf = io.BytesIO()
    rgb_frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=rgb_frames[1:],
        duration=duration_ms,
        loop=0,
    )
    return buf.getvalue()


def make_talking_gif(emotion: str = "neutral", duration_ms: int = 150):
    """Generate a four-frame talking animation from one mouth anchor."""
    poses = ((0.16, 0.45), (0.58, 0.65), (1.0, 0.9), (0.58, 0.65))
    frames = [
        render_face_frame(emotion=emotion, openness=open_state, pulse=pulse, sparkle=int((pulse - 0.5) * 16))
        for open_state, pulse in poses
    ]
    return save_gif(frames, duration_ms), frames[0]


def make_idle_gif():
    """Generate a calm idle animation with breathing glow and blink."""
    specs = (
        (0.20, 0.0, -2),
        (0.35, 0.0, 0),
        (0.55, 0.0, 2),
        (0.50, 0.95, 0),
        (0.35, 0.0, -1),
        (0.25, 0.0, 0),
    )
    frames = [
        render_face_frame(emotion="neutral", openness=0.15, pulse=pulse, blink=blink, gaze=gaze)
        for pulse, blink, gaze in specs
    ]
    return save_gif(frames, 460), frames[0]


def make_processing_gif():
    """Generate a thinking animation with a gentle scan and curious expression."""
    frames = [
        render_face_frame(emotion="curious", openness=0.12, pulse=0.35 + idx * 0.1, gaze=(-3 + idx), processing_y=y)
        for idx, y in enumerate((78, 104, 130, 156, 182, 156))
    ]
    return save_gif(frames, 330), frames[0]


def make_listening_face() -> Image.Image:
    """Generate an attentive listening still."""
    return render_face_frame(emotion="curious", openness=0.18, pulse=0.85, gaze=0, listening=True)


def make_contact_sheet(assets_dir: Path):
    """Write a PNG contact sheet for quick visual review."""
    names = [
        "face_idle.png",
        "face_listening.png",
        "face_processing.png",
        "face_talking_neutral.png",
        "face_talking_happy.png",
        "face_talking_excited.png",
        "face_talking_sad.png",
        "face_talking_curious.png",
    ]
    thumb_w, thumb_h = 240, 136
    sheet = Image.new("RGB", (thumb_w * 2, thumb_h * 4), (8, 8, 14))
    for idx, name in enumerate(names):
        with Image.open(assets_dir / name).convert("RGB") as image:
            thumb = image.resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        sheet.paste(thumb, ((idx % 2) * thumb_w, (idx // 2) * thumb_h))
    sheet.save(assets_dir / PREVIEW_CONTACT_SHEET)
    return assets_dir / PREVIEW_CONTACT_SHEET


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
        print(f"  [FAIL] Upload {filename} failed: HTTP {resp.status_code}")
        return False
    except Exception as e:
        print(f"  [FAIL] Upload {filename} error: {e}")
        return False


def generate_all(assets_dir: Path, misty_ip: str = None, upload: bool = False, preview: bool = False):
    """Generate all face assets and optionally upload to Misty."""
    assets_dir.mkdir(parents=True, exist_ok=True)

    print("Generating vector face assets...")

    idle_gif, idle_static = make_idle_gif()
    (assets_dir / "face_idle.gif").write_bytes(idle_gif)
    idle_static.save(assets_dir / "face_idle.png")
    print(f"  face_idle.gif: {len(idle_gif)} bytes (6 frames @ 460ms)")

    idle_smile = render_face_frame(emotion="happy", openness=0.14, pulse=0.55)
    idle_smile.save(assets_dir / "face_idle_smile.png")
    print("  face_idle_smile.png")

    listening = make_listening_face()
    listening.save(assets_dir / "face_listening.png")
    print("  face_listening.png")

    proc_gif, proc_static = make_processing_gif()
    (assets_dir / "face_processing.gif").write_bytes(proc_gif)
    proc_static.save(assets_dir / "face_processing.png")
    print(f"  face_processing.gif: {len(proc_gif)} bytes (6 frames @ 330ms)")

    generated_gifs = {
        "face_idle.gif": idle_gif,
        "face_processing.gif": proc_gif,
    }
    for emotion in EMOTIONS:
        gif_data, static = make_talking_gif(emotion, duration_ms=150)
        (assets_dir / f"face_talking_{emotion}.gif").write_bytes(gif_data)
        static.save(assets_dir / f"face_talking_{emotion}.png")
        generated_gifs[f"face_talking_{emotion}.gif"] = gif_data
        print(f"  face_talking_{emotion}.gif: {len(gif_data)} bytes (4 frames @ 150ms)")

    talking_gif, _ = make_talking_gif("neutral", duration_ms=150)
    (assets_dir / "face_talking.gif").write_bytes(talking_gif)
    generated_gifs["face_talking.gif"] = talking_gif
    print(f"  face_talking.gif: {len(talking_gif)} bytes (4 frames @ 150ms)")

    contact_sheet = make_contact_sheet(assets_dir)
    if preview:
        print(f"  Preview contact sheet: {contact_sheet}")

    if upload and misty_ip:
        print(f"\nUploading to Misty at {misty_ip}...")
        upload_to_misty(misty_ip, "face_idle.gif", idle_gif, is_gif=True)
        upload_to_misty(misty_ip, "face_listening.png", (assets_dir / "face_listening.png").read_bytes())
        upload_to_misty(misty_ip, "face_processing.gif", proc_gif, is_gif=True)
        for emotion in EMOTIONS:
            filename = f"face_talking_{emotion}.gif"
            upload_to_misty(misty_ip, filename, generated_gifs[filename], is_gif=True)
        upload_to_misty(misty_ip, "face_talking.gif", talking_gif, is_gif=True)

    print("\nDone!")


def main():
    parser = argparse.ArgumentParser(description="Generate Misty vector face assets")
    parser.add_argument("--misty-ip", default=os.environ.get("MISTY_IP", "10.0.0.23"))
    parser.add_argument("--upload", action="store_true", help="Upload assets to Misty")
    parser.add_argument("--preview", action="store_true", help="Print preview contact-sheet path")
    parser.add_argument("--assets-dir", default="assets", help="Output directory")
    args = parser.parse_args()

    generate_all(Path(args.assets_dir), misty_ip=args.misty_ip, upload=args.upload, preview=args.preview)


if __name__ == "__main__":
    main()

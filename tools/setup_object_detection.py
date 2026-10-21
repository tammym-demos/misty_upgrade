"""Download the pinned Apache-2.0 OpenCV Zoo YOLOX-s model, verifying SHA-256."""

import argparse
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "windows-orchestration"))
import config_defaults

MODEL_REVISION = "0b263e423d012606b83d1f81238d11c177da2b9c"
MODEL_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
MODEL_URL = (
    f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{MODEL_REVISION}"
    "/models/object_detection_yolox/object_detection_yolox_2022nov.onnx"
)


def download_model(destination: Path) -> None:
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == MODEL_SHA256:
        print(f"Verified existing model: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen(MODEL_URL, timeout=60) as response:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix="yolox-", suffix=".download", delete=False
            ) as output:
                temporary = Path(output.name)
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(block)
                    output.write(block)
        if digest.hexdigest() != MODEL_SHA256:
            raise ValueError("YOLOX checksum mismatch; refusing to install the model")
        os.replace(temporary, destination)
        print(f"Downloaded and verified model: {destination}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-path", type=Path,
        default=Path(os.getenv("OBJECT_DETECTOR_MODEL_PATH", config_defaults.OBJECT_DETECTOR_MODEL_PATH)),
    )
    args = parser.parse_args()
    try:
        download_model(args.model_path)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print(f"Object model setup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

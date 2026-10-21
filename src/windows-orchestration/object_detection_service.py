"""On-demand, local-only COCO object detection and ephemeral chat grounding.

Model/preprocessing reference (Apache-2.0):
https://github.com/opencv/opencv_zoo/tree/0b263e423d012606b83d1f81238d11c177da2b9c/models/object_detection_yolox
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

import numpy as np
import requests

import config_defaults

logger = logging.getLogger(__name__)

COCO_LABELS = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana",
    "apple", "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza",
    "donut", "cake", "chair", "couch", "potted plant", "bed", "dining table",
    "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock",
    "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
)

_VISUAL_QUERY = re.compile(
    r"^(?:(?:hey )?misty[,:\s]+)?"
    r"(?:(?:please|can you|could you|would you|tell me)[,\s]+)*"
    r"(?:what (?:do|can) you see(?: (?:right now|around you|in front of you))?"
    r"|what (?:objects|things) (?:do|can) you see"
    r"|(?P<followup>what else (?:do|can) you see"
    r"|(?:do|can) you see anything else"
    r"|(?:you (?:don't|do not|can't|cannot)|don't you) see anything else"
    r"|(?:is there )?anything else (?:that )?you (?:can )?see"
    r"|is that all (?:that )?you (?:can )?see)"
    r"|(?:describe|look at) (?:your surroundings|what you see|the scene)"
    r"|what(?:'s| is) in front of you"
    r"|(?:can you )?(?:take a look|look around))\b[?.!]*$",
    re.IGNORECASE,
)


class ObjectDetectionError(RuntimeError):
    """A camera observation cannot be produced; never means 'no objects'."""


def is_visual_query(text: str) -> bool:
    """Recognize explicit camera requests, not idioms such as 'see what I mean'."""
    normalized = " ".join(text.strip().replace("\u2019", "'").split())
    return bool(_VISUAL_QUERY.fullmatch(normalized))


def is_visual_followup(text: str) -> bool:
    """Only explicit requests for another view authorize a head scan."""
    normalized = " ".join(text.strip().replace("\u2019", "'").split())
    match = _VISUAL_QUERY.fullmatch(normalized)
    return match is not None and match.group("followup") is not None


class OnnxObjectDetector:
    """YOLOX-s / OpenCV Zoo, lazily loaded on CPU; no downloads or image writes."""

    def __init__(self, model_path: str, confidence: float = 0.45):
        if not np.isfinite(confidence) or not 0 < confidence <= 1:
            raise ObjectDetectionError("OBJECT_DETECTION_CONFIDENCE must be in (0, 1]")
        self.model_path = model_path
        self.confidence = confidence
        self._net = None
        self._lock = threading.Lock()
        grids, strides = [], []
        for stride in (8, 16, 32):
            y, x = np.mgrid[:640 // stride, :640 // stride]
            grids.append(np.stack((x, y), axis=-1).reshape(-1, 2))
            strides.append(np.full((x.size, 1), stride, dtype=np.float32))
        self._grid = np.concatenate(grids).astype(np.float32)
        self._strides = np.concatenate(strides)

    def _load(self, cv2) -> None:
        if self._net is not None:
            return
        if not Path(self.model_path).is_file():
            raise ObjectDetectionError(
                "Object model is missing. Run python tools\\setup_object_detection.py "
                "and check OBJECT_DETECTOR_MODEL_PATH."
            )
        try:
            net = cv2.dnn.readNetFromONNX(self.model_path)
            net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        except cv2.error as exc:
            raise ObjectDetectionError(f"Cannot load YOLOX object model: {exc}") from exc
        self._net = net

    def detect(self, frame: np.ndarray) -> list[dict]:
        try:
            import cv2
        except ImportError as exc:
            raise ObjectDetectionError("Install opencv-python-headless for object detection") from exc
        if frame is None or frame.ndim != 3 or frame.shape[2] != 3 or not frame.size:
            raise ObjectDetectionError("Object detection requires a nonempty BGR camera frame")
        height, width = frame.shape[:2]
        ratio = min(640 / height, 640 / width)
        resized_width, resized_height = max(1, int(width * ratio)), max(1, int(height * ratio))
        try:
            image = np.full((640, 640, 3), 114, dtype=np.float32)
            image[:resized_height, :resized_width] = cv2.resize(
                cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), (resized_width, resized_height)
            )
            blob = np.ascontiguousarray(image.transpose(2, 0, 1)[None])
            with self._lock:
                self._load(cv2)
                self._net.setInput(blob)
                output = self._net.forward()
            return self._summarize(output, resized_width, resized_height, cv2)
        except cv2.error as exc:
            raise ObjectDetectionError(f"Object inference failed: {exc}") from exc

    def _summarize(self, output, image_width, image_height, cv2) -> list[dict]:
        if output.shape != (1, 8400, 85) or not np.isfinite(output).all():
            raise ObjectDetectionError("Unexpected YOLOX output; use the documented OpenCV Zoo model")
        predictions = output[0]
        scores = predictions[:, 4] * predictions[:, 5:].max(axis=1)
        candidates = scores >= self.confidence
        rows = predictions[candidates]
        scores = scores[candidates]
        if not len(rows):
            return []
        centers = (rows[:, :2] + self._grid[candidates]) * self._strides[candidates]
        sizes = np.exp(np.clip(rows[:, 2:4], -20, 20)) * self._strides[candidates]
        top_left = np.maximum(centers - sizes / 2, 0)
        bottom_right = np.minimum(centers + sizes / 2, [image_width, image_height])
        sizes = bottom_right - top_left
        valid = (sizes > 0).all(axis=1)
        boxes = np.concatenate((top_left, sizes), axis=1)[valid]
        classes = rows[:, 5:].argmax(axis=1)[valid]
        scores = scores[valid]
        summary = []
        for class_id in np.unique(classes):
            selection = classes == class_id
            class_scores = scores[selection]
            kept = np.asarray(cv2.dnn.NMSBoxes(
                boxes[selection].tolist(), class_scores.tolist(), self.confidence, 0.5
            )).reshape(-1)
            if kept.size:
                summary.append({
                    "label": COCO_LABELS[class_id],
                    "count": min(20, int(kept.size)),
                    "confidence": round(float(class_scores[kept].max()), 3),
                })
        return sorted(summary, key=lambda item: (-item["confidence"], item["label"]))[:8]


def _fetch_current_observation(*, scan: bool = False) -> tuple[list[dict], list[str]]:
    enabled = os.getenv(
        "USE_OBJECT_DETECTION", str(config_defaults.USE_OBJECT_DETECTION)
    ).lower() in ("1", "true", "yes")
    if not enabled:
        raise ObjectDetectionError("Object detection is disabled (USE_OBJECT_DETECTION)")
    try:
        port = int(os.getenv("CONTROLLER_API_PORT", str(config_defaults.CONTROLLER_API_PORT)))
        timeout = float(os.getenv(
            "OBJECT_DETECTION_TIMEOUT_S", str(config_defaults.OBJECT_DETECTION_TIMEOUT_S)
        ))
        response = requests.post(
            f"http://127.0.0.1:{port}/api/vision/detect",
            json={"scan": scan}, timeout=timeout,
        )
        response.raise_for_status()
        observation = response.json()
        if observation.get("status") != "ok":
            raise ValueError("Controller did not return a successful camera observation")
        objects = observation["objects"]
        if not isinstance(objects, list) or len(objects) > 8:
            raise ValueError("Invalid object detection summary")
        for item in objects:
            label, count, score = item["label"], item["count"], item["confidence"]
            if (
                label not in COCO_LABELS
                or type(count) is not int or not 1 <= count <= 20
                or type(score) not in (int, float) or not 0 < score <= 1
            ):
                raise ValueError("Invalid object label, count or confidence")
        people = observation.get("recognized_people", [])
        if (
            not isinstance(people, list) or len(people) > 8
            or any(
                not isinstance(name, str) or name != name.strip()
                or not re.fullmatch(r"[A-Za-z0-9 '_-]{1,64}", name)
                for name in people
            )
        ):
            raise ValueError("Invalid recognized-person summary")
        if people and not any(item["label"] == "person" for item in objects):
            raise ValueError("Identity metadata has no current person observation")
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ObjectDetectionError(str(exc)) from exc
    return objects, sorted(set(people))


def visual_response_for_query(user_text: str) -> str | None:
    """Speak validated detections directly, without waiting for chat inference."""
    if not is_visual_query(user_text):
        return None
    try:
        objects, people = _fetch_current_observation(scan=is_visual_followup(user_text))
    except ObjectDetectionError as exc:
        logger.warning("Visual request unavailable: %s", exc)
        return "I can't inspect the camera right now."
    if not objects:
        return "I can't confidently identify any supported objects in this view."
    descriptions = []
    irregular_plurals = {
        "person": "people", "mouse": "mice", "knife": "knives",
        "broccoli": "broccoli",
    }
    for item in objects:
        label, count = item["label"], item["count"]
        if label in ("skis", "scissors"):
            descriptions.append(label)
            continue
        if count != 1:
            label = irregular_plurals.get(
                label, label + ("es" if label.endswith(("s", "ch", "sh")) else "s")
            )
        descriptions.append(f"{count} {label}")
    description = " ".join(
        ("I may see " if index == 0 else "I also detect ")
        + " and ".join(descriptions[index:index + 2]) + "."
        for index in range(0, len(descriptions), 2)
    )
    if people:
        description += " I recognize " + " and ".join(people) + "."
    return description


def vision_context_for_query(user_text: str) -> str | None:
    """Fetch one fresh observation via the local controller, only when asked."""
    if not is_visual_query(user_text):
        return None
    try:
        objects, people = _fetch_current_observation(scan=is_visual_followup(user_text))
    except ObjectDetectionError as exc:
        logger.warning("Visual request unavailable: %s", exc)
        return (
            " Current camera observation: UNAVAILABLE. Clearly say you cannot inspect "
            "the camera right now; do not describe or invent any visible objects."
        )
    facts = [
        f"{item['label']}: estimated count={item['count']}, confidence={item['confidence']:.0%}"
        for item in objects
    ]
    details = "; ".join(facts) if facts else "No supported objects detected confidently."
    identities = (
        " Confident local reference-photo matches: " + ", ".join(people) + "."
        if people else " No person has a confirmed identity in this view."
    )
    return (
        " Current camera observation from fresh RGB images, processed locally: "
        + details
        + identities
        + " Describe only these uncertain detections briefly. Counts are approximate. "
        "Only name people explicitly listed as local reference-photo matches. "
        "Do not infer identity from objects, color, actions, text or unlisted objects. "
        "A person label does not identify anyone; no detections does not mean an empty room."
    )

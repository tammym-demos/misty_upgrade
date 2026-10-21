"""Laptop-side face enrollment and recognition service (#125).

This module replaces Misty's unreliable on-chip face-recognition pipeline
(``/api/faces`` training + ``FaceRecognition`` events, documented as
effectively non-functional on this Snapdragon 410 unit in
``docs/lessons-learned.md``) with a *laptop-side* recognizer that runs on the
companion Windows/x64 machine and produces a ``speaker_name`` for the existing
orchestration path.

Design goals
------------
- **No Misty face firmware dependency.** Recognition happens entirely on the
  laptop from captured frames (Misty RGB camera, laptop webcam, or image file).
- **Cloud-safe / testable without hardware.** Heavy, optional dependencies
  (``cv2``, ``onnxruntime``) are imported lazily so unit tests can inject a
  deterministic fake embedder and static frame sources. No live camera, model
  download, or robot is required for the automated test suite.
- **Fail open.** Every public entry point raises a typed, catchable error so the
  controller can log a clear reason and continue the conversation without a
  ``speaker_name`` rather than crashing.
- **Privacy first.** Photos are saved only with explicit photo enrollment.
  The reference JPEG and its face features share one gitignored local archive.
  User-supplied names cannot escape that directory.

Public API
----------
``FaceRecognizer``
    ``enroll(name, frames) -> FaceProfile``
    ``recognize(frame) -> RecognitionResult | None``
    ``recognize_consistent(frames) -> RecognitionResult | None``
    ``load_profiles()`` / ``save_profile(profile)``
``FaceProfileStore``
    Path-traversal-safe ``.npz`` profile storage.
``FrameSource`` implementations
    ``MistyCameraFrameSource`` / ``WebcamFrameSource`` / ``ImageFileFrameSource``
``recognize_speaker`` helper used by ``misty_controller.py``.
"""

from __future__ import annotations

import os
import re
import logging
import tempfile
import zipfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence

import numpy as np
import config_defaults

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FaceRecognitionError(Exception):
    """Base class for all recoverable face-recognition failures."""


class NoFaceDetected(FaceRecognitionError):
    """No face was found in the supplied frame."""


class MultipleFacesDetected(FaceRecognitionError):
    """More than one face was found where exactly one was required."""


class LowConfidence(FaceRecognitionError):
    """A face was found but no profile matched above the confidence threshold."""


class FaceModelUnavailable(FaceRecognitionError):
    """The face detection/embedding model or its dependencies are unavailable."""


class FrameSourceError(FaceRecognitionError):
    """A frame source could not produce a usable frame."""


class ProfileNameError(FaceRecognitionError, ValueError):
    """A profile name is empty, too long, or contains unsafe characters."""


class ProfileExistsError(FaceRecognitionError):
    """Explicit deletion is required before replacing a photo profile."""


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# A frame is a HxWx3 uint8 BGR image (OpenCV convention) as a numpy array.
Frame = np.ndarray

# Profile names: letters, digits, spaces, apostrophes, underscores and hyphens. This
# both prevents path traversal and keeps file names portable across OSes.
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9 '_-]{1,64}$")
MAX_PROFILE_NAME_LEN = 64

MODEL_NAME = "laptop-face-embedder"
MODEL_VERSION = "1"


def validate_profile_name(name: str) -> str:
    """Return a normalized, safe profile name or raise ``ProfileNameError``.

    Rejects empty names, names longer than :data:`MAX_PROFILE_NAME_LEN`, path
    separators, ``..`` traversal, and any character outside
    ``[A-Za-z0-9 '_-]``. The returned name is stripped of surrounding
    whitespace and is safe to use as a single-path-segment file stem.
    """
    if not isinstance(name, str):
        raise ProfileNameError("profile name must be a string")
    stripped = name.strip()
    if not stripped:
        raise ProfileNameError("profile name must not be empty")
    if len(stripped) > MAX_PROFILE_NAME_LEN:
        raise ProfileNameError(
            f"profile name too long (max {MAX_PROFILE_NAME_LEN} characters)"
        )
    if os.sep in stripped or (os.altsep and os.altsep in stripped):
        raise ProfileNameError("profile name must not contain path separators")
    if ".." in stripped:
        raise ProfileNameError("profile name must not contain '..'")
    if not _VALID_NAME_RE.match(stripped):
        raise ProfileNameError(
            "profile name may only contain letters, digits, spaces, apostrophes, '_' and '-'"
        )
    return stripped


@dataclass
class FaceProfile:
    """An enrolled identity: its embeddings plus provenance metadata.

    A reference JPEG is optional and requires explicit photo enrollment.
    """

    name: str
    embeddings: np.ndarray  # shape (num_samples, embedding_dim), float32, L2-normalized
    model_name: str = MODEL_NAME
    model_version: str = MODEL_VERSION
    created_at: str = ""
    sample_count: int = 0
    embedding_dim: int = 0
    reference_jpeg: Optional[np.ndarray] = None

    def __post_init__(self):
        self.embeddings = np.asarray(self.embeddings, dtype=np.float32)
        self.name = validate_profile_name(self.name)
        if (
            self.embeddings.ndim != 2
            or 0 in self.embeddings.shape
            or not np.isfinite(self.embeddings).all()
            or np.any(np.linalg.norm(self.embeddings, axis=1) == 0)
        ):
            raise FaceRecognitionError(
                "profile embeddings must be a non-empty 2D array"
            )
        self.sample_count = int(self.embeddings.shape[0])
        self.embedding_dim = int(self.embeddings.shape[1])
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if self.reference_jpeg is not None:
            image = np.asarray(self.reference_jpeg)
            if image.dtype != np.uint8 or image.ndim != 1 or image.size == 0:
                raise FaceRecognitionError("reference JPEG must be a non-empty byte array")
            self.reference_jpeg = image.copy()

    @property
    def mean_embedding(self) -> np.ndarray:
        """L2-normalized mean of the enrolled embeddings (the match centroid)."""
        mean = self.embeddings.mean(axis=0)
        return _l2_normalize(mean)


@dataclass
class RecognitionResult:
    """Outcome of a successful recognition."""

    name: str
    distance: float  # cosine distance in [0, 2]; lower is a better match
    threshold: float
    source: str = "unknown"
    confidence: float = field(default=0.0)

    def __post_init__(self):
        # Map cosine distance to a bounded [0, 1] confidence for logging. Clamp
        # both ends so floating-point error (or distance > threshold) cannot
        # produce a value outside [0, 1].
        raw = 1.0 - (self.distance / max(self.threshold, 1e-6))
        self.confidence = min(1.0, max(0.0, raw))


@dataclass(frozen=True)
class FaceDetection:
    """A detected face location in image pixels."""

    x: float
    y: float
    width: float
    height: float
    confidence: float
    frame_width: int
    frame_height: int

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def area_ratio(self) -> float:
        return self.area / max(1, self.frame_width * self.frame_height)

    @property
    def center_x_normalized(self) -> float:
        return ((self.x + self.width / 2.0) / max(1, self.frame_width)) - 0.5

    @property
    def center_y_normalized(self) -> float:
        return ((self.y + self.height / 2.0) / max(1, self.frame_height)) - 0.5


def select_largest_face(detections: Sequence[FaceDetection]) -> Optional[FaceDetection]:
    """Return the largest detected face, used as the closest-person proxy."""
    return max(detections, key=lambda detection: detection.area, default=None)


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    vec = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    if norm < 1e-12:
        return vec
    return vec / norm


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine distance (1 - cosine similarity) between two vectors, in [0, 2]."""
    a = _l2_normalize(a)
    b = _l2_normalize(b)
    return float(1.0 - np.dot(a, b))


# ---------------------------------------------------------------------------
# Embedder abstraction
# ---------------------------------------------------------------------------


class FaceEmbedder:
    """Interface for a face detector + embedding backend.

    Implementations must return one embedding vector *per detected face* for a
    frame. The :class:`FaceRecognizer` enforces the "exactly one face" policy so
    embedders stay simple and testable.
    """

    def extract_embeddings(self, frame: Frame) -> List[np.ndarray]:  # pragma: no cover - interface
        raise NotImplementedError


class OnnxFaceDetector:
    """OpenCV YuNet-compatible detector that does not require face enrollment."""

    def __init__(
        self,
        model_path: str,
        score_threshold: float = 0.6,
        max_input_dimension: int = 1280,
    ):
        self.model_path = model_path
        self.score_threshold = score_threshold
        self.max_input_dimension = max(320, int(max_input_dimension))
        self._detector = None

    def _ensure_loaded(self):
        if self._detector is not None:
            return
        try:
            import cv2
        except Exception as exc:  # pragma: no cover - depends on host env
            raise FaceModelUnavailable(
                "OpenCV (cv2) is required for face detection; install "
                "opencv-python-headless (or opencv-python). Original error: " + str(exc)
            )
        if not self.model_path or not os.path.isfile(self.model_path):
            raise FaceModelUnavailable(
                f"Face detector model file not found: {self.model_path!r}. "
                "Set FACE_DETECTOR_MODEL_PATH to a YuNet-compatible ONNX model."
            )
        try:
            self._detector = cv2.FaceDetectorYN_create(
                self.model_path, "", (320, 320), self.score_threshold
            )
        except Exception as exc:  # pragma: no cover - depends on host env
            raise FaceModelUnavailable("Failed to initialize face detector: " + str(exc))

    def detect(self, frame: Frame) -> List[FaceDetection]:  # pragma: no cover - needs cv2/model
        self._ensure_loaded()
        import cv2

        if frame is None or getattr(frame, "size", 0) == 0:
            raise FrameSourceError("empty frame supplied to detector")
        height, width = frame.shape[:2]
        scale = min(1.0, self.max_input_dimension / max(height, width))
        inference_frame = frame
        if scale < 1.0:
            inference_frame = cv2.resize(
                frame,
                (max(1, round(width * scale)), max(1, round(height * scale))),
            )
        inference_height, inference_width = inference_frame.shape[:2]
        self._detector.setInputSize((inference_width, inference_height))
        _, faces = self._detector.detect(inference_frame)
        if faces is None:
            return []
        coordinate_scale = 1.0 / scale
        return [
            FaceDetection(
                x=float(face[0]) * coordinate_scale,
                y=float(face[1]) * coordinate_scale,
                width=float(face[2]) * coordinate_scale,
                height=float(face[3]) * coordinate_scale,
                confidence=float(face[-1]),
                frame_width=width,
                frame_height=height,
            )
            for face in faces
            if float(face[-1]) >= self.score_threshold
        ]


class OnnxFaceEmbedder(FaceEmbedder):
    """Default OpenCV + ONNX Runtime face detector/embedder.

    Both ``cv2`` and ``onnxruntime`` and the model files are loaded lazily on
    first use. If any dependency or model file is missing, a
    :class:`FaceModelUnavailable` error is raised with actionable guidance
    instead of failing at import time. This keeps the module importable (and the
    rest of the pipeline testable) on machines that do not have the models.

    Model files are supplied via env/config paths and are intentionally *not*
    bundled in git (``*.onnx`` is gitignored). See the README for how to obtain
    a detector (e.g. YuNet) and an embedding model (e.g. SFace/ArcFace) for live
    laptop recognition.
    """

    def __init__(
        self,
        detector_model_path: str,
        embedder_model_path: str,
        detector_score_threshold: float = 0.6,
    ):
        self.detector_model_path = detector_model_path
        self.embedder_model_path = embedder_model_path
        self.detector_score_threshold = detector_score_threshold
        self._detector = None
        self._recognizer = None
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        if self._detector is not None and self._recognizer is not None:
            return
        try:
            import cv2  # noqa: F401  (lazy, optional dependency)
        except Exception as exc:  # pragma: no cover - depends on host env
            raise FaceModelUnavailable(
                "OpenCV (cv2) is required for laptop face recognition; install "
                "opencv-python-headless (or opencv-python). Original error: " + str(exc)
            )
        for path, label in (
            (self.detector_model_path, "detector"),
            (self.embedder_model_path, "embedding"),
        ):
            if not path or not os.path.isfile(path):
                raise FaceModelUnavailable(
                    f"Face {label} model file not found: {path!r}. Set the model "
                    "path via config/env and download the model (see README)."
                )
        try:
            self._detector = cv2.FaceDetectorYN_create(
                self.detector_model_path, "", (320, 320),
                self.detector_score_threshold,
            )
            self._recognizer = cv2.FaceRecognizerSF_create(
                self.embedder_model_path, ""
            )
        except Exception as exc:  # pragma: no cover - depends on host env
            raise FaceModelUnavailable(
                "Failed to initialize OpenCV face models: " + str(exc)
            )

    def extract_embeddings(self, frame: Frame) -> List[np.ndarray]:  # pragma: no cover - needs cv2/models
        with self._lock:
            self._ensure_loaded()
            import cv2

            try:
                return self._extract_embeddings(frame)
            except cv2.error as exc:
                raise FaceRecognitionError(f"Face inference failed: {exc}") from exc

    def _extract_embeddings(self, frame: Frame) -> List[np.ndarray]:
        self._ensure_loaded()
        import cv2

        if frame is None or getattr(frame, "size", 0) == 0:
            raise FrameSourceError("empty frame supplied to embedder")
        h, w = frame.shape[:2]
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(frame)
        if faces is None:
            return []
        embeddings: List[np.ndarray] = []
        for face in faces:
            aligned = self._recognizer.alignCrop(frame, face)
            feature = self._recognizer.feature(aligned)
            embeddings.append(_l2_normalize(np.asarray(feature, dtype=np.float32).ravel()))
        return embeddings


# ---------------------------------------------------------------------------
# Profile storage
# ---------------------------------------------------------------------------


class FaceProfileStore:
    """Stores :class:`FaceProfile` objects as ``.npz`` files in one directory.

    Names are validated with :func:`validate_profile_name` and every resolved
    path is confirmed to stay inside the profile directory, preventing path
    traversal from a hostile ``--name``.
    """

    SUFFIX = ".npz"

    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)

    def _safe_path(self, name: str) -> str:
        safe = validate_profile_name(name)
        path = os.path.abspath(os.path.join(self.directory, safe + self.SUFFIX))
        # Defense in depth: the resolved path must live directly inside the
        # profile directory (single path segment, no traversal).
        if os.path.dirname(path) != self.directory:
            raise ProfileNameError("resolved profile path escapes profile directory")
        return path

    def path_for(self, name: str) -> str:
        """Return the (validated) on-disk path for ``name`` without touching disk."""
        return self._safe_path(name)

    def ensure_dir(self):
        os.makedirs(self.directory, exist_ok=True)

    def save(self, profile: FaceProfile, *, replace: bool = True) -> str:
        """Atomically persist the features and optional photo as one archive."""
        self.ensure_dir()
        path = self._safe_path(profile.name)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.directory, prefix=".profile-", suffix=".npz", delete=False
            ) as output:
                temporary = output.name
                np.savez_compressed(
                    output, name=profile.name, embeddings=profile.embeddings,
                    model_name=profile.model_name, model_version=profile.model_version,
                    created_at=profile.created_at, sample_count=profile.sample_count,
                    embedding_dim=profile.embedding_dim,
                    reference_jpeg=(
                        profile.reference_jpeg if profile.reference_jpeg is not None
                        else np.empty(0, dtype=np.uint8)
                    ),
                )
                output.flush()
                os.fsync(output.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                # A hard link publishes the complete archive without a check/write race.
                os.link(temporary, path)
        except FileExistsError as exc:
            raise ProfileExistsError(f"A face profile for {profile.name!r} already exists") from exc
        except OSError as exc:
            raise FaceRecognitionError(f"Could not save face profile: {exc}") from exc
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.remove(temporary)
        return path

    def load(self, name: str) -> FaceProfile:
        path = self._safe_path(name)
        if not os.path.isfile(path):
            raise FaceRecognitionError(f"no enrolled profile named {name!r}")
        with np.load(path, allow_pickle=False) as data:
            return FaceProfile(
                name=str(data["name"]),
                embeddings=np.asarray(data["embeddings"], dtype=np.float32),
                model_name=str(data["model_name"]),
                model_version=str(data["model_version"]),
                created_at=str(data["created_at"]),
                reference_jpeg=(
                    np.asarray(data["reference_jpeg"], dtype=np.uint8)
                    if "reference_jpeg" in data and data["reference_jpeg"].size else None
                ),
            )

    def list_names(self) -> List[str]:
        if not os.path.isdir(self.directory):
            return []
        names = []
        for entry in sorted(os.listdir(self.directory)):
            if entry.endswith(self.SUFFIX) and not entry.startswith("."):
                names.append(entry[: -len(self.SUFFIX)])
        return names

    def load_all(self) -> List[FaceProfile]:
        profiles = []
        for name in self.list_names():
            try:
                profiles.append(self.load(name))
            except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile, FaceRecognitionError) as exc:
                logger.warning("Cannot load local face profile %r: %s", name, exc)
                continue
        return profiles

    def delete(self, name: str) -> bool:
        path = self._safe_path(name)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False


# ---------------------------------------------------------------------------
# Recognizer
# ---------------------------------------------------------------------------


class FaceRecognizer:
    """Enrolls and recognizes named local face profiles.

    The recognizer is deliberately agnostic to how frames were produced and how
    embeddings are computed: it composes a :class:`FaceProfileStore` and a
    :class:`FaceEmbedder`. Tests inject a deterministic fake embedder; production
    uses :class:`OnnxFaceEmbedder`.
    """

    def __init__(
        self,
        store: FaceProfileStore,
        embedder: FaceEmbedder,
        threshold: float = 0.4,
        min_samples: int = 5,
        min_consistent_frames: int = 2,
        match_margin: float = 0.05,
    ):
        if not np.isfinite(threshold) or threshold <= 0:
            raise ValueError("threshold must be > 0")
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        if min_consistent_frames < 1:
            raise ValueError("min_consistent_frames must be >= 1")
        if not np.isfinite(match_margin) or match_margin < 0:
            raise ValueError("match_margin must be finite and >= 0")
        self.store = store
        self.embedder = embedder
        self.threshold = float(threshold)
        self.min_samples = int(min_samples)
        self.min_consistent_frames = int(min_consistent_frames)
        self.match_margin = float(match_margin)
        self._profiles: Optional[List[FaceProfile]] = None

    # --- profile access ---------------------------------------------------
    def load_profiles(self, force: bool = False) -> List[FaceProfile]:
        if self._profiles is None or force:
            self._profiles = self.store.load_all()
        return self._profiles

    def save_profile(self, profile: FaceProfile, *, replace: bool = True) -> str:
        path = self.store.save(profile, replace=replace)
        self._profiles = None  # invalidate cache
        return path

    # --- embedding --------------------------------------------------------
    def count_valid_samples(self, frames: Sequence[Frame]) -> tuple[int, int]:
        """Return ``(valid, rejected)`` single-face sample counts for ``frames``.

        A public, side-effect-free helper for dry-run/preview flows: it does not
        persist anything. A frame counts as valid only when exactly one face is
        detected (no face or multiple faces are rejected).
        """
        valid = 0
        rejected = 0
        for frame in frames:
            try:
                self._single_face_embedding(frame)
                valid += 1
            except FaceRecognitionError:
                rejected += 1
        return valid, rejected

    def _single_face_embedding(self, frame: Frame) -> np.ndarray:
        """Return exactly one embedding for ``frame`` or raise a typed error."""
        embeddings = self.embedder.extract_embeddings(frame)
        if not embeddings:
            raise NoFaceDetected("no face detected in frame")
        if len(embeddings) > 1:
            raise MultipleFacesDetected(
                f"{len(embeddings)} faces detected; exactly one is required"
            )
        embedding = np.asarray(embeddings[0], dtype=np.float32).ravel()
        if not embedding.size or not np.isfinite(embedding).all() or np.linalg.norm(embedding) == 0:
            raise FaceRecognitionError("invalid face features")
        return _l2_normalize(embedding)

    # --- enrollment -------------------------------------------------------
    def enroll(
        self, name: str, frames: Sequence[Frame], *,
        save_reference_photo: bool = False, replace: bool = True,
    ) -> FaceProfile:
        """Enroll locally; saving an actual photo requires an explicit opt-in."""
        profile = self.prepare_enrollment(
            name, frames, save_reference_photo=save_reference_photo
        )
        self.save_profile(profile, replace=replace)
        return profile

    def prepare_enrollment(
        self, name: str, frames: Sequence[Frame], *, save_reference_photo: bool = False,
    ) -> FaceProfile:
        """Extract an enrollment without writing, allowing a final cancellation check."""
        safe_name = validate_profile_name(name)
        valid: List[np.ndarray] = []
        rejected = 0
        for frame in frames:
            try:
                embedding = self._single_face_embedding(frame)
                if save_reference_photo and valid and cosine_distance(valid[0], embedding) > self.threshold:
                    raise LowConfidence("The same person must remain alone in view")
                valid.append(embedding)
            except FaceRecognitionError:
                if save_reference_photo:
                    raise
                rejected += 1
        if len(valid) < self.min_samples:
            raise FaceRecognitionError(
                f"only {len(valid)} valid face sample(s) captured for {safe_name!r}; "
                f"need at least {self.min_samples} (rejected {rejected})"
            )
        reference_jpeg = None
        if save_reference_photo:
            try:
                import cv2
            except ImportError as exc:
                raise FaceModelUnavailable("OpenCV is required to save a reference photo") from exc

            frame = frames[0]
            if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
                raise FrameSourceError("reference photo must be a BGR camera image")
            try:
                ok, reference_jpeg = cv2.imencode(".jpg", frame)
            except cv2.error as exc:
                raise FrameSourceError(f"could not encode the reference photo: {exc}") from exc
            if not ok:
                raise FrameSourceError("could not encode the reference photo")
            reference_jpeg = reference_jpeg.ravel()
        return FaceProfile(
            name=safe_name, embeddings=np.vstack(valid), reference_jpeg=reference_jpeg
        )

    # --- recognition ------------------------------------------------------
    def recognize(self, frame: Frame, source: str = "unknown") -> Optional[RecognitionResult]:
        """Recognize a single frame.

        Returns a :class:`RecognitionResult` when the best profile match is at
        or below :attr:`threshold`, otherwise ``None`` (unknown / low
        confidence). Raises :class:`NoFaceDetected` / :class:`MultipleFacesDetected`
        for framing problems so callers can distinguish "no face" from "unknown
        face".
        """
        probe = self._single_face_embedding(frame)
        profiles = self.load_profiles()
        best_name: Optional[str] = None
        best_dist = float("inf")
        second_dist = float("inf")
        for profile in profiles:
            if (
                profile.embedding_dim != probe.size
                or profile.model_name != MODEL_NAME or profile.model_version != MODEL_VERSION
            ):
                logger.warning("Face profile %r uses incompatible model features", profile.name)
                continue
            dist = cosine_distance(probe, profile.mean_embedding)
            if dist < best_dist:
                second_dist = best_dist
                best_dist = dist
                best_name = profile.name
            elif dist < second_dist:
                second_dist = dist
        if (
            best_name is None or best_dist > self.threshold
            or second_dist - best_dist < self.match_margin
        ):
            return None
        return RecognitionResult(
            name=best_name, distance=best_dist, threshold=self.threshold, source=source
        )

    def recognize_consistent(
        self, frames: Sequence[Frame], source: str = "unknown"
    ) -> Optional[RecognitionResult]:
        """Require the same name across at least ``min_consistent_frames`` frames.

        This guards against single-frame false positives. Frames that fail to
        produce a face are ignored. Returns the best-scoring result for the
        winning name, or ``None`` if no name reaches the consistency threshold.
        """
        counts: dict[str, int] = {}
        best_by_name: dict[str, RecognitionResult] = {}
        for frame in frames:
            try:
                result = self.recognize(frame, source=source)
            except FaceModelUnavailable:
                raise
            except FaceRecognitionError:
                continue
            if result is None:
                continue
            counts[result.name] = counts.get(result.name, 0) + 1
            prev = best_by_name.get(result.name)
            if prev is None or result.distance < prev.distance:
                best_by_name[result.name] = result
        # Conflicting identities are not evidence that one of them is the speaker.
        qualified = [name for name, count in counts.items() if count >= self.min_consistent_frames]
        if not qualified or len(counts) != 1:
            return None
        winner = min(qualified, key=lambda n: (-counts[n], best_by_name[n].distance))
        return best_by_name[winner]


# ---------------------------------------------------------------------------
# Frame sources
# ---------------------------------------------------------------------------


class FrameSource:
    """Interface for producing frames the recognizer can consume."""

    name = "unknown"

    def capture(self) -> Frame:  # pragma: no cover - interface
        raise NotImplementedError

    def capture_many(self, count: int, interval_s: float = 0.2) -> List[Frame]:
        frames: List[Frame] = []
        for i in range(max(1, count)):
            frames.append(self.capture())
            if interval_s > 0 and i < count - 1:
                time.sleep(interval_s)
        return frames

    def close(self):  # pragma: no cover - default no-op
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ImageFileFrameSource(FrameSource):
    """Frame source backed by image files. Primary source for tests/offline use.

    ``reader`` is an injectable callable ``path -> Frame`` (default: cv2.imread),
    which lets unit tests supply static numpy arrays without OpenCV installed.
    """

    name = "image_file"

    def __init__(self, paths: Sequence[str], reader: Optional[Callable[[str], Frame]] = None):
        self._paths = list(paths)
        if not self._paths:
            raise FrameSourceError("ImageFileFrameSource requires at least one path")
        self._reader = reader or self._default_reader
        self._index = 0

    @staticmethod
    def _default_reader(path: str) -> Frame:  # pragma: no cover - needs cv2
        try:
            import cv2
        except Exception as exc:
            raise FaceModelUnavailable(
                "OpenCV (cv2) is required to read image files; install opencv-python-headless (or opencv-python). "
                + str(exc)
            )
        img = cv2.imread(path)
        if img is None:
            raise FrameSourceError(f"could not read image file: {path!r}")
        return img

    def capture(self) -> Frame:
        path = self._paths[self._index % len(self._paths)]
        self._index += 1
        return self._reader(path)


class WebcamFrameSource(FrameSource):  # pragma: no cover - needs a camera
    """Frame source backed by a laptop webcam via OpenCV ``VideoCapture``."""

    name = "webcam"

    def __init__(self, device_index: int = 0, warmup_frames: int = 3):
        self.device_index = device_index
        self.warmup_frames = warmup_frames
        self._cap = None

    def _ensure_open(self):
        if self._cap is not None:
            return
        try:
            import cv2
        except Exception as exc:
            raise FaceModelUnavailable(
                "OpenCV (cv2) is required for webcam capture; install opencv-python-headless (or opencv-python). "
                + str(exc)
            )
        cap = cv2.VideoCapture(self.device_index)
        if not cap.isOpened():
            raise FrameSourceError(f"could not open webcam device {self.device_index}")
        for _ in range(self.warmup_frames):
            cap.read()
        self._cap = cap

    def capture(self) -> Frame:
        self._ensure_open()
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise FrameSourceError("failed to read frame from webcam")
        return frame

    def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class MistyCameraFrameSource(FrameSource):
    """Frame source backed by Misty's RGB camera (``GET /api/cameras/rgb``).

    Fetches a JPEG from the robot and decodes it with OpenCV. Timeouts and
    malformed responses raise :class:`FrameSourceError` with a clear message so
    the caller can fall back to a webcam or continue without a name.
    """

    name = "misty_camera"
    _capture_locks: dict = {}
    _capture_locks_guard = threading.Lock()

    def __init__(
        self,
        misty_ip: str,
        timeout_s: Optional[float] = None,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ):
        if not misty_ip:
            raise FrameSourceError("MistyCameraFrameSource requires a misty_ip")
        self.misty_ip = misty_ip
        self.timeout_s = (
            float(os.getenv("MISTY_CAMERA_TIMEOUT_S", str(config_defaults.MISTY_CAMERA_TIMEOUT_S)))
            if timeout_s is None else timeout_s
        )
        self.width = (
            int(os.getenv("MISTY_CAMERA_WIDTH", str(config_defaults.MISTY_CAMERA_WIDTH)))
            if width is None else width
        )
        self.height = (
            int(os.getenv("MISTY_CAMERA_HEIGHT", str(config_defaults.MISTY_CAMERA_HEIGHT)))
            if height is None else height
        )
        if self.width <= 0 or self.height <= 0 or not np.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise FrameSourceError("Misty camera dimensions and timeout must be positive")
        self.url = f"http://{misty_ip}/api/cameras/rgb"
        with self._capture_locks_guard:
            self._capture_lock = self._capture_locks.setdefault(
                misty_ip.strip().lower(), threading.Lock()
            )

    def capture(self) -> Frame:
        try:
            import requests
        except ImportError as exc:
            raise FrameSourceError("requests is required for Misty camera capture: " + str(exc)) from exc
        try:
            import cv2
        except ImportError as exc:
            raise FaceModelUnavailable(
                "OpenCV (cv2) is required to decode Misty camera frames; install "
                "opencv-python-headless (or opencv-python). " + str(exc)
            )
        if not self._capture_lock.acquire(timeout=self.timeout_s):
            raise FrameSourceError("Misty camera is busy with another local capture")
        try:
            resp = requests.get(
                self.url,
                params={"base64": "false", "width": self.width, "height": self.height},
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise FrameSourceError(f"Misty camera request failed: {exc}") from exc
        finally:
            self._capture_lock.release()
        if not resp.content:
            raise FrameSourceError("Misty camera returned an empty response")
        buffer = np.frombuffer(resp.content, dtype=np.uint8)
        try:
            frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        except cv2.error as exc:
            raise FrameSourceError(f"Misty camera JPEG decode failed: {exc}") from exc
        if frame is None:
            raise FrameSourceError("Misty camera returned a non-image / malformed response")
        return frame


# ---------------------------------------------------------------------------
# Controller integration helper
# ---------------------------------------------------------------------------


def recognize_speaker(
    recognizer: FaceRecognizer,
    source: FrameSource,
    frame_count: Optional[int] = None,
    interval_s: float = 0.15,
) -> Optional[str]:
    """Capture frames and return a recognized speaker name, or ``None``.

    This is the single entry point ``misty_controller.py`` uses. It is
    deliberately *fail-open*: any :class:`FaceRecognitionError` (no face,
    unknown face, unavailable model, camera/frame failure) results in ``None``
    so the conversation continues without a name. It never raises.

    ``frame_count`` defaults to the recognizer's ``min_consistent_frames`` so a
    match must be confirmed across multiple frames before a name is returned.
    """
    if frame_count is None:
        frame_count = recognizer.min_consistent_frames
    try:
        frames = source.capture_many(frame_count, interval_s=interval_s)
    except (FaceRecognitionError, OSError, ValueError, RuntimeError) as exc:
        logger.warning("Speaker camera capture unavailable: %s", exc)
        return None
    try:
        result = recognizer.recognize_consistent(frames, source=getattr(source, "name", "unknown"))
    except (FaceRecognitionError, OSError, ValueError, RuntimeError) as exc:
        logger.warning("Speaker recognition unavailable: %s", exc)
        return None
    return result.name if result is not None else None

"""Cloud-safe unit tests for laptop-side face recognition (#125).

These tests use a deterministic fake embedder and in-memory / static frame
sources, so they run without Misty hardware, a camera, OpenCV, or ONNX models.
"""

import importlib.util
import os
import sys

import numpy as np
import pytest

_ORCH_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration")
)
if _ORCH_DIR not in sys.path:
    sys.path.insert(0, _ORCH_DIR)

import face_recognition_service as frs  # noqa: E402


DIM = 4
TAMMY = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
ALEX = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
STRANGER = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)


def make_frame(*embeddings) -> np.ndarray:
    """Build a fake frame whose 'faces' are the given embedding rows.

    Shape (num_faces, DIM). An empty frame (num_faces == 0) means 'no face'.
    """
    if not embeddings:
        return np.zeros((0, DIM), dtype=np.float32)
    return np.vstack([np.asarray(e, dtype=np.float32) for e in embeddings])


class FakeEmbedder(frs.FaceEmbedder):
    """Interprets a frame as an (n_faces, DIM) array of pre-baked embeddings."""

    def extract_embeddings(self, frame):
        arr = np.asarray(frame, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] == 0:
            return []
        return [arr[i] for i in range(arr.shape[0])]


class FakeSource(frs.FrameSource):
    name = "fake"

    def __init__(self, frames, raise_on_capture=False):
        self._frames = list(frames)
        self._i = 0
        self._raise = raise_on_capture

    def capture(self):
        if self._raise:
            raise frs.FrameSourceError("boom")
        frame = self._frames[self._i % len(self._frames)]
        self._i += 1
        return frame


def make_recognizer(tmp_path, **kwargs):
    store = frs.FaceProfileStore(str(tmp_path))
    defaults = dict(threshold=0.4, min_samples=3, min_consistent_frames=2)
    defaults.update(kwargs)
    return frs.FaceRecognizer(store=store, embedder=FakeEmbedder(), **defaults)


# ---------------------------------------------------------------------------
# Name validation / path-traversal safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Tammy", "Tammy Two", "tammy_2", "a-b-c"])
def test_validate_profile_name_accepts_safe(name):
    assert frs.validate_profile_name(name) == name.strip()


@pytest.mark.parametrize(
    "name",
    ["", "   ", "..", "../evil", "a/b", "a\\b", "x" * 65, "bad*name", "semi;colon"],
)
def test_validate_profile_name_rejects_unsafe(name):
    with pytest.raises(frs.ProfileNameError):
        frs.validate_profile_name(name)


def test_store_path_never_escapes_directory(tmp_path):
    store = frs.FaceProfileStore(str(tmp_path))
    with pytest.raises(frs.ProfileNameError):
        store.path_for("../../etc/passwd")
    good = store.path_for("Tammy")
    assert os.path.abspath(good).startswith(os.path.abspath(str(tmp_path)))


# ---------------------------------------------------------------------------
# Profile storage
# ---------------------------------------------------------------------------


def test_profile_store_roundtrip(tmp_path):
    store = frs.FaceProfileStore(str(tmp_path))
    embeddings = np.vstack([TAMMY, TAMMY, TAMMY])
    profile = frs.FaceProfile(name="Tammy", embeddings=embeddings)
    path = store.save(profile)
    assert os.path.isfile(path)

    loaded = store.load("Tammy")
    assert loaded.name == "Tammy"
    assert loaded.sample_count == 3
    assert loaded.embedding_dim == DIM
    assert loaded.model_name == frs.MODEL_NAME
    np.testing.assert_allclose(loaded.embeddings, embeddings)


def test_profile_store_list_and_delete(tmp_path):
    store = frs.FaceProfileStore(str(tmp_path))
    store.save(frs.FaceProfile(name="Tammy", embeddings=np.vstack([TAMMY, TAMMY])))
    store.save(frs.FaceProfile(name="Alex", embeddings=np.vstack([ALEX, ALEX])))
    assert store.list_names() == ["Alex", "Tammy"]
    deleted = store.delete("Alex")
    assert deleted is True
    assert store.list_names() == ["Tammy"]
    deleted_again = store.delete("Alex")
    assert deleted_again is False


def test_load_all_skips_corrupt_files(tmp_path):
    store = frs.FaceProfileStore(str(tmp_path))
    store.save(frs.FaceProfile(name="Tammy", embeddings=np.vstack([TAMMY, TAMMY])))
    # Drop a foreign .npz that is not a valid profile.
    with open(os.path.join(str(tmp_path), "junk.npz"), "wb") as fh:
        fh.write(b"not a real npz")
    profiles = store.load_all()
    assert [p.name for p in profiles] == ["Tammy"]


def test_profile_rejects_empty_embeddings():
    with pytest.raises(frs.FaceRecognitionError):
        frs.FaceProfile(name="Tammy", embeddings=np.zeros((0, DIM), dtype=np.float32))


def test_mean_embedding_is_normalized():
    profile = frs.FaceProfile(name="Tammy", embeddings=np.vstack([TAMMY * 3, TAMMY * 5]))
    assert pytest.approx(np.linalg.norm(profile.mean_embedding), abs=1e-5) == 1.0


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


def test_cosine_distance_identity_and_orthogonal():
    assert frs.cosine_distance(TAMMY, TAMMY) == pytest.approx(0.0, abs=1e-6)
    assert frs.cosine_distance(TAMMY, ALEX) == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


def test_enroll_success(tmp_path):
    rec = make_recognizer(tmp_path, min_samples=3)
    frames = [make_frame(TAMMY) for _ in range(4)]
    profile = rec.enroll("Tammy", frames)
    assert profile.name == "Tammy"
    assert profile.sample_count == 4
    assert "Tammy" in rec.store.list_names()


def test_enroll_rejects_too_few_valid_samples(tmp_path):
    rec = make_recognizer(tmp_path, min_samples=5)
    # Only 2 usable frames; the rest have no face or multiple faces.
    frames = [make_frame(TAMMY), make_frame(TAMMY), make_frame(), make_frame(TAMMY, ALEX)]
    with pytest.raises(frs.FaceRecognitionError):
        rec.enroll("Tammy", frames)
    assert "Tammy" not in rec.store.list_names()


def test_enroll_validates_name(tmp_path):
    rec = make_recognizer(tmp_path)
    with pytest.raises(frs.ProfileNameError):
        rec.enroll("../evil", [make_frame(TAMMY)] * 5)


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------


def test_recognize_known_face(tmp_path):
    rec = make_recognizer(tmp_path, min_samples=3)
    rec.enroll("Tammy", [make_frame(TAMMY)] * 3)
    result = rec.recognize(make_frame(TAMMY), source="test")
    assert result is not None
    assert result.name == "Tammy"
    assert result.distance <= rec.threshold
    assert result.source == "test"
    assert 0.0 <= result.confidence <= 1.0


def test_recognize_unknown_face_returns_none(tmp_path):
    rec = make_recognizer(tmp_path, min_samples=3)
    rec.enroll("Tammy", [make_frame(TAMMY)] * 3)
    assert rec.recognize(make_frame(STRANGER)) is None


def test_recognize_no_face_raises(tmp_path):
    rec = make_recognizer(tmp_path, min_samples=3)
    rec.enroll("Tammy", [make_frame(TAMMY)] * 3)
    with pytest.raises(frs.NoFaceDetected):
        rec.recognize(make_frame())


def test_recognize_multiple_faces_raises(tmp_path):
    rec = make_recognizer(tmp_path, min_samples=3)
    rec.enroll("Tammy", [make_frame(TAMMY)] * 3)
    with pytest.raises(frs.MultipleFacesDetected):
        rec.recognize(make_frame(TAMMY, ALEX))


def test_recognize_with_no_profiles_returns_none(tmp_path):
    rec = make_recognizer(tmp_path)
    assert rec.recognize(make_frame(TAMMY)) is None


def test_recognize_consistent_requires_min_frames(tmp_path):
    rec = make_recognizer(tmp_path, min_samples=3, min_consistent_frames=2)
    rec.enroll("Tammy", [make_frame(TAMMY)] * 3)
    # Only one Tammy frame + noise → not enough agreement.
    frames = [make_frame(TAMMY), make_frame(STRANGER), make_frame()]
    assert rec.recognize_consistent(frames) is None
    # Two agreeing frames → recognized.
    frames = [make_frame(TAMMY), make_frame(TAMMY), make_frame(STRANGER)]
    result = rec.recognize_consistent(frames)
    assert result is not None and result.name == "Tammy"


def test_recognize_consistent_prefers_strongest_evidence(tmp_path):
    rec = make_recognizer(tmp_path, min_samples=3, min_consistent_frames=2)
    rec.enroll("Tammy", [make_frame(TAMMY)] * 3)
    rec.enroll("Alex", [make_frame(ALEX)] * 3)
    # Both clear min_consistent_frames=2; Tammy has more agreeing frames and
    # must win regardless of frame order.
    frames = [make_frame(ALEX), make_frame(TAMMY), make_frame(ALEX),
              make_frame(TAMMY), make_frame(TAMMY)]
    result = rec.recognize_consistent(frames)
    assert result is not None and result.name == "Tammy"


# ---------------------------------------------------------------------------
# Controller integration helper (recognize_speaker)
# ---------------------------------------------------------------------------


def test_recognize_speaker_returns_name(tmp_path):
    rec = make_recognizer(tmp_path, min_samples=3, min_consistent_frames=2)
    rec.enroll("Tammy", [make_frame(TAMMY)] * 3)
    source = FakeSource([make_frame(TAMMY), make_frame(TAMMY)])
    assert frs.recognize_speaker(rec, source, frame_count=2, interval_s=0) == "Tammy"


def test_recognize_speaker_unknown_returns_none(tmp_path):
    rec = make_recognizer(tmp_path, min_samples=3, min_consistent_frames=2)
    rec.enroll("Tammy", [make_frame(TAMMY)] * 3)
    source = FakeSource([make_frame(STRANGER), make_frame(STRANGER)])
    assert frs.recognize_speaker(rec, source, frame_count=2, interval_s=0) is None


def test_recognize_speaker_is_fail_open(tmp_path):
    rec = make_recognizer(tmp_path, min_samples=3)
    rec.enroll("Tammy", [make_frame(TAMMY)] * 3)
    source = FakeSource([make_frame(TAMMY)], raise_on_capture=True)
    # Capture raises → helper must swallow and return None, never raise.
    assert frs.recognize_speaker(rec, source, frame_count=2, interval_s=0) is None


# ---------------------------------------------------------------------------
# Frame sources
# ---------------------------------------------------------------------------


def test_image_file_frame_source_uses_injected_reader():
    calls = []

    def fake_reader(path):
        calls.append(path)
        return make_frame(TAMMY)

    src = frs.ImageFileFrameSource(["a.jpg", "b.jpg"], reader=fake_reader)
    frames = src.capture_many(3, interval_s=0)
    assert len(frames) == 3
    assert calls == ["a.jpg", "b.jpg", "a.jpg"]  # cycles through paths


def test_image_file_frame_source_requires_paths():
    with pytest.raises(frs.FrameSourceError):
        frs.ImageFileFrameSource([])


def test_capture_many_count():
    src = FakeSource([make_frame(TAMMY)])
    assert len(src.capture_many(5, interval_s=0)) == 5


def test_misty_source_requires_ip():
    with pytest.raises(frs.FrameSourceError):
        frs.MistyCameraFrameSource("")


# ---------------------------------------------------------------------------
# OnnxFaceEmbedder unavailability (no model files / deps)
# ---------------------------------------------------------------------------


def test_onnx_embedder_reports_unavailable_without_models(tmp_path):
    embedder = frs.OnnxFaceEmbedder(
        detector_model_path=str(tmp_path / "missing_detector.onnx"),
        embedder_model_path=str(tmp_path / "missing_embedder.onnx"),
    )
    with pytest.raises(frs.FaceModelUnavailable):
        embedder.extract_embeddings(make_frame(TAMMY))


# ---------------------------------------------------------------------------
# CLI argument validation (mocked/static; no models needed)
# ---------------------------------------------------------------------------


def _load_tool(module_name, filename):
    path = os.path.join(os.path.dirname(__file__), "..", "tools", filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_enroll_cli_requires_exactly_one_action(tmp_path):
    mod = _load_tool("enroll_face", "enroll_face.py")
    with pytest.raises(SystemExit):
        mod.main(["--profile-dir", str(tmp_path)])  # no action
    with pytest.raises(SystemExit):
        mod.main(["--list", "--delete", "X", "--profile-dir", str(tmp_path)])  # two actions


def test_enroll_cli_list_empty_dir(tmp_path):
    mod = _load_tool("enroll_face", "enroll_face.py")
    assert mod.main(["--list", "--profile-dir", str(tmp_path)]) == 0


def test_enroll_cli_delete_missing_returns_one(tmp_path):
    mod = _load_tool("enroll_face", "enroll_face.py")
    assert mod.main(["--delete", "Nobody", "--profile-dir", str(tmp_path)]) == 1


def test_enroll_cli_rejects_unsafe_name(tmp_path):
    mod = _load_tool("enroll_face", "enroll_face.py")
    rc = mod.main(["--name", "../evil", "--source", "image", "--image", "x.jpg",
                   "--profile-dir", str(tmp_path)])
    assert rc == 1


def test_enroll_cli_rejects_bad_samples(tmp_path):
    mod = _load_tool("enroll_face", "enroll_face.py")
    with pytest.raises(SystemExit):
        mod.main(["--name", "Tammy", "--samples", "0", "--profile-dir", str(tmp_path)])


def test_recognize_cli_no_profiles_returns_one(tmp_path):
    mod = _load_tool("recognize_face", "recognize_face.py")
    assert mod.main(["--source", "webcam", "--profile-dir", str(tmp_path)]) == 1


def test_recognize_cli_rejects_bad_frames(tmp_path):
    mod = _load_tool("recognize_face", "recognize_face.py")
    with pytest.raises(SystemExit):
        mod.main(["--frames", "-1", "--profile-dir", str(tmp_path)])

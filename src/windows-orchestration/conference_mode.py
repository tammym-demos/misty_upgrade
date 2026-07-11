"""
Conference Mode for scripted Misty stage dialog (issue #128).

Conference Mode lets Misty participate in an on-stage scripted dialog (for
example ``talks/20260710-2.md``) by playing *predetermined* audio cues instead
of routing each scripted Misty line through the live STT -> LLM -> TTS
conversation path. The presenter speaks naturally and Misty plays the next
predetermined cue once the presenter finishes speaking, with manual override
controls available at all times for stage safety.

Design goals
------------
* **Companion-side only.** Misty stays a physical I/O endpoint; all parsing,
  preparation, cue selection and control logic run on the Windows companion
  laptop. Misty runs no inference or on-robot conference logic.
* **Opt-in and isolated.** Conference Mode is gated by ``CONFERENCE_MODE_ENABLED``
  (default off) and lives in its own module. Normal wake-word conversation
  behavior in ``misty_controller.py`` is unchanged when the mode is off.
* **Deterministic and testable.** Script parsing, cue-ID assignment, manifest
  generation and the control state machine are pure companion-side logic. All
  hardware/live dependencies (Misty playback, presenter voice-activity
  detection, Foundry Local TTS) are injected callables, so the logic is fully
  unit-testable in the cloud without a robot, Foundry Local or Windows audio.
* **No LLM at showtime.** Runtime never invokes the LLM for a scripted cue.
  When explicitly enabled, the fallback path synthesizes the known scripted text
  through TTS only if a cue's predetermined audio is missing.

The module exposes:

* :func:`parse_script` -> :class:`ConferenceScript` (ordered cues + stable IDs)
* :func:`prepare_assets` -> :class:`ConferenceManifest` (generate/import/reuse
  WAVs and describe them)
* :class:`ConferenceController` -> start/pause/resume/next/replay/previous/
  jump-to-slide/stop/auto-advance/safe-shutdown state machine
* a CLI with ``dry-run``, ``prepare``, ``verify`` and ``run`` subcommands.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import wave
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

try:  # Allow use both as a package module and as a stand-alone script.
    import importlib.util as _importlib_util

    if _importlib_util.find_spec("config_defaults") is None:  # pragma: no cover
        # Stand-alone script: make the module's own directory importable.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except ImportError:  # pragma: no cover - very old/unusual runtimes
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_defaults  # noqa: E402 - path may be adjusted above

try:  # Load .env so documented CONFERENCE_* settings apply (repo convention).
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is optional for unit tests
    pass

logger = logging.getLogger("conference_mode")


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Environment-overridable Conference Mode settings (env var -> config default),
# following the same pattern as misty_controller.py / orchestration_service.py so
# the values documented in .env.example actually take effect.
CONFERENCE_MODE_ENABLED = _env_bool(
    "CONFERENCE_MODE_ENABLED", config_defaults.CONFERENCE_MODE_ENABLED
)
CONFERENCE_SCRIPT_PATH = os.getenv(
    "CONFERENCE_SCRIPT_PATH", config_defaults.CONFERENCE_SCRIPT_PATH
)
CONFERENCE_ASSET_DIR = os.getenv(
    "CONFERENCE_ASSET_DIR", config_defaults.CONFERENCE_ASSET_DIR
)
CONFERENCE_MANIFEST_NAME = os.getenv(
    "CONFERENCE_MANIFEST_NAME", config_defaults.CONFERENCE_MANIFEST_NAME
)
CONFERENCE_MISTY_FILENAME_PREFIX = os.getenv(
    "CONFERENCE_MISTY_FILENAME_PREFIX", config_defaults.CONFERENCE_MISTY_FILENAME_PREFIX
)
CONFERENCE_AUTO_ADVANCE = _env_bool(
    "CONFERENCE_AUTO_ADVANCE", config_defaults.CONFERENCE_AUTO_ADVANCE
)
CONFERENCE_PRESENTER_SILENCE_S = _env_float(
    "CONFERENCE_PRESENTER_SILENCE_S", config_defaults.CONFERENCE_PRESENTER_SILENCE_S
)
CONFERENCE_PRESENTER_MAX_WAIT_S = _env_float(
    "CONFERENCE_PRESENTER_MAX_WAIT_S", config_defaults.CONFERENCE_PRESENTER_MAX_WAIT_S
)
CONFERENCE_TTS_FALLBACK = _env_bool(
    "CONFERENCE_TTS_FALLBACK",
    _env_bool("CONFERENCE_LLM_FALLBACK", config_defaults.CONFERENCE_TTS_FALLBACK),
)
CONFERENCE_VARS = os.getenv("CONFERENCE_VARS", config_defaults.CONFERENCE_VARS)
CONFERENCE_PRESENTER_SIDE = os.getenv(
    "CONFERENCE_PRESENTER_SIDE", config_defaults.CONFERENCE_PRESENTER_SIDE
).strip().lower()
CONFERENCE_TALKING_FACE = os.getenv(
    "CONFERENCE_TALKING_FACE", config_defaults.CONFERENCE_TALKING_FACE
)
ORCHESTRATION_URL = os.getenv("ORCHESTRATION_URL", config_defaults.ORCHESTRATION_URL)

MANIFEST_VERSION = 1

# Talk-script markers. The script uses Markdown headers like
# "### **Slide 1: Title Slide**" and speaker lines like "**[You]:** ..." /
# "**[Misty]:** ...". Inline "[cite: 1, 2]" markers are stripped from spoken text.
_SLIDE_RE = re.compile(r"^\s*#{2,6}\s*\*\*\s*Slide\s+(?P<label>.+?)\s*\*\*", re.IGNORECASE)
_SPEAKER_RE = re.compile(r"^\s*\*\*\s*\[(?P<speaker>You|Misty)\]\s*:\s*\*\*\s*(?P<text>.*)$", re.IGNORECASE)
_CITE_RE = re.compile(r"\[\s*cite\s*:[^\]]*\]", re.IGNORECASE)
_VAR_RE = re.compile(r"\{\{\s*(?P<key>[A-Za-z0-9_.-]+)\s*\}\}")
_ANNOTATION_RE = re.compile(r"\[(?P<tag>[A-Za-z+]+)(?::(?P<value>[^\]]+))?\]")
_WS_RE = re.compile(r"\s+")

ARM_MIN, ARM_MAX = -29.0, 90.0
HEAD_PITCH_MIN, HEAD_PITCH_MAX = -40.0, 26.0
HEAD_ROLL_MIN, HEAD_ROLL_MAX = -40.0, 40.0
HEAD_YAW_MIN, HEAD_YAW_MAX = -81.0, 81.0
NEUTRAL_ARMS = [80.0, 80.0]
NEUTRAL_HEAD = [-10.0, 0.0, 0.0]
# Yaw angle for glancing at the presenter (sign depends on CONFERENCE_PRESENTER_SIDE).
PRESENTER_GLANCE_YAW = 40.0

GESTURE_LIBRARY = {
    "wave": {"arms": [-30, 0], "head": [0, 0, 5], "face": "e_Joy.jpg"},
    "shrug": {"arms": [-20, -20], "head": [0, 0, 0]},
    "nod": {"head_motion": "nod"},
    "excited": {"arms": [-40, -40], "face": "e_Joy.jpg"},
    "thinking": {"head": [0, 10, 0], "face": "e_ApprehensionConcerned.jpg"},
    "talking": {"face": "__talking__"},
    "glance": {"head_motion": "glance_presenter"},
}

_FACE_ALIASES = {
    "happy": "e_Joy.jpg",
    "joy": "e_Joy.jpg",
    "excited": "e_Joy.jpg",
    "sarcastic": "e_Disgust.jpg",
    "thinking": "e_ApprehensionConcerned.jpg",
    "thoughtful": "e_ApprehensionConcerned.jpg",
    "neutral": "e_DefaultContent.jpg",
    "talking": "__talking__",
}


class ConferenceError(Exception):
    """Base class for Conference Mode errors."""


class ScriptParseError(ConferenceError):
    """Raised when a talk script cannot be parsed into any cues."""


class ConferencePreparationError(ConferenceError):
    """Raised when a predetermined cue asset cannot be produced."""


class ConferenceAssetMissing(ConferenceError):
    """Raised when a scripted cue has no resolvable predetermined audio."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Cue:
    """A single predetermined Misty line with a stable cue ID.

    ``preceding_presenter`` is the presenter text that immediately precedes this
    Misty line (the natural auto-advance trigger context). It is informational
    and is not spoken by Misty.
    """

    cue_id: str
    slide_seq: int
    slide_label: str
    slide_title: str
    order: int
    text: str
    preceding_presenter: str = ""
    movements: list[dict] = field(default_factory=list)


@dataclass
class ConferenceScript:
    """An ordered parse of a talk script into predetermined Misty cues."""

    source: str
    cues: list[Cue] = field(default_factory=list)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.cues)

    def slide_keys(self) -> list[int]:
        seen: list[int] = []
        for cue in self.cues:
            if cue.slide_seq not in seen:
                seen.append(cue.slide_seq)
        return seen


@dataclass
class CueAsset:
    """A prepared predetermined audio asset for one cue."""

    cue_id: str
    text: str
    asset_source: str  # "generated" or "recorded"
    wav_path: str
    duration_s: float
    text_hash: str
    misty_filename: Optional[str] = None
    slide_seq: int = 0
    slide_label: str = ""
    slide_title: str = ""
    movements: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cue_id": self.cue_id,
            "text": self.text,
            "asset_source": self.asset_source,
            "wav_path": self.wav_path,
            "duration_s": round(self.duration_s, 3),
            "text_hash": self.text_hash,
            "misty_filename": self.misty_filename,
            "slide_seq": self.slide_seq,
            "slide_label": self.slide_label,
            "slide_title": self.slide_title,
            "movements": deepcopy(self.movements),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CueAsset":
        return cls(
            cue_id=data["cue_id"],
            text=data["text"],
            asset_source=data["asset_source"],
            wav_path=data["wav_path"],
            duration_s=float(data.get("duration_s", 0.0)),
            text_hash=data.get("text_hash", ""),
            misty_filename=data.get("misty_filename"),
            slide_seq=int(data.get("slide_seq", 0)),
            slide_label=data.get("slide_label", ""),
            slide_title=data.get("slide_title", ""),
            movements=deepcopy(data.get("movements", [])),
        )


@dataclass
class ConferenceManifest:
    """Maps cue IDs to predetermined audio assets for a prepared talk."""

    script_path: str
    cues: list[CueAsset] = field(default_factory=list)
    generated_at: float = 0.0
    version: int = MANIFEST_VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "script_path": self.script_path,
            "generated_at": self.generated_at,
            "cues": [c.to_dict() for c in self.cues],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConferenceManifest":
        return cls(
            script_path=data.get("script_path", ""),
            cues=[CueAsset.from_dict(c) for c in data.get("cues", [])],
            generated_at=float(data.get("generated_at", 0.0)),
            version=int(data.get("version", MANIFEST_VERSION)),
        )

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "ConferenceManifest":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _clean_text(raw: str) -> str:
    """Strip inline [cite: ...] markers and collapse whitespace."""
    return _WS_RE.sub(" ", _CITE_RE.sub("", raw)).strip()


def parse_conference_vars(raw: str) -> dict[str, str]:
    """Parse CONFERENCE_VARS from 'key=value,key2=value2' into a dict."""
    variables: dict[str, str] = {}
    if not raw.strip():
        return variables
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ScriptParseError(
                "CONFERENCE_VARS entries must use key=value pairs separated by commas"
            )
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ScriptParseError("CONFERENCE_VARS contains an empty variable name")
        variables[key] = value.strip()
    return variables


def resolve_variables(text: str, variables_dict: dict[str, str]) -> str:
    """Resolve {{variable}} placeholders or raise on any missing keys."""
    missing: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        key = match.group("key")
        if key not in variables_dict:
            missing.add(key)
            return match.group(0)
        return variables_dict[key]

    resolved = _VAR_RE.sub(_replace, text)
    if missing:
        raise ScriptParseError(
            "Unresolved conference variables: " + ", ".join(sorted(missing))
        )
    return resolved


def _resolve_face_name(value: str) -> str:
    face = value.strip()
    return _FACE_ALIASES.get(face.lower(), face)


def _parse_floats(raw: str, expected: int, tag: str) -> list[float]:
    parts = [piece.strip() for piece in raw.split(",")]
    if len(parts) != expected or any(not piece for piece in parts):
        raise ScriptParseError(
            f"Annotation [{tag}:{raw}] requires exactly {expected} comma-separated values"
        )
    try:
        return [float(piece) for piece in parts]
    except ValueError as exc:
        raise ScriptParseError(
            f"Annotation [{tag}:{raw}] contains a non-numeric value"
        ) from exc


def _merge_movements(base: dict, overlay: dict) -> dict:
    """Merge two movement dicts — overlay wins for conflicting keys."""
    merged = dict(base)
    merged.update(overlay)
    return merged


def parse_annotations(text: str) -> tuple[str, list[dict]]:
    """Strip inline movement annotations from spoken text and return movements.

    Supports ``+`` chaining: ``[talking+excited]`` merges the gesture dicts so
    you get excited arms *and* the talking face in a single movement entry.
    """
    movements: list[dict] = []
    for match in _ANNOTATION_RE.finditer(text):
        full_tag = match.group("tag").strip().lower()
        value = (match.group("value") or "").strip()

        # Chained gestures: [talking+excited], [wave+nod], etc.
        if "+" in full_tag and not value:
            parts = [p.strip() for p in full_tag.split("+") if p.strip()]
            merged: dict = {}
            for part in parts:
                if part not in GESTURE_LIBRARY:
                    raise ScriptParseError(
                        f"Unknown gesture '{part}' in chained annotation [{full_tag}]"
                    )
                merged = _merge_movements(merged, GESTURE_LIBRARY[part])
            movements.append(deepcopy(merged))
            continue

        tag = full_tag
        if tag in GESTURE_LIBRARY and not value:
            movements.append(deepcopy(GESTURE_LIBRARY[tag]))
            continue
        if tag == "face" and value:
            movements.append({"face": _resolve_face_name(value)})
            continue
        if tag == "arms" and value:
            movements.append({"arms": _parse_floats(value, 2, tag)})
            continue
        if tag == "head" and value:
            movements.append({"head": _parse_floats(value, 3, tag)})
            continue
        raise ScriptParseError(f"Unsupported conference annotation: [{tag}{':' + value if value else ''}]")
    return _WS_RE.sub(" ", _ANNOTATION_RE.sub("", text)).strip(), movements


def _split_slide_label(label: str) -> tuple[str, str]:
    """Split a slide header label like "1: Title Slide" into (label, title)."""
    label = label.strip()
    if ":" in label:
        num, _, title = label.partition(":")
        return num.strip(), title.strip()
    return label, ""


def parse_script(
    source: str,
    *,
    is_text: bool = False,
    variables_dict: Optional[dict[str, str]] = None,
) -> ConferenceScript:
    """Parse a talk script into an ordered list of predetermined Misty cues.

    Parameters
    ----------
    source:
        Path to the talk script, or the raw script text when ``is_text`` is set.
    is_text:
        When True, ``source`` is treated as the raw script text rather than a
        filesystem path (useful for tests and piping).

    Cue IDs are stable and deterministic: ``slide{NN}-misty{MM}`` where ``NN`` is
    the sequential slide number (order of ``### **Slide ...**`` headers, 1-based)
    and ``MM`` is the Misty-line index within that slide (1-based). Misty lines
    that appear before any slide header are assigned slide sequence ``00``.
    """
    if is_text:
        text = source
        origin = "<text>"
    else:
        with open(source, "r", encoding="utf-8") as fh:
            text = fh.read()
        origin = source

    if variables_dict is None:
        variables_dict = parse_conference_vars(CONFERENCE_VARS)

    cues: list[Cue] = []
    slide_seq = 0
    slide_label = ""
    slide_title = ""
    misty_in_slide = 0
    last_presenter = ""
    order = 0

    for line in text.splitlines():
        slide_match = _SLIDE_RE.match(line)
        if slide_match:
            slide_seq += 1
            slide_label, slide_title = _split_slide_label(slide_match.group("label"))
            misty_in_slide = 0
            last_presenter = ""
            continue

        speaker_match = _SPEAKER_RE.match(line)
        if not speaker_match:
            continue

        speaker = speaker_match.group("speaker").lower()
        spoken = _clean_text(speaker_match.group("text"))
        if not spoken:
            continue
        spoken = resolve_variables(spoken, variables_dict)

        if speaker == "you":
            last_presenter = spoken
            continue

        spoken, movements = parse_annotations(spoken)
        if not spoken:
            continue

        # Misty line -> a predetermined cue.
        misty_in_slide += 1
        order += 1
        cue_id = f"slide{slide_seq:02d}-misty{misty_in_slide:02d}"
        cues.append(
            Cue(
                cue_id=cue_id,
                slide_seq=slide_seq,
                slide_label=slide_label,
                slide_title=slide_title,
                order=order,
                text=spoken,
                preceding_presenter=last_presenter,
                movements=movements,
            )
        )

    if not cues:
        raise ScriptParseError(
            f"No Misty cues found in {origin!r}. Expected lines like "
            "'**[Misty]:** ...' under '### **Slide N: Title**' headers."
        )
    return ConferenceScript(source=origin, cues=cues)


# ---------------------------------------------------------------------------
# WAV helpers
# ---------------------------------------------------------------------------


def wav_duration(wav_bytes: bytes) -> float:
    """Return the duration in seconds of a WAV byte string (0.0 on failure)."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / rate if rate > 0 else 0.0
    except Exception:
        return 0.0


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------

TtsFn = Callable[[str], bytes]


def http_tts(orchestration_url: str, timeout: float = 30.0) -> TtsFn:
    """Build a TTS function that calls the orchestration ``/api/tts`` endpoint.

    The endpoint accepts ``{"text": ...}`` and returns raw ``audio/wav`` bytes.
    This is the default live preparation backend; unit tests inject a fake.
    """
    import urllib.request  # noqa: PLC0415 - lazily imported; only used live

    url = orchestration_url.rstrip("/") + "/api/tts"

    def _tts(text: str) -> bytes:
        payload = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    return _tts


def prepare_assets(
    script: ConferenceScript,
    out_dir: str,
    tts_fn: TtsFn,
    *,
    recorded_dir: Optional[str] = None,
    reuse: bool = True,
    misty_prefix: Optional[str] = None,
) -> ConferenceManifest:
    """Generate, import, or reuse a predetermined WAV for every Misty cue.

    For each cue, resolution order is:

    1. **recorded** — if ``recorded_dir`` contains ``{cue_id}.wav``, use it as-is.
    2. **reuse** — if ``reuse`` and a previously generated ``{cue_id}.wav`` exists
       whose sidecar ``{cue_id}.wav.sha256`` matches the cue text hash, keep it.
    3. **generate** — otherwise call ``tts_fn(text)`` and write ``{cue_id}.wav``
       plus its hash sidecar.

    Returns a :class:`ConferenceManifest`; the caller decides where to save it.
    """
    if misty_prefix is None:
        misty_prefix = CONFERENCE_MISTY_FILENAME_PREFIX
    os.makedirs(out_dir, exist_ok=True)
    assets: list[CueAsset] = []

    for cue in script.cues:
        text_hash = _sha256(cue.text)
        wav_name = f"{cue.cue_id}.wav"
        gen_path = os.path.join(out_dir, wav_name)
        hash_path = gen_path + ".sha256"

        recorded_path = None
        if recorded_dir:
            candidate = os.path.join(recorded_dir, wav_name)
            if os.path.isfile(candidate):
                recorded_path = candidate

        if recorded_path is not None:
            asset_source = "recorded"
            final_path = recorded_path
        else:
            need_generate = True
            if reuse and os.path.isfile(gen_path) and os.path.isfile(hash_path):
                try:
                    with open(hash_path, "r", encoding="utf-8") as fh:
                        need_generate = fh.read().strip() != text_hash
                    if not need_generate and _wav_file_duration(gen_path) <= 0:
                        need_generate = True
                except OSError:
                    need_generate = True
            if need_generate:
                wav_bytes = tts_fn(cue.text)
                if not wav_bytes:
                    raise ConferencePreparationError(
                        f"TTS produced no audio for cue {cue.cue_id!r}"
                    )
                with open(gen_path, "wb") as fh:
                    fh.write(wav_bytes)
                with open(hash_path, "w", encoding="utf-8") as fh:
                    fh.write(text_hash)
            asset_source = "generated"
            final_path = gen_path

        try:
            with open(final_path, "rb") as fh:
                duration = wav_duration(fh.read())
        except OSError as exc:
            raise ConferencePreparationError(
                f"Prepared audio for cue {cue.cue_id!r} is unreadable: {exc}"
            ) from exc
        if duration <= 0:
            raise ConferencePreparationError(
                f"Prepared audio for cue {cue.cue_id!r} is not a playable WAV: "
                f"{final_path}"
            )

        assets.append(
            CueAsset(
                cue_id=cue.cue_id,
                text=cue.text,
                asset_source=asset_source,
                wav_path=os.path.abspath(final_path),
                duration_s=duration,
                text_hash=text_hash,
                misty_filename=f"{misty_prefix}{cue.cue_id}.wav",
                slide_seq=cue.slide_seq,
                slide_label=cue.slide_label,
                slide_title=cue.slide_title,
                movements=deepcopy(cue.movements),
            )
        )

    return ConferenceManifest(
        script_path=script.source,
        cues=assets,
        generated_at=time.time(),
    )


def _wav_file_duration(path: str) -> float:
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / rate if rate > 0 else 0.0
    except (EOFError, OSError, wave.Error):
        return 0.0


def verify_manifest(
    manifest: ConferenceManifest, *, allow_audio_fallback: bool = False
) -> list[str]:
    """Return a list of human-readable problems; empty means showtime-ready.

    Duration is recomputed from the current on-disk WAV rather than trusting the
    manifest's stored value, so a cue that became unreadable or empty after the
    manifest was written is still reported. When explicit fallback is enabled,
    missing/unplayable cue audio is allowed because the live runner will route
    that cue through the injected fallback callable instead.
    """
    problems: list[str] = []
    if not manifest.cues:
        problems.append("manifest contains no cues")
    for asset in manifest.cues:
        if not asset.wav_path or not os.path.isfile(asset.wav_path):
            if not allow_audio_fallback:
                problems.append(f"{asset.cue_id}: missing WAV at {asset.wav_path!r}")
            continue
        actual = _wav_file_duration(asset.wav_path)
        if actual <= 0:
            if not allow_audio_fallback:
                problems.append(
                    f"{asset.cue_id}: WAV at {asset.wav_path!r} has non-positive duration"
                )
    return problems


# ---------------------------------------------------------------------------
# Runtime control state machine
# ---------------------------------------------------------------------------


class ConferenceStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass
class ShutdownHooks:
    """Injected safe-shutdown callables. Any may be omitted (defaults to no-op).

    Called in the documented order on :meth:`ConferenceController.shutdown`:
    release audio -> stop recording -> cancel skills -> halt movement -> rest.
    """

    release_audio: Optional[Callable[[], None]] = None
    stop_recording: Optional[Callable[[], None]] = None
    cancel_skills: Optional[Callable[[], None]] = None
    halt_movement: Optional[Callable[[], None]] = None
    rest_state: Optional[Callable[[], None]] = None


# play_fn(cue_asset) -> optional playback duration (seconds)
PlayFn = Callable[[CueAsset], Optional[float]]
# wait_for_presenter_fn() -> True when the presenter finished speaking, else False
WaitFn = Callable[[], bool]
MovementFn = Callable[[list[dict]], None]
NeutralFn = Callable[[], None]
# glance_fn() -> glance at presenter briefly, then return to audience
GlanceFn = Callable[[], None]
# face_fn(filename) -> display a face image on Misty
FaceFn = Callable[[str], None]


class ConferenceController:
    """State machine for stage playback of predetermined Misty cues.

    Hardware/live dependencies are injected so the control logic is fully
    unit-testable without a robot or live services:

    * ``play_fn(cue_asset)`` performs the actual Misty upload/playback.
    * ``wait_for_presenter_fn()`` performs presenter voice-activity detection and
      returns True once the presenter has finished speaking (auto-advance).
    * ``shutdown_hooks`` releases audio/recording/skills/movement on stop.
    * ``tts_fallback_fn(text)`` is used **only** when ``use_tts_fallback`` is set
      and a cue's predetermined audio is missing.

    When ``enabled`` is False (the default), all playback/advance methods are
    no-ops, guaranteeing that normal behavior is untouched when Conference Mode
    is off.
    """

    def __init__(
        self,
        manifest: ConferenceManifest,
        play_fn: PlayFn,
        *,
        wait_for_presenter_fn: Optional[WaitFn] = None,
        shutdown_hooks: Optional[ShutdownHooks] = None,
        enabled: bool = False,
        tts_fallback_fn: Optional[Callable[[str], Optional[float]]] = None,
        use_tts_fallback: bool = False,
        movement_fn: Optional[MovementFn] = None,
        neutral_fn: Optional[NeutralFn] = None,
        glance_fn: Optional[GlanceFn] = None,
        face_fn: Optional[FaceFn] = None,
        talking_face: str = "",
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.manifest = manifest
        self._play_fn = play_fn
        self._wait_fn = wait_for_presenter_fn
        self._hooks = shutdown_hooks or ShutdownHooks()
        self.enabled = enabled
        self._tts_fallback_fn = tts_fallback_fn
        self.use_tts_fallback = use_tts_fallback
        self._movement_fn = movement_fn
        self._neutral_fn = neutral_fn
        self._glance_fn = glance_fn
        self._face_fn = face_fn
        self._talking_face = talking_face or CONFERENCE_TALKING_FACE
        self._sleep_fn = sleep_fn or time.sleep

        self.status = ConferenceStatus.IDLE
        self._cursor = 0  # index of the NEXT cue to play
        self._last = -1  # index of the most recently played cue (-1 = none)
        self._shutdown_done = False
        # Observability counters (used by tests and stage logging).
        self.play_count = 0
        self.tts_fallback_calls = 0

    # -- enable / lifecycle -------------------------------------------------

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False
        if self.status != ConferenceStatus.STOPPED:
            self.status = ConferenceStatus.IDLE

    def start(self) -> bool:
        """Arm the runner. Returns False when Conference Mode is disabled."""
        if not self.enabled:
            logger.info("Conference Mode disabled; start() is a no-op")
            return False
        if self.status == ConferenceStatus.STOPPED:
            return False
        self.status = ConferenceStatus.RUNNING
        return True

    def _active(self) -> bool:
        # Requires an explicit start(): IDLE (never armed) and STOPPED are inert.
        return self.enabled and self.status not in (
            ConferenceStatus.IDLE,
            ConferenceStatus.STOPPED,
        )

    @property
    def total(self) -> int:
        return len(self.manifest.cues)

    def remaining(self) -> int:
        return max(0, self.total - self._cursor)

    def current_cue(self) -> Optional[CueAsset]:
        if 0 <= self._last < self.total:
            return self.manifest.cues[self._last]
        return None

    @property
    def auto_advance_available(self) -> bool:
        return self._wait_fn is not None

    def peek_next(self) -> Optional[CueAsset]:
        if 0 <= self._cursor < self.total:
            return self.manifest.cues[self._cursor]
        return None

    # -- playback -----------------------------------------------------------

    def _has_face_annotation(self, movements: list[dict]) -> bool:
        """Check if any movement in the list sets a face."""
        return any(m.get("face") for m in movements)

    def _play_index(self, index: int) -> Optional[CueAsset]:
        if not (0 <= index < self.total):
            return None
        asset = self.manifest.cues[index]
        resolvable = bool(asset.wav_path) and _wav_file_duration(asset.wav_path) > 0
        if not resolvable:
            if self.use_tts_fallback and self._tts_fallback_fn is not None:
                logger.warning(
                    "Cue %s audio missing; using explicit TTS fallback", asset.cue_id
                )
                self.tts_fallback_calls += 1
                duration = float(self._tts_fallback_fn(asset.text) or 0.0)
            else:
                raise ConferenceAssetMissing(
                    f"Cue {asset.cue_id!r} has no predetermined audio at "
                    f"{asset.wav_path!r} and TTS fallback is disabled"
                )
        else:
            # Scripted playback path: predetermined audio only, never the LLM.
            duration = float(self._play_fn(asset) or 0.0)

        # Auto-apply talking face during speech unless a [face:...] annotation
        # already sets one. The __talking__ sentinel is resolved to the configured
        # talking face GIF at runtime.
        if self._face_fn and not self._has_face_annotation(asset.movements):
            self._face_fn(self._talking_face)

        if self._movement_fn is not None and asset.movements:
            resolved = deepcopy(asset.movements)
            # Resolve __talking__ sentinel to the configured talking face
            for m in resolved:
                if m.get("face") == "__talking__":
                    m["face"] = self._talking_face
            self._movement_fn(resolved)
        self.play_count += 1
        self._last = index
        self._cursor = index + 1
        if duration > 0:
            self._sleep_fn(max(0.0, duration))
        if self._neutral_fn is not None:
            self._neutral_fn()
        return asset

    def play_next(self) -> Optional[CueAsset]:
        """Manual/auto 'play next cue'. Works whenever active (even if paused)."""
        if not self._active():
            return None
        return self._play_index(self._cursor)

    def replay(self) -> Optional[CueAsset]:
        """Replay the most recently played cue."""
        if not self._active() or self._last < 0:
            return None
        return self._play_index(self._last)

    def previous(self) -> Optional[CueAsset]:
        """Go back one cue and play it."""
        if not self._active():
            return None
        target = (self._last - 1) if self._last >= 0 else (self._cursor - 2)
        if target < 0:
            return None
        return self._play_index(target)

    def jump_to_slide(self, slide_key, *, play: bool = False) -> Optional[CueAsset]:
        """Position the cursor at the first cue of a slide.

        ``slide_key`` matches ``slide_seq`` (int) or a case-insensitive substring
        of ``slide_label``. Returns the target cue (played when ``play`` is set).
        """
        if not self._active():
            return None
        key_str = str(slide_key).strip().lower()
        for index, asset in enumerate(self.manifest.cues):
            label = (asset.slide_label or "").lower()
            title = (asset.slide_title or "").lower()
            if (
                str(asset.slide_seq) == key_str
                or (label and key_str in label)
                or (title and key_str in title)
            ):
                self._cursor = index
                self._last = index - 1
                if play:
                    return self._play_index(index)
                return asset
        return None

    # -- pause / resume / auto-advance -------------------------------------

    def pause(self) -> None:
        if self._active():
            self.status = ConferenceStatus.PAUSED

    def resume(self) -> None:
        if self._active():
            self.status = ConferenceStatus.RUNNING

    def auto_advance_once(self) -> Optional[CueAsset]:
        """Listen for the presenter to finish, then play the next cue.

        While waiting for the presenter, Misty glances toward them (if a
        glance_fn is provided) to appear attentive and engaged.

        Respects manual override: returns None (no playback) while paused or
        stopped, or when the presenter-wait times out.
        """
        if not self._active() or self.status == ConferenceStatus.PAUSED:
            return None
        if self._wait_fn is None:
            raise ConferenceError(
                "auto_advance_once requires a wait_for_presenter_fn (VAD) callable"
            )
        # Glance toward presenter while they speak
        if self._glance_fn is not None:
            self._glance_fn()
        finished = self._wait_fn()
        # Manual override may have paused/stopped us while we were listening.
        if not finished or not self._active() or self.status == ConferenceStatus.PAUSED:
            return None
        return self.play_next()

    def run_auto(self, max_cues: Optional[int] = None) -> int:
        """Auto-advance until the script ends, pause, or stop. Returns count."""
        played = 0
        while self._active() and self.status != ConferenceStatus.PAUSED:
            if self.remaining() <= 0:
                break
            if max_cues is not None and played >= max_cues:
                break
            cue = self.auto_advance_once()
            if cue is None:
                break
            played += 1
        return played

    # -- safe shutdown ------------------------------------------------------

    def shutdown(self) -> list[str]:
        """Stop playback and release stage resources. Idempotent.

        Returns the ordered list of hook names invoked. Each hook is guarded so a
        single failing hook cannot prevent the rest of the safe shutdown.
        """
        self.status = ConferenceStatus.STOPPED
        if self._shutdown_done:
            return []
        self._shutdown_done = True

        invoked: list[str] = []
        ordered = [
            ("release_audio", self._hooks.release_audio),
            ("stop_recording", self._hooks.stop_recording),
            ("cancel_skills", self._hooks.cancel_skills),
            ("halt_movement", self._hooks.halt_movement),
            ("rest_state", self._hooks.rest_state),
        ]
        for name, hook in ordered:
            if hook is None:
                continue
            try:
                hook()
                invoked.append(name)
            except Exception as exc:  # stage safety: never abort shutdown
                logger.error("Conference shutdown hook %s failed: %s", name, exc)
        return invoked

    # ``stop`` is an alias for ``shutdown`` for control-surface symmetry.
    def stop(self) -> list[str]:
        return self.shutdown()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_cue_plan(script: ConferenceScript, stream=sys.stdout) -> None:
    print(f"Conference cue plan for {script.source}", file=stream)
    print(f"Total predetermined Misty cues: {len(script.cues)}", file=stream)
    print("-" * 72, file=stream)
    for cue in script.cues:
        title = cue.slide_title or cue.slide_label or "(no slide)"
        label = cue.slide_label or f"{cue.slide_seq:02d}"
        preview = cue.text if len(cue.text) <= 60 else cue.text[:57] + "..."
        print(
            f"[{cue.order:02d}] {cue.cue_id}  (Slide {label}: {title})",
            file=stream,
        )
        print(f"      Misty: {preview}", file=stream)
        if cue.movements:
            print(f"      Movements: {_format_movements(cue.movements)}", file=stream)


def _format_movements(movements: list[dict]) -> str:
    parts: list[str] = []
    for movement in movements:
        if "face" in movement:
            parts.append(f"face={movement['face']}")
        if "arms" in movement:
            left, right = movement["arms"]
            parts.append(f"arms=[{left:g}, {right:g}]")
        if "head" in movement:
            pitch, roll, yaw = movement["head"]
            parts.append(f"head=[{pitch:g}, {roll:g}, {yaw:g}]")
        if "head_motion" in movement:
            parts.append(f"head_motion={movement['head_motion']}")
    return ", ".join(parts)


def _cmd_dry_run(args) -> int:
    script = parse_script(args.script)
    _print_cue_plan(script)
    return 0


def _cmd_prepare(args) -> int:
    script = parse_script(args.script)
    tts_fn = http_tts(args.orchestration_url)
    manifest = prepare_assets(
        script,
        args.out,
        tts_fn,
        recorded_dir=args.recorded,
        reuse=not args.no_reuse,
        misty_prefix=args.misty_prefix,
    )
    manifest_path = os.path.join(args.out, args.manifest_name)
    manifest.save(manifest_path)
    generated = sum(1 for c in manifest.cues if c.asset_source == "generated")
    recorded = sum(1 for c in manifest.cues if c.asset_source == "recorded")
    print(f"Prepared {len(manifest.cues)} cues -> {manifest_path}")
    print(f"  generated: {generated}   recorded: {recorded}")
    problems = verify_manifest(manifest)
    if problems:
        print("Manifest problems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Manifest verified: every cue has playable predetermined audio.")
    return 0


def _cmd_verify(args) -> int:
    manifest = ConferenceManifest.load(args.manifest)
    problems = verify_manifest(
        manifest, allow_audio_fallback=CONFERENCE_TTS_FALLBACK
    )
    if problems:
        print(f"{len(problems)} problem(s) found in {args.manifest}:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"OK: {len(manifest.cues)} cues ready for showtime.")
    return 0


def _build_presenter_wait(listener, max_wait_s, silence_s):  # pragma: no cover - live audio
    """Wrap the wake-word speech monitor into a blocking presenter-wait.

    Returns a callable that starts RMS-based end-of-speech detection on the
    laptop mic, blocks until the presenter finishes speaking, and returns
    False when the stage-safety timeout elapses so auto-advance yields to manual
    control instead of treating a hard cap as end-of-speech.
    """
    import threading

    def _wait() -> bool:
        done = threading.Event()
        result = {"presenter_finished": False}

        def _on_speech_end(*a, **k) -> None:
            result["presenter_finished"] = bool(
                getattr(listener, "speech_detected", False)
            )
            done.set()

        listener.start_speech_monitor(
            on_speech_end=_on_speech_end,
            min_duration=silence_s,
            max_duration=max_wait_s + max(1.0, silence_s),
            silence_duration=silence_s,
        )
        try:
            return done.wait(timeout=max_wait_s) and result["presenter_finished"]
        finally:
            listener.stop_speech_monitor()

    return _wait


def _build_live_controller(args):  # pragma: no cover - requires Misty + services
    """Wire the state machine to a live MistyController for on-stage use."""
    manifest = ConferenceManifest.load(args.manifest)
    problems = verify_manifest(
        manifest, allow_audio_fallback=CONFERENCE_TTS_FALLBACK
    )
    if problems:
        raise ConferenceError(
            "Manifest is not showtime-ready: " + "; ".join(problems)
        )

    import misty_controller as mc  # lazy: heavy, hardware-oriented

    robot = mc.MistyController()

    # Start the laptop wake word listener explicitly — normally done inside
    # MistyController.run(), but conference mode doesn't call run().  Needed for
    # both self-wake suppression during playback and auto-advance VAD.
    if getattr(mc, "USE_LAPTOP_WAKE_WORD", False):
        try:
            robot._start_laptop_wake_word()
            # In conference mode, suppress the normal wake-word-fires-conversation
            # callback. The listener is only used for its speech monitor (VAD) to
            # detect when the presenter finishes speaking for auto-advance.
            if robot._wake_word_listener is not None:
                robot._wake_word_listener.on_wake_word = lambda: None
        except Exception as exc:
            logger.warning("Could not start laptop wake word listener: %s", exc)

    def play_fn(asset: CueAsset):
        # Pause wake word during playback to prevent self-wake from Misty's speaker
        listener = getattr(robot, "_wake_word_listener", None)
        if listener is not None:
            listener.pause()
        try:
            with open(asset.wav_path, "rb") as fh:
                wav_bytes = fh.read()
            filename = asset.misty_filename or os.path.basename(asset.wav_path)
            return float(robot.upload_and_play_audio(wav_bytes, filename) or 0.0)
        except Exception:
            if listener is not None:
                listener.resume()
            raise

    tts_fallback_backend = http_tts(ORCHESTRATION_URL)

    def tts_fallback_fn(text: str):
        # Pause wake word during fallback playback to prevent self-wake
        listener = getattr(robot, "_wake_word_listener", None)
        if listener is not None:
            listener.pause()
        try:
            wav_bytes = tts_fallback_backend(text)
            return float(robot.upload_and_play_audio(wav_bytes, "conference_fallback.wav") or 0.0)
        except Exception:
            if listener is not None:
                listener.resume()
            raise

    def _clamp(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, float(value)))

    # Determine presenter yaw direction based on configured side
    _presenter_yaw = PRESENTER_GLANCE_YAW if CONFERENCE_PRESENTER_SIDE == "left" else -PRESENTER_GLANCE_YAW

    def face_fn(filename: str) -> None:
        if hasattr(robot, "show_face"):
            robot.show_face(filename)
        elif hasattr(robot, "display_image"):
            robot.display_image(filename)

    def glance_fn() -> None:
        """Glance toward the presenter then back to audience (neutral head)."""
        if hasattr(robot, "move_head"):
            robot.move_head(
                pitch=NEUTRAL_HEAD[0],
                roll=0,
                yaw=_clamp(_presenter_yaw, HEAD_YAW_MIN, HEAD_YAW_MAX),
                velocity=30,
            )
        if hasattr(robot, "show_face"):
            robot.show_face("e_DefaultContent.jpg")

    def movement_fn(movements: list[dict]) -> None:
        for movement in movements:
            face = movement.get("face")
            if face:
                if hasattr(robot, "show_face"):
                    robot.show_face(face)
                elif hasattr(robot, "display_image"):
                    robot.display_image(face)

            arms = movement.get("arms")
            if arms and hasattr(robot, "move_arms"):
                robot.move_arms(
                    left=_clamp(arms[0], ARM_MIN, ARM_MAX),
                    right=_clamp(arms[1], ARM_MIN, ARM_MAX),
                    velocity=40,
                )

            head = movement.get("head")
            if head and hasattr(robot, "move_head"):
                robot.move_head(
                    pitch=_clamp(head[0], HEAD_PITCH_MIN, HEAD_PITCH_MAX),
                    roll=_clamp(head[1], HEAD_ROLL_MIN, HEAD_ROLL_MAX),
                    yaw=_clamp(head[2], HEAD_YAW_MIN, HEAD_YAW_MAX),
                    velocity=40,
                )

            if movement.get("head_motion") == "nod" and hasattr(robot, "move_head"):
                robot.move_head(pitch=10, roll=0, yaw=0, velocity=40)
                time.sleep(0.2)
                robot.move_head(pitch=-10, roll=0, yaw=0, velocity=40)
                time.sleep(0.2)
                robot.move_head(pitch=0, roll=0, yaw=0, velocity=40)

            if movement.get("head_motion") == "glance_presenter" and hasattr(robot, "move_head"):
                robot.move_head(
                    pitch=NEUTRAL_HEAD[0],
                    roll=0,
                    yaw=_clamp(_presenter_yaw, HEAD_YAW_MIN, HEAD_YAW_MAX),
                    velocity=30,
                )
                time.sleep(0.5)
                robot.move_head(
                    pitch=NEUTRAL_HEAD[0],
                    roll=NEUTRAL_HEAD[1],
                    yaw=NEUTRAL_HEAD[2],
                    velocity=30,
                )

    def neutral_fn() -> None:
        if hasattr(robot, "move_arms"):
            robot.move_arms(left=NEUTRAL_ARMS[0], right=NEUTRAL_ARMS[1], velocity=40)
        if hasattr(robot, "move_head"):
            robot.move_head(
                pitch=NEUTRAL_HEAD[0],
                roll=NEUTRAL_HEAD[1],
                yaw=NEUTRAL_HEAD[2],
                velocity=40,
            )
        listener = getattr(robot, "_wake_word_listener", None)
        if listener is not None:
            listener.resume()

    def _release_audio():
        # First safe-shutdown step: release the mic/audio stack so keyphrase and
        # recording stop holding the device.
        listener = getattr(robot, "_wake_word_listener", None)
        if listener is not None:
            if hasattr(listener, "stop_speech_monitor"):
                listener.stop_speech_monitor()
            if hasattr(listener, "stop"):
                listener.stop()
            else:
                listener.pause()

    hooks = ShutdownHooks(
        release_audio=_release_audio,
        stop_recording=robot.stop_recording,
        cancel_skills=robot._cancel_all_skills,
        halt_movement=robot.halt,
        rest_state=lambda: robot.move_head(pitch=0, roll=0, yaw=0),
    )

    wait_fn = None
    if getattr(args, "auto", False):
        listener = getattr(robot, "_wake_word_listener", None)
        if listener is None:
            raise ConferenceError(
                "Auto-advance requested but no wake-word listener is available on "
                "the controller (laptop wake-word mode required); run with "
                "--no-auto and use manual controls."
            )
        wait_fn = _build_presenter_wait(
            listener, CONFERENCE_PRESENTER_MAX_WAIT_S, CONFERENCE_PRESENTER_SILENCE_S
        )

    return ConferenceController(
        manifest,
        play_fn,
        wait_for_presenter_fn=wait_fn,
        shutdown_hooks=hooks,
        enabled=CONFERENCE_MODE_ENABLED,
        tts_fallback_fn=tts_fallback_fn,
        use_tts_fallback=CONFERENCE_TTS_FALLBACK,
        movement_fn=movement_fn,
        neutral_fn=neutral_fn,
        glance_fn=glance_fn,
        face_fn=face_fn,
        talking_face=CONFERENCE_TALKING_FACE,
    )


def _cmd_run(args) -> int:  # pragma: no cover - requires Misty + live services
    """Live interactive conference runner (requires Misty hardware).

    This path wires the state machine to a live ``MistyController`` and presenter
    voice-activity detection. It cannot run in the cloud; validate on the target
    hardware during rehearsal.
    """
    if not CONFERENCE_MODE_ENABLED:
        print(
            "Conference Mode is disabled (CONFERENCE_MODE_ENABLED=false). "
            "Set CONFERENCE_MODE_ENABLED=true in your environment or .env to run."
        )
        return 2
    controller = _build_live_controller(args)
    controller.start()
    print(
        "Conference Mode live. Controls: [n]ext [r]eplay [p]revious "
        "[space]pause/resume [a]uto [s]top"
    )
    try:
        while controller.status != ConferenceStatus.STOPPED:
            try:
                raw = input("> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            key = raw.strip().lower()
            # A single space toggles pause/resume (handled before stripping,
            # since a bare space would otherwise strip to "" == next).
            if raw == " " or key in ("pause", "resume"):
                if controller.status == ConferenceStatus.PAUSED:
                    controller.resume()
                else:
                    controller.pause()
            elif key in ("n", "next", ""):
                controller.play_next()
            elif key in ("r", "replay"):
                controller.replay()
            elif key in ("p", "prev", "previous"):
                controller.previous()
            elif key in ("a", "auto"):
                if not controller.auto_advance_available:
                    print("Auto-advance is unavailable; use manual controls.")
                else:
                    controller.run_auto()
            elif key in ("s", "stop", "q", "quit"):
                break
    finally:
        controller.shutdown()
    print("Conference Mode stopped; Misty returned to rest state.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conference_mode",
        description="Scripted Misty stage dialog (Conference Mode, issue #128).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    default_script = CONFERENCE_SCRIPT_PATH
    default_out = CONFERENCE_ASSET_DIR
    default_manifest = CONFERENCE_MANIFEST_NAME
    default_prefix = CONFERENCE_MISTY_FILENAME_PREFIX

    p_dry = sub.add_parser("dry-run", help="Print the ordered cue plan (no hardware).")
    p_dry.add_argument("--script", default=default_script)
    p_dry.set_defaults(func=_cmd_dry_run)

    p_prep = sub.add_parser("prepare", help="Generate/import/reuse cue audio + manifest.")
    p_prep.add_argument("--script", default=default_script)
    p_prep.add_argument("--out", default=default_out)
    p_prep.add_argument("--manifest-name", default=default_manifest)
    p_prep.add_argument("--recorded", default=None,
                        help="Directory of pre-recorded {cue_id}.wav overrides.")
    p_prep.add_argument("--orchestration-url",
                        default=ORCHESTRATION_URL)
    p_prep.add_argument("--misty-prefix", default=default_prefix)
    p_prep.add_argument("--no-reuse", action="store_true",
                        help="Regenerate every cue even if a cached WAV exists.")
    p_prep.set_defaults(func=_cmd_prepare)

    p_ver = sub.add_parser("verify", help="Check a manifest is showtime-ready.")
    p_ver.add_argument("--manifest",
                       default=os.path.join(default_out, default_manifest))
    p_ver.set_defaults(func=_cmd_verify)

    p_run = sub.add_parser("run", help="Live interactive runner (requires Misty).")
    p_run.add_argument("--manifest",
                       default=os.path.join(default_out, default_manifest))
    p_run.add_argument("--auto", action=argparse.BooleanOptionalAction,
                       default=CONFERENCE_AUTO_ADVANCE,
                       help="Enable silence-triggered auto-advance (default from "
                            "CONFERENCE_AUTO_ADVANCE); use --no-auto for manual only.")
    p_run.set_defaults(func=_cmd_run)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

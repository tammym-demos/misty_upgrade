"""Evaluate the bundled wake-word model against labeled local WAV files."""

from __future__ import annotations

import argparse
import json
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = REPO_ROOT / "models" / "hey_misty.onnx"
SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280


@dataclass(frozen=True)
class Evaluation:
    threshold: float
    trigger_frames: int
    true_positives: int
    false_negatives: int
    false_positives: int
    true_negatives: int

    def as_dict(self) -> dict:
        positive_total = self.true_positives + self.false_negatives
        negative_total = self.false_positives + self.true_negatives
        return {
            "threshold": self.threshold,
            "trigger_frames": self.trigger_frames,
            "true_positives": self.true_positives,
            "false_negatives": self.false_negatives,
            "false_positives": self.false_positives,
            "true_negatives": self.true_negatives,
            "recall": (
                round(self.true_positives / positive_total, 4)
                if positive_total
                else None
            ),
            "false_positive_rate": (
                round(self.false_positives / negative_total, 4)
                if negative_total
                else None
            ),
        }


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit mono WAV")
        if wav.getframerate() != SAMPLE_RATE:
            raise ValueError(f"{path}: expected {SAMPLE_RATE} Hz WAV")
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)


def score_file(model, path: Path, model_name: str) -> list[tuple[float, int]]:
    audio = load_wav(path)
    model.reset()
    scores: list[tuple[float, int]] = []
    for offset in range(0, len(audio), FRAME_SAMPLES):
        frame = audio[offset : offset + FRAME_SAMPLES]
        if len(frame) < FRAME_SAMPLES:
            frame = np.pad(frame, (0, FRAME_SAMPLES - len(frame)))
        predictions = model.predict(frame)
        score = float(predictions.get(model_name, max(predictions.values(), default=0.0)))
        rms = int(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
        scores.append((score, rms))
    return scores


def accepts(
    scores: list[tuple[float, int]],
    threshold: float,
    trigger_frames: int,
    min_rms: int,
) -> bool:
    streak = 0
    recent_rms: list[int] = []
    for score, rms in scores:
        recent_rms.append(rms)
        recent_rms = recent_rms[-10:]
        streak = streak + 1 if score >= threshold else 0
        if streak >= trigger_frames:
            if max(recent_rms, default=0) >= min_rms:
                return True
            streak = 0
    return False


def wav_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise ValueError(f"Directory does not exist: {directory}")
    return sorted(directory.glob("*.wav"))


def parse_csv(raw: str, cast):
    return [cast(value.strip()) for value in raw.split(",") if value.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure wake recall and false activations on local WAV corpora."
    )
    parser.add_argument("--positive-dir", type=Path, required=True)
    parser.add_argument("--negative-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--model-name", default="hey_misty")
    parser.add_argument("--thresholds", default="0.80,0.85,0.90,0.95")
    parser.add_argument("--trigger-frames", default="2,3")
    parser.add_argument("--min-rms", type=int, default=100)
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.model.is_file():
        parser.error(f"Model does not exist: {args.model}")
    if not 0.0 <= args.vad_threshold <= 1.0:
        parser.error("--vad-threshold must be between 0 and 1")

    try:
        positives = wav_files(args.positive_dir)
        negatives = wav_files(args.negative_dir)
    except ValueError as exc:
        parser.error(str(exc))
    if not positives or not negatives:
        parser.error("Both corpora must contain at least one .wav file")

    from openwakeword.model import Model

    model = Model(
        wakeword_models=[str(args.model)],
        vad_threshold=args.vad_threshold,
        inference_framework="onnx",
    )
    scored_positives = [
        score_file(model, path, args.model_name) for path in positives
    ]
    scored_negatives = [
        score_file(model, path, args.model_name) for path in negatives
    ]

    results = []
    for threshold in parse_csv(args.thresholds, float):
        for trigger_frames in parse_csv(args.trigger_frames, int):
            positive_hits = sum(
                accepts(scores, threshold, trigger_frames, args.min_rms)
                for scores in scored_positives
            )
            negative_hits = sum(
                accepts(scores, threshold, trigger_frames, args.min_rms)
                for scores in scored_negatives
            )
            results.append(
                Evaluation(
                    threshold=threshold,
                    trigger_frames=trigger_frames,
                    true_positives=positive_hits,
                    false_negatives=len(positives) - positive_hits,
                    false_positives=negative_hits,
                    true_negatives=len(negatives) - negative_hits,
                ).as_dict()
            )

    payload = {
        "model": str(args.model),
        "positive_files": len(positives),
        "negative_files": len(negatives),
        "min_rms": args.min_rms,
        "vad_threshold": args.vad_threshold,
        "results": results,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            "threshold frames recall false_positive_rate false_positives "
            "false_negatives"
        )
        for result in results:
            print(
                f"{result['threshold']:.2f}      "
                f"{result['trigger_frames']:>2}     "
                f"{result['recall']!s:>6} "
                f"{result['false_positive_rate']!s:>19} "
                f"{result['false_positives']:>15} "
                f"{result['false_negatives']:>15}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

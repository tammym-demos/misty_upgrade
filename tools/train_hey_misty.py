"""
Train a custom openWakeWord model for the "Hey Misty" wake phrase.

Uses Kokoro TTS to generate diverse synthetic training data, then trains
an openWakeWord-compatible DNN and exports it as ONNX.

Prerequisites:
    pip install torch openwakeword kokoro-onnx scipy numpy tqdm

Usage:
    python tools/train_hey_misty.py

Output:
    models/hey_misty.onnx — ready for OWW_CUSTOM_MODEL_PATH
"""

import time
import copy
import logging
import hashlib
from pathlib import Path

import numpy as np
import scipy.io.wavfile
import scipy.signal
import torch
from torch import nn, optim
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ============================================================================
# PATHS
# ============================================================================
REPO_ROOT = Path(__file__).resolve().parent.parent
KOKORO_MODEL = REPO_ROOT / "src" / "windows-orchestration" / "kokoro-v1.0.int8.onnx"
KOKORO_VOICES = REPO_ROOT / "src" / "windows-orchestration" / "voices-v1.0.bin"
OUTPUT_MODEL = REPO_ROOT / "models" / "hey_misty.onnx"
WORK_DIR = REPO_ROOT / "models" / "_training_work"

# ============================================================================
# TTS CONFIGURATION
# ============================================================================
# English voices from Kokoro — diverse gender/accent for robust training
VOICES = [
    # American Female
    "af_alloy", "af_bella", "af_heart", "af_jessica", "af_nova",
    "af_river", "af_sarah", "af_sky",
    # American Male
    "am_adam", "am_echo", "am_eric", "am_liam", "am_michael", "am_onyx", "am_puck",
    # British Female
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    # British Male
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]

# Speed variations (simulate fast/slow speakers)
SPEEDS = [0.8, 0.9, 1.0, 1.1, 1.2]

# Positive phrases (the wake word in natural variations)
POSITIVE_PHRASES = [
    "Hey Misty",
    "Hey Misty!",
    "hey misty",
]

# Negative phrases (similar-sounding, partial, adversarial)
NEGATIVE_PHRASES = [
    "Hey Mister",
    "Hey Missy",
    "Hey Mickey",
    "Hey Matey",
    "Hey Marty",
    "Hey mystery",
    "Hey history",
    "Misty",  # no "hey"
    "Hey",    # no "misty"
    "Hey Siri",
    "Hey Alexa",
    "Hey Google",
    "Hey Jarvis",
    "OK Google",
    "Hello there",
    "Good morning",
    "What's up",
    "How are you",
    "Nice to meet you",
    "The weather is nice today",
    "Can you help me",
    "Tell me a story",
    "Play some music",
    "Turn off the lights",
    "Set a timer",
    "Remind me later",
    "What time is it",
    "Thank you very much",
    "I need some help",
]

SAMPLE_RATE = 16000  # openWakeWord native rate
CLIP_DURATION_S = 2.0  # target clip duration in seconds
CLIP_SAMPLES = int(CLIP_DURATION_S * SAMPLE_RATE)

# Training hyperparameters
TRAINING_STEPS = 15000
LAYER_DIM = 128
N_BLOCKS = 1
BATCH_SIZE = 256
LEARNING_RATE = 0.001


def generate_clips_kokoro(phrases: list[str], output_dir: Path, label: str) -> int:
    """Generate TTS clips for all phrase×voice×speed combinations."""
    import kokoro_onnx

    if not KOKORO_MODEL.exists() or not KOKORO_VOICES.exists():
        raise FileNotFoundError(
            "Kokoro model files not found. Download them from:\n"
            "  https://github.com/thewh1teagle/kokoro-onnx/releases\n"
            f"Expected:\n  {KOKORO_MODEL}\n  {KOKORO_VOICES}"
        )

    kokoro = kokoro_onnx.Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
    output_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    total = len(phrases) * len(VOICES) * len(SPEEDS)
    logger.info(f"Generating {total} {label} clips...")

    for phrase in phrases:
        for voice in VOICES:
            for speed in SPEEDS:
                fname = f"{label}_{hashlib.md5(f'{phrase}_{voice}_{speed}'.encode()).hexdigest()[:12]}.wav"
                fpath = output_dir / fname
                if fpath.exists():
                    count += 1
                    continue
                try:
                    samples, sr = kokoro.create(phrase, voice=voice, speed=speed)
                    # Resample to 16kHz if needed
                    if sr != SAMPLE_RATE:
                        n_samples = int(len(samples) * SAMPLE_RATE / sr)
                        samples = scipy.signal.resample(samples, n_samples)

                    # Convert to int16
                    audio_int16 = (samples * 32767).astype(np.int16)
                    scipy.io.wavfile.write(str(fpath), SAMPLE_RATE, audio_int16)
                    count += 1
                except Exception as e:
                    logger.warning(f"Failed to generate {fname}: {e}")

    logger.info(f"Generated {count}/{total} {label} clips in {output_dir}")
    return count


def augment_clip(audio: np.ndarray, sr: int = SAMPLE_RATE) -> list[np.ndarray]:
    """Apply simple augmentations to create training diversity."""
    augmented = []
    rng = np.random.default_rng()

    # 1. Add white noise at various SNR levels
    for snr_db in [30, 20, 15, 10]:
        noise = rng.normal(0, 1, len(audio))
        signal_power = np.mean(audio.astype(np.float64) ** 2)
        noise_power = signal_power / (10 ** (snr_db / 10))
        noisy = audio + (noise * np.sqrt(noise_power)).astype(np.int16)
        augmented.append(np.clip(noisy, -32768, 32767).astype(np.int16))

    # 2. Pitch shift (simple resampling-based)
    for shift_factor in [0.95, 1.05]:
        n = int(len(audio) * shift_factor)
        shifted = scipy.signal.resample(audio.astype(np.float64), n)
        augmented.append(np.clip(shifted, -32768, 32767).astype(np.int16))

    # 3. Volume variation
    for gain in [0.5, 0.7, 1.3]:
        gained = (audio.astype(np.float64) * gain)
        augmented.append(np.clip(gained, -32768, 32767).astype(np.int16))

    return augmented


def pad_or_trim(audio: np.ndarray, target_len: int) -> np.ndarray:
    """Pad with silence or trim audio to target length."""
    if len(audio) >= target_len:
        # Random crop
        start = np.random.randint(0, len(audio) - target_len + 1) if len(audio) > target_len else 0
        return audio[start:start + target_len]
    else:
        # Random pad (place audio at random position within target)
        pad_total = target_len - len(audio)
        pad_left = np.random.randint(0, pad_total + 1)
        return np.pad(audio, (pad_left, pad_total - pad_left), mode='constant')


def load_and_prepare_clips(clip_dir: Path, target_len: int) -> list[np.ndarray]:
    """Load all WAV files, augment, pad/trim to uniform length."""
    clips = []
    wav_files = list(clip_dir.glob("*.wav"))
    logger.info(f"Loading {len(wav_files)} clips from {clip_dir}...")

    for wav_path in wav_files:
        try:
            sr, audio = scipy.io.wavfile.read(str(wav_path))
            if audio.dtype != np.int16:
                audio = (audio * 32767).astype(np.int16) if audio.dtype == np.float32 else audio.astype(np.int16)

            # Resample if not 16kHz
            if sr != SAMPLE_RATE:
                n_samples = int(len(audio) * SAMPLE_RATE / sr)
                audio = scipy.signal.resample(audio, n_samples).astype(np.int16)

            # Original clip
            clips.append(pad_or_trim(audio, target_len))

            # Augmented versions
            for aug in augment_clip(audio):
                clips.append(pad_or_trim(aug, target_len))

        except Exception as e:
            logger.warning(f"Failed to load {wav_path.name}: {e}")

    logger.info(f"Prepared {len(clips)} clips (with augmentation)")
    return clips


def generate_silence_clips(n: int, target_len: int) -> list[np.ndarray]:
    """Generate silence/ambient noise clips as additional negatives."""
    clips = []
    rng = np.random.default_rng()
    for _ in range(n):
        # Low-level noise (simulating quiet room)
        noise_level = rng.uniform(10, 100)
        clip = (rng.normal(0, noise_level, target_len)).astype(np.int16)
        clips.append(clip)
    return clips


def compute_features(clips: list[np.ndarray], chunk_size: int = 500) -> np.ndarray:
    """Compute openWakeWord features for a list of clips, processing in chunks to limit RAM use."""
    from openwakeword.utils import AudioFeatures

    F = AudioFeatures(device='cpu')
    all_features = []

    logger.info(f"Computing features for {len(clips)} clips in chunks of {chunk_size}...")
    for start in tqdm(range(0, len(clips), chunk_size), desc="Feature extraction"):
        chunk = clips[start:start + chunk_size]
        batch = np.vstack([c[np.newaxis, :] for c in chunk])
        features = F.embed_clips(batch, batch_size=32)
        all_features.append(features)

    result = np.concatenate(all_features, axis=0)
    logger.info(f"Features shape: {result.shape}")
    return result


class WakeWordDNN(nn.Module):
    """Simple DNN matching openWakeWord's architecture."""

    def __init__(self, input_shape=(16, 96), layer_dim=128, n_blocks=1):
        super().__init__()
        self.input_shape = input_shape
        flat_dim = input_shape[0] * input_shape[1]

        layers = [
            nn.Flatten(),
            nn.Linear(flat_dim, layer_dim),
            nn.LayerNorm(layer_dim),
            nn.ReLU(),
        ]
        for _ in range(n_blocks):
            layers.extend([
                nn.Linear(layer_dim, layer_dim),
                nn.LayerNorm(layer_dim),
                nn.ReLU(),
            ])
        layers.extend([
            nn.Linear(layer_dim, 1),
            nn.Sigmoid(),
        ])
        self.model = nn.Sequential(*layers)

    def forward(self, x):
        return self.model(x)


def train_model(
    positive_features: np.ndarray,
    negative_features: np.ndarray,
    input_shape: tuple,
    steps: int = TRAINING_STEPS,
    lr: float = LEARNING_RATE,
) -> nn.Module:
    """Train the wake word DNN."""
    logger.info(f"Training with {len(positive_features)} positive, {len(negative_features)} negative samples")

    model = WakeWordDNN(input_shape=input_shape, layer_dim=LAYER_DIM, n_blocks=N_BLOCKS)
    optimizer = optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.BCELoss(reduction='none')

    # Prepare windowed features (sliding window of `input_shape[0]` frames)
    n_frames = input_shape[0]

    def extract_windows(features, label):
        """Extract sliding windows from feature sequences."""
        windows = []
        labels = []
        for feat in features:
            if feat.shape[0] >= n_frames:
                for i in range(0, feat.shape[0] - n_frames + 1, 2):  # stride of 2
                    windows.append(feat[i:i + n_frames, :])
                    labels.append(label)
        return windows, labels

    pos_windows, pos_labels = extract_windows(positive_features, 1.0)
    neg_windows, neg_labels = extract_windows(negative_features, 0.0)

    logger.info(f"Windowed: {len(pos_windows)} positive, {len(neg_windows)} negative windows")

    all_windows = np.array(pos_windows + neg_windows, dtype=np.float32)
    all_labels = np.array(pos_labels + neg_labels, dtype=np.float32)

    # Convert to tensors
    X = torch.from_numpy(all_windows)
    Y = torch.from_numpy(all_labels)

    # Split: 90% train, 10% val
    n_total = len(X)
    indices = np.random.permutation(n_total)
    n_val = max(int(n_total * 0.1), 1)
    val_idx = indices[:n_val]
    train_idx = indices[n_val:]

    X_train, Y_train = X[train_idx], Y[train_idx]
    X_val, Y_val = X[val_idx], Y[val_idx]

    logger.info(f"Train: {len(X_train)}, Val: {len(X_val)}")

    best_model = None
    best_val_loss = float('inf')
    best_val_acc = 0.0

    for step in tqdm(range(steps), desc="Training"):
        model.train()

        # Random batch
        batch_idx = np.random.choice(len(X_train), min(BATCH_SIZE, len(X_train)), replace=False)
        x_batch = X_train[batch_idx]
        y_batch = Y_train[batch_idx]

        # Compute class weights (upweight minority class in loss)
        n_pos = (y_batch == 1).sum().item()
        n_neg = (y_batch == 0).sum().item()
        weights = torch.ones_like(y_batch)
        if n_pos > 0 and n_neg > 0:
            pos_weight = max(n_neg / n_pos, 1.0)
            weights[y_batch == 1] = pos_weight

        optimizer.zero_grad()
        preds = model(x_batch).squeeze()
        loss = (loss_fn(preds, y_batch) * weights).mean()
        loss.backward()
        optimizer.step()

        # Validation every 500 steps
        if (step + 1) % 500 == 0:
            model.eval()
            with torch.no_grad():
                val_preds = model(X_val).squeeze()
                val_loss = loss_fn(val_preds, Y_val).mean().item()
                val_acc = ((val_preds > 0.5) == (Y_val > 0.5)).float().mean().item()
                val_recall = ((val_preds > 0.5) & (Y_val > 0.5)).sum().item() / max((Y_val > 0.5).sum().item(), 1)
                val_fp = ((val_preds > 0.5) & (Y_val < 0.5)).sum().item()
                val_tn = (Y_val < 0.5).sum().item()
                val_fpr = val_fp / max(val_tn, 1)

            logger.info(
                f"Step {step+1}: loss={loss.item():.4f} val_loss={val_loss:.4f} "
                f"val_acc={val_acc:.3f} recall={val_recall:.3f} FPR={val_fpr:.4f}"
            )

            if val_acc > best_val_acc or (val_acc == best_val_acc and val_loss < best_val_loss):
                best_val_loss = val_loss
                best_val_acc = val_acc
                best_model = copy.deepcopy(model.state_dict())

    # Load best model
    if best_model is not None:
        model.load_state_dict(best_model)
        logger.info(f"Loaded best model (val_acc={best_val_acc:.3f}, val_loss={best_val_loss:.4f})")

    return model


def export_onnx(model: nn.Module, input_shape: tuple, output_path: Path):
    """Export model to ONNX format compatible with openWakeWord."""
    model.eval()
    dummy_input = torch.rand(1, *input_shape)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    torch.onnx.export(
        model.model,  # export the inner Sequential, matching openWakeWord convention
        dummy_input,
        str(output_path),
        opset_version=13,
        input_names=["input"],
        output_names=["hey_misty"],
        dynamic_axes={"input": {0: "batch_size"}},
    )

    # Ensure weights are embedded inline (not in a separate .data file).
    # Newer PyTorch versions may use external data format by default, which
    # splits the model into .onnx + .onnx.data files. Our model is tiny
    # (~14KB) so we always inline it for simpler distribution.
    import onnx
    from onnx.external_data_helper import convert_model_to_external_data
    onnx_model = onnx.load(str(output_path), load_external_data=True)
    onnx.save_model(onnx_model, str(output_path), save_as_external_data=False)
    # Clean up any stray .data file from the initial export
    data_file = output_path.with_suffix(".onnx.data")
    if data_file.exists():
        data_file.unlink()

    logger.info(f"Exported ONNX model to {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")


def verify_model(model_path: Path):
    """Quick verification that the model loads in openWakeWord."""
    from openwakeword.model import Model as OWWModel

    logger.info(f"Verifying model loads in openWakeWord...")
    oww = OWWModel(
        wakeword_models=[str(model_path)],
        inference_framework="onnx",
    )
    logger.info(f"Model loaded successfully. Models: {list(oww.models.keys())}")

    # Test with random audio
    test_audio = np.random.randint(-100, 100, SAMPLE_RATE * 2, dtype=np.int16)
    oww.predict(test_audio)
    scores = {name: oww.prediction_buffer[name][-1] for name in oww.models}
    logger.info(f"Test prediction on noise: {scores} (should be near 0)")

    return True


def main():
    start_time = time.time()
    logger.info("=" * 60)
    logger.info("Training 'Hey Misty' openWakeWord model")
    logger.info("=" * 60)

    # Create work directories
    pos_dir = WORK_DIR / "positive"
    neg_dir = WORK_DIR / "negative"
    pos_dir.mkdir(parents=True, exist_ok=True)
    neg_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Generate synthetic clips
    logger.info("\n--- Step 1: Generating synthetic TTS clips ---")
    n_pos = generate_clips_kokoro(POSITIVE_PHRASES, pos_dir, "pos")
    n_neg = generate_clips_kokoro(NEGATIVE_PHRASES, neg_dir, "neg")
    logger.info(f"Total: {n_pos} positive, {n_neg} negative raw clips")

    # Step 2: Load, augment, and prepare clips
    logger.info("\n--- Step 2: Loading and augmenting clips ---")
    pos_clips = load_and_prepare_clips(pos_dir, CLIP_SAMPLES)
    neg_clips = load_and_prepare_clips(neg_dir, CLIP_SAMPLES)

    # Add silence/noise as extra negatives
    silence_clips = generate_silence_clips(200, CLIP_SAMPLES)
    neg_clips.extend(silence_clips)
    logger.info(f"Total prepared: {len(pos_clips)} positive, {len(neg_clips)} negative")

    # Step 3: Compute openWakeWord features
    logger.info("\n--- Step 3: Computing openWakeWord features ---")
    pos_features = compute_features(pos_clips)
    neg_features = compute_features(neg_clips)

    # Step 4: Train
    logger.info("\n--- Step 4: Training model ---")
    from openwakeword.utils import AudioFeatures
    F = AudioFeatures(device='cpu')
    input_shape = F.get_embedding_shape(CLIP_DURATION_S)
    logger.info(f"Model input shape: {input_shape}")

    model = train_model(pos_features, neg_features, input_shape)

    # Step 5: Export to ONNX
    logger.info("\n--- Step 5: Exporting ONNX model ---")
    export_onnx(model, input_shape, OUTPUT_MODEL)

    # Step 6: Verify
    logger.info("\n--- Step 6: Verifying model ---")
    verify_model(OUTPUT_MODEL)

    elapsed = time.time() - start_time
    logger.info(f"\n{'=' * 60}")
    logger.info(f"Training complete in {elapsed:.0f}s")
    logger.info(f"Model: {OUTPUT_MODEL}")
    logger.info(f"Configure: OWW_CUSTOM_MODEL_PATH={OUTPUT_MODEL}")
    logger.info(f"{'=' * 60}")


if __name__ == "__main__":
    main()

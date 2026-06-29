# Wake Word Model: Hey Misty

## Overview

The `hey_misty.onnx` model is a custom-trained openWakeWord model that detects the
phrase "Hey Misty" spoken into the companion laptop's microphone. It is used by the
`WakeWordListener` in the Misty controller for wake word detection.

## Model Details

| Property | Value |
|----------|-------|
| Framework | openWakeWord (ONNX runtime) |
| Architecture | DNN (128-dim, 1 block) |
| Input shape | (1, 16, 96) — 16 frames × 96-dim audio embeddings |
| Output | Single sigmoid score (0.0–1.0) |
| Size | ~14 KB |
| Training data | Synthetic TTS (Kokoro) — 23 voices × multiple speeds |
| Positive samples | 3,450 (345 raw × 10 augmentations) |
| Negative samples | 33,550 (3,335 raw × 10 augmentations + 200 silence) |
| Validation accuracy | 99.7% |
| Recall | 97% |
| False positive rate | 0.3% |

## Configuration

Set the following in `src/windows-orchestration/.env`:

```env
OWW_CUSTOM_MODEL_PATH=C:\path\to\misty_upgrade\models\hey_misty.onnx
OWW_MODEL_NAME=hey_misty
OWW_THRESHOLD=0.7
```

Or use environment variables directly when starting the controller.

## Threshold Tuning

- **0.7** (default): Balanced — good detection with low false positives.
- **0.6**: More sensitive — catches quieter/further speech but may fire on "Hey Missy" etc.
- **0.8**: More selective — fewer false positives but may miss soft utterances.

Adjust based on your environment's background noise level.

## Retraining / Refreshing the Model

To retrain (e.g., after adjusting phrases or adding voices):

```powershell
# Requires: torch, openwakeword, kokoro-onnx, scipy, numpy, tqdm
# First time install:
#   pip install torch --index-url https://download.pytorch.org/whl/cpu
#   pip install openwakeword torchinfo torchmetrics onnxscript tqdm

$env:PYTHONIOENCODING = "utf-8"
python tools/train_hey_misty.py
```

The script:
1. Generates synthetic clips using Kokoro TTS (reuses cached clips if present)
2. Augments with noise, pitch shift, and volume variation
3. Computes openWakeWord audio embeddings
4. Trains a DNN classifier
5. Exports to `models/hey_misty.onnx`
6. Verifies the model loads in openWakeWord

**Training time**: ~20 minutes (first run; ~5 minutes on subsequent runs with cached clips).

To regenerate clips from scratch, delete `models/_training_work/` before running.

## Training Customization

Edit `tools/train_hey_misty.py` to:

- **Add voices**: Extend the `VOICES` list (Kokoro has ~54 voices; the script defaults to a 23-voice subset)
- **Change speeds**: Adjust `SPEEDS` for fast/slow speaker simulation
- **Add negative phrases**: Add similar-sounding phrases to `NEGATIVE_PHRASES`
- **Increase training steps**: Change `TRAINING_STEPS` (default: 15000)
- **Adjust architecture**: Change `LAYER_DIM` or `N_BLOCKS`

## Dependencies (Training Only)

These are only needed for training, not for runtime:

- `torch` (CPU-only, ~200 MB)
- `torchinfo`
- `torchmetrics`
- `onnxscript`
- `kokoro-onnx` (already installed for TTS)

Runtime only requires `openwakeword` and `onnxruntime`.

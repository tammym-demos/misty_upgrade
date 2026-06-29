# Copilot Instructions — Misty Upgrade

Keep this file short. Load repo-local skills for detailed context instead of carrying full runtime history in every session.

## When to load skills

- Use `misty-runtime` for Misty robot runtime work, Windows orchestration, Foundry Local, wake word/audio, REST/WebSocket control, hardware safety, or live troubleshooting.
- Use `feature-planning` for future-feature planning or GitHub issue drafting without immediate implementation.
- Use `issue-implementation-loop` only for autonomous GitHub issue loops that open PRs, request Copilot review, merge, tag, verify issue closure, and clean up.
- Use `log-updates` for repository daily logs, request history, AI/Copilot usage, or AIC/AI Credits summaries.

## Essential architecture

- This is a two-device system: Misty II provides physical I/O; the Windows companion laptop runs all AI work in `src\windows-orchestration`.
- Main runtime files:
  - `orchestration_service.py`: Flask STT -> LLM -> TTS pipeline.
  - `misty_controller.py`: Misty REST/WebSocket controller, laptop wake word, recording, playback, LED/display/movement.
  - `wake_word_listener.py`: laptop microphone OpenWakeWord + recording/VAD.
- Misty cannot run inference. Use Foundry Local on the companion device for chat, faster-whisper in-process for STT, and Kokoro-ONNX/pyttsx3 for TTS.
- Foundry Local uses a dynamic port. Discover it via `foundry service status`; never hardcode the port.

## Hard rules

- Never push directly to `main`; use a feature branch and PR.
- Do not use sensory-only reboot. Only full Core+Sensory reboot is safe: `{"Core": true, "SensoryServices": true}`.
- Misty built-in keyphrase is unsupported. Supported wake path is laptop-side OpenWakeWord with a custom `OWW_CUSTOM_MODEL_PATH` for "Hey Misty".
- Keep Misty keyphrase stop calls only for legacy cleanup/audio-resource release, not as a supported detection path.
- Before ending live robot work, release resources: stop legacy keyphrase, stop recording, cancel skills, LED off.
- Preserve user changes in the worktree. Do not reset, checkout, or delete unmerged work unless explicitly requested.

## Build, run, and test

Manual service order:

```powershell
foundry
cd src\windows-orchestration
python -m pip install -r requirements.txt
python orchestration_service.py
python misty_controller.py
```

Useful checks:

```powershell
curl http://localhost:5000/api/health
python -m pytest tests\test_integration.py -m "not live" -q
python -m pytest tests\test_integration.py::TestWakeWordConfiguration -q
python -m py_compile src\windows-orchestration\misty_controller.py
```

Live tests require services/hardware and are marked `live` in `pytest.ini`.

## Configuration conventions

- Canonical defaults live in `src\windows-orchestration\config_defaults.py`.
- When adding or changing defaults, update `config_defaults.py`, `.env.example`, and directly related docs/tests.
- Key env vars: `MISTY_IP`, `ORCHESTRATION_URL`, `FOUNDRY_LOCAL_HOST`, `OWW_CUSTOM_MODEL_PATH`, `OWW_MODEL_NAME`, `OWW_THRESHOLD`, `LAPTOP_MISTY_RECORDING_MODE`.

## Runtime conventions

- Model stack: chat `Phi-3.5-mini-instruct-openvino-gpu:2`, STT `openai-whisper-tiny-generic-cpu:3`, TTS Kokoro-ONNX primary with pyttsx3 fallback.
- Keep only `phi-3.5-mini` loaded in Foundry during normal operation; unload stray models such as `phi-4-mini`.
- Latency logs use `[Pipeline Xms] STT=X LLM=X TTS=X history=N fallback=F cached=C`.
- LED scheme: green ready/recording cue, orange wake/prep, blue processing, purple playback, cyan follow-up listening, yellow warning/recovery, off charging, red error.

## Documentation pointers

- Architecture: `README.md`, `docs\ADR-001-companion-device-over-onrobot-inference.md`.
- Audio pattern: `docs\ADR-002-non-blocking-audio-pattern.md`.
- Firmware/keyphrase history: `docs\keyphrase-debugging.md`, `docs\lessons-learned.md`.
- Foundry setup: `docs\FOUNDRY_LOCAL_SETUP.md`.

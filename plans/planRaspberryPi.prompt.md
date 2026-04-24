## Plan: Misty + Raspberry Pi Companion

Use Misty as the robot client and a Raspberry Pi as the local AI host. This path is the best fit when the goal is a small attached or nearby companion computer. Misty handles wake word, recording, playback, and embodiment. The Raspberry Pi runs a Pi-friendly local runtime such as Ollama or llama.cpp for local inference, plus local STT and TTS. This path does not assume Microsoft Foundry Local is the right runtime for Pi.

**Steps**
1. Confirm the deployment boundary: Misty is the audio and robot interface, and the Raspberry Pi is the inference host. Included scope: wake word, audio handoff, local STT, local SLM, local TTS, and spoken response playback. Excluded scope: direct SLM execution on Misty.
2. Select the Pi hardware target. Recommended default: Raspberry Pi 5 with 8 GB RAM.
3. Choose the Pi deployment posture. Preferred default: keep the Pi off-robot on the same Wi‑Fi network as Misty. Optional alternative: mount the Pi on Misty using the backpack area if a self-contained package is required.
4. Validate physical integration assumptions if mounting the Pi. Misty documents backpack extensibility, USB power, and UART support for devices such as a Raspberry Pi. If mounting, account for heat, weight, cable management, and power draw.
5. Install and validate the local runtime on the Pi. Recommended first options: Ollama or llama.cpp with a small quantized chat model.
6. Select the initial local model stack: a small quantized chat model, a lightweight local transcription path, and a local TTS engine that can generate WAV output for Misty.
7. Define the interaction loop: Misty hears “Hey, Misty!”, records a short WAV clip, sends it to the Pi service, receives synthesized response audio, and plays it back.
8. Implement the robot side with an on-robot JavaScript skill using Misty’s wake word, recording, external request, playback, and optional embodiment APIs.
9. Implement the Pi-side service as a lightweight REST service for transcription, inference, context handling, TTS generation, and fallback behavior.
10. Add latency controls: short utterances, silence trimming, capped output, and re-enable key phrase recognition after each turn.
11. Add Misty embodiment only after the voice path works.
12. Validate in stages: network control, wake word, recording, direct Pi inference, direct TTS, then full turn-taking.

**Verification**
1. Confirm Misty responds over REST on the local network.
2. Confirm KeyPhraseRecognized fires for “Hey, Misty!”.
3. Confirm Misty records and exposes a short WAV file.
4. Confirm the Pi runs the chosen local runtime and model with acceptable latency before integrating Misty.
5. Confirm the Pi service returns valid text and WAV audio for known inputs.
6. If the Pi is mounted on Misty, confirm power and thermals remain stable during repeated interactions.
7. Confirm full end-to-end Misty interaction works within the target latency budget.
8. Confirm graceful fallback when the Pi service or model is unavailable.

**Decisions**
- Recommended runtime: Ollama or llama.cpp, not Foundry Local by default.
- Recommended hardware target: Raspberry Pi 5 with 8 GB RAM.
- Recommended communication path: Wi‑Fi over REST first, UART only as a later optimization.
- Recommended mounting posture: keep the Pi off-robot unless a self-contained package is required.
- Deliberately excluded: direct SLM installation on Misty’s onboard processors.

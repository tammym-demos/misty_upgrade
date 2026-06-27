# ESP32 Companion AI Lessons

## Summary

The ESP32 examples are useful inspiration for adding low-cost physical interactivity to Misty, but they do not change the core architecture decision for this project: Misty's AI workload should stay on the Windows companion device.

The key lesson is that an ESP32 can be a strong **peripheral controller**, not a realistic replacement for the companion laptop running STT, LLM, and TTS.

## Video and implementation findings

### BMO local AI agent

The BMO reference uses a Raspberry Pi as the main computer, not an ESP32. It runs a local pipeline with OpenWakeWord, Whisper, Ollama/Gemma, Moondream, Piper, and a direct LCD GUI for reactive face animations.

Applicable ideas:

- State-driven face animations with folders like `idle`, `listening`, `thinking`, and `speaking`.
- Randomized or looped expression frames so the robot feels alive.
- Startup/warmup behavior that preloads models and gives feedback while waiting.

Not directly applicable:

- Misty cannot run a desktop GUI on her face display.
- We should not copy BMO/Adventure Time assets, voice, or identity.
- The Raspberry Pi compute profile is much closer to a backpack computer than to an ESP32.

### Pixel / OmniBot ESP32 assistant

The Pixel video and OmniBot repo are the better ESP32 reference. Pixel uses a Seeed XIAO ESP32S3 Sense with camera, mic, round display, touch/gesture UI, and Wi-Fi.

Important distinction: Pixel does **not** run the LLM locally on the ESP32. The ESP32 streams mic/camera/display events to an OmniBot hub over Wi-Fi, and the hub connects to Gemini. The transcript explicitly rejects local LLM inference after choosing ESP32 over Raspberry Pi.

Applicable ideas:

- ESP32 as a cheap physical interface for mic, camera, buttons, touch, LEDs, and small display.
- Hub-managed persona files such as `SOUL.md`, `IDENTITY.md`, `USER.md`, and `MEMORY.md`.
- Daily logs plus periodic memory consolidation for longer-term personality continuity.
- Face/emotion tools that let the model request expressions.

Not directly applicable:

- Gemini Live / cloud API use conflicts with this repo's local/offline demo goal.
- ESP32 cannot replace the Windows companion device for Phi/Whisper/Kokoro-class inference.
- Pixel is a purpose-built robot shell; Misty already has motors, sensors, speakers, display, and REST/WebSocket APIs.

## Misty + ESP32 feasibility

Misty can be wired to an ESP32 through the backpack area. Misty's hardware extensibility docs expose UART serial and USB/power:

- UART serial supports external microcontrollers.
- Logic level is 3.3V.
- Serial settings for the Arduino backpack are 9600 baud, 8-N-1.
- Misty can provide 3.3V power up to 1A.
- USB can provide up to 500mA power.
- Misty can send data to attached hardware via `POST /api/serial`.
- Incoming UART data appears as `SerialMessage` events.

This makes ESP32 feasible for a Misty backpack peripheral, especially for:

- hardware buttons or touch wake
- extra LEDs or expressive accessories
- environmental sensors
- small auxiliary display
- physical controls for demos
- a dedicated sensor/microphone/camera module that reports events to the companion controller

## Recommended architecture

Use ESP32 as an optional **Misty backpack peripheral**, not an AI host.

Recommended flow:

```text
ESP32 backpack peripheral
  -> UART / Wi-Fi events
  -> Windows companion controller
  -> Foundry Local / faster-whisper / Kokoro
  -> Misty REST + WebSocket behavior
```

This keeps the AI stack local and reliable while allowing cheaper, swappable hardware experiments.

## Candidate future issues

- Add ESP32 backpack feasibility spike for Misty UART communication.
- Add a physical touch/button wake accessory.
- Add external LEDs or expressive accessory control synchronized with Misty's state machine.
- Explore markdown persona/memory files inspired by OmniBot, but implemented in the orchestration service rather than on ESP32.

## Decision guidance

Choose ESP32 when the task is physical I/O, low-power sensing, buttons, LEDs, or simple display behavior.

Choose Raspberry Pi, Jetson, or the existing Windows companion device when the task involves local LLM inference, STT, TTS, vision models, or rich GUI rendering.

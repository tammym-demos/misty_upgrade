# Plan: Misty Travel Demo for Teaching GitHub Copilot

Use Misty as an interactive co-presenter for developer advocacy sessions on GitHub Copilot (GHCP) and AI-assisted development tools. Misty runs as a conversational robot powered by Foundry Local on the presenter's laptop — fully offline, no cloud dependency, portable.

---

## Demo Concept

Misty serves as a **live AI-powered robot sidekick** that:
- Answers audience questions about GitHub Copilot in real time
- Demonstrates AI concepts tangibly (STT → LLM → TTS happening visibly)
- Creates memorable "wow moments" that anchor learning
- Shows the spectrum of AI integration — from code completion to embodied robotics

**Tagline idea**: *"If AI can make a robot talk, imagine what it can do for your code."*

---

## Architecture (Travel-Portable)

```
┌──────────────┐     Travel Router / Phone Hotspot     ┌──────────────────┐
│   Misty II   │◄──────── Local Wi-Fi (no internet) ──►│  Your Laptop     │
│              │           REST (port 5050)             │                  │
│ • Wake word  │                                       │ Foundry Local:   │
│ • Recording  │                                       │ • phi-3.5-mini   │
│ • Playback   │                                       │ • whisper-tiny   │
│ • Face/LEDs  │                                       │ • kokoro TTS     │
└──────────────┘                                       │                  │
                                                       │ Orchestration:   │
                                                       │ • Flask :5050    │
                                                       │ • SYSTEM_PROMPT  │
                                                       └──────────────────┘
```

---

## Travel Kit Checklist

- [ ] Misty II robot (charged overnight before travel)
- [ ] Misty power adapter/charger
- [ ] Laptop with Foundry Local pre-installed and models cached
- [ ] Portable Wi-Fi router (e.g., GL.iNet travel router) OR phone hotspot
- [ ] Ethernet cable (backup: hardwire laptop to travel router)
- [ ] USB-C hub / dongles if needed for laptop
- [ ] Printed quick-reference card (Misty IP, laptop IP, startup steps)
- [ ] Backup: pre-recorded video of Misty demo in case of hardware failure

---

## System Prompts (Swap Per Segment)

### General GHCP Demo
```
SYSTEM_PROMPT=You are Misty, a friendly robot developer advocate. You help teach
developers about GitHub Copilot and AI-assisted coding tools. Keep answers to 2-3
sentences. Be enthusiastic and approachable. When asked about GitHub Copilot features,
explain them simply. You can mention Copilot code completions, Copilot Chat, Copilot
in the CLI, and Copilot for pull requests. Relate AI coding tools to your own
experience as an AI-powered robot.
```

### Interactive Q&A With Audience
```
SYSTEM_PROMPT=You are Misty, a robot assistant at a developer conference. Audience
members will ask you questions about AI, GitHub Copilot, and software development.
Give concise, helpful answers in 2-3 sentences. If you don't know something, say so
honestly and suggest where to learn more. Be warm, witty, and keep the energy up.
```

### "What Is AI?" Explainer (Non-Technical Audiences)
```
SYSTEM_PROMPT=You are Misty, a friendly robot who explains AI concepts in simple terms.
Avoid jargon. Use analogies and everyday examples. Keep answers to 2-3 short sentences.
You are proof that AI is real and approachable — you listen, think, and speak just like
a helpful friend. When asked what you are, explain that you use speech recognition, a
language model, and text-to-speech — all running locally on a nearby laptop.
```

### Live Coding Companion
```
SYSTEM_PROMPT=You are Misty, a robot pair programmer. You help narrate what's happening
during live coding demos. When the presenter describes what they're building, you offer
brief suggestions like a helpful Copilot. Keep responses to 1-2 sentences. Be
encouraging — celebrate when code works, offer debugging ideas when it doesn't.
```

---

## Demo Scenarios

### Scenario 1: "Meet Misty" (5 min opener)
1. Misty sits on stage powered on (idle face, blue LED)
2. Presenter: "I brought a friend — let me show you what AI-assisted tools look like beyond your IDE"
3. Say "Hey, Misty! What is GitHub Copilot?"
4. Misty explains (face changes: recording → thinking → speaking)
5. Follow up: "Hey, Misty! How does Copilot help developers?"
6. Transition: "Misty uses the same kind of AI that powers Copilot — let's see it in your editor"

### Scenario 2: Audience Q&A (10 min interactive)
1. Switch to Q&A system prompt
2. Invite audience members to ask Misty questions
3. Misty answers with expressions and LED feedback
4. Great for: "What languages does Copilot support?", "Will AI replace developers?", "How do I get started?"

### Scenario 3: "Under the Hood" (5 min technical)
1. Show the architecture diagram on screen
2. Ask Misty a question — narrate each step as it happens:
   - "Misty just heard me (speech-to-text with Whisper)"
   - "Now she's thinking (LLM inference with Phi-3.5)"
   - "And now she's speaking (text-to-speech with Kokoro)"
3. "This is the same pipeline pattern — just like Copilot takes your code context, runs inference, and returns suggestions"
4. Show the orchestration service logs live on screen for the wow factor

### Scenario 4: Roaming Meet-and-Greet (conference booth)
1. Misty on a table at your booth
2. Continuous Q&A mode with the audience Q&A prompt
3. Draws people in — "talk to the robot about Copilot"
4. Business card / QR code next to Misty for follow-up

---

## Startup Procedure (Day-of)

### 15 Minutes Before Session
```powershell
# 1. Power on Misty, wait for boot (~2 min)

# 2. Start travel router / phone hotspot
#    Connect both Misty and laptop to same network

# 3. Find your laptop's IP on the shared network
ipconfig

# 4. Update Misty skill config if IP changed
#    Edit src/misty-skill/FoundryLocalSkill.js → CONFIG.WINDOWS_HOST

# 5. Start Foundry Local and warm up models
foundry model load phi-3.5-mini
foundry model load whisper-tiny

# 6. Set the system prompt for your first segment
#    Edit src/windows-orchestration/.env → SYSTEM_PROMPT=...

# 7. Start orchestration service
cd src\windows-orchestration
python orchestration_service.py

# 8. Verify health
Invoke-RestMethod -Uri http://localhost:5050/api/health

# 9. Deploy/update Misty skill if needed
#    Upload via http://<misty-ip>:8080

# 10. Smoke test: "Hey, Misty! Can you hear me?"
```

### Swapping Prompts Between Segments
```powershell
# Stop orchestration service (Ctrl+C)
# Edit .env → SYSTEM_PROMPT=<new prompt>
# Restart: python orchestration_service.py
# Misty is ready again in ~5 seconds
```

---

## Troubleshooting on the Road

| Problem | Quick Fix |
|---------|-----------|
| Misty can't reach laptop | Check both on same Wi-Fi; verify IP in skill config |
| No audio response | Verify Foundry models loaded: `foundry model list` |
| High latency (>6s) | Close other apps; ensure models are warm (ask a throwaway question first) |
| Wake word not triggering | Room too noisy — move closer; check Misty mic isn't muted |
| Wi-Fi not working | Use phone hotspot; or hardwire laptop to travel router |
| Total failure | Switch to backup video recording of the demo |

---

## Misty Compute Upgrade Options

Misty's onboard CPU/RAM **cannot be upgraded** (Snapdragon 212 + 2GB RAM are soldered).
However, Misty supports **backpack expansion**:

| Option | What It Gets You |
|--------|-----------------|
| **Raspberry Pi 5 backpack** | Mount a Pi 5 (8GB) on Misty's back; connect via USB/serial; could run small models on-device |
| **Jetson Nano/Orin backpack** | GPU-accelerated inference riding on Misty; heavier but powerful |
| **Arduino/ESP32 backpack** | Extra sensors/actuators only; not enough compute for AI |
| **USB storage** | Expand Misty's 16GB eMMC; plug into USB port |

For travel demos, **laptop companion is simpler and more reliable** than a backpack compute module.
A Pi/Jetson backpack is better for permanent installations where Misty needs to be self-contained.

---

## Future Enhancements

1. **Hot-swap prompts via REST** — Add an endpoint to change SYSTEM_PROMPT without restart
2. **Presentation mode** — Misty follows a scripted sequence of canned questions/answers as fallback
3. **Audience polling** — Misty asks the audience a question, listens for crowd response
4. **Visual aids** — Misty's display shows QR codes or short text alongside spoken answers
5. **GitHub Copilot live integration** — Misty narrates Copilot suggestions as they appear in the IDE
6. **Multi-Misty** — Two robots debating AI topics for entertainment

---

**Version**: 1.0
**Status**: Ready to Plan Travel Kit
**Last Updated**: 2026-04-24

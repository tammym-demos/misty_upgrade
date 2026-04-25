# Architecture Decision Record: Why Companion Device, Not On-Robot Inference

**Date**: 2026-04-24
**Status**: Accepted
**Deciders**: Tim McClell

---

## Context

We want Misty II to function as a conversational AI robot — listening, thinking, and speaking using local AI models (STT, LLM, TTS). The key architectural question is: **where does inference run?**

Three options were evaluated:

1. **On-robot** — Run models directly on Misty's onboard hardware
2. **Backpack compute module** — Mount a Raspberry Pi or Jetson on Misty's back
3. **Companion device** — Offload inference to a nearby laptop over local Wi-Fi

---

## Decision

**We chose Option 3: Companion device (Windows laptop + Foundry Local).**

---

## Misty II Hardware Constraints

| Spec | Value | Implication |
|------|-------|-------------|
| CPU | Qualcomm Snapdragon 820 (4× Kryo @ 2.15 GHz) + Snapdragon 410 | Capable ARM cores, but 2 GB RAM is the binding constraint for inference |
| RAM | 2 GB LPDDR3 | Smallest quantized LLMs need ~1 GB+ for weights alone, leaving almost nothing for the OS, skill runtime, and sensors |
| Storage | 16 GB eMMC | Limited space for model files (Phi-3.5-mini Q4 ≈ 2 GB, Whisper-tiny ≈ 75 MB, Kokoro ≈ 100 MB) |
| OS | Windows 10 IoT Core | Restricted runtime; no native support for common ML frameworks |
| GPU | None | No hardware acceleration for matrix operations |

**The CPU and RAM are soldered to the board and cannot be upgraded.**

### Why On-Robot Inference Is Not Viable

- **Memory**: Loading even a tiny 1B-parameter Q4 model consumes ~600 MB–1 GB. With 2 GB total and the OS + skill runtime already resident, there isn't enough headroom to load a model reliably — let alone run STT, LLM, and TTS concurrently.
- **Compute**: The Cortex-A7 cores lack NEON SIMD optimizations found in modern ARM chips. Inference on a 1B model would take **tens of seconds to minutes per response** — far outside our 3–6 second latency SLO.
- **Thermal/Power**: Sustained inference would thermally throttle the Snapdragon 820 and drain Misty's 10,200 mAh battery rapidly.
- **Framework support**: Windows 10 IoT Core on this chipset has limited support for ONNX Runtime, llama.cpp, or other inference engines. Porting and testing would be high-effort with low reward given the above constraints.

### Why Backpack Was Deferred (Not Rejected)

A Raspberry Pi 5 (8 GB) or Jetson Orin Nano mounted on Misty's backpack connector could run small models:

| Backpack Option | Pros | Cons |
|----------------|------|------|
| Raspberry Pi 5 (8 GB) | Affordable, good community support, runs Ollama/llama.cpp | Adds weight and bulk; needs separate power; ~10–20s inference for 3B models on CPU |
| Jetson Orin Nano | GPU-accelerated; fast inference | Expensive, heavier, higher power draw, thermal management needed |

**We deferred this option because:**
- The primary use case is **travel demos for developer advocacy** — a laptop is already in the travel kit
- Backpack adds physical complexity (mounting, cabling, power, heat) that increases failure risk on the road
- Latency on a Pi 5 for the full STT → LLM → TTS pipeline is borderline (~10–15s) without a GPU
- A backpack solution is better suited for **permanent installations** where Misty needs to be untethered

**This decision can be revisited** if we pursue a self-contained Misty deployment (e.g., conference booth robot that runs unattended).

### Why Companion Device Wins for Our Use Case

| Factor | Companion Device | On-Robot | Backpack |
|--------|-----------------|----------|----------|
| Latency (p50) | ~2–3s ✅ | Minutes ❌ | ~10–15s ⚠️ |
| Reliability | High (laptop is proven) ✅ | Untested, likely unstable ❌ | Moderate (added failure points) ⚠️ |
| Portability | Laptop already in bag ✅ | N/A | Extra hardware to pack ⚠️ |
| Setup complexity | Minimal (Wi-Fi + start service) ✅ | High (porting ML stack) ❌ | Moderate (mounting, power) ⚠️ |
| Model flexibility | Any model that fits laptop ✅ | Tiny models only ❌ | Small models only ⚠️ |
| Offline capable | Yes (Foundry Local) ✅ | Would be, if feasible ✅ | Yes ✅ |
| Cost | $0 (use existing laptop) ✅ | $0 but not viable ❌ | $80–500 for hardware ⚠️ |

---

## Consequences

- Misty depends on a companion device for AI capabilities — she cannot operate conversationally standalone
- The companion device and Misty must be on the same local network
- If the companion device is unavailable, Misty falls back to pre-programmed error responses
- Future backpack expansion remains a viable path if untethered operation becomes a priority

---

## References

- Misty II specs: https://docs.mistyrobotics.com/
- Misty backpack expansion: https://docs.mistyrobotics.com/misty-ii/backpack/
- Foundry Local: https://learn.microsoft.com/en-us/azure/foundry-local/
- Raspberry Pi plan: `plans/planRaspberryPi.prompt.md`
- Travel demo plan: `plans/planTravelDemo-GHCP.prompt.md`

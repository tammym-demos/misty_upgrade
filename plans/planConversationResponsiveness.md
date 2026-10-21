# Plan: Reliable, Faster Misty Conversations

**Date:** 2026-10-16  
**Status:** Implementation in progress; live performance validation pending  
**Priority:** High - live-demo reliability and responsiveness  
**Baseline revision:** `2d1e69d`  
**Working branch:** `review/misty-latency-and-greetings`

## Summary

Address the six improvements identified during live debugging:

1. Persist the intended microphone and reliable robot connection settings.
2. Remove the six-second minimum recording window.
3. Prevent Misty's greeting from entering the user's recording.
4. Share streaming behavior across initial/follow-up turns and distinguish errors from silence.
5. Measure and reduce prompt, grounding, and history overhead.
6. Measure and reduce first-chunk speech synthesis latency.

The user goal is for the **actual answer to begin approximately one second after
the person finishes speaking**, not a canned acknowledgment within one second.
This is a target to investigate, not an established capability or guaranteed
outcome. Preserve Misty's personality, answer quality, and local-first operation.

## Current state

The companion architecture, custom laptop wake word, streaming endpoint,
sentence-level audio delivery, follow-up listening, session history, local
grounding, and TTS cache already exist. Extend and correct those components;
do not rebuild them.

Live observations and isolated benchmarks on October 16:

| Observation | Evidence / interpretation |
|---|---|
| Wake word initially did not trigger | Configured microphone was the Jabra headset. After a session-only switch to the user-selected Surface microphones, wake detection succeeded with score `0.9986`. |
| Configured robot address was stale | The runtime cache contained a reachable hotspot address. The live session used an override without editing `.env`. |
| Initial capture required six seconds | The local `RECORDING_DURATION_S=6.0` overrides the canonical `1.25` default. Trailing silence was `1.5` seconds. |
| Greeting contaminated recognition | The STT transcript included Misty's own spoken greeting. The controller waits only `0.2` seconds after starting the greeting before recording. |
| First live audio was slow | Server first audio was `29.855` seconds after STT; Foundry HTTP response headers took about `15.26` seconds and the first sentence's TTS took `11.42` seconds. |
| Follow-up failed after successful STT | It used buffered orchestration, hit a 30-second LLM timeout, and was subsequently described as no follow-up speech. |
| Minimal inference was much faster | A small direct prompt returned its first token in approximately `0.75-0.95` seconds and its first sentence in `2.8-3.9` seconds. This is not equivalent to the application's full prompt. |
| Uncached and cached TTS differ substantially | A short uncached phrase took `3.369` seconds; the same cached phrase took approximately `0.02` seconds. |
| Hotspot was not the main observed delay | Five small robot HTTP requests took `47-75` milliseconds. Audio-upload throughput was not independently measured. |

Local diagnostic artifacts are in `test-results\debug-20261016\` and are
gitignored. Do not commit raw conversation recordings, transcripts, enrollment
profiles, credentials, or machine-specific configuration.

**Measurement caveat:** the first full-service synthetic benchmark used
one-byte SSE reads, adding client parsing overhead. Its client first-audio and
total times are not valid product baselines. The server timing remains useful;
subsequent comparisons below use the corrected 512-byte SSE reader. No acoustic
speech-onset measurement has yet been made.

Initial offline changes persist the selected Surface microphone by name and
host API, use the canonical 1.25-second capture floor, wait for the known
greeting duration before opening capture, route follow-up through the shared
streaming dispatcher, enforce shared prompt limits, and add metadata-only
latency measurements. Subsequent host-only measurements are recorded below.
Robot playback, pause tolerance, and the complete end-of-speech-to-audible-answer
path remain unverified; the one-second response target has not been reached.

## Host-only backend comparison: October 16

Ten serial synthetic streaming requests were completed successfully per backend:
`Phi-3.5-mini-instruct-generic-cpu:2` and the already-cached
`Phi-3.5-mini-instruct-openvino-gpu:2`. Each used a fresh conversation, the same
synthetic "What is your name?" question, unchanged full prompt
(1,998 characters, including 644 characters of grounding), and unchanged
STT/voice configuration. Models were loaded before measurement; these are not
model-startup measurements. The GPU service used a process-only `CHAT_MODEL_ID`
override, not a persistent configuration change.

Both profiles had one first-chunk TTS cache miss followed by nine cache hits,
confirmed in the service log. Repeated-answer cache performance must not be
presented as uncached generative conversation performance.

| Measurement | Samples per backend | CPU p50 / p90 (s) | OpenVINO GPU p50 / p90 (s) |
|---|---|---|---|
| Foundry response headers, full prompt | 10 | 21.164 / 22.597 | 0.310 / 0.434 |
| Client first audio, all requests | 10 | 23.767 / 27.729 | 1.646 / 2.035 |
| Client first audio, first-chunk TTS cache hits only | 9 | 23.274 / 27.729 | 1.626 / 2.035 |
| Client first audio, first-chunk TTS cache miss | 1 | 27.772 / not estimated | 4.894 / not estimated |
| Server first audio, all requests | 10 | 23.250 / 26.998 | 1.051 / 1.509 |
| Client stream completion | 10 | 27.040 / 33.543 | 2.135 / 2.473 |

Medians use the mean of the two central values for even sample counts; p90 uses
nearest rank. With nine samples, p90 is the maximum. These are small,
non-randomized sequential profiles, not a controlled thermal/power study.

Timing boundaries are intentionally different:

- Foundry response-header timing starts with the model request; it is not
  identical to the first parsed content token. The GPU's measured first-token
  median was `0.310` seconds, with p90 `0.434` seconds.
- Client first audio starts with the orchestration HTTP request and includes
  STT and receipt of the first audio SSE event. It excludes microphone
  endpointing, robot upload, and physical playback.
- Server first audio starts after STT. Neither audio metric measures when a
  person actually hears Misty speak.

The first uncached 44-character chunk took `5.492` seconds to synthesize in the
CPU profile and `2.755` seconds in the GPU profile. Kokoro was unchanged; these
single observations do not demonstrate GPU-accelerated TTS. CPU contention and
other runtime effects were not independently controlled. CPU responses had
three audio chunks and GPU responses had two, so completion times also reflect
generated-output differences; answer-quality equivalence was not evaluated.

A separate CPU-only experiment removed irrelevant grounding while retaining
the persona: ten trials still had first-token p50/p90 of `13.967 / 14.318`
seconds and first-sentence p50/p90 of `17.775 / 18.205` seconds. Prompt reduction
alone did not resolve the observed CPU latency.

**Decision supported by this evidence:** the cached OpenVINO GPU variant is a
strong candidate for the next runtime profile, but has not been adopted in
persistent settings. Uncached synthesis remains a material bottleneck, and
neither these cache-heavy trials nor server timing establish the one-second
audible-answer goal. Broader questions, personality/factuality checks, and a
separately approved live rehearsal are still needed before claiming that result.

The owned orchestration process was stopped and the test GPU model unloaded
after measurement. Port 5000 was verified closed; the pre-existing Foundry
service was retained with no loaded models. No robot was contacted during these
host-only comparisons. Reproducible local helpers and raw timing metadata remain
in the gitignored diagnostic directory; they are not repository deliverables.

## Proposed approach

### 0. Establish trustworthy comparison measurements

Before optimizing, promote the useful diagnostic measurements into a small,
repeatable benchmark using existing tooling, without requiring a new framework.

- Record separate timestamps for speech end, capture completion, STT completion,
  LLM request/first token/first speakable segment, TTS start/finish, SSE receipt,
  robot upload start/finish, and playback request acknowledgment.
- Use monotonic elapsed time and a shared turn/conversation ID. Label audio
  generated, audio received, playback requested, and audio actually heard as
  different measurements. Do not call server audio availability audible onset.
- Measure audible onset during controlled live trials using an available,
  verified playback event or manual observation; do not invent an unsupported
  robot event or require persistent room recordings.
- Benchmark cold and warm execution, cached and uncached TTS, initial and
  follow-up turns, short and longer utterances, and empty/populated history.
- Report per-stage p50/p90 and timeout/error counts. Use the same fixed question
  set, model, voice, laptop power conditions, and network for comparisons.
  Run performance experiments serially to avoid CPU-contention confounds.
- Use at least ten warm synthetic trials per compared profile and a smaller
  agreed live rehearsal set. Label small samples and cold-start observations.

**Acceptance:** an unchanged baseline can be reproduced; benchmark instrumentation
does not add material buffering or mix incompatible latency definitions.

### 1. Persist the intended input and verified connection

**Surfaces:** `tools\misty-services.mjs`, `wake_word_listener.py`, local `.env`,
runtime state, `.env.example`, and directly related setup documentation.

- Persist the user-selected Surface microphone in local configuration only.
  Prefer an unambiguous supported device name; if an index is needed, verify the
  resolved device at startup because enumeration can change after docking.
- Log the resolved input name, host API, and sample rate. Fail with actionable
  guidance if the configured device is absent or ambiguous; do not silently
  switch to the headset or OS default.
- Preserve explicit robot-address overrides. If the configured address fails,
  try the cached address and verify robot identity before any approved discovery.
  Respect `--no-scan` and do not broaden network scans.
- Persist successful discovery in existing runtime state. Ensure startup and
  cleanup use the verified robot address rather than a stale `.env` value.
  Do not commit private addresses or change canonical defaults for this laptop.
- Verify the configured chat model is loaded when establishing the baseline;
  an HTTP health response alone did not prove inference readiness in this session.

**Acceptance:** after stop/start without temporary shell overrides, the intended
microphone captures speech, the wake word triggers, and the same robot is reached.
Stale IP, missing microphone, and device-renumbering cases have explicit outcomes.

### 2. Remove the forced six-second capture floor

**Surfaces:** `config_defaults.py`, `.env.example`, local `.env`,
`wake_word_listener.py`, and both controller recording paths.

- Replace the stale local six-second setting with the existing canonical
  `1.25`-second minimum as the first comparison profile.
- Keep VAD-driven completion, bounded maximum capture, no-speech handling, and
  the existing explicit recording fallback behavior.
- Centralize trailing-silence configuration, which is currently hardcoded in
  `wake_word_listener.py`. Compare the current `1.5` seconds with shorter values
  in the `0.5-0.8`-second range; select a default only after pause/noise trials.
- Check initial and follow-up paths for additional minimum waits, including
  `FOLLOWUP_LISTEN_S` when Misty fallback recording is enabled. Changing one
  constant must not leave another hidden floor.
- Do not change the recording fallback mode merely to make a benchmark faster.

**Acceptance:** short questions are no longer held to six seconds; longer speech
and natural pauses are not clipped; silence/noisy-room cases terminate safely.
Tests use controlled clocks and synthetic frames, not wall-clock sleeps.

### 3. Keep robot speech out of user capture

**Surfaces:** `_upload_greeting()`, `_do_conversation_exchange()`, playback
duration tracking, and wake-word/recording lifecycle tests.

- Reuse the known duration of preloaded greeting audio and a bounded completion
  guard before opening user capture. Do not replace `0.2` seconds with another
  arbitrary fixed wait.
- Give the listening cue only when capture is ready so the user does not begin
  speaking while the application is deliberately ignoring audio.
- Verify that the thinking phrase and follow-up transitions also cannot enter
  a user recording.
- Preserve the existing greeting initially. Evaluate a short chime only as an
  intentional UX option if the spoken greeting remains a meaningful delay.
- Preserve mute, shutdown, and cancellation behavior during the greeting.

**Acceptance:** the standardized utterance transcribes without Misty's greeting;
cue/capture ordering is covered by mocks and confirmed in a live trial.

### 4. Unify initial/follow-up delivery and error handling

**Surfaces:** `_listen_for_followup()`, `_do_orchestrate_and_respond()`,
`_do_streaming_orchestrate_and_respond()`, and orchestration result handling.

- After follow-up capture, reuse the existing common response dispatcher rather
  than maintaining a second buffered orchestration/playback implementation.
- Preserve conversation ID, recognized speaker context, movement/control
  handling, ordered audio, history rollback, and existing safety gates.
- Distinguish no speech, successful speech response, control/movement result,
  cancellation, and inference/transport/playback failure using a small explicit
  result contract. Follow repository logging conventions.
- Do not report a timeout as silence. Surface a concise operator-visible error,
  log the failed stage and turn ID, and recover to a usable state.
- Preserve buffered compatibility fallback only before streamed audio starts;
  never replay a partial answer or issue an automatic duplicate movement.
- Close streaming responses and cancel pending work on every exit path.

**Acceptance:** initial and follow-up answers use the same selected delivery
mode. Injected LLM timeout, stream interruption, upload failure, mute, and shutdown
do not masquerade as silence, duplicate speech/actions, or corrupt history.

### 5. Isolate and reduce prompt-processing overhead

**Surfaces:** prompt preparation in both orchestration routes,
`retrieve_local_context()`, conversation history, and response-mode configuration.

- Capture prompt character/token estimates, history length, grounding size,
  first-token latency, and first-segment latency without logging private prompts
  by default.
- Compare the current full prompt with a compact equivalent persona. Preserve
  Misty's personality, approved facts, uncertainty behavior, and conversation
  continuity rather than optimizing an unrelated minimal prompt.
- Add grounding only when relevant; bound excerpts and remove redundant context.
  Do not silently drop required system behavior or the latest user utterance.
- Share prompt preparation and context limits between streaming and buffered
  routes so switching delivery does not change policy or input limits.
- Change one factor at a time. Attribute the live delay to prompt overhead only
  if controlled measurements support it; inspect model execution/cold-start
  behavior if the discrepancy remains.
- Keep the current model for the first comparison. A model/provider change is
  a separate experiment, not an assumed fix.

**Acceptance:** the selected profile has lower measured first-token/first-segment
latency without regressions on a fixed personality, factuality, and follow-up
question set. Publish both improvements and unresolved discrepancies.

### 6. Reduce and stabilize first-chunk synthesis

**Surfaces:** `_complete_sentences()`, streaming orchestration,
`text_to_speech()`, Kokoro initialization, and controller stream playback.

- Measure uncached synthesis across fixed text lengths and separate model
  initialization, synthesis, queue/lock wait, serialization, and robot upload.
- Benchmark a shorter, meaningful opening clause against full-sentence chunking.
  Do not count filler or prerecorded acknowledgment as the actual answer.
- Preserve pronunciation, prosody, and audio order. Test punctuation,
  abbreviations, unfinished final text, and very short replies.
- If blocking token consumption or playback consumption is material, introduce
  only the bounded producer/consumer separation needed to overlap LLM output,
  TTS, upload, and playback. Preserve cancellation and avoid unbounded queues.
- Profile supported runtime/thread options before adopting them. Do not infer
  GPU/NPU compatibility merely from the laptop's hardware or registered providers.
- Keep cache-hit results separate from uncached generative performance, and keep
  the current voice unless a voice change is deliberately evaluated.

**Acceptance:** uncached first-audio latency improves on the fixed phrase set
and real service workload, with no missing, duplicated, reordered, or clipped
speech and no substantial increase in gaps between chunks.

## Delivery order and dependencies

Implement small, reviewable changes in this order:

1. Establish measurements, then correct and verify persistent input/connection settings.
2. Correct recording completion and greeting/capture ordering.
3. Consolidate response delivery and failure recovery.
4. Run prompt experiments against the corrected shared pipeline.
5. Run synthesis/chunking experiments while holding the selected prompt fixed.
6. Repeat the end-to-end live rehearsal and document the measured result.

Run prompt and synthesis experiments separately. Do not mix prompt/model/voice/
network changes in one comparison or require the user to repeat live prompts
while a known inference failure remains unresolved.

## Verification

Extend existing suites rather than introducing another test runner:

```powershell
python -m pytest tests\test_integration.py tests\test_issue_135.py -m "not live" -q
python -m pytest tests\test_conference_mode.py tests\test_face_tracking_controller.py -q
```

Relevant current test defects must be handled explicitly: canonical-default
tests read local `.env`, and the failed-upload regression fixture omits
`_audio_action_lock`. Isolate configuration and repair applicable fixtures;
do not mistake those existing failures for product-performance measurements.
Add focused coverage for launcher address selection and microphone resolution
using the repository's existing test conventions.

Live rehearsal must confirm restart persistence, short/long questions, pauses,
no speech, initial/follow-up delivery, mute/recovery, and clean shutdown.
Report actual end-of-speech-to-audible-answer results separately from server
timing. Reaching one second is not a prerequisite for honestly reporting an
intermediate improvement; failing to reach it remains an explicit open gap.

## Risks / constraints

- All inference remains on the companion laptop; Foundry's port is discovered.
- Shorter endpointing can clip pauses; smaller chunks can damage prosody or
  increase upload overhead. Measurements must include those regressions.
- Never reintroduce native keyphrase detection, sensory-only reboot, or implicit
  robot movement as recovery. No drive commands are needed for these tests.
- Keep conference mode deterministic and preserve its cue generation, manual
  controls, and shutdown behavior.
- Preserve user changes and keep local settings and diagnostic artifacts out of git.
- At the end of live testing, mute, stop legacy keyphrase and recording, cancel
  skills, halt motion, restore the idle face, and turn the LED off.

## Scope exclusions

Face enrollment, crowd-aware greetings, navigation, firmware/BLE reverse
engineering, cloud migration, and broad CI/documentation cleanup are separate
work. This plan does not restart completed features or implement those requests.

## Related issues and references

- #135 (closed): existing conversation/conference hardening and streaming work;
  this plan closes observed residual gaps and pursues the newer response-time goal.
- #125 (closed): laptop recognition framework; enrollment/recognition activation
  is outside these six improvements.
- #112 (open): historical native-recognition activation request; do not use it as
  instructions to re-enable the unsupported native path.
- `docs\ADR-001-companion-device-over-onrobot-inference.md`
- `docs\ADR-002-non-blocking-audio-pattern.md`
- `docs\lessons-learned.md`
- Local evidence: `test-results\debug-20261016\findings.json`,
  `events.jsonl`, `wake-metrics.jsonl`, and `benchmarks.jsonl`.

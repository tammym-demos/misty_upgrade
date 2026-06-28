# Misty Test Questions — Richer Responses Edition

Updated for the richer responses changes in PR #45 and adaptive conversations (issue #46). Key differences from the original `test-questions.md`:

| Setting | Before | After |
|---------|--------|-------|
| Response length (short) | ≤10 words, 1 sentence | ~35 words, 1-2 sentences |
| Response length (summary) | N/A | ~40 words, 2-3 sentences + "Want more?" |
| Response length (continuation) | N/A | ~40 words, 2-3 sentences per chunk |
| max_tokens (short / summary) | 20 | 60 / 80 |
| Recording duration | Fixed 6s | VAD-controlled: 6-15s (min_duration=RECORDING_DURATION_S) |
| Follow-up window | 60s | 90s |
| Follow-up turn cap | unlimited | 12 turns |
| Conversation history | 4 messages (2 turns) | 8 messages (4 turns) |
| Wake word | Misty built-in keyphrase | "Hey Misty" custom OpenWakeWord model on laptop mic |
| Intent detection | None | Story/recipe/explain/continuation patterns |

---

## Setup

**Start the controller:**
```powershell
cd src\windows-orchestration
$env:OWW_CUSTOM_MODEL_PATH = "C:\path\to\hey_misty.onnx"
python misty_controller.py
```
Say **"Hey Misty"** near the laptop mic, then speak after the orange LED. Misty's built-in keyphrase path is unsupported; if the custom model path is missing, the controller should fail fast instead of falling back.


---

## 1. Quick Check (Single Turn)

Verify the full pipeline works with richer responses:

1. "How are you doing today?"
   - **Before**: "I'm great, thanks!" (5 words)
   - **Now expect**: "I'm doing great, thanks for asking! How about you?" (1-2 sentences, ~10-20 words)

2. "What's your name?"
3. "Tell me a joke."
   - Good test for richer responses — jokes need setup + punchline (2 sentences)
4. "What color are you?"
5. "What time is it?"

**What to watch for**: Responses should feel more conversational — not one-word answers, but not paragraphs either. Target is 1-2 natural sentences.

## 2. Knowledge & Facts

Tests whether richer responses improve factual answers:

6. "What is the capital of France?"
   - **Before**: "Paris." (1 word)
   - **Now expect**: "The capital of France is Paris, a beautiful city known for the Eiffel Tower." (~15 words)

7. "What is the largest ocean?"
8. "How many planets are in our solar system?"
9. "Who wrote Romeo and Juliet?"
10. "What is the speed of light?"

**What to watch for**: Answers should include the fact PLUS a brief interesting detail. Not just the bare answer.

## 3. Follow-Up Conversation (Multi-Turn)

Tests the extended 90s/12-turn follow-up window and 8-message history:

11. "What is the capital of Japan?" → "What language do they speak there?" → "What's a popular food there?" → "Do they have robots there?"
    - 4th follow-up tests the expanded history (old limit was 2 turns)

12. "Do you like music?" → "What kind?" → "Can you sing?" → "Try singing something!"

13. "Tell me about dogs." → "What's the biggest breed?" → "Do you have a favorite?" → "Would you want to be a dog?"

**What to watch for**:
- Follow-ups should reference context from earlier turns (history now spans 4 turns)
- Conversation should flow naturally for 90 seconds without wake word
- After 12 follow-up turns, Misty should end the conversation gracefully

## 4. Longer Questions (6-Second Recording Window)

Test whether the full question gets captured in the recording:

14. "Do you know what the largest lake in the United States is?"
15. "Can you tell me something interesting about the planet Jupiter?"
16. "What would you do if you could go anywhere in the world?"
17. "If you had to pick your favorite season of the year, which one would it be?"

**What to watch for**: With richer responses, answers to these should be more substantive — a sentence or two rather than a single word.

## 5. Personality & Creativity

Tests whether richer responses bring more personality:

18. "Are you a boy or a girl?"
    - **Before**: "I'm a robot!" (3 words)
    - **Now expect**: "I'm Misty, a friendly robot! I don't have a gender, but I'm happy to chat." (~15 words)

19. "Do you have any friends?"
20. "What do you dream about?"
21. "Would you rather be a cat or a dog?"
22. "What's your favorite thing about being a robot?"

**What to watch for**: Personality should shine through — playful, curious, warm. Two sentences gives room for a real answer.

## 6. Edge Cases

Tests STT accuracy and error handling:

23. "Supercalifragilisticexpialidocious" *(unusual word)*
24. "One plus one equals what?" *(math)*
25. "Say something in Spanish." *(language switching)*
26. *(Say nothing — test silence detection)*
27. *(Whisper very quietly — test mic sensitivity)*
28. *(Speak very fast — test STT with rapid speech)*

## 7. Stress Test — Extended Conversation

Run these as a continuous follow-up chain to test pipeline stability under the 90s window and 12-turn cap:

29. "What's two plus two?"
30. "What's the opposite of hot?"
31. "Name a color."
32. "Name an animal."
33. "What day is it?"
34. "Say something funny."
35. "What's your favorite number?"
36. "Tell me a fun fact."
37. "What's the weather like where you are?"
38. "Do you get tired?"
39. "What should we talk about next?"
40. "Goodbye!"

**What to watch for**:
- Pipeline should stay stable through all 12 turns
- Response quality shouldn't degrade over time
- Misty should end conversation after turn 12 or 90s, whichever comes first
- Latency should stay consistent (~2s follow-ups, ~5s first turn)

## 8. Laptop Wake Word Specific

Tests unique to the laptop mic wake word mode:

41. *(Walk across the room and say "Hey Misty" — test laptop mic range)*
42. *(Say "Hey Misty" during Misty's response — self-wake prevention should block it)*
43. *(Say "Hey Misty" immediately after conversation ends — should re-arm within 2s)*
44. *(Play music near laptop — test false positive rate)*
45. *(Say "Hey Misty" while facing away from laptop — test directional sensitivity)*

---

## Expected Behavior

| LED Color | State | Duration |
|-----------|-------|----------|
| 🟢 Green | Ready — listening for wake word | Until triggered |
| 🟠 Orange | Recording your question | 3-15s (VAD-controlled) |
| 🔵 Blue | Processing (STT → LLM → TTS) | ~2-5s |
| 🟣 Purple | Playing response | Varies by response length |
| 🩵 Cyan | Follow-up listening | Up to 90s / 12 turns |
| 🟡 Yellow | Low battery warning / recovery notice | Varies |
| 🔴 Red | Error | Check logs |

**Response time**: depends mostly on TTS generation; check `[Pipeline ...]` logs for STT/LLM/TTS breakdown
**Response length (short)**: 1-2 sentences, ~35 words (max_tokens=60, truncation at 35 words)
**Response length (summary/continuation)**: 2-3 sentences, ~40 words (max_tokens=80, truncation at 50 words)
**Recording duration**: VAD-controlled — 6s minimum (RECORDING_DURATION_S), up to 15s for long utterances
**Follow-up window**: 90 seconds or 12 turns, whichever comes first
**History**: Misty remembers the last 4 turns (8 messages) of conversation

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| No response to wake word | Missing/incorrect custom "Hey Misty" model, laptop mic issue, or OpenWakeWord threshold | Check logs; verify `OWW_CUSTOM_MODEL_PATH` and `sounddevice` mic selection |
| 44-byte recording (empty) | Mic or recording pipeline issue | Check `misty_controller.log` for recording errors |
| Response too long/wordy | Brevity drift past 35 words | Post-truncation should catch this; check max_tokens=60 |
| Response too short | Old behavior persisted | Verify running updated code; check system prompt |
| Follow-up doesn't work | Silence threshold too aggressive | Check `FOLLOWUP_SILENCE_THRESHOLD` (default 1000 bytes) |
| Laptop wake word false positives | Threshold too low | Increase threshold: `OWW_THRESHOLD=0.7` |
| Laptop wake word misses | Threshold too high or mic issue | Decrease `OWW_THRESHOLD` or check `sounddevice` mic selection |
| Self-wake (Misty triggers herself) | Pause/resume not working | Check `wake_word_listener.py` pause/resume flow in logs |

---

## 9. Adaptive Response Length — Summary Mode

Tests for requests that need longer responses (stories, recipes, explanations):

46. "Tell me a bedtime story."
    - **Expect**: 2-3 sentence story teaser (~40 words), ends with "Want to hear more?"
47. "Give me a recipe for chicken pot pie."
    - **Expect**: Key ingredients + 1-sentence method overview, ends with "Want to hear more?"
48. "Explain how gravity works."
    - **Expect**: 2-3 sentence clear summary, ends with "Want to hear more?"
49. "Make up a story about a robot who goes to space."
50. "How do I make chocolate chip cookies?"
51. "Tell me about the solar system."

**What to watch for**:
- Response should be noticeably longer than short mode (~40 words vs ~20)
- Should include "Want to hear more?" or similar continuation offer
- TTS should take ~4-6s (acceptable tradeoff for meaningful content)
- LED/face behavior same as normal response

## 10. Continuation Chains

After getting a summary response, test chunked continuation:

52. [After #46 story] → "Yes, tell me more."
    - **Expect**: Next 2-3 sentences of the story, may end with "Want more?"
53. [After #52] → "Keep going."
    - **Expect**: Another chunk of story
54. [After #47 recipe] → "Yes, what are the steps?"
    - **Expect**: Method/steps portion of recipe
55. [After #48 explanation] → "Go on."
    - **Expect**: Deeper explanation, more detail
56. [Mid-chain] → "Actually, tell me a joke."
    - **Expect**: Breaks out of continuation, returns to short mode (~20 words, punchy joke)

**What to watch for**:
- Continuation should flow naturally (LLM has context from history)
- Each chunk should be ~40 words, not a repeat of the summary
- Topic change should reset cleanly to short mode
- `responseMode` in logs should show: summary → continuation → continuation → short

## 11. VAD Dynamic Recording — Short Utterances

Test that short questions end recording early (should save ~3s per turn):

57. "Hi." (very short — recording should stop at ~3s)
58. "What's your name?" (~1.5s of speech)
59. "Yes." (continuation, ~0.5s of speech)
60. "No thanks." (~1s of speech)
61. "Tell me a joke." (~1.5s of speech)

**What to watch for**:
- Orange LED (recording) duration should be shorter than the usual 6s
- Total turn time should be noticeably faster (~5s instead of ~8s)
- Logs should show "Speech monitor: end of utterance detected" with elapsed < 6s
- Responses should still be accurate (STT captures full utterance)

## 12. VAD Dynamic Recording — Long Utterances

Test that long questions extend recording past 6s:

62. "Tell me a bedtime story about a princess who lives in a castle by the sea and has a pet dragon."
63. "I want you to recommend a recipe for chicken pot pie with a flaky crust and lots of vegetables inside."
64. "Can you explain to me how the internet works, like how does a website get from a server to my computer?"
65. "My favorite thing about today was going to the park with my dog and we played fetch for like an hour."

**What to watch for**:
- Recording should extend past 6s (up to 15s max)
- Logs should show speech detected with recording > 6s
- STT should capture the **complete** sentence (not cut off)
- Misty's response should address the full question, not just the beginning

## 13. Mixed Scenario — Full Adaptive Conversation

A realistic end-to-end flow testing all features together:

66. [Wake word] → "How are you today?" (short mode, VAD stops early ~3s)
67. → "Tell me a bedtime story." (summary mode, VAD stops ~3s)
68. → "Yes, tell me more." (continuation, VAD stops ~1.5s)
69. → "What happens next?" (continuation, VAD stops ~2s)
70. → "That was fun. What's your favorite color?" (topic change → short mode)

**What to watch for**:
- Smooth transitions between modes (check logs for `responseMode`)
- VAD-controlled recording adapts to each utterance length
- No awkward pauses or timing issues
- Continuation flows naturally through conversation history
- Topic change resets response length cleanly

## 14. Edge Cases

71. Say nothing after wake word — VAD should exit at ~4s (no speech timeout)
72. Whisper a question very quietly — tests VAD RMS threshold sensitivity
73. Say "yes" without a prior summary — should stay in short mode (not continuation)
74. Very long continuous speech (>15s) — recording should hard-cap at 15s
75. "Tell me something interesting." — ambiguous, should default to short mode

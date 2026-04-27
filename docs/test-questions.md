# Misty Test Questions

Questions to ask Misty during testing. Say **"Hey Misty"** first, then ask your question after the beep. For follow-up questions, just speak — no wake word needed within the 60-second follow-up window.

## Quick Check (Single Turn)

These verify basic wake word → STT → LLM → TTS pipeline:

1. "How are you doing today?"
2. "What's your name?"
3. "Tell me a joke."
4. "What color are you?"
5. "What time is it?"

## Knowledge & Facts

Tests LLM accuracy with short factual answers:

6. "What is the capital of France?"
7. "What is the largest ocean?"
8. "How many planets are in our solar system?"
9. "Who wrote Romeo and Juliet?"
10. "What is the speed of light?"

## Follow-Up Conversation (Multi-Turn)

Start with the first question, then continue without saying "Hey Misty":

11. "What is the capital of Japan?" → "What language do they speak there?" → "What's a popular food there?"
12. "Do you like music?" → "What kind of music?" → "Can you sing?"
13. "Tell me about dogs." → "What's the biggest breed?" → "Do you have a favorite?"

## Longer Questions (Tests 6-Second Recording Window)

These test whether the full question gets captured:

14. "Do you know what the largest lake in the United States is?"
15. "Can you tell me something interesting about the planet Jupiter?"
16. "What would you do if you could go anywhere in the world?"
17. "If you had to pick your favorite season of the year, which one would it be?"

## Personality & Creativity

Tests the LLM's personality (should be brief and playful):

18. "Are you a boy or a girl?"
19. "Do you have any friends?"
20. "What do you dream about?"
21. "Would you rather be a cat or a dog?"
22. "What's your favorite thing about being a robot?"

## Edge Cases

Tests STT accuracy and error handling:

23. "Supercalifragilisticexpialidocious" *(unusual word)*
24. "One plus one equals what?" *(math)*
25. "Say something in Spanish." *(language switching)*
26. *(Say nothing — test silence detection)*
27. *(Whisper very quietly — test mic sensitivity)*
28. *(Speak very fast — test STT with rapid speech)*

## Stress Test Sequence

Run these back-to-back as follow-ups to test pipeline stability:

29. "What's two plus two?"
30. "What's the opposite of hot?"
31. "Name a color."
32. "Name an animal."
33. "What day is it?"
34. "Say something funny."
35. "Goodbye!"

## What to Watch For

| Symptom | Likely Cause | Issue |
|---------|-------------|-------|
| No response to "Hey Misty" | Keyphrase silent failure — watchdog auto-recovers in ~3.5min, or reboot | #22 |
| Response cuts off your question | Recording window is 6s — speak promptly after beep | #20 |
| Long pause before response | LLM/TTS latency | #21 |
| Misty misheard your words | STT accuracy | #27 |
| Response too long/wordy | Brevity drift | #24 |
| Chest LED stays orange | Stuck in recording state | Check logs |
| Chest LED turns red | Error state | Check logs |

## Expected Behavior

- **Green LED** = Ready, listening for "Hey Misty"
- **Orange LED** = Recording your question
- **Blue LED** = Processing (STT → LLM → TTS)
- **Purple LED** = Playing response
- **Response time**: ~2s for follow-ups, ~5s for first turn (TTS cold start)
- **Response length**: ≤10 words (brief, conversational)
- **Follow-up window**: 60 seconds after last response

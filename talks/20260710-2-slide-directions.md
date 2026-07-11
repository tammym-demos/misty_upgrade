# Claude Design Prompt: Upgrading Misty's Brain

Create a clean, professional 9-slide presentation for a Rockwell Automation hackathon opening talk titled **"Upgrading Misty's Brain."**

The slides should be mostly word-based, with simple layouts and clear speaker-support content. I will add photos and visuals later, so include obvious image/visual placeholders where helpful, but do not depend on images to make the slides work.

Tone: energetic, technical, warm, slightly humorous, and hackathon-friendly. Audience: Rockwell Automation developers, FIRST Robotics alumni/fans, and hardware/software hackers.

## Slide outline

1. **Microsoft and GitHub Logos**
   - Title: "Upgrading Misty's Brain"
   - Subtitle: Rockwell Automation Hackathon
   - Visual placeholder: Microsoft + GitHub logos

2. **Welcome to the Hackathon**
   - Welcome the room.
   - Frame the event around creativity, hardware, software, and GitHub Copilot.
   - Set an energetic tone for the next two days.

3. **Saline Singularity 5066**
   - Briefly connect the story to FIRST Robotics.
   - Mention coaching Team 5066, the Saline Singularity.
   - Visual placeholder: FIRST Robotics or team photo.

4. **Misty's Architecture**
   - Explain that Misty is now the body and the Windows companion laptop is the brain.
   - Misty II provides speakers, LED, display, movement, camera/mic/tally-light behavior.
   - The laptop runs the companion services and local AI stack.
   - Visual placeholder: simple flow diagram from Misty to laptop companion services.

5. **GitHub Copilot Training Hub**
   - Bring up `https://tammym-demos.github.io/ghcp-agentic-hack/copilot-dev-training/`.
   - Explain that this is the participant home base for modules, agenda, prerequisites, and skills.
   - Frame it as "pick your own journey": participants can follow the guided path or jump to the modules and skills most relevant to their hack.
   - Visual placeholder: screenshot of the GitHub Pages training site.

7. **Artifact Slide**
   - Bring up the problem statement artifact.
   - Leave room for the artifact screenshot or visual content.
   - Use only short framing text around the artifact.

8. **Introducing the Coaches & Wrap-Up**
   - Reinforce that participants are not working alone.
   - Encourage collaboration, asking questions, and learning from other teams.
   - Transition toward the coach introductions.

9. **Coach Introductions**
   - Introduce Kevin, Sam, Tom, Tammy, Kelly, and Lyle.
   - Close with momentum: "Let the hacking begin."
   - Visual placeholder: coach names, headshots, or simple team grid.

## Architecture details to include where relevant

- Foundry Local runs the Phi-3.5-mini language model.
- `orchestration_service.py` handles speech-to-text, reasoning, and text-to-speech.
- `misty_controller.py` handles Misty REST/WebSocket robot control.
- `wake_word_listener.py` handles laptop-side "Hey Misty" wake-word detection.
- Misty cannot run modern inference herself; the laptop does that locally.

## Slide style

- Minimal text per slide.
- Strong headings.
- 3 to 5 short bullets max per slide.
- Speaker-friendly phrasing, not dense documentation.
- Occasional witty Misty-style subtitle or callout is fine, but keep it professional.
- Use placeholders like `[Photo of Misty here]`, `[FIRST Robotics photo here]`, or `[Simple architecture diagram placeholder]`.

## Deliver for each slide

- Slide title.
- Slide subtitle or hook.
- Main bullets.
- Optional visual placeholder.
- Brief speaker note.

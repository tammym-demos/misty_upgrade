# Misty Skills Backup

These skills were installed on Misty II (firmware v2.0.2.140) before cleanup.

## Why removed

The `faceDetection` skill had `StartupRules: ["Startup", "Robot"]` which caused it to
auto-start on every boot. It grabs the microphone and interferes with keyphrase
recognition, causing silent failures in the wake word pipeline.

Other skills with `Robot` startup rules were also removed to prevent similar conflicts.

## What's here

- `all_skills_metadata.json` — Full metadata for all 11 skills that were installed,
  including audio/image assets, parameters, and startup rules.

**Note:** Misty's REST API does not expose an endpoint to download skill JavaScript
source code. Only metadata is retrievable. The original JS files exist on Misty's
internal storage but are not accessible via API.

## Skills removed

| Skill | Reason |
|-------|--------|
| `faceDetection` | Auto-started on boot, grabbed mic, broke keyphrase |
| `kids` | Had `Robot` startup rule, could interfere |
| `mistycog` | Had `Robot` startup rule, could interfere |
| `MistyReads` | Had `Robot` startup rule, could interfere |
| `AnnounceKnownPerson` | Had `Robot` startup rule, could interfere |

## Restoring

Skills cannot be restored from metadata alone — they would need to be redeployed
from source. These were demo/experiment skills from earlier development and are
not needed for the current Foundry Local conversational AI pipeline.

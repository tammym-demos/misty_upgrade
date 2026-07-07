# Local face profiles (#125)

This directory holds **laptop-side** enrolled face profiles created by
`tools/enroll_face.py`. It replaces Misty's unreliable on-chip `/api/faces`
pipeline (see `docs/lessons-learned.md`).

## Privacy and git policy

- Profiles are stored as `<Name>.npz` files containing **only numeric face
  embeddings and metadata** (name, created timestamp, model name/version,
  sample count, embedding dimensions). **No source photos are ever stored.**
- Face embeddings are **biometric data**. They are kept **local only** and are
  **gitignored** (`data/face_profiles/*.npz`, `*.npy`). Do not commit them.
- This `README.md` is the only file in this directory that is tracked by git,
  so the directory exists on a fresh clone.

## Usage

```powershell
# Enroll (from the repo root)
python tools\enroll_face.py --name Tammy --source misty --misty-ip 10.0.0.15 --samples 10
python tools\enroll_face.py --name Tammy --source webcam --samples 10

# List / delete
python tools\enroll_face.py --list
python tools\enroll_face.py --delete Tammy

# Recognize (smoke test)
python tools\recognize_face.py --source webcam
```

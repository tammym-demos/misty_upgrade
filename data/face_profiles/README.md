# Local face profiles (#125)

This directory holds **laptop-side** enrolled face profiles created by
`tools/enroll_face.py`. It replaces Misty's unreliable on-chip `/api/faces`
pipeline (see `docs/lessons-learned.md`).

## Privacy and git policy

- Profiles are stored as `<Name>.npz` files containing face features and metadata.
  Ordinary CLI enrollment is embeddings-only. Explicit spoken photo enrollment,
  or CLI `--save-reference-photo`, additionally stores an **actual reference JPEG**
  in the archive's `reference_jpeg` byte array. This is not just a feature vector.
- Photos and face embeddings are **biometric data**. They are kept **local only** and are
  **gitignored** (`data/face_profiles/*.npz`, `*.npy`). Do not commit them.
- This `README.md` is the only file in this directory that is tracked by git,
  so the directory exists on a fresh clone.

## Usage

```powershell
# Enroll (from the repo root)
python tools\enroll_face.py --name Tammy --source misty --misty-ip 10.0.0.15 --samples 10
python tools\enroll_face.py --name Tammy --source webcam --samples 10
# Explicit photo opt-in; requires one consenting person throughout capture.
python tools\enroll_face.py --name Morgan --source misty --misty-ip 10.0.0.15 --samples 5 --save-reference-photo

# List / delete
python tools\enroll_face.py --list
python tools\enroll_face.py --delete Tammy

# Recognize (smoke test)
python tools\recognize_face.py --source webcam
```

With recognition enabled, say "Take a picture of this person and remember them.
Their name is Morgan." Photo enrollment refuses an existing name, multiple faces,
or inconsistent samples. Say "Forget the face named Morgan" to remove the whole
archive, including the photo. Deletion does not erase separately configured
personal memories. No reference image is saved on Misty or uploaded to the cloud.

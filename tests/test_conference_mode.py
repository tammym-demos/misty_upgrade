"""
Unit tests for Conference Mode (issue #128).

Validates the companion-side scripted-dialog logic without Misty hardware,
Foundry Local, or Windows audio:

- talk-script parsing produces every Misty cue in order with stable cue IDs,
  strips inline [cite: ...] markers, and captures slide + presenter context
- parsing the shipped talks/20260710-2.md yields the expected 9 ordered cues
- asset preparation generates/imports/reuses WAVs and writes a manifest
- manifest verification flags missing/zero-duration assets
- the control state machine supports enable/disable, next/replay/previous,
  jump-to-slide, pause/resume, silence-triggered auto-advance, run-auto
- scripted playback never invokes the LLM unless explicit fallback is enabled
- safe shutdown releases resources in the documented order and is idempotent
- a disabled controller is a strict no-op (normal behavior unchanged when off)
"""

import io
import os
import sys
import types
import wave

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration"))

import conference_mode as cm

from conference_mode import (
    ConferenceAssetMissing,
    ConferenceController,
    ConferenceManifest,
    ConferenceStatus,
    ScriptParseError,
    ShutdownHooks,
    parse_script,
    prepare_assets,
    verify_manifest,
    wav_duration,
)

REAL_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "talks", "20260710-2.md"
)

FIXTURE = """\
# Presentation Script

### **Slide 1: Intro Slide**
*Visual: title.*[cite: 2]

**[You]:** Welcome everyone to the show.[cite: 1]

**[Misty]:** Hello   humans.[cite: 1] I am awake.[cite: 1, 2]

---

### **Slide 2: The Middle**

**[You]:** Now for the main event.

**[Misty]:** First Misty line on slide two.

**[You]:** Keep going.

**[Misty]:** Second Misty line on slide two.

---

### **Slide 3 & 4: Combo**

**[Misty]:** Final line.[cite: 9]
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_wav_bytes(seconds: float = 0.1, rate: int = 8000) -> bytes:
    """Return a valid mono 16-bit PCM WAV of the requested duration."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class FakeTts:
    def __init__(self):
        self.calls = []

    def __call__(self, text: str) -> bytes:
        self.calls.append(text)
        return make_wav_bytes(0.2)


class PlayRecorder:
    def __init__(self):
        self.played = []

    def __call__(self, asset):
        self.played.append(asset.cue_id)
        return asset.duration_s


def build_ready_controller(tmp_path, **kwargs):
    """Parse the fixture, prepare real WAVs, and return (controller, recorder)."""
    script = parse_script(FIXTURE, is_text=True)
    manifest = prepare_assets(script, str(tmp_path / "assets"), FakeTts())
    recorder = PlayRecorder()
    controller = ConferenceController(manifest, recorder, enabled=True, **kwargs)
    return controller, recorder, manifest


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_parse_fixture_cue_count_order_and_ids():
    script = parse_script(FIXTURE, is_text=True)
    assert [c.cue_id for c in script.cues] == [
        "slide01-misty01",
        "slide02-misty01",
        "slide02-misty02",
        "slide03-misty01",
    ]
    assert [c.order for c in script.cues] == [1, 2, 3, 4]


def test_parse_strips_cites_and_collapses_whitespace():
    script = parse_script(FIXTURE, is_text=True)
    first = script.cues[0]
    assert first.text == "Hello humans. I am awake."
    assert "cite" not in first.text
    assert "  " not in first.text


def test_parse_captures_slide_and_presenter_context():
    script = parse_script(FIXTURE, is_text=True)
    slide2_first = script.cues[1]
    assert slide2_first.slide_seq == 2
    assert slide2_first.slide_title == "The Middle"
    assert slide2_first.preceding_presenter == "Now for the main event."
    # Second Misty line on slide 2 sees the nearer presenter line.
    assert script.cues[2].preceding_presenter == "Keep going."


def test_parse_empty_script_raises():
    with pytest.raises(ScriptParseError):
        parse_script("# Nothing here\n\nJust prose.\n", is_text=True)


def test_parse_real_shipped_script_yields_nine_ordered_cues():
    if not os.path.isfile(REAL_SCRIPT):
        pytest.skip("shipped talks/20260710-2.md not present")
    script = parse_script(REAL_SCRIPT)
    assert len(script.cues) == 9
    assert [c.order for c in script.cues] == list(range(1, 10))
    # Slide 4 contributes two consecutive Misty cues.
    assert "slide04-misty01" in {c.cue_id for c in script.cues}
    assert "slide04-misty02" in {c.cue_id for c in script.cues}
    # First and last known lines parse in order.
    assert script.cues[0].text.startswith("Hello, creators at Rockwell.")
    assert script.cues[-1].text.startswith("To the developers in the room")
    # Cue IDs are unique and stable.
    ids = [c.cue_id for c in script.cues]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# preparation + manifest
# ---------------------------------------------------------------------------


def test_prepare_generates_wavs_and_manifest(tmp_path):
    script = parse_script(FIXTURE, is_text=True)
    tts = FakeTts()
    manifest = prepare_assets(script, str(tmp_path), tts)
    assert len(manifest.cues) == 4
    assert len(tts.calls) == 4
    for asset in manifest.cues:
        assert asset.asset_source == "generated"
        assert os.path.isfile(asset.wav_path)
        assert asset.duration_s > 0
        assert asset.misty_filename == f"conf_{asset.cue_id}.wav"
    assert verify_manifest(manifest) == []


def test_prepare_default_misty_prefix_uses_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "CONFERENCE_MISTY_FILENAME_PREFIX", "stage_")

    manifest = prepare_assets(parse_script(FIXTURE, is_text=True), str(tmp_path), FakeTts())

    assert manifest.cues[0].misty_filename == "stage_slide01-misty01.wav"


def test_prepare_reuses_unchanged_and_regenerates_on_no_reuse(tmp_path):
    script = parse_script(FIXTURE, is_text=True)
    out = str(tmp_path)
    tts1 = FakeTts()
    prepare_assets(script, out, tts1)
    assert len(tts1.calls) == 4

    # Second run with reuse should regenerate nothing (hashes match).
    tts2 = FakeTts()
    prepare_assets(script, out, tts2, reuse=True)
    assert tts2.calls == []

    # no_reuse forces regeneration of every cue.
    tts3 = FakeTts()
    prepare_assets(script, out, tts3, reuse=False)
    assert len(tts3.calls) == 4


def test_prepare_regenerates_when_text_hash_changes(tmp_path):
    out = str(tmp_path)
    prepare_assets(parse_script(FIXTURE, is_text=True), out, FakeTts())
    # Change the first cue's text; only that cue must be regenerated.
    changed = FIXTURE.replace("Hello   humans.", "Hello altered humans.")
    tts = FakeTts()
    prepare_assets(parse_script(changed, is_text=True), out, tts, reuse=True)
    assert tts.calls == ["Hello altered humans. I am awake."]


def test_prepare_prefers_recorded_override(tmp_path):
    script = parse_script(FIXTURE, is_text=True)
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    (recorded / "slide01-misty01.wav").write_bytes(make_wav_bytes(0.5))
    tts = FakeTts()
    manifest = prepare_assets(
        script, str(tmp_path / "gen"), tts, recorded_dir=str(recorded)
    )
    by_id = {a.cue_id: a for a in manifest.cues}
    assert by_id["slide01-misty01"].asset_source == "recorded"
    assert "slide01-misty01" not in tts.calls  # not synthesized
    assert by_id["slide02-misty01"].asset_source == "generated"


def test_manifest_round_trip(tmp_path):
    script = parse_script(FIXTURE, is_text=True)
    manifest = prepare_assets(script, str(tmp_path / "a"), FakeTts())
    path = str(tmp_path / "manifest.json")
    manifest.save(path)
    loaded = ConferenceManifest.load(path)
    assert [c.cue_id for c in loaded.cues] == [c.cue_id for c in manifest.cues]
    assert loaded.cues[0].wav_path == manifest.cues[0].wav_path


def test_verify_manifest_flags_missing_and_unreadable(tmp_path):
    script = parse_script(FIXTURE, is_text=True)
    manifest = prepare_assets(script, str(tmp_path), FakeTts())
    manifest.cues[0].wav_path = str(tmp_path / "does-not-exist.wav")
    # A file that exists but is not a valid/non-empty WAV must still be flagged,
    # even though the manifest recorded a positive duration when it was written.
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    manifest.cues[1].wav_path = str(empty)
    problems = verify_manifest(manifest)
    assert any("does-not-exist" in p for p in problems)
    assert any("non-positive duration" in p for p in problems)


def test_verify_manifest_allows_missing_audio_only_for_explicit_fallback(tmp_path):
    script = parse_script(FIXTURE, is_text=True)
    manifest = prepare_assets(script, str(tmp_path), FakeTts())
    manifest.cues[0].wav_path = str(tmp_path / "does-not-exist.wav")

    assert verify_manifest(manifest) != []
    assert verify_manifest(manifest, allow_audio_fallback=True) == []


def test_wav_duration_roundtrip():
    assert wav_duration(make_wav_bytes(0.5, rate=8000)) == pytest.approx(0.5, abs=0.02)
    assert wav_duration(b"not a wav") == 0.0


# ---------------------------------------------------------------------------
# control state machine
# ---------------------------------------------------------------------------


def test_disabled_controller_is_a_noop(tmp_path):
    controller, recorder, _ = build_ready_controller(tmp_path)
    controller.disable()
    assert controller.start() is False
    assert controller.play_next() is None
    assert controller.replay() is None
    assert recorder.played == []


def test_playback_requires_explicit_start(tmp_path):
    # Enabled but never armed: IDLE is inert until start() is called.
    controller, recorder, _ = build_ready_controller(tmp_path)
    assert controller.status is ConferenceStatus.IDLE
    assert controller.play_next() is None
    assert recorder.played == []
    assert controller.start() is True
    assert controller.play_next().cue_id == "slide01-misty01"


def test_play_next_advances_in_order(tmp_path):
    controller, recorder, _ = build_ready_controller(tmp_path)
    assert controller.start() is True
    cues = []
    while True:
        cue = controller.play_next()
        if cue is None:
            break
        cues.append(cue.cue_id)
    assert cues == [
        "slide01-misty01",
        "slide02-misty01",
        "slide02-misty02",
        "slide03-misty01",
    ]
    assert recorder.played == cues
    assert controller.play_count == 4
    assert controller.remaining() == 0


def test_replay_and_previous(tmp_path):
    controller, recorder, _ = build_ready_controller(tmp_path)
    controller.start()
    controller.play_next()  # slide01
    controller.play_next()  # slide02-misty01
    controller.replay()     # slide02-misty01 again
    assert recorder.played[-1] == "slide02-misty01"
    controller.previous()   # back to slide01
    assert recorder.played[-1] == "slide01-misty01"


def test_jump_to_slide(tmp_path):
    controller, recorder, _ = build_ready_controller(tmp_path)
    controller.start()
    target = controller.jump_to_slide(2)
    assert target.cue_id == "slide02-misty01"
    played = controller.play_next()
    assert played.cue_id == "slide02-misty01"
    # jump by label substring also works
    assert controller.jump_to_slide("middle").slide_seq == 2


def test_auto_advance_plays_next_when_presenter_finishes(tmp_path):
    controller, recorder, _ = build_ready_controller(
        tmp_path, wait_for_presenter_fn=lambda: True
    )
    controller.start()
    cue = controller.auto_advance_once()
    assert cue.cue_id == "slide01-misty01"
    assert recorder.played == ["slide01-misty01"]


def test_auto_advance_holds_when_presenter_not_finished(tmp_path):
    controller, recorder, _ = build_ready_controller(
        tmp_path, wait_for_presenter_fn=lambda: False
    )
    controller.start()
    assert controller.auto_advance_once() is None
    assert recorder.played == []


def test_auto_advance_respects_pause_manual_override(tmp_path):
    controller, recorder, _ = build_ready_controller(
        tmp_path, wait_for_presenter_fn=lambda: True
    )
    controller.start()
    controller.pause()
    assert controller.auto_advance_once() is None
    assert recorder.played == []
    # Manual next still works while paused (stage override).
    assert controller.play_next().cue_id == "slide01-misty01"
    controller.resume()
    assert controller.auto_advance_once().cue_id == "slide02-misty01"


def test_run_auto_plays_through(tmp_path):
    controller, recorder, _ = build_ready_controller(
        tmp_path, wait_for_presenter_fn=lambda: True
    )
    controller.start()
    played = controller.run_auto()
    assert played == 4
    assert len(recorder.played) == 4


def test_presenter_wait_passes_silence_setting_and_yields_on_timeout():
    class Listener:
        def __init__(self):
            self.kwargs = None
            self.stopped = False

        def start_speech_monitor(self, **kwargs):
            self.kwargs = kwargs

        def stop_speech_monitor(self):
            self.stopped = True

    listener = Listener()
    wait = cm._build_presenter_wait(listener, max_wait_s=0.01, silence_s=0.25)

    assert wait() is False
    assert listener.kwargs["min_duration"] == 0.25
    assert listener.kwargs["silence_duration"] == 0.25
    assert listener.kwargs["max_duration"] > 0.01
    assert listener.stopped is True


def test_presenter_wait_returns_true_only_when_monitor_signals_speech_end():
    class Listener:
        def start_speech_monitor(self, **kwargs):
            kwargs["on_speech_end"]()

        def stop_speech_monitor(self):
            pass

    wait = cm._build_presenter_wait(Listener(), max_wait_s=1.0, silence_s=0.25)
    assert wait() is True


# ---------------------------------------------------------------------------
# LLM bypass
# ---------------------------------------------------------------------------


def test_scripted_playback_never_calls_llm(tmp_path):
    llm_calls = []
    controller, recorder, _ = build_ready_controller(
        tmp_path,
        llm_fallback_fn=lambda text: llm_calls.append(text),
        use_llm_fallback=True,
    )
    controller.start()
    while controller.play_next() is not None:
        pass
    assert controller.llm_calls == 0
    assert llm_calls == []


def test_missing_asset_without_fallback_raises(tmp_path):
    controller, _, manifest = build_ready_controller(tmp_path)
    manifest.cues[0].wav_path = str(tmp_path / "missing.wav")
    controller.start()
    with pytest.raises(ConferenceAssetMissing):
        controller.play_next()


def test_missing_asset_with_explicit_fallback_uses_llm(tmp_path):
    llm_calls = []
    controller, recorder, manifest = build_ready_controller(
        tmp_path,
        llm_fallback_fn=lambda text: llm_calls.append(text),
        use_llm_fallback=True,
    )
    manifest.cues[0].wav_path = str(tmp_path / "missing.wav")
    controller.start()
    controller.play_next()
    assert controller.llm_calls == 1
    assert len(llm_calls) == 1
    assert recorder.played == []  # LLM path, not scripted playback


def test_live_controller_wires_llm_fallback_flag(tmp_path, monkeypatch):
    _, _, manifest = build_ready_controller(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest.cues[0].wav_path = str(tmp_path / "missing.wav")
    manifest.save(str(manifest_path))

    class FakeRobot:
        def __init__(self):
            self.played = []

        def upload_and_play_audio(self, wav_bytes, filename):
            self.played.append((wav_bytes, filename))
            return 0.0

        def stop_recording(self):
            pass

        def _cancel_all_skills(self):
            pass

        def halt(self):
            pass

        def move_head(self, **kwargs):
            pass

    robot = FakeRobot()
    monkeypatch.setitem(
        sys.modules,
        "misty_controller",
        types.SimpleNamespace(MistyController=lambda: robot),
    )
    monkeypatch.setattr(cm, "CONFERENCE_MODE_ENABLED", True)
    monkeypatch.setattr(cm, "CONFERENCE_LLM_FALLBACK", True)
    monkeypatch.setattr(cm, "http_tts", lambda url: lambda text: make_wav_bytes())

    controller = cm._build_live_controller(
        types.SimpleNamespace(manifest=str(manifest_path), auto=False)
    )

    controller.start()
    controller.play_next()

    assert controller.use_llm_fallback is True
    assert controller.llm_calls == 1
    assert robot.played[0][1] == "conference_fallback.wav"


def test_live_controller_rejects_missing_manifest_without_fallback(tmp_path, monkeypatch):
    _, _, manifest = build_ready_controller(tmp_path)
    manifest.cues[0].wav_path = str(tmp_path / "missing.wav")
    manifest_path = tmp_path / "manifest.json"
    manifest.save(str(manifest_path))

    monkeypatch.setitem(
        sys.modules,
        "misty_controller",
        types.SimpleNamespace(MistyController=lambda: object()),
    )
    monkeypatch.setattr(cm, "CONFERENCE_LLM_FALLBACK", False)

    with pytest.raises(cm.ConferenceError, match="Manifest is not showtime-ready"):
        cm._build_live_controller(types.SimpleNamespace(manifest=str(manifest_path), auto=False))


# ---------------------------------------------------------------------------
# safe shutdown
# ---------------------------------------------------------------------------


def test_shutdown_invokes_hooks_in_order_and_is_idempotent(tmp_path):
    order = []
    hooks = ShutdownHooks(
        release_audio=lambda: order.append("release_audio"),
        stop_recording=lambda: order.append("stop_recording"),
        cancel_skills=lambda: order.append("cancel_skills"),
        halt_movement=lambda: order.append("halt_movement"),
        rest_state=lambda: order.append("rest_state"),
    )
    controller, _, _ = build_ready_controller(tmp_path, shutdown_hooks=hooks)
    controller.start()
    invoked = controller.shutdown()
    assert invoked == [
        "release_audio",
        "stop_recording",
        "cancel_skills",
        "halt_movement",
        "rest_state",
    ]
    assert order == invoked
    assert controller.status == ConferenceStatus.STOPPED
    # Idempotent: a second shutdown does not re-run hooks.
    assert controller.shutdown() == []
    assert order == [
        "release_audio",
        "stop_recording",
        "cancel_skills",
        "halt_movement",
        "rest_state",
    ]


def test_shutdown_isolates_failing_hook(tmp_path):
    order = []

    def boom():
        raise RuntimeError("audio device busy")

    hooks = ShutdownHooks(
        release_audio=boom,
        stop_recording=lambda: order.append("stop_recording"),
        rest_state=lambda: order.append("rest_state"),
    )
    controller, _, _ = build_ready_controller(tmp_path, shutdown_hooks=hooks)
    controller.start()
    invoked = controller.shutdown()
    # The failing hook is skipped but later safety hooks still run.
    assert invoked == ["stop_recording", "rest_state"]
    assert order == ["stop_recording", "rest_state"]


def test_stopped_controller_stops_playback(tmp_path):
    controller, recorder, _ = build_ready_controller(tmp_path)
    controller.start()
    controller.shutdown()
    assert controller.play_next() is None
    assert recorder.played == []

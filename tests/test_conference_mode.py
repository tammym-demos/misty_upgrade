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
- scripted playback never invokes the LLM; explicit fallback uses scripted TTS
- safe shutdown releases resources in the documented order and is idempotent
- a disabled controller is a strict no-op (normal behavior unchanged when off)
"""

import io
import os
import sys
import threading
import types
import wave

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration"))

import conference_mode as cm

from conference_mode import (
    ConferenceAssetMissing,
    ConferenceController,
    ConferenceManifest,
    ConferencePreparationError,
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

**[Misty]:** Hello   humans.[cite: 1] I am awake.[cite: 1, 2] [wave] [face:happy]

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
    kwargs.setdefault("sleep_fn", lambda seconds: None)
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


def test_parse_does_not_bleed_presenter_context_across_slides():
    text = """\
### **Slide 1: First**
**[You]:** Closing line from slide one.
### **Slide 2: Second**
**[Misty]:** First line on slide two.
"""

    script = parse_script(text, is_text=True)

    assert script.cues[0].preceding_presenter == ""


def test_parse_empty_script_raises():
    with pytest.raises(ScriptParseError):
        parse_script("# Nothing here\n\nJust prose.\n", is_text=True)


def test_parse_real_shipped_script_yields_nine_ordered_cues(monkeypatch):
    if not os.path.isfile(REAL_SCRIPT):
        pytest.skip("shipped talks/20260710-2.md not present")
    monkeypatch.setattr(cm, "CONFERENCE_VARS", "customer=Rockwell,event=Hackathon")
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


def test_dry_run_prints_no_slide_cues_with_slide_zero():
    script = parse_script("**[Misty]:** Before any slide.", is_text=True)
    stream = io.StringIO()

    cm._print_cue_plan(script, stream=stream)

    assert "(Slide 00: (no slide))" in stream.getvalue()


def test_resolve_variables_basic():
    assert (
        cm.resolve_variables(
            "Hello, {{customer}} at {{event}}!",
            {"customer": "Contoso", "event": "Hackathon"},
        )
        == "Hello, Contoso at Hackathon!"
    )


def test_resolve_variables_missing_raises():
    with pytest.raises(ScriptParseError, match="customer, event"):
        cm.resolve_variables("Hello, {{customer}} at {{event}}!", {})


def test_parse_annotations_extracts_gestures():
    spoken, movements = cm.parse_annotations(
        "Hello there. [wave] [head:10,5,0] [face:happy]"
    )

    assert spoken == "Hello there."
    assert movements == [
        cm.GESTURE_LIBRARY["wave"],
        {"head": [10.0, 5.0, 0.0]},
        {"face": "face_talking_happy.gif"},
    ]


def test_parse_annotations_strips_from_text():
    script = parse_script(
        "**[Misty]:** Ready to go. [thinking] [arms:-30,0]",
        is_text=True,
    )

    assert script.cues[0].text == "Ready to go."
    assert "[" not in script.cues[0].text


def test_gesture_library_lookup():
    _, movements = cm.parse_annotations("Thinking... [thinking]")
    assert movements == [cm.GESTURE_LIBRARY["thinking"]]


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


def test_prepare_regenerates_corrupted_cached_wav(tmp_path):
    script = parse_script(FIXTURE, is_text=True)
    out = str(tmp_path)
    prepare_assets(script, out, FakeTts())
    (tmp_path / "slide01-misty01.wav").write_bytes(b"")

    tts = FakeTts()
    manifest = prepare_assets(script, out, tts, reuse=True)

    assert tts.calls == ["Hello humans. I am awake."]
    assert manifest.cues[0].duration_s > 0
    assert verify_manifest(manifest) == []


def test_prepare_rejects_unplayable_tts_audio(tmp_path):
    script = parse_script(FIXTURE, is_text=True)

    with pytest.raises(ConferencePreparationError, match="not a playable WAV"):
        prepare_assets(script, str(tmp_path), lambda text: b"not a wav")


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
    assert by_id["slide01-misty01"].text not in tts.calls  # not synthesized
    assert by_id["slide02-misty01"].asset_source == "generated"


def test_manifest_round_trip(tmp_path):
    script = parse_script(FIXTURE, is_text=True)
    manifest = prepare_assets(script, str(tmp_path / "a"), FakeTts())
    path = str(tmp_path / "manifest.json")
    manifest.save(path)
    loaded = ConferenceManifest.load(path)
    assert [c.cue_id for c in loaded.cues] == [c.cue_id for c in manifest.cues]
    assert loaded.cues[0].wav_path == manifest.cues[0].wav_path


def test_movements_in_manifest(tmp_path):
    script = parse_script(FIXTURE, is_text=True)
    manifest = prepare_assets(script, str(tmp_path / "a"), FakeTts())

    assert manifest.cues[0].movements == [
        cm.GESTURE_LIBRARY["wave"],
        {"face": "face_talking_happy.gif"},
    ]
    assert manifest.cues[0].to_dict()["movements"] == manifest.cues[0].movements


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


def test_cmd_verify_honors_tts_fallback_readiness(tmp_path, monkeypatch):
    script = parse_script(FIXTURE, is_text=True)
    manifest = prepare_assets(script, str(tmp_path), FakeTts())
    manifest.cues[0].wav_path = str(tmp_path / "does-not-exist.wav")
    manifest_path = tmp_path / "manifest.json"
    manifest.save(str(manifest_path))

    monkeypatch.setattr(cm, "CONFERENCE_TTS_FALLBACK", True)

    assert cm._cmd_verify(types.SimpleNamespace(manifest=str(manifest_path))) == 0


def test_wav_duration_roundtrip():
    assert wav_duration(make_wav_bytes(0.5, rate=8000)) == pytest.approx(0.5, abs=0.02)
    assert wav_duration(b"not a wav") == 0.0


def test_dry_run_shows_movements():
    script = parse_script(FIXTURE, is_text=True)
    stream = io.StringIO()

    cm._print_cue_plan(script, stream=stream)

    output = stream.getvalue()
    assert "Movements:" in output
    assert "face=face_talking_happy.gif" in output


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


def test_disable_disarms_until_start_is_called_again(tmp_path):
    controller, recorder, _ = build_ready_controller(tmp_path)
    controller.start()
    controller.disable()
    controller.enable()

    assert controller.status is ConferenceStatus.IDLE
    assert controller.play_next() is None
    assert recorder.played == []
    assert controller.start() is True
    assert controller.play_next().cue_id == "slide01-misty01"


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
    assert (
        listener.kwargs["rms_threshold"]
        == cm.CONFERENCE_PRESENTER_RMS_THRESHOLD
    )
    assert listener.kwargs["max_duration"] > 0.01
    assert listener.stopped is True


def test_presenter_wait_returns_true_only_when_monitor_signals_speech_end():
    class Listener:
        speech_detected = True

        def start_speech_monitor(self, **kwargs):
            kwargs["on_speech_end"]()

        def stop_speech_monitor(self):
            pass

    wait = cm._build_presenter_wait(Listener(), max_wait_s=1.0, silence_s=0.25)
    assert wait() is True


def test_presenter_wait_does_not_advance_on_no_speech_timeout():
    class Listener:
        speech_detected = False

        def start_speech_monitor(self, **kwargs):
            kwargs["on_speech_end"]()

        def stop_speech_monitor(self):
            pass

    wait = cm._build_presenter_wait(Listener(), max_wait_s=1.0, silence_s=0.25)
    assert wait() is False


def test_presenter_wait_ambient_loop_cannot_overshoot_timeout(monkeypatch):
    class Listener:
        speech_detected = True

        def start_speech_monitor(self, **kwargs):
            threading.Timer(0.03, kwargs["on_speech_end"]).start()

        def stop_speech_monitor(self):
            pass

    monkeypatch.setattr(__import__("random"), "uniform", lambda _a, _b: 0.03)
    wait = cm._build_presenter_wait(
        Listener(),
        max_wait_s=0.01,
        silence_s=0.25,
        ambient_fn=lambda: None,
    )

    assert wait() is False


# ---------------------------------------------------------------------------
# LLM bypass / TTS fallback
# ---------------------------------------------------------------------------


def test_scripted_playback_never_calls_llm(tmp_path):
    fallback_calls = []
    controller, recorder, _ = build_ready_controller(
        tmp_path,
        tts_fallback_fn=lambda text: fallback_calls.append(text),
        use_tts_fallback=True,
    )
    controller.start()
    while controller.play_next() is not None:
        pass
    assert controller.tts_fallback_calls == 0
    assert fallback_calls == []


def test_missing_asset_without_fallback_raises(tmp_path):
    controller, _, manifest = build_ready_controller(tmp_path)
    manifest.cues[0].wav_path = str(tmp_path / "missing.wav")
    controller.start()
    with pytest.raises(ConferenceAssetMissing):
        controller.play_next()


def test_missing_asset_with_explicit_fallback_uses_tts(tmp_path):
    fallback_calls = []
    controller, recorder, manifest = build_ready_controller(
        tmp_path,
        tts_fallback_fn=lambda text: fallback_calls.append(text),
        use_tts_fallback=True,
    )
    manifest.cues[0].wav_path = str(tmp_path / "missing.wav")
    controller.start()
    controller.play_next()
    assert controller.tts_fallback_calls == 1
    assert len(fallback_calls) == 1
    assert recorder.played == []  # TTS fallback path, not predetermined playback


def test_controller_calls_movement_fn(tmp_path):
    calls = []
    controller, _, _ = build_ready_controller(
        tmp_path,
        movement_fn=lambda movements: calls.append(movements),
    )

    controller.start()
    controller.play_next()

    assert calls == [
        [cm.GESTURE_LIBRARY["wave"], {"face": "face_talking_happy.gif"}]
    ]


def test_controller_restores_neutral(tmp_path):
    calls = []
    controller, _, _ = build_ready_controller(
        tmp_path,
        neutral_fn=lambda: calls.append("neutral"),
    )

    controller.start()
    controller.play_next()
    controller.play_next()

    assert calls == ["neutral", "neutral"]


def test_live_controller_wires_tts_fallback_flag_and_releases_listener(tmp_path, monkeypatch):
    _, _, manifest = build_ready_controller(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest.cues[0].wav_path = str(tmp_path / "missing.wav")
    manifest.save(str(manifest_path))

    class FakeListener:
        def __init__(self):
            self.speech_monitor_stopped = False
            self.stopped = False

        def stop_speech_monitor(self):
            self.speech_monitor_stopped = True

        def stop(self):
            self.stopped = True

        def pause(self):
            pass

        def resume(self):
            pass

    class FakeRobot:
        def __init__(self):
            self.played = []
            self._wake_word_listener = FakeListener()

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
    monkeypatch.setattr(cm, "CONFERENCE_TTS_FALLBACK", True)
    monkeypatch.setattr(cm, "http_tts", lambda url: lambda text: make_wav_bytes())

    controller = cm._build_live_controller(
        types.SimpleNamespace(manifest=str(manifest_path), auto=False)
    )

    controller.start()
    controller.play_next()
    controller.shutdown()

    assert controller.use_tts_fallback is True
    assert controller.tts_fallback_calls == 1
    assert robot.played[0][1] == "conference_fallback.wav"
    assert robot._wake_word_listener.speech_monitor_stopped is True
    assert robot._wake_word_listener.stopped is True


def test_cmd_run_disabled_does_not_touch_hardware(tmp_path, monkeypatch, capsys):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")

    def fail_build(args):
        raise AssertionError("disabled run should not build live controller")

    monkeypatch.setattr(cm, "CONFERENCE_MODE_ENABLED", False)
    monkeypatch.setattr(cm, "_build_live_controller", fail_build)

    assert cm._cmd_run(types.SimpleNamespace(manifest=str(manifest_path), auto=False)) == 2
    assert "Conference Mode is disabled" in capsys.readouterr().out


def test_cmd_run_auto_key_is_safe_when_auto_unavailable(monkeypatch, capsys):
    class FakeController:
        def __init__(self):
            self.status = ConferenceStatus.RUNNING
            self.auto_advance_available = False
            self.shutdown_called = False

        def start(self):
            return True

        def run_auto(self):
            raise AssertionError("manual runner must not call run_auto without VAD")

        def shutdown(self):
            self.shutdown_called = True
            self.status = ConferenceStatus.STOPPED

    controller = FakeController()
    commands = iter(["a", "s"])

    monkeypatch.setattr(cm, "CONFERENCE_MODE_ENABLED", True)
    monkeypatch.setattr(cm, "_build_live_controller", lambda args: controller)
    monkeypatch.setattr("builtins.input", lambda prompt: next(commands))

    assert cm._cmd_run(types.SimpleNamespace(manifest="manifest.json", auto=False)) == 0
    output = capsys.readouterr().out
    assert "Auto-advance is unavailable" in output
    assert controller.shutdown_called is True


def test_cmd_run_keyboard_interrupt_exits_cleanly(monkeypatch, capsys):
    class FakeController:
        def __init__(self):
            self.status = ConferenceStatus.RUNNING
            self.shutdown_called = False

        def start(self):
            return True

        def shutdown(self):
            self.shutdown_called = True
            self.status = ConferenceStatus.STOPPED

    controller = FakeController()

    monkeypatch.setattr(cm, "CONFERENCE_MODE_ENABLED", True)
    monkeypatch.setattr(cm, "_build_live_controller", lambda args: controller)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert cm._cmd_run(types.SimpleNamespace(manifest="manifest.json", auto=False)) == 0
    assert controller.shutdown_called is True
    assert "Conference Mode stopped" in capsys.readouterr().out


def test_live_controller_rejects_missing_manifest_without_fallback(tmp_path, monkeypatch):
    _, _, manifest = build_ready_controller(tmp_path)
    manifest.cues[0].wav_path = str(tmp_path / "missing.wav")
    manifest_path = tmp_path / "manifest.json"
    manifest.save(str(manifest_path))

    monkeypatch.setitem(
        sys.modules,
        "misty_controller",
        types.SimpleNamespace(MistyController=object),
    )
    monkeypatch.setattr(cm, "CONFERENCE_TTS_FALLBACK", False)

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


# ---------------------------------------------------------------------------
# talking face, presenter glance, and chaining
# ---------------------------------------------------------------------------


def test_talking_face_always_applied_during_playback(tmp_path):
    """The talking face is always shown during playback, even with face annotations."""
    face_calls = []
    controller, _, _ = build_ready_controller(
        tmp_path,
        face_fn=lambda f: face_calls.append(f),
    )
    controller.start()
    controller.play_next()  # slide01-misty01 has [wave][face:happy]
    controller.play_next()  # slide02-misty01 has no annotations
    # Both cues get the talking face twice (before + after play) to minimize
    # the firmware face-reset flash during audio start.
    assert face_calls == [
        "face_talking_happy.gif", "face_talking_happy.gif",  # cue 1: before + after
        "face_talking_happy.gif", "face_talking_happy.gif",  # cue 2: before + after
    ]


def test_explicit_cue_face_persists_through_playback(tmp_path):
    face_calls = []
    controller, _, _ = build_ready_controller(
        tmp_path,
        face_fn=lambda filename: face_calls.append(filename),
    )
    controller.manifest.cues[0].movements = [
        {"face": "face_talking_contempt.gif"}
    ]

    controller.start()
    controller.play_next()

    assert face_calls == [
        "face_talking_contempt.gif",
        "face_talking_contempt.gif",
    ]


def test_active_face_is_reasserted_during_long_playback(tmp_path):
    _, _, manifest = build_ready_controller(tmp_path)
    face_calls = []
    controller = ConferenceController(
        manifest,
        lambda asset: 0.06,
        enabled=True,
        face_fn=lambda filename: face_calls.append(filename),
        talking_face="face_talking_happy.gif",
        sleep_fn=cm.time.sleep,
        face_refresh_interval_s=0.01,
    )

    controller.start()
    controller.play_next()

    assert len(face_calls) >= 3
    assert set(face_calls) == {"face_talking_happy.gif"}


@pytest.mark.parametrize("action", ["pause", "stop"])
def test_cancellation_during_slow_face_refresh_does_not_advance(tmp_path, monkeypatch, action):
    _, _, manifest = build_ready_controller(tmp_path)
    clock = [0.0]
    face_calls = []
    monkeypatch.setattr(cm.time, "monotonic", lambda: clock[0])

    def refresh_face(filename):
        face_calls.append(filename)
        if len(face_calls) == 3:
            getattr(controller, action)()
            clock[0] += 2.0

    controller = ConferenceController(
        manifest, lambda asset: 1.0, enabled=True,
        face_fn=refresh_face, sleep_fn=cm.time.sleep,
        face_refresh_interval_s=0.1,
    )

    def wait(seconds):
        clock[0] += seconds
        return controller._interrupt.is_set()

    monkeypatch.setattr(controller._interrupt, "wait", wait)
    controller.start()
    assert controller.play_next() is None
    assert len(face_calls) == 3
    assert controller.play_count == 0
    assert controller.peek_next() == manifest.cues[0]
    if action == "pause":
        controller.resume()
        assert controller.play_next() == manifest.cues[0]


def test_glance_fn_called_during_auto_advance(tmp_path):
    """Glance function is called while waiting for presenter to finish."""
    glance_calls = []
    controller, _, _ = build_ready_controller(
        tmp_path,
        wait_for_presenter_fn=lambda: True,
        glance_fn=lambda: glance_calls.append("glance"),
    )
    controller.start()
    controller.auto_advance_once()
    assert glance_calls == ["glance"]


def test_glance_fn_not_called_during_manual_next(tmp_path):
    """Manual play_next does not glance — only auto-advance does."""
    glance_calls = []
    controller, _, _ = build_ready_controller(
        tmp_path,
        glance_fn=lambda: glance_calls.append("glance"),
    )
    controller.start()
    controller.play_next()
    assert glance_calls == []


def test_chained_gesture_merges_movements():
    """[talking+excited] merges both gesture dicts (last wins on conflict)."""
    text, movements = cm.parse_annotations("Hello! [talking+excited]")
    assert text == "Hello!"
    assert len(movements) == 1
    merged = movements[0]
    # excited provides arms and face; talking face is overwritten by excited's
    assert merged["arms"] == [-40, -40]
    assert merged["face"] == "face_talking_excited.gif"


def test_chained_gesture_last_wins_on_conflict():
    """Later gesture in chain overwrites earlier for same key."""
    text, movements = cm.parse_annotations("Hi [wave+shrug]")
    assert text == "Hi"
    merged = movements[0]
    # shrug arms overwrite wave arms
    assert merged["arms"] == [-20, -20]
    # wave face remains (shrug has no face)
    assert merged["face"] == "face_talking_happy.gif"


def test_chained_unknown_gesture_raises():
    """Unknown gesture in chain raises ScriptParseError."""
    with pytest.raises(cm.ScriptParseError, match="component.*dance"):
        cm.parse_annotations("Hi [dance+wave]")


def test_chained_parameterized_annotations():
    text, movements = cm.parse_annotations(
        "Hello [glance+face:happy] [shrug+head:0,10,0] "
        "[excited+arms:20,20]"
    )

    assert text == "Hello"
    assert movements == [
        {
            "head_motion": "glance_presenter",
            "face": "face_talking_happy.gif",
        },
        {"arms": [-20, -20], "head": [0.0, 10.0, 0.0]},
        {"arms": [20.0, 20.0], "face": "face_talking_excited.gif"},
    ]


@pytest.mark.parametrize("annotation,head,arms", [
    ("head:0,+10,0", [0.0, 10.0, 0.0], None),
    ("head:0,1e+1,0", [0.0, 10.0, 0.0], None),
    ("nod+arms:+45,+45", None, [45.0, 45.0]),
    ("head:0,+10,0+arms:+4e+1,+45", [0.0, 10.0, 0.0], [40.0, 45.0]),
])
def test_annotation_chaining_preserves_numeric_signs_and_exponents(annotation, head, arms):
    text, movements = cm.parse_annotations(f"Hello [{annotation}]")
    assert text == "Hello"
    assert len(movements) == 1
    if head is not None:
        assert movements[0]["head"] == head
    if arms is not None:
        assert movements[0]["arms"] == arms


def test_talking_gesture_resolves_sentinel_at_runtime(tmp_path):
    """The __talking__ face sentinel is resolved to the configured talking face."""
    movement_calls = []
    controller, _, _ = build_ready_controller(
        tmp_path,
        movement_fn=lambda m: movement_calls.append(m),
    )
    # Manually inject a talking annotation into the first cue
    controller.manifest.cues[1].movements = [{"face": "__talking__"}]
    controller.start()
    controller.play_next()  # cue 0
    controller.play_next()  # cue 1 with __talking__ sentinel
    # The second call should have resolved __talking__ to the configured face
    assert movement_calls[1] == [{"face": "face_talking_happy.gif"}]


def test_glance_gesture_parsed():
    """[glance] is parsed as a head_motion: glance_presenter."""
    text, movements = cm.parse_annotations("Interesting. [glance]")
    assert text == "Interesting."
    assert movements == [{"head_motion": "glance_presenter"}]


def test_presenter_side_config_default():
    """CONFERENCE_PRESENTER_SIDE defaults to 'right'."""
    import config_defaults
    assert config_defaults.CONFERENCE_PRESENTER_SIDE == "right"


def test_conference_audio_output_defaults_to_misty():
    import config_defaults

    assert config_defaults.CONFERENCE_AUDIO_OUTPUT == "misty"
    assert config_defaults.CONFERENCE_LAPTOP_AUDIO_DEVICE == ""


def test_audio_output_validation():
    assert cm._normalize_audio_output(" MISTY ") == "misty"
    assert cm._normalize_audio_output("laptop") == "laptop"
    with pytest.raises(cm.ConferenceError, match="misty.*laptop"):
        cm._normalize_audio_output("both")


def test_parse_output_device():
    assert cm._parse_output_device("") is None
    assert cm._parse_output_device(" 7 ") == 7
    assert cm._parse_output_device("Conference Speakers") == "Conference Speakers"


def test_run_parser_accepts_laptop_audio_output():
    args = cm.build_parser().parse_args(["run", "--audio-output", "laptop"])

    assert args.audio_output == "laptop"


def test_laptop_audio_player_plays_file_bytes_and_stops(monkeypatch):
    calls = []

    fake_sd = types.SimpleNamespace(
        check_output_settings=lambda **kwargs: calls.append(("check", kwargs)),
        play=lambda samples, sample_rate, **kwargs: calls.append(
            ("play", samples, sample_rate, kwargs)
        ),
        stop=lambda: calls.append(("stop",)),
    )
    fake_sf = types.SimpleNamespace(
        read=lambda source, **kwargs: ([0.0] * 8, 8)
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)

    player = cm.LaptopAudioPlayer("Conference Speakers")

    assert player.play_file("cue.wav") == 1.0
    assert player.play_bytes(make_wav_bytes()) == 1.0
    player.stop()
    assert calls[0] == ("check", {"device": "Conference Speakers"})
    assert calls[-1] == ("stop",)
    assert len([call for call in calls if call[0] == "play"]) == 2


def test_long_tts_text_splits_and_rejoins_wavs():
    text = ("First sentence has useful words. " * 12) + (
        "Second sentence also has useful words. " * 12
    )

    chunks = cm._split_tts_text(text, max_chars=120)

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 120 for chunk in chunks)
    joined = cm._concatenate_wavs([make_wav_bytes(0.1), make_wav_bytes(0.2)])
    assert cm.wav_duration(joined) == pytest.approx(0.3, abs=0.01)


def test_live_controller_routes_audio_to_laptop(tmp_path, monkeypatch):
    _, _, manifest = build_ready_controller(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest.save(str(manifest_path))

    class FakeFaceAnimator:
        def __init__(self):
            self.stop_calls = 0

        def stop(self):
            self.stop_calls += 1

    class FakeRobot:
        def __init__(self):
            self.played = []
            self.halted = 0
            self.face_sync_calls = []
            self.displayed = []
            self.cancel_skills_calls = 0
            self._wake_word_listener = None
            self._face_animator = FakeFaceAnimator()

        def ensure_face_assets(self, *, overwrite=None):
            self.face_sync_calls.append(overwrite)
            return True

        def play_preloaded_audio(self, filename):
            self.played.append(filename)

        def stop_recording(self):
            pass

        def _cancel_all_skills(self):
            self.cancel_skills_calls += 1

        def display_image(self, filename):
            self.displayed.append(filename)

        def halt(self):
            self.halted += 1

        def move_head(self, **kwargs):
            pass

    class FakeLaptopPlayer:
        instances = []

        def __init__(self, device):
            self.device = device
            self.played = []
            self.stopped = 0
            self.__class__.instances.append(self)

        def play_file(self, wav_path):
            self.played.append(wav_path)
            return 0.0

        def play_bytes(self, wav_bytes, description=""):
            self.played.append(description or "bytes")
            return 0.0

        def stop(self):
            self.stopped += 1

    robot = FakeRobot()
    monkeypatch.setitem(
        sys.modules,
        "misty_controller",
        types.SimpleNamespace(MistyController=lambda: robot),
    )
    monkeypatch.setattr(cm, "CONFERENCE_MODE_ENABLED", True)
    monkeypatch.setattr(cm, "CONFERENCE_LAPTOP_AUDIO_DEVICE", "4")
    monkeypatch.setattr(cm, "LaptopAudioPlayer", FakeLaptopPlayer)

    controller = cm._build_live_controller(
        types.SimpleNamespace(
            manifest=str(manifest_path),
            auto=False,
            audio_output="laptop",
        )
    )
    controller.start()
    controller.play_next()
    controller.shutdown()

    player = FakeLaptopPlayer.instances[0]
    assert controller.audio_output == "laptop"
    assert player.device == 4
    assert player.played == [manifest.cues[0].wav_path]
    assert player.stopped >= 1
    assert robot.played == []
    assert robot.halted >= 1
    assert robot.face_sync_calls == [True]
    assert robot.cancel_skills_calls >= 1
    assert "face_talking_happy.gif" in robot.displayed
    assert robot._face_animator.stop_calls == 1

import importlib.util
import os


def load_probe_module():
    module_path = os.path.join(os.path.dirname(__file__), "..", "tools", "face_display_probe.py")
    spec = importlib.util.spec_from_file_location("face_display_probe", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frame_rate_uses_unrounded_duration_for_fps(monkeypatch):
    probe_mod = load_probe_module()
    probe = probe_mod.MistyDisplayProbe("127.0.0.1")
    probe._restore_default = lambda: None

    perf_counter_values = iter([0.0, 0.0004])
    monkeypatch.setattr(probe_mod.time, "perf_counter", lambda: next(perf_counter_values))
    monkeypatch.setattr(probe, "_display", lambda filename, timeout=None: 0.001)

    result = probe.test_frame_rate(num_frames=1)

    assert result.duration_s == 0.0
    assert result.achieved_fps == 2500.0


def test_audio_regression_uses_bounded_sleep_schedule(monkeypatch):
    probe_mod = load_probe_module()
    probe = probe_mod.MistyDisplayProbe("127.0.0.1")
    probe._restore_default = lambda: None

    class FakeClock:
        def __init__(self):
            self.t = 0.0
            self.sleep_calls = []

        def perf_counter(self):
            self.t += 0.01
            return self.t

        def sleep(self, duration):
            self.sleep_calls.append(duration)
            self.t += duration

    clock = FakeClock()
    monkeypatch.setattr(probe_mod.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(probe_mod.time, "sleep", clock.sleep)
    monkeypatch.setattr(probe_mod.requests, "post", lambda *args, **kwargs: None)

    def fail_display(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(probe, "_display", fail_display)

    result = probe.test_audio_regression(duration_s=0.03)

    assert any(0 < duration < 1.0 for duration in clock.sleep_calls)
    assert result.display_calls_during == 0


def test_restore_default_warns_on_non_fatal_failure(monkeypatch, capsys):
    probe_mod = load_probe_module()
    probe = probe_mod.MistyDisplayProbe("127.0.0.1")

    def fail_display(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(probe, "_display", fail_display)

    probe._restore_default()

    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "restoring default face" in captured.out

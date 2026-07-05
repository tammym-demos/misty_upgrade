"""
Unit tests for the centralized custom-face resolver and asset replacement (#116).

Covers:
- builtin_fallback_for_asset() mapping (custom asset -> firmware e_*.jpg)
- FaceAnimator.resolve_asset()/show_asset() with custom-available vs unavailable
- disabled-animation (USE_FACE_ANIMATION=false equivalent) custom identity and
  fallback still resolve/push correctly
- ensure_face_assets() overwrite vs missing sync mode behavior
"""

import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "windows-orchestration"))

from face_animator import (
    FaceAnimator,
    ASSET_BUILTIN_FALLBACK,
    DEFAULT_BUILTIN_FALLBACK,
    builtin_fallback_for_asset,
)
import config_defaults


class TestBuiltinFallbackForAsset:
    def test_every_required_asset_has_builtin_fallback(self):
        for asset in config_defaults.REQUIRED_FACE_ASSETS:
            fb = builtin_fallback_for_asset(asset)
            assert fb in ASSET_BUILTIN_FALLBACK.values()
            assert not fb.startswith("face_")

    def test_known_asset_maps_to_expected_builtin(self):
        assert builtin_fallback_for_asset("face_talking_sad.gif") == "e_Sadness.jpg"
        assert builtin_fallback_for_asset("face_idle.gif") == "e_DefaultContent.jpg"

    def test_unknown_custom_asset_uses_default_builtin(self):
        assert builtin_fallback_for_asset("face_unknown.gif") == DEFAULT_BUILTIN_FALLBACK

    def test_non_custom_asset_passes_through(self):
        # Already a firmware/built-in asset — displayed as-is.
        assert builtin_fallback_for_asset("e_Joy.jpg") == "e_Joy.jpg"

    def test_empty_filename_uses_default(self):
        assert builtin_fallback_for_asset("") == DEFAULT_BUILTIN_FALLBACK


class TestResolveAsset:
    def test_custom_available_returns_custom(self):
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        assert animator.custom_faces_available is True
        assert animator.resolve_asset("face_talking_happy.gif") == "face_talking_happy.gif"

    def test_custom_unavailable_returns_builtin(self):
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.set_custom_faces_available(False)
        assert animator.resolve_asset("face_talking_happy.gif") == "e_Joy.jpg"


class TestShowAsset:
    @patch("face_animator.requests.post")
    def test_show_asset_pushes_custom_when_available(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.show_asset("face_listening.png")
        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert filenames == ["face_listening.png"]

    @patch("face_animator.requests.post")
    def test_show_asset_pushes_builtin_when_unavailable(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.set_custom_faces_available(False)
        animator.show_asset("face_processing.gif")
        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert filenames == [ASSET_BUILTIN_FALLBACK["face_processing.gif"]]

    @patch("face_animator.requests.post")
    def test_show_asset_does_not_change_target_state(self, mock_post):
        """show_asset is for one-off displays and must not alter animation state."""
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.set_state("IDLE")
        animator.show_asset("face_talking_happy.gif")
        # Target state stays IDLE even though a talking face was shown one-off.
        assert animator._target_state == "IDLE"

    @patch("face_animator.requests.post")
    def test_show_asset_display_failure_triggers_builtin_fallback(self, mock_post):
        """A failed custom display flips availability and retries with a built-in."""
        def _resp(url, json=None, timeout=None):
            fn = json["FileName"]
            # Custom face_* pushes fail; built-in e_* pushes succeed.
            return MagicMock(status_code=500 if fn.startswith("face_") else 200)

        mock_post.side_effect = _resp
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        assert animator.custom_faces_available is True

        ok = animator.show_asset("face_talking_sad.gif")

        assert ok is True  # built-in fallback push succeeded
        # Availability flipped so subsequent resolves use built-ins.
        assert animator.custom_faces_available is False
        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert "face_talking_sad.gif" in filenames  # attempted custom first
        assert "e_Sadness.jpg" in filenames  # then built-in fallback


class TestDisabledIdentityAndFallback:
    """USE_FACE_ANIMATION=false equivalent: identity + fallback still work."""

    @patch("face_animator.requests.post")
    def test_disabled_emotion_identity_resolves(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)  # no thread
        animator.set_state("PLAYING", emotion="excited")
        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert "face_talking_excited.gif" in filenames

    @patch("face_animator.requests.post")
    def test_disabled_fallback_resolves(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        animator = FaceAnimator("http://10.0.0.23", enabled=False)
        animator.set_custom_faces_available(False)
        animator.set_state("PLAYING", emotion="excited")
        filenames = [c.kwargs["json"]["FileName"] for c in mock_post.call_args_list]
        assert "e_Joy2.jpg" in filenames  # excited built-in fallback
        assert not any(f.startswith("face_") for f in filenames)


class TestEnsureFaceAssetsSyncMode:
    """ensure_face_assets() respects missing vs overwrite sync mode (#116)."""

    def _make_controller(self):
        import misty_controller
        # Instantiate without connecting to hardware (run() is not called).
        ctrl = misty_controller.MistyController()
        return misty_controller, ctrl

    def test_missing_mode_skips_present_assets(self):
        mc, ctrl = self._make_controller()
        present = set(mc.REQUIRED_FACE_ASSETS)
        with patch.object(mc, "FACE_ASSETS_SYNC_MODE", "missing"), \
             patch.object(ctrl, "_get_misty_image_names", return_value=present), \
             patch.object(ctrl, "_upload_face_image", return_value=True) as up, \
             patch.object(os.path, "exists", return_value=True):
            ok = ctrl.ensure_face_assets()
        assert ok is True
        assert up.call_count == 0  # all present -> nothing uploaded

    def test_overwrite_mode_reuploads_all(self):
        mc, ctrl = self._make_controller()
        present = set(mc.REQUIRED_FACE_ASSETS)
        with patch.object(mc, "FACE_ASSETS_SYNC_MODE", "overwrite"), \
             patch.object(ctrl, "_get_misty_image_names", return_value=present) as inv, \
             patch.object(ctrl, "_upload_face_image", return_value=True) as up, \
             patch.object(os.path, "exists", return_value=True):
            ok = ctrl.ensure_face_assets()
        assert ok is True
        # Overwrite ignores inventory and re-uploads every required asset.
        assert up.call_count == len(mc.REQUIRED_FACE_ASSETS)
        assert inv.call_count == 0  # inventory not consulted in overwrite mode

    def test_missing_local_asset_triggers_fallback(self):
        mc, ctrl = self._make_controller()
        with patch.object(mc, "FACE_ASSETS_SYNC_MODE", "missing"), \
             patch.object(ctrl, "_get_misty_image_names", return_value=set()), \
             patch.object(ctrl, "_upload_face_image", return_value=True), \
             patch.object(os.path, "exists", return_value=False):
            ok = ctrl.ensure_face_assets()
        assert ok is False  # missing local files -> not fully available
        assert ctrl._face_animator.custom_faces_available is False

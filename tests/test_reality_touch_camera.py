# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_private_companion.wakeup_alarm import WakeupAlarmMixin


class CameraHarness(WakeupAlarmMixin):
    def __init__(self) -> None:
        self.data = {"users": {"u": {}}}
        self.enable_experimental_bluetooth_wakeup = True
        self.enable_reality_touch_camera = True
        self.reality_touch_camera_index = 0
        self.reality_touch_camera_min_interval_seconds = 60
        self.reality_touch_camera_capture_timeout_seconds = 5
        self.plugin_vision_provider_id = ""
        self.context = types.SimpleNamespace(get_provider_by_id=lambda _provider_id: None)
        self.save_count = 0

    def _save_data_sync(self) -> None:
        self.save_count += 1


class RealityTouchCameraConsentTests(unittest.TestCase):
    def test_audio_consent_does_not_grant_camera(self) -> None:
        harness = CameraHarness()
        user = {"reality_touch_consent": {"confirmed": True, "version": 1, "granted_capabilities": ["local_audio"]}}
        self.assertFalse(harness._reality_touch_camera_consented(user))

    def test_camera_requires_complete_manual_confirmation(self) -> None:
        harness = CameraHarness()
        user: dict = {}
        reply, requested = harness._reality_touch_camera_command(user, "摄像头确认 我同意使用摄像头")
        self.assertFalse(requested)
        self.assertIn("确认口令不正确", reply)
        self.assertNotIn("reality_touch_camera_consent", user)
        reply, requested = harness._reality_touch_camera_command(
            user, "摄像头确认 " + harness._REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT
        )
        self.assertFalse(requested)
        self.assertIn("独立授权已记录", reply)
        self.assertTrue(harness._reality_touch_camera_consented(user))
        self.assertEqual(["camera_single_frame"], user["reality_touch_camera_consent"]["granted_capabilities"])

    def test_camera_risk_prompt_then_bare_phrase_grants_pending_capability(self) -> None:
        harness = CameraHarness()
        user: dict = {}
        prompt, requested = harness._reality_touch_camera_command(user, "摄像头确认")
        self.assertFalse(requested)
        self.assertIn("我理解风险并确认授权", prompt)
        self.assertEqual("camera_single_frame", user["reality_touch_pending_consent"]["capability"])
        reply = harness._reality_touch_apply_pending_confirmation(user, "我理解风险并确认授权")
        self.assertIn("摄像头独立授权已记录", reply)
        self.assertTrue(harness._reality_touch_camera_consented(user))
        self.assertNotIn("reality_touch_pending_consent", user)

    def test_revoking_camera_preserves_audio_consent(self) -> None:
        harness = CameraHarness()
        user = {"reality_touch_consent": {"confirmed": True, "version": 1, "granted_capabilities": ["local_audio"]}}
        harness._reality_touch_camera_command(user, "摄像头确认 " + harness._REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT)
        reply, _ = harness._reality_touch_camera_command(user, "撤销摄像头授权")
        self.assertIn("本机音频授权不受影响", reply)
        self.assertIn("reality_touch_consent", user)
        self.assertNotIn("reality_touch_camera_consent", user)

    def test_sanitizer_drops_identity_and_free_text_fields(self) -> None:
        harness = CameraHarness()
        observation = harness._sanitize_reality_touch_camera_observation(
            {
                "presence": "present",
                "activity": "reading_screen_text",
                "interruptibility": "high",
                "brightness": "normal",
                "confidence": 4,
                "identity": "某位具体用户",
                "face": "可识别人脸",
                "reason": "房间与屏幕里的敏感细节",
            },
            local_brightness="normal",
            width=640,
            height=480,
            analyzed=True,
        )
        self.assertEqual("unknown", observation["activity"])
        self.assertEqual(1.0, observation["confidence"])
        self.assertNotIn("identity", observation)
        self.assertNotIn("face", observation)
        self.assertNotIn("reason", observation)


class RealityTouchCameraCaptureTests(unittest.TestCase):
    def test_capture_reads_one_frame_and_always_releases_device(self) -> None:
        harness = CameraHarness()

        class Frame:
            shape = (480, 640, 3)
            size = 480 * 640 * 3

            @staticmethod
            def mean() -> float:
                return 100.0

        class Capture:
            read_count = 0
            released = False

            @staticmethod
            def isOpened() -> bool:
                return True

            def read(self):
                self.read_count += 1
                return True, Frame()

            def release(self) -> None:
                self.released = True

        capture = Capture()
        fake_cv2 = types.SimpleNamespace(
            VideoCapture=lambda _index: capture,
            imencode=lambda *_args, **_kwargs: (True, bytearray(b"jpeg")),
            IMWRITE_JPEG_QUALITY=1,
        )
        with patch.dict(sys.modules, {"cv2": fake_cv2}):
            result = harness._capture_reality_touch_camera_frame()
        self.assertEqual(1, capture.read_count)
        self.assertTrue(capture.released)
        self.assertEqual(b"jpeg", result["jpeg_bytes"])

    def test_capture_failure_still_releases_device(self) -> None:
        harness = CameraHarness()

        class Capture:
            released = False

            @staticmethod
            def isOpened() -> bool:
                return True

            @staticmethod
            def read():
                return False, None

            def release(self) -> None:
                self.released = True

        capture = Capture()
        fake_cv2 = types.SimpleNamespace(VideoCapture=lambda _index: capture)
        with patch.dict(sys.modules, {"cv2": fake_cv2}):
            with self.assertRaisesRegex(RuntimeError, "未返回画面"):
                harness._capture_reality_touch_camera_frame()
        self.assertTrue(capture.released)


class RealityTouchCameraSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = CameraHarness()
        self.user = self.harness.data["users"]["u"]

    def grant(self) -> None:
        self.harness._reality_touch_camera_command(
            self.user, "摄像头确认 " + self.harness._REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT
        )

    async def test_global_switch_and_user_consent_are_both_required(self) -> None:
        self.harness.enable_reality_touch_camera = False
        result = await self.harness._reality_touch_camera_snapshot_for_user("u", "判断是否适合主动问候")
        self.assertEqual("disabled", result["status"])
        self.harness.enable_reality_touch_camera = True
        result = await self.harness._reality_touch_camera_snapshot_for_user("u", "判断是否适合主动问候")
        self.assertEqual("forbidden", result["status"])

    async def test_snapshot_returns_only_limited_state_and_enforces_cooldown(self) -> None:
        self.grant()
        self.harness._capture_reality_touch_camera_frame = lambda: {
            "jpeg_bytes": b"temporary-in-memory-frame", "width": 640, "height": 480, "brightness": "normal"
        }
        self.harness._analyze_reality_touch_camera_frame = AsyncMock(return_value={
            "presence": "present", "activity": "at_desk", "interruptibility": "medium",
            "brightness": "normal", "confidence": 0.8, "analyzed": True, "width": 640, "height": 480,
            "summary": "在场=present，活动=at_desk，可打扰性=medium，光线=normal",
        })
        result = await self.harness._reality_touch_camera_snapshot_for_user("u", "判断是否适合主动问候")
        self.assertEqual("success", result["status"])
        serialized = json.dumps(result, ensure_ascii=False)
        for forbidden in ("jpeg", "path", "identity", "face"):
            self.assertNotIn(forbidden, serialized.lower())
        second = await self.harness._reality_touch_camera_snapshot_for_user("u", "再次判断")
        self.assertEqual("cooldown", second["status"])
        self.assertGreater(second["retry_after"], 0)

    async def test_failed_capture_audit_contains_no_raw_frame(self) -> None:
        self.grant()

        def fail():
            raise RuntimeError("设备被占用")

        self.harness._capture_reality_touch_camera_frame = fail
        result = await self.harness._reality_touch_camera_snapshot_for_user("u", "手动检查设备")
        self.assertEqual("error", result["status"])
        latest = self.user["reality_touch_camera_policy"]["last_observation"]
        self.assertFalse(latest["success"])
        self.assertNotIn("jpeg_bytes", latest)
        self.assertNotIn("path", latest)


if __name__ == "__main__":
    unittest.main()

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
        self.owner_user_ids = {"u"}
        self.admin_user_ids: set[str] = set()
        self.enable_experimental_bluetooth_wakeup = True
        self.enable_reality_touch_camera = True
        self.reality_touch_camera_index = 0
        self.reality_touch_camera_min_interval_seconds = 60
        self.reality_touch_camera_capture_timeout_seconds = 5
        self.enable_reality_touch_camera_proactive_curiosity = False
        self.reality_touch_camera_proactive_min_tier = 4
        self.reality_touch_camera_proactive_max_daily = 1
        self.reality_touch_camera_proactive_cooldown_minutes = 240
        self.proactive_tier = 4
        self.plugin_vision_provider_id = ""
        self.context = types.SimpleNamespace(get_provider_by_id=lambda _provider_id: None)
        self.save_count = 0

    def _save_data_sync(self) -> None:
        self.save_count += 1

    def _permission_identity_id(self, user_id) -> str:
        value = str(user_id or "").strip()
        return value if value in self.data["users"] else ""

    def _is_configured_admin_user_id(self, user_id) -> bool:
        return self._permission_identity_id(user_id) in self.admin_user_ids

    def _relationship_owner_user_ids(self) -> set[str]:
        return set(self.owner_user_ids)

    def _proactive_quota_policy(self, _user) -> dict:
        return {"tier": self.proactive_tier, "label": f"L{self.proactive_tier}"}

    @staticmethod
    def _environment_today_key() -> str:
        return "2026-08-11"


class RealityTouchCameraConsentTests(unittest.TestCase):
    def test_camera_eligibility_does_not_inherit_target_or_proactive_permission(self) -> None:
        harness = CameraHarness()
        harness.owner_user_ids.clear()
        harness.target_user_ids = ["u"]
        harness.data["users"]["u"]["proactive_private_enabled"] = True
        self.assertFalse(harness._reality_touch_camera_user_eligible("u"))

    def test_camera_eligibility_accepts_admin_or_explicit_owner(self) -> None:
        harness = CameraHarness()
        self.assertTrue(harness._reality_touch_camera_user_eligible("u"))
        harness.owner_user_ids.clear()
        harness.admin_user_ids.add("u")
        self.assertTrue(harness._reality_touch_camera_user_eligible("u"))

    def test_ineligible_user_cannot_enable_camera_policy(self) -> None:
        harness = CameraHarness()
        harness.owner_user_ids.clear()
        user = harness.data["users"]["u"]
        user["reality_touch_camera_consent"] = {
            "confirmed": True,
            "version": 1,
            "granted_capabilities": ["camera_single_frame"],
        }
        with self.assertRaisesRegex(ValueError, "只允许 AstrBot 管理员或主要用户"):
            harness._reality_touch_update_camera_policy(user, {"camera_enabled": True}, user_id="u")

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
    def test_device_catalog_is_only_enumerated_on_explicit_refresh(self) -> None:
        harness = CameraHarness()
        enumerate_calls = 0

        class CameraInfo:
            index = 1400
            name = "FHD Webcam"

        def enumerate_cameras():
            nonlocal enumerate_calls
            enumerate_calls += 1
            return [CameraInfo()]

        fake_enumerator = types.SimpleNamespace(enumerate_cameras=enumerate_cameras)
        fake_cv2 = types.SimpleNamespace(
            videoio_registry=types.SimpleNamespace(getBackendName=lambda _code: "MSMF")
        )
        with patch.dict(sys.modules, {"cv2": fake_cv2, "cv2_enumerate_cameras": fake_enumerator}):
            self.assertEqual([], harness._reality_touch_camera_devices(refresh=False)["devices"])
            self.assertEqual(0, enumerate_calls)
            catalog = harness._reality_touch_camera_devices(refresh=True)
        self.assertEqual(1, enumerate_calls)
        self.assertEqual(1400, catalog["devices"][0]["index"])
        self.assertEqual("FHD Webcam", catalog["devices"][0]["name"])
        self.assertEqual("MSMF", catalog["devices"][0]["backend"])

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

    async def test_legacy_consent_cannot_bypass_current_camera_eligibility(self) -> None:
        self.grant()
        self.harness.owner_user_ids.clear()
        self.user["proactive_private_enabled"] = True
        snapshot = self.harness._reality_touch_camera_user_snapshot(self.user, user_id="u")
        self.assertFalse(snapshot["eligible"])
        self.assertFalse(snapshot["consented"])
        self.assertFalse(snapshot["enabled"])
        result = await self.harness._reality_touch_camera_snapshot_for_user("u", "测试历史授权边界")
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

    async def test_page_preview_is_opt_in_and_not_written_to_user_data(self) -> None:
        self.grant()
        self.harness._capture_reality_touch_camera_frame = lambda: {
            "jpeg_bytes": b"one-frame-preview",
            "width": 320,
            "height": 240,
            "brightness": "normal",
        }
        self.harness._analyze_reality_touch_camera_frame = AsyncMock(return_value={
            "presence": "uncertain", "activity": "unknown", "interruptibility": "unknown",
            "brightness": "normal", "confidence": 0.0, "analyzed": False,
            "width": 320, "height": 240, "summary": "有限状态不可确定",
        })
        result = await self.harness._reality_touch_camera_snapshot_for_user(
            "u",
            "管理员页面手动预览",
            include_preview=True,
        )
        self.assertTrue(result["preview_data_url"].startswith("data:image/jpeg;base64,"))
        persisted = json.dumps(self.user, ensure_ascii=False)
        self.assertNotIn("preview_data_url", persisted)
        self.assertNotIn("one-frame-preview", persisted)


class RealityTouchCameraProactiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = CameraHarness()
        self.user = self.harness.data["users"]["u"]
        self.user["user_id"] = "u"
        self.harness._reality_touch_camera_command(
            self.user,
            "摄像头确认 " + self.harness._REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT,
        )
        self.harness.enable_reality_touch_camera_proactive_curiosity = True

    def test_auto_mode_downgrades_to_ask_below_minimum_tier(self) -> None:
        self.user["reality_touch_camera_policy"]["proactive_mode"] = "auto"
        self.harness.proactive_tier = 3

        state = self.harness._reality_touch_camera_proactive_state(self.user, user_id="u")
        prompt = self.harness._reality_touch_camera_proactive_prompt(self.user, user_id="u")

        self.assertEqual("ask", state["effective_mode"])
        self.assertFalse(state["direct_allowed"])
        self.assertTrue(state["ask_allowed"])
        self.assertIn("不能调用摄像头工具", prompt)

    def test_auto_mode_allows_optional_direct_glance_at_matching_tier(self) -> None:
        self.user["reality_touch_camera_policy"]["proactive_mode"] = "auto"

        state = self.harness._reality_touch_camera_proactive_state(self.user, user_id="u")
        prompt = self.harness._reality_touch_camera_proactive_prompt(self.user, user_id="u")

        self.assertTrue(state["direct_allowed"])
        self.assertEqual(1, state["remaining_today"])
        self.assertIn("独立、低频的可选能力", prompt)
        self.assertIn("pc_reality_touch_camera_snapshot", prompt)
        self.assertIn("普通问候", prompt)

    def test_silence_disables_chain_but_zero_override_keeps_ask_mode(self) -> None:
        policy = self.user["reality_touch_camera_policy"]
        policy["proactive_mode"] = "auto"
        self.user["ignored_streak"] = 1
        state = self.harness._reality_touch_camera_proactive_state(self.user, user_id="u")
        self.assertFalse(state["ask_allowed"])
        self.assertIn("沉默", state["reason"])

        self.user["ignored_streak"] = 0
        policy["proactive_max_daily"] = 0
        state = self.harness._reality_touch_camera_proactive_state(self.user, user_id="u")
        self.assertFalse(state["direct_allowed"])
        self.assertTrue(state["ask_allowed"])
        self.assertIn("日额度", state["direct_reason"])
        self.assertIn("不能调用摄像头工具", self.harness._reality_touch_camera_proactive_prompt(self.user, user_id="u"))

    async def test_proactive_snapshot_uses_independent_daily_counter(self) -> None:
        policy = self.user["reality_touch_camera_policy"]
        policy["proactive_mode"] = "auto"
        self.harness._capture_reality_touch_camera_frame = lambda: {
            "jpeg_bytes": b"one-frame",
            "width": 320,
            "height": 240,
            "brightness": "normal",
        }
        self.harness._analyze_reality_touch_camera_frame = AsyncMock(return_value={
            "presence": "present",
            "activity": "eating",
            "interruptibility": "medium",
            "brightness": "normal",
            "confidence": 0.8,
            "analyzed": True,
            "width": 320,
            "height": 240,
            "summary": "在场，正在进行日常活动",
        })

        result = await self.harness._reality_touch_camera_snapshot_for_user(
            "u",
            "看看用户刚提到的现实活动，决定如何自然接话",
            source="proactive_curiosity",
        )

        self.assertEqual("success", result["status"])
        self.assertEqual(1, policy["proactive_used_today"])
        self.assertEqual("2026-08-11", policy["proactive_used_day"])
        self.assertEqual("proactive_curiosity", policy["last_observation"]["source"])

        second = await self.harness._reality_touch_camera_snapshot_for_user(
            "u",
            "再次主动查看",
            source="proactive_curiosity",
        )
        self.assertEqual("forbidden", second["status"])
        self.assertIn("额度", second["message"])

    def test_policy_update_normalizes_mode_and_user_quota(self) -> None:
        policy = self.harness._reality_touch_update_camera_policy(
            self.user,
            {
                "camera_enabled": True,
                "proactive_mode": "authorized",
                "proactive_max_daily": 99,
            },
            user_id="u",
        )
        self.assertEqual("auto", policy["proactive_mode"])
        self.assertEqual(10, policy["proactive_max_daily"])


if __name__ == "__main__":
    unittest.main()

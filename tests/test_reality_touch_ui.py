# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KEY = "enable_experimental_bluetooth_wakeup"


class RealityTouchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        cls.primary = (ROOT / "pages" / "companion-panel" / "app.js").read_text(encoding="utf-8")
        cls.localized = (ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
        cls.primary_css = (ROOT / "pages" / "companion-panel" / "app.css").read_text(encoding="utf-8")
        cls.localized_css = (ROOT / "pages" / "陪伴面板" / "app.css").read_text(encoding="utf-8")
        cls.api = (ROOT / "page_api.py").read_text(encoding="utf-8")
        cls.main = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.settings = (ROOT / "page_api_settings.py").read_text(encoding="utf-8")
        cls.proactive = (ROOT / "proactive_message.py").read_text(encoding="utf-8")
        cls.requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    def test_schema_places_switch_in_experimental_group(self) -> None:
        grouped = self.schema["experimental_motivation_config"]["items"][KEY]
        self.assertFalse(grouped["default"])
        self.assertEqual("bool", grouped["type"])
        self.assertIn("摄像头", grouped["hint"])
        self.assertTrue(self.schema[KEY]["invisible"])

    def test_both_panel_bundles_expose_reality_touch_card(self) -> None:
        self.assertEqual(self.primary, self.localized)
        self.assertEqual(self.primary_css, self.localized_css)
        for marker in (
            f'"{KEY}",',
            f"{KEY}: {{",
            'label: "现实触及"',
            'mark: "触"',
            "用户本人在私聊分别确认",
            "摄像头单帧环境观察",
            "data-reality-touch-form",
            "复制确认命令",
            "电脑音频输出设备",
            "现实触及播放音量",
            "主动语音播放音量",
            "等待用户确认醒来",
            "稍后再叫",
            "起始音量",
            "每轮增量",
            "最高音量",
            "淡入时间",
            "音频失败时发消息",
            "停止当前这轮触达",
            "data-reality-touch-volume",
            "电脑音频输出设备与音量已保存",
            "data-reality-touch-device-save",
            "播放固定测试音频",
            'data-reality-touch-test-kind="device"',
            "data-reality-touch-camera-config",
            "data-reality-touch-camera-policy-form",
            "data-reality-touch-camera-test",
            "data-reality-touch-camera-scan",
            "扫描摄像头",
            "读取并预览单帧",
            "reality-camera-preview",
            "刷新或离开页面即消失",
            "camera_single_frame",
            "叫醒偏好（可选）",
            "每次触发时按人格、关系与当天语境动态生成",
            "生成并试听",
            "主动语音同步到所选设备",
            "这是现实触及的一个使用示例",
            "未授权，且不会继承音频或主动权限",
            "自定义现实触及提醒",
            "官方 Cron",
            "data-reality-touch-reminder-cancel",
        ):
            self.assertIn(marker, self.primary)
        self.assertIn(".reality-touch-grid", self.primary_css)
        self.assertIn(".reality-consent-strip", self.primary_css)

    def test_host_camera_uses_stricter_identity_boundary_than_private_management(self) -> None:
        self.assertIn("普通私聊、目标用户名单与主动权限均不会授予摄像头访问", self.primary)
        self.assertGreaterEqual(self.main.count("_reality_touch_camera_user_eligible(user_id)"), 3)

    def test_page_api_can_read_save_and_normalize_switch(self) -> None:
        self.assertGreaterEqual(self.api.count(f'"{KEY}"'), 3)
        self.assertIn(f'if key == "{KEY}":', self.settings)
        self.assertIn("return self._normalize_bool_value(value)", self.settings)

    def test_page_api_exposes_real_console_actions(self) -> None:
        for marker in (
            '("/reality-touch", self.get_reality_touch',
            '("/reality-touch/update", self.update_reality_touch',
            '"save_camera_config", "save_camera_policy", "test_camera"',
            '"scan_cameras"',
            '"该用户尚未在私聊中完成现实触及知情确认"',
            'action == "cancel_reminder"',
            "await test_audio_player(",
            "await wakeup_player(",
            "await camera_snapshotter(",
            "include_preview=True",
        ):
            self.assertIn(marker, self.api)

    def test_selected_device_extends_existing_proactive_voice_action(self) -> None:
        for marker in (
            "_reality_touch_proactive_voice_allowed",
            "_mirror_reality_touch_proactive_voice",
            "已同步到所选电脑音频设备",
            "defer_local_playback=touch_allowed",
        ):
            self.assertIn(marker, self.proactive)
        self.assertIn("sounddevice>=", self.requirements)
        self.assertIn("soundfile>=", self.requirements)
        self.assertIn("opencv-python-headless>=", self.requirements)
        self.assertIn("cv2-enumerate-cameras>=", self.requirements)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock


PACKAGE_NAME = "astrbot_plugin_private_companion"
ROOT = Path(__file__).resolve().parents[1]
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = package

if "astrbot.api" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        warning=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
    )
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from astrbot_plugin_private_companion.wakeup_alarm import WakeupAlarmMixin


class AlarmHarness(WakeupAlarmMixin):
    def __init__(self) -> None:
        self.enable_experimental_bluetooth_wakeup = True
        self.environment_perception_timezone = "Asia/Shanghai"
        self.data = {"users": {"u": {"umo": "bot:FriendMessage:u"}}}
        self.played = 0

    def _save_data_sync(self) -> None:
        return None

    def _schedule_data_save(self, **kwargs) -> None:
        return None

    async def _play_wakeup_alarm(self, alarm, *, test=False):
        self.played += 1
        return True


class WakeupAlarmTests(unittest.IsolatedAsyncioTestCase):
    def test_command_and_day_normalization(self) -> None:
        harness = AlarmHarness()
        user = harness.data["users"]["u"]
        response, test = harness._wakeup_alarm_command(user, "07:30")
        self.assertFalse(test)
        self.assertIn("完整确认信息", response)
        self.assertFalse(user["wakeup_alarm"].get("enabled"))

        confirmation = f"确认 {harness._REALITY_TOUCH_CONFIRMATION_TEXT}"
        response, test = harness._wakeup_alarm_command(user, confirmation)
        self.assertFalse(test)
        self.assertIn("未授权摄像头", response)
        self.assertEqual(["local_audio"], user["reality_touch_consent"]["granted_capabilities"])
        self.assertFalse(user["reality_touch_consent"]["camera_granted"])
        self.assertFalse(harness._reality_touch_capability_consented(user, "camera"))

        response, test = harness._wakeup_alarm_command(user, "07:30 周一")
        self.assertFalse(test)
        self.assertIn("07:30", response)
        self.assertEqual([0], user["wakeup_alarm"]["days"])
        self.assertEqual("07:30", harness._wakeup_parse_time("7：30"))
        self.assertEqual(list(range(7)), harness._wakeup_days([0, 1, 2, 3, 4, 5, 6]))

    def test_page_console_snapshot_and_update_keep_consent_boundary(self) -> None:
        harness = AlarmHarness()
        harness._wakeup_now = lambda: datetime(2026, 8, 10, 7, 0)
        user = harness.data["users"]["u"]
        with self.assertRaisesRegex(ValueError, "知情确认"):
            harness._reality_touch_update_alarm(
                user,
                {"enabled": True, "time": "08:00", "days": [0], "message": "起床"},
            )

        harness._wakeup_alarm_command(user, f"确认 {harness._REALITY_TOUCH_CONFIRMATION_TEXT}")
        alarm = harness._reality_touch_update_alarm(
            user,
            {
                "enabled": True,
                "time": "08:00",
                "days": [0],
                "message": "起床，先喝水。",
                "repeat_count": 2,
                "repeat_interval_seconds": 15,
            },
        )
        self.assertEqual([0], alarm["days"])
        self.assertEqual(2, alarm["repeat_count"])

        snapshot = harness._reality_touch_page_snapshot()
        self.assertTrue(snapshot["global_enabled"])
        self.assertEqual(1, snapshot["counts"]["consented"])
        self.assertEqual(1, snapshot["counts"]["scheduled"])
        self.assertIn("陪伴 现实触及 确认", snapshot["confirmation_command"])
        row = snapshot["users"][0]
        self.assertTrue(row["consent"]["local_audio"])
        self.assertFalse(row["consent"]["camera"])
        self.assertEqual("08-10 08:00", row["alarm"]["next_trigger_text"])

    async def test_tick_is_idempotent_for_one_minute(self) -> None:
        harness = AlarmHarness()
        user = harness.data["users"]["u"]
        harness._wakeup_alarm_command(user, f"确认 {harness._REALITY_TOUCH_CONFIRMATION_TEXT}")
        harness._wakeup_alarm_command(user, "07:30")
        harness._wakeup_now = lambda: datetime(2026, 8, 10, 7, 30)
        await harness._run_wakeup_alarm_tick()
        await harness._run_wakeup_alarm_tick()
        self.assertEqual(1, harness.played)

    async def test_tick_requires_current_consent(self) -> None:
        harness = AlarmHarness()
        user = harness.data["users"]["u"]
        user["wakeup_alarm"] = {"enabled": True, "time": "07:30", "days": list(range(7))}
        harness._wakeup_now = lambda: datetime(2026, 8, 10, 7, 30)
        await harness._run_wakeup_alarm_tick()
        self.assertEqual(0, harness.played)

        harness._wakeup_alarm_command(user, f"确认 {harness._REALITY_TOUCH_CONFIRMATION_TEXT}")
        harness._wakeup_alarm_command(user, "撤销确认")
        self.assertFalse(user["wakeup_alarm"]["enabled"])
        self.assertNotIn("reality_touch_consent", user)


if __name__ == "__main__":
    unittest.main()

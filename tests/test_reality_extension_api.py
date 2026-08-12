# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import time

from astrbot_plugin_private_companion.main import PrivateCompanionExtensionAPI
from astrbot_plugin_private_companion.reality_companion_bridge import RealityCompanionBridgeMixin


class _Host:
    admin_user_ids = ["admin-1"]

    def __init__(self) -> None:
        self.data = {
            "users": {
                "target-1": {"user_id": "target-1", "nickname": "Primary"},
                "owner-1": {"user_id": "owner-1", "relationship_role": "owner"},
            }
        }

    @staticmethod
    def _configured_target_ids() -> list[str]:
        return ["target-1"]

    @staticmethod
    def _relationship_owner_user_ids() -> set[str]:
        return {"owner-1"}

    @staticmethod
    def _is_configured_admin_user_id(value: str) -> bool:
        return value == "admin-1"


def test_reality_extension_api_recognizes_configured_primary_targets() -> None:
    api = PrivateCompanionExtensionAPI(_Host())

    assert api.get_reality_touch_authorized_user_ids() == ["admin-1", "owner-1", "target-1"]
    target = api.get_reality_touch_host_context("target-1")

    assert target["is_primary_user"] is True
    assert target["eligible"] is True


class _RecordingHost(RealityCompanionBridgeMixin):
    def __init__(self) -> None:
        self.data = {"users": {"u": {"user_id": "u"}}}
        self._data_lock = asyncio.Lock()
        self.saved = 0

    def _get_user(self, user_id: str) -> dict:
        return self.data["users"][user_id]

    def _save_data_sync(self) -> None:
        self.saved += 1


def test_reality_output_is_recorded_as_cross_device_conversation_context() -> None:
    host = _RecordingHost()
    api = PrivateCompanionExtensionAPI(host)

    result = asyncio.run(
        api.record_reality_touch_output(
            "u",
            "早呀，该起床啦。",
            source="wakeup_alarm",
            delivered_at=1000,
        )
    )
    user = host.data["users"]["u"]
    user.update({"last_user_message": "早", "last_user_message_at": 1001})

    assert result["recorded"] is True
    assert user["last_companion_message"] == "早呀，该起床啦。"
    assert user["last_companion_message_at"] == 1000
    assert user["last_reality_touch_output"]["source"] == "wakeup_alarm"
    assert host.saved == 1


def test_recent_reality_output_and_user_reply_form_one_continuous_exchange() -> None:
    host = _RecordingHost()
    delivered_at = time.time() - 3
    asyncio.run(
        host._record_reality_touch_output(
            "u",
            "早呀，该起床啦。",
            source="wakeup_alarm",
            delivered_at=delivered_at,
        )
    )
    user = host.data["users"]["u"]
    user.update({"last_user_message": "早", "last_user_message_at": delivered_at + 2})

    context = host._format_reality_touch_continuity_context(user)

    assert "Bot 已通过现实音频设备对用户说：早呀，该起床啦。" in context
    assert "用户随后在私聊回应：早" in context
    assert "不要把它当作首次问候" in context

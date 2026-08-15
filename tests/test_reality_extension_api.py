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


def test_reality_extension_api_forwards_mobile_location_updates() -> None:
    class Host(_Host):
        async def _handle_mobile_location_update(self, user_id: str) -> dict:
            return {"handled": user_id == "target-1"}

    api = PrivateCompanionExtensionAPI(Host())

    result = asyncio.run(api.notify_mobile_location_update("target-1"))

    assert result == {"handled": True}


class _RecordingHost(RealityCompanionBridgeMixin):
    def __init__(self) -> None:
        self.data = {"users": {"u": {"user_id": "u"}}}
        self._data_lock = asyncio.Lock()
        self.saved = 0

    def _get_user(self, user_id: str) -> dict:
        return self.data["users"][user_id]

    def _save_data_sync(self) -> None:
        self.saved += 1


class _RealityApi:
    def __init__(self) -> None:
        self.output: dict = {}

    async def record_reality_touch_output(self, _user_id: str, text: str, **kwargs) -> dict:
        self.output = {
            "text": text,
            "source": kwargs.get("source", "reality_touch_audio"),
            "delivered_at": kwargs.get("delivered_at"),
        }
        return {"recorded": True}

    def recent_output(self, _user_id: str) -> dict:
        return dict(self.output)


def test_core_extension_does_not_record_reality_output_without_split_plugin() -> None:
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
    assert result == {"recorded": False, "reason": "reality_companion_unavailable"}
    assert "last_reality_touch_output" not in host.data["users"]["u"]
    assert host.saved == 0


def test_recent_reality_output_and_user_reply_form_one_continuous_exchange() -> None:
    host = _RecordingHost()
    reality_api = _RealityApi()
    host._reality_companion_api = lambda: reality_api
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


def test_missing_reality_plugin_does_not_write_new_runtime_state_to_core() -> None:
    host = _RecordingHost()
    host._reality_companion_api = lambda: None

    result = asyncio.run(host._record_reality_touch_output("u", "不会写进本体"))

    assert result == {"recorded": False, "reason": "reality_companion_unavailable"}
    assert "last_reality_touch_output" not in host.data["users"]["u"]
    assert host.saved == 0

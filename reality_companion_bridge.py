# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import sys
from typing import Any

from astrbot.api import logger

from .helpers import _single_line


class RealityCompanionBridgeMixin:
    """Optional bridge to astrbot_plugin_reality_companion.

    The main companion intentionally owns no device implementation. These
    adapters preserve the old call surface for prompts, commands and timers.
    """

    def _reality_companion_api(self) -> Any | None:
        module_names = (
            "data.plugins.astrbot_plugin_reality_companion.main",
            "astrbot_plugin_reality_companion.main",
        )
        suffixes = tuple(name.removeprefix("data.plugins.") for name in module_names)
        modules = [sys.modules.get(name) for name in module_names]
        modules.extend(
            module
            for name, module in list(sys.modules.items())
            if module is not None and any(name.endswith(suffix) for suffix in suffixes)
        )
        for module in modules:
            if module is None:
                continue
            getter = getattr(module, "get_reality_companion_api", None)
            try:
                api = getter() if callable(getter) else None
            except Exception:
                api = None
            if api is not None:
                return api
        getter = getattr(getattr(self, "context", None), "get_registered_star", None)
        if callable(getter):
            try:
                metadata = getter("astrbot_plugin_reality_companion")
                instance = getattr(metadata, "star_cls", None) if metadata is not None else None
                api = getattr(instance, "extension_api", None)
                if api is not None:
                    return api
            except Exception:
                pass
        return None

    @staticmethod
    def _reality_bridge_user_id(user: Any) -> str:
        if isinstance(user, dict):
            return _single_line(user.get("user_id"), 120)
        return _single_line(user, 120)

    def _reality_touch_audio_consented(self, user: dict[str, Any]) -> bool:
        api = self._reality_companion_api()
        checker = getattr(api, "audio_consented", None) if api is not None else None
        return bool(callable(checker) and checker(self._reality_bridge_user_id(user)))

    def _reality_companion_enabled(self) -> bool:
        api = self._reality_companion_api()
        getter = getattr(api, "status", None) if api is not None else None
        if not callable(getter):
            return False
        try:
            status = getter()
        except Exception:
            return False
        return bool(isinstance(status, dict) and status.get("enabled"))

    def _reality_mobile_context(self, user_id: Any = "") -> dict[str, Any]:
        """Return the short-lived, coarse Android location context when available."""
        api = self._reality_companion_api()
        getter = getattr(api, "mobile_context", None) if api is not None else None
        normalized = _single_line(user_id, 120)
        if not callable(getter) or not normalized:
            return {"available": False, "user_id": normalized, "location": {"available": False}}
        try:
            value = getter(normalized)
        except Exception:
            return {"available": False, "user_id": normalized, "location": {"available": False}}
        return value if isinstance(value, dict) else {"available": False, "user_id": normalized, "location": {"available": False}}

    def _reality_touch_proactive_voice_allowed(self, user: dict[str, Any]) -> bool:
        api = self._reality_companion_api()
        checker = getattr(api, "proactive_voice_allowed", None) if api is not None else None
        return bool(callable(checker) and checker(self._reality_bridge_user_id(user)))

    async def _mirror_reality_touch_proactive_voice(self, user: dict[str, Any], audio_path: str) -> bool:
        api = self._reality_companion_api()
        mirror = getattr(api, "mirror_proactive_voice", None) if api is not None else None
        if not callable(mirror):
            return False
        return bool(await mirror(self._reality_bridge_user_id(user), audio_path))

    def _reality_touch_camera_user_eligible(self, user_id: Any) -> bool:
        api = self._reality_companion_api()
        checker = getattr(api, "camera_user_eligible", None) if api is not None else None
        return bool(callable(checker) and checker(_single_line(user_id, 120)))

    def _reality_touch_camera_proactive_state(
        self,
        user: dict[str, Any],
        *,
        user_id: str = "",
    ) -> dict[str, Any]:
        api = self._reality_companion_api()
        getter = getattr(api, "camera_proactive_state", None) if api is not None else None
        normalized = _single_line(user_id, 120) or self._reality_bridge_user_id(user)
        if not callable(getter):
            return {"available": False, "direct_allowed": False, "reason": "reality_companion_missing"}
        result = getter(normalized)
        return result if isinstance(result, dict) else {"available": False, "direct_allowed": False}

    def _reality_touch_camera_proactive_prompt(
        self,
        user: dict[str, Any],
        *,
        user_id: str = "",
    ) -> str:
        api = self._reality_companion_api()
        getter = getattr(api, "camera_proactive_prompt", None) if api is not None else None
        normalized = _single_line(user_id, 120) or self._reality_bridge_user_id(user)
        return str(getter(normalized) or "") if callable(getter) else ""

    async def _schedule_reality_touch_official_reminder(
        self,
        user_id: str,
        payload: dict[str, Any],
        *,
        source_text: str,
        trigger_umo: str = "",
    ) -> bool:
        api = self._reality_companion_api()
        scheduler = getattr(api, "schedule_reminder", None) if api is not None else None
        if not callable(scheduler):
            logger.info("[PrivateCompanion] 未安装或未启用“我会来到你身边”，现实提醒未创建")
            return False
        return bool(
            await scheduler(
                _single_line(user_id, 120),
                payload,
                source_text=source_text,
                trigger_umo=trigger_umo,
            )
        )

    def _wakeup_alarm_command(self, user: dict[str, Any], value: str) -> tuple[str, Any]:
        api = self._reality_companion_api()
        handler = getattr(api, "legacy_command", None) if api is not None else None
        if not callable(handler):
            return (
                "现实触及已拆分为联动插件“我会来到你身边”。请先安装并启用 "
                "astrbot_plugin_reality_companion。",
                False,
            )
        return handler(
            self._reality_bridge_user_id(user),
            value,
            umo=_single_line(user.get("umo"), 180),
        )

    async def _test_wakeup_alarm(self, user: dict[str, Any]) -> None:
        api = self._reality_companion_api()
        tester = getattr(api, "test_wakeup", None) if api is not None else None
        if callable(tester):
            await tester(self._reality_bridge_user_id(user))

    async def _reality_touch_camera_snapshot_for_user(
        self,
        user_id: str,
        purpose: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        api = self._reality_companion_api()
        snapshotter = getattr(api, "camera_snapshot", None) if api is not None else None
        if not callable(snapshotter):
            return {"status": "unavailable", "message": "未安装或未启用“我会来到你身边”"}
        return await snapshotter(_single_line(user_id, 120), purpose, **kwargs)

    def _reality_touch_apply_pending_confirmation(self, user: dict[str, Any], text: str) -> str | None:
        api = self._reality_companion_api()
        handler = getattr(api, "apply_pending_confirmation", None) if api is not None else None
        return handler(self._reality_bridge_user_id(user), text) if callable(handler) else None

    async def _acknowledge_official_reality_touch_trigger(self, event: Any) -> bool:
        return False

    async def _record_official_reality_touch_tool_result(self, event: Any, tool: Any, tool_result: Any) -> bool:
        return False

    async def _complete_official_reality_touch_reminder(self, event: Any) -> bool:
        return False

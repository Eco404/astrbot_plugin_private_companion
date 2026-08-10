# -*- coding: utf-8 -*-
"""现实触及摄像头：独立授权、任务触发的单帧环境观察。"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Any

from astrbot.api import logger

from .helpers import _now_ts, _safe_float, _safe_int, _single_line


class RealityTouchCameraMixin:
    """Provide a privacy-bounded, single-frame camera capability."""

    _REALITY_TOUCH_CAMERA_CONSENT_VERSION = 1
    _REALITY_TOUCH_CAMERA_CAPABILITY = "camera_single_frame"
    _REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT = "我理解风险并确认授权"
    _REALITY_TOUCH_CAMERA_PRESENCE = {"present", "absent", "uncertain"}
    _REALITY_TOUCH_CAMERA_ACTIVITY = {"sleeping", "at_desk", "eating", "moving", "unknown"}
    _REALITY_TOUCH_CAMERA_INTERRUPTIBILITY = {"low", "medium", "high", "unknown"}
    _REALITY_TOUCH_CAMERA_BRIGHTNESS = {"dark", "normal", "bright", "unknown"}

    def _reality_touch_camera_consent(self, user: dict[str, Any]) -> dict[str, Any]:
        consent = user.get("reality_touch_camera_consent")
        return consent if isinstance(consent, dict) else {}

    def _reality_touch_camera_consented(self, user: dict[str, Any]) -> bool:
        consent = self._reality_touch_camera_consent(user)
        capabilities = consent.get("granted_capabilities")
        return (
            consent.get("confirmed") is True
            and _safe_int(consent.get("version"), 0, 0) >= self._REALITY_TOUCH_CAMERA_CONSENT_VERSION
            and isinstance(capabilities, list)
            and self._REALITY_TOUCH_CAMERA_CAPABILITY in capabilities
        )

    def _reality_touch_camera_user_eligible(self, user_id: Any) -> bool:
        """Only a host manager/owner may bind the host camera to a chat identity."""
        resolver = getattr(self, "_permission_identity_id", None)
        permission_id = resolver(user_id) if callable(resolver) else ""
        if not permission_id:
            return False
        admin_checker = getattr(self, "_is_configured_admin_user_id", None)
        if callable(admin_checker) and admin_checker(permission_id):
            return True
        owner_getter = getattr(self, "_relationship_owner_user_ids", None)
        owner_ids = owner_getter() if callable(owner_getter) else set()
        return permission_id in set(owner_ids or ())

    def _reality_touch_camera_confirmation_prompt(self) -> str:
        return (
            "摄像头是现实触及的独立高风险能力，不会继承音频授权。启用后也只允许按明确任务读取单帧，"
            "不持续录像、不做人脸识别或身份比对、不做情绪读脸。单帧可能发送给已配置的视觉模型做"
            "有限状态分析，插件默认不保存原图；视觉服务商自身的数据政策仍以其配置为准。\n"
            "风险说明展示后，用户本人只需在 10 分钟内单独发送：\n"
            f"{self._REALITY_TOUCH_CAMERA_CONFIRMATION_TEXT}"
        )

    @staticmethod
    def _reality_touch_camera_confirmation_valid(text: str) -> bool:
        compact = re.sub(r"[\s，,。.!！;；:：、]+", "", str(text or ""))
        return compact == "我理解风险并确认授权"

    def _reality_touch_camera_command(self, user: dict[str, Any], text: str) -> tuple[str, Any] | None:
        value = str(text or "").strip()
        compact = re.sub(r"\s+", "", value).lower()
        confirmation_prefixes = ("摄像头确认", "确认摄像头")
        if compact in {"摄像头", "摄像头授权", "摄像头确认", "确认摄像头"}:
            user["reality_touch_pending_consent"] = {
                "capability": self._REALITY_TOUCH_CAMERA_CAPABILITY,
                "requested_at": _now_ts(),
                "expires_at": _now_ts() + 600,
            }
            self._save_data_sync()
            return self._reality_touch_camera_confirmation_prompt(), False
        if any(value.startswith(prefix) for prefix in confirmation_prefixes):
            prefix = next(prefix for prefix in confirmation_prefixes if value.startswith(prefix))
            confirmation = value[len(prefix):].strip()
            if not self._reality_touch_camera_confirmation_valid(confirmation):
                user["reality_touch_pending_consent"] = {
                    "capability": self._REALITY_TOUCH_CAMERA_CAPABILITY,
                    "requested_at": _now_ts(),
                    "expires_at": _now_ts() + 600,
                }
                self._save_data_sync()
                return (
                    "确认口令不正确，请在阅读风险说明后手动输入“我理解风险并确认授权”。\n"
                    + self._reality_touch_camera_confirmation_prompt(),
                    False,
                )
            user["reality_touch_camera_consent"] = {
                "confirmed": True,
                "version": self._REALITY_TOUCH_CAMERA_CONSENT_VERSION,
                "confirmed_at": _now_ts(),
                "confirmation_text": _single_line(confirmation, 360),
                "granted_capabilities": [self._REALITY_TOUCH_CAMERA_CAPABILITY],
            }
            user.pop("reality_touch_pending_consent", None)
            policy = self._reality_touch_camera_policy(user)
            policy["enabled"] = True
            policy["updated_at"] = _now_ts()
            self._save_data_sync()
            return "现实触及摄像头独立授权已记录。当前仅允许按明确任务读取单帧，默认不保存原图。", False
        if compact in {"撤销摄像头授权", "撤销摄像头确认", "取消摄像头授权", "关闭摄像头授权"}:
            user.pop("reality_touch_camera_consent", None)
            pending = user.get("reality_touch_pending_consent")
            if isinstance(pending, dict) and pending.get("capability") == self._REALITY_TOUCH_CAMERA_CAPABILITY:
                user.pop("reality_touch_pending_consent", None)
            self._reality_touch_camera_policy(user)["enabled"] = False
            self._save_data_sync()
            return "已撤销现实触及摄像头授权；本机音频授权不受影响。", False
        if compact in {"摄像头状态", "查看摄像头", "查看摄像头状态"}:
            policy = self._reality_touch_camera_policy(user)
            latest = policy.get("last_observation") if isinstance(policy.get("last_observation"), dict) else {}
            return (
                "现实触及摄像头："
                + ("已独立授权" if self._reality_touch_camera_consented(user) else "未授权")
                + ("，用户策略已开启" if policy.get("enabled") else "，用户策略已关闭")
                + (f"；最近读取：{_single_line(latest.get('summary'), 160)}" if latest else "；暂无读取记录"),
                False,
            )
        for prefix in ("摄像头读取", "读取摄像头", "摄像头测试", "测试摄像头"):
            if value.startswith(prefix):
                purpose = _single_line(value[len(prefix):].strip(), 120)
                if not purpose:
                    purpose = "用户手动请求查看当前环境是否适合互动"
                return "正在按本次明确目的读取一帧；原始画面不会写入插件数据。", {
                    "camera_snapshot": True,
                    "purpose": purpose,
                }
        return None

    @staticmethod
    def _reality_touch_camera_policy(user: dict[str, Any]) -> dict[str, Any]:
        policy = user.get("reality_touch_camera_policy")
        if not isinstance(policy, dict):
            policy = {}
            user["reality_touch_camera_policy"] = policy
        return policy

    def _reality_touch_update_camera_policy(
        self,
        user: dict[str, Any],
        payload: dict[str, Any],
        *,
        user_id: str = "",
    ) -> dict[str, Any]:
        if not self._reality_touch_camera_user_eligible(user_id):
            raise ValueError("主机摄像头只允许 AstrBot 管理员或主要用户本人使用")
        enabled = bool(payload.get("camera_enabled"))
        if enabled and not self._reality_touch_camera_consented(user):
            raise ValueError("该用户尚未在私聊中完成摄像头独立知情确认")
        policy = self._reality_touch_camera_policy(user)
        policy["enabled"] = enabled
        policy["updated_at"] = _now_ts()
        return policy

    @staticmethod
    def _reality_touch_camera_backend_snapshot() -> dict[str, Any]:
        try:
            import cv2  # type: ignore

            try:
                import cv2_enumerate_cameras  # type: ignore  # noqa: F401
                enumerator_available = True
            except Exception:
                enumerator_available = False

            version = _single_line(getattr(cv2, "__version__", ""), 40)
            return {
                "available": True,
                "backend": "opencv",
                "version": version,
                "enumerator_available": enumerator_available,
                "error": "" if enumerator_available else "缺少摄像头名称枚举依赖，仍可手动填写索引",
            }
        except Exception as exc:
            return {
                "available": False,
                "backend": "unavailable",
                "version": "",
                "enumerator_available": False,
                "error": "当前 AstrBot 运行环境缺少 OpenCV 摄像头依赖" + (f"：{_single_line(exc, 120)}" if exc else ""),
            }

    def _reality_touch_camera_devices(self, *, refresh: bool = False) -> dict[str, Any]:
        """Return a cached device catalog; enumerate only after an explicit page action."""
        store_getter = getattr(self, "_reality_touch_store", None)
        store = store_getter() if callable(store_getter) else {}
        cached = store.get("camera_device_catalog") if isinstance(store, dict) else None
        if not refresh:
            return dict(cached) if isinstance(cached, dict) else {"devices": [], "scanned_at": 0, "error": ""}
        try:
            import cv2  # type: ignore
            from cv2_enumerate_cameras import enumerate_cameras  # type: ignore

            devices: list[dict[str, Any]] = []
            seen: set[int] = set()
            for info in enumerate_cameras():
                index = _safe_int(getattr(info, "index", -1), -1, -1, 100000)
                if index < 0 or index in seen:
                    continue
                seen.add(index)
                api_code = (index // 100) * 100
                backend_name = ""
                registry = getattr(cv2, "videoio_registry", None)
                backend_getter = getattr(registry, "getBackendName", None)
                if callable(backend_getter) and api_code > 0:
                    try:
                        backend_name = _single_line(backend_getter(api_code), 40)
                    except Exception:
                        backend_name = ""
                name = _single_line(getattr(info, "name", ""), 100) or f"摄像头 {index}"
                devices.append(
                    {
                        "index": index,
                        "name": name,
                        "backend": backend_name,
                        "virtual": any(marker in name.lower() for marker in ("virtual", "vtube", "obs")),
                    }
                )
            catalog = {
                "devices": devices,
                "scanned_at": _now_ts(),
                "error": "" if devices else "没有枚举到摄像头设备",
            }
        except Exception as exc:
            catalog = {
                "devices": [],
                "scanned_at": _now_ts(),
                "error": "摄像头设备枚举失败：" + (_single_line(exc, 160) or "未知错误"),
            }
        if isinstance(store, dict):
            store["camera_device_catalog"] = catalog
            self._save_data_sync()
        return dict(catalog)

    def _reality_touch_scan_camera_devices(self) -> dict[str, Any]:
        return self._reality_touch_camera_devices(refresh=True)

    def _capture_reality_touch_camera_frame(self) -> dict[str, Any]:
        """Capture exactly one frame and always release the device."""
        try:
            import cv2  # type: ignore
        except Exception as exc:
            raise RuntimeError("当前 AstrBot 运行环境缺少 OpenCV 摄像头依赖") from exc
        index = _safe_int(getattr(self, "reality_touch_camera_index", 0), 0, 0, 100000)
        capture = cv2.VideoCapture(index)
        try:
            if not capture or not capture.isOpened():
                raise RuntimeError(f"无法打开摄像头索引 {index}")
            ok, frame = capture.read()
            if not ok or frame is None:
                raise RuntimeError(f"摄像头索引 {index} 未返回画面")
            height, width = frame.shape[:2]
            mean = float(frame.mean()) if getattr(frame, "size", 0) else 0.0
            encoded, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
            if not encoded:
                raise RuntimeError("摄像头单帧编码失败")
            brightness = "dark" if mean < 45 else "bright" if mean > 190 else "normal"
            return {
                "jpeg_bytes": bytes(buffer),
                "width": int(width),
                "height": int(height),
                "brightness": brightness,
            }
        finally:
            if capture is not None:
                capture.release()

    @staticmethod
    def _reality_touch_camera_json(text: Any) -> dict[str, Any]:
        raw = str(text or "").strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.I | re.S)
        candidate = fenced.group(1) if fenced else raw
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            match = re.search(r"\{.*\}", candidate, flags=re.S)
            if not match:
                return {}
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}

    def _sanitize_reality_touch_camera_observation(
        self,
        raw: dict[str, Any],
        *,
        local_brightness: str,
        width: int,
        height: int,
        analyzed: bool,
    ) -> dict[str, Any]:
        def pick(key: str, allowed: set[str], fallback: str) -> str:
            value = _single_line(raw.get(key), 32).lower()
            return value if value in allowed else fallback

        observation = {
            "presence": pick("presence", self._REALITY_TOUCH_CAMERA_PRESENCE, "uncertain"),
            "activity": pick("activity", self._REALITY_TOUCH_CAMERA_ACTIVITY, "unknown"),
            "interruptibility": pick("interruptibility", self._REALITY_TOUCH_CAMERA_INTERRUPTIBILITY, "unknown"),
            "brightness": pick(
                "brightness",
                self._REALITY_TOUCH_CAMERA_BRIGHTNESS,
                local_brightness if local_brightness in self._REALITY_TOUCH_CAMERA_BRIGHTNESS else "unknown",
            ),
            "confidence": round(max(0.0, min(1.0, _safe_float(raw.get("confidence"), 0.0))), 2),
            "analyzed": bool(analyzed),
            "width": _safe_int(width, 0, 0, 10000),
            "height": _safe_int(height, 0, 0, 10000),
        }
        observation["summary"] = (
            f"在场={observation['presence']}，活动={observation['activity']}，"
            f"可打扰性={observation['interruptibility']}，光线={observation['brightness']}"
        )
        return observation

    async def _analyze_reality_touch_camera_frame(self, frame: dict[str, Any], purpose: str) -> dict[str, Any]:
        fallback = self._sanitize_reality_touch_camera_observation(
            {},
            local_brightness=_single_line(frame.get("brightness"), 16).lower(),
            width=_safe_int(frame.get("width"), 0, 0),
            height=_safe_int(frame.get("height"), 0, 0),
            analyzed=False,
        )
        jpeg_bytes = frame.get("jpeg_bytes")
        if not isinstance(jpeg_bytes, (bytes, bytearray)) or not jpeg_bytes:
            return fallback
        provider_id = _single_line(getattr(self, "plugin_vision_provider_id", ""), 160)
        getter = getattr(getattr(self, "context", None), "get_provider_by_id", None)
        provider = getter(provider_id) if provider_id and callable(getter) else None
        supports_image = getattr(self, "_provider_supports_image", None)
        if provider is None or (callable(supports_image) and not supports_image(provider)):
            return fallback
        prompt = (
            "你正在执行经过用户单独授权的现实触及单帧环境观察。只判断是否适合主动互动，不描述具体人物、"
            "身份、长相、年龄、性别、身体特征、情绪、房间隐私、屏幕文字或任何可识别信息。"
            "禁止人脸识别、身份猜测、情绪读脸和 OCR。只输出 JSON，不要附加解释："
            '{"presence":"present|absent|uncertain","activity":"sleeping|at_desk|eating|moving|unknown",'
            '"interruptibility":"low|medium|high|unknown","brightness":"dark|normal|bright|unknown",'
            '"confidence":0.0}。无法可靠判断时必须用 uncertain/unknown。'
            f"\n本次任务目的：{_single_line(purpose, 120)}"
        )
        data_url = "data:image/jpeg;base64," + base64.b64encode(bytes(jpeg_bytes)).decode("ascii")
        started = time.time()
        try:
            call = provider.text_chat(prompt=prompt, image_urls=[data_url], max_tokens=180)
            timeout = _safe_int(getattr(self, "reality_touch_camera_analysis_timeout_seconds", 25), 25, 5, 90)
            result = await asyncio.wait_for(call, timeout=timeout)
            completion = str(getattr(result, "completion_text", result) or "").strip()
            recorder = getattr(self, "_record_llm_usage", None)
            if callable(recorder):
                recorder(
                    provider_id=provider_id,
                    task="reality_touch_camera",
                    prompt=prompt,
                    completion=completion,
                    elapsed_ms=int((time.time() - started) * 1000),
                    success=bool(completion),
                    resp=result,
                )
            parsed = self._reality_touch_camera_json(completion)
            return self._sanitize_reality_touch_camera_observation(
                parsed,
                local_brightness=_single_line(frame.get("brightness"), 16).lower(),
                width=_safe_int(frame.get("width"), 0, 0),
                height=_safe_int(frame.get("height"), 0, 0),
                analyzed=bool(parsed),
            )
        except Exception as exc:
            logger.warning("[PrivateCompanion] 现实触及摄像头有限状态分析失败: %s", _single_line(exc, 180))
            recorder = getattr(self, "_record_llm_usage", None)
            if callable(recorder):
                recorder(
                    provider_id=provider_id,
                    task="reality_touch_camera",
                    prompt=prompt,
                    completion="",
                    elapsed_ms=int((time.time() - started) * 1000),
                    success=False,
                    error=str(exc),
                )
            return fallback

    def _record_reality_touch_camera_observation(
        self,
        user: dict[str, Any],
        *,
        purpose: str,
        success: bool,
        observation: dict[str, Any] | None = None,
        error: Any = "",
    ) -> dict[str, Any]:
        policy = self._reality_touch_camera_policy(user)
        item = {
            "at": _now_ts(),
            "purpose": _single_line(purpose, 120),
            "success": bool(success),
            "error": _single_line(error, 180),
        }
        if success and isinstance(observation, dict):
            for key in ("presence", "activity", "interruptibility", "brightness", "confidence", "analyzed", "width", "height", "summary"):
                if key in observation:
                    item[key] = observation[key]
        policy["last_observation"] = item
        history = policy.get("audit")
        if not isinstance(history, list):
            history = []
        history.append(dict(item))
        policy["audit"] = history[-20:]
        return item

    async def _reality_touch_camera_snapshot_for_user(
        self,
        user_id: str,
        purpose: str,
        *,
        include_preview: bool = False,
    ) -> dict[str, Any]:
        purpose_text = _single_line(purpose, 120)
        if not purpose_text:
            return {"status": "error", "message": "摄像头读取必须提供明确目的"}
        if not bool(getattr(self, "enable_experimental_bluetooth_wakeup", False)):
            return {"status": "disabled", "message": "现实触及总开关未开启"}
        if not bool(getattr(self, "enable_reality_touch_camera", False)):
            return {"status": "disabled", "message": "现实触及摄像头总开关未开启"}
        lock = getattr(self, "_reality_touch_camera_operation_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._reality_touch_camera_operation_lock = lock
        async with lock:
            users = self.data.get("users", {}) if isinstance(getattr(self, "data", None), dict) else {}
            user = users.get(str(user_id)) if isinstance(users, dict) else None
            if not isinstance(user, dict):
                return {"status": "error", "message": "没有找到对应的私聊用户"}
            if not self._reality_touch_camera_user_eligible(user_id):
                return {"status": "forbidden", "message": "主机摄像头只允许 AstrBot 管理员或主要用户本人使用"}
            if not self._reality_touch_camera_consented(user):
                return {"status": "forbidden", "message": "该用户尚未完成摄像头独立知情确认"}
            policy = self._reality_touch_camera_policy(user)
            if not bool(policy.get("enabled")):
                return {"status": "disabled", "message": "该用户的摄像头能力策略已关闭"}
            now = _now_ts()
            interval = _safe_int(getattr(self, "reality_touch_camera_min_interval_seconds", 60), 60, 10, 3600)
            last_attempt = _safe_int(policy.get("last_attempt_at"), 0, 0)
            remaining = interval - max(0, now - last_attempt)
            if last_attempt and remaining > 0:
                return {"status": "cooldown", "message": f"摄像头单帧读取仍在冷却中，请 {remaining} 秒后再试", "retry_after": remaining}
            policy["last_attempt_at"] = now
            policy["last_purpose"] = purpose_text
            self._save_data_sync()
            try:
                capture_timeout = _safe_int(getattr(self, "reality_touch_camera_capture_timeout_seconds", 5), 5, 2, 20)
                frame = await asyncio.wait_for(
                    asyncio.to_thread(self._capture_reality_touch_camera_frame),
                    timeout=capture_timeout,
                )
                preview_data_url = ""
                if include_preview:
                    jpeg_bytes = frame.get("jpeg_bytes")
                    if isinstance(jpeg_bytes, (bytes, bytearray)) and jpeg_bytes:
                        preview_data_url = "data:image/jpeg;base64," + base64.b64encode(bytes(jpeg_bytes)).decode("ascii")
                observation = await self._analyze_reality_touch_camera_frame(frame, purpose_text)
                item = self._record_reality_touch_camera_observation(
                    user,
                    purpose=purpose_text,
                    success=True,
                    observation=observation,
                )
                self._save_data_sync()
                result = {"status": "success", "message": "已完成一次单帧有限状态观察", "observation": item}
                if preview_data_url:
                    result["preview_data_url"] = preview_data_url
                return result
            except asyncio.TimeoutError:
                message = "摄像头单帧读取超时"
            except Exception as exc:
                message = _single_line(exc, 180) or "摄像头单帧读取失败"
            self._record_reality_touch_camera_observation(user, purpose=purpose_text, success=False, error=message)
            self._save_data_sync()
            return {"status": "error", "message": message}

    def _reality_touch_camera_user_snapshot(self, user: dict[str, Any], *, user_id: str = "") -> dict[str, Any]:
        consent = self._reality_touch_camera_consent(user)
        policy = self._reality_touch_camera_policy(user)
        latest = policy.get("last_observation") if isinstance(policy.get("last_observation"), dict) else {}
        eligible = self._reality_touch_camera_user_eligible(user_id)
        return {
            "eligible": eligible,
            "consented": eligible and self._reality_touch_camera_consented(user),
            "consent_version": _safe_int(consent.get("version"), 0, 0),
            "confirmed_at": _safe_int(consent.get("confirmed_at"), 0, 0),
            "enabled": eligible and bool(policy.get("enabled")),
            "last_attempt_at": _safe_int(policy.get("last_attempt_at"), 0, 0),
            "last_observation": dict(latest),
        }

    def _reality_touch_camera_page_snapshot(self) -> dict[str, Any]:
        catalog = self._reality_touch_camera_devices(refresh=False)
        return {
            "global_enabled": bool(getattr(self, "enable_reality_touch_camera", False)),
            "camera_index": _safe_int(getattr(self, "reality_touch_camera_index", 0), 0, 0, 100000),
            "min_interval_seconds": _safe_int(getattr(self, "reality_touch_camera_min_interval_seconds", 60), 60, 10, 3600),
            "capture_timeout_seconds": _safe_int(getattr(self, "reality_touch_camera_capture_timeout_seconds", 5), 5, 2, 20),
            "analysis_timeout_seconds": _safe_int(getattr(self, "reality_touch_camera_analysis_timeout_seconds", 25), 25, 5, 90),
            "confirmation_command": "陪伴 现实触及 摄像头确认",
            "backend": self._reality_touch_camera_backend_snapshot(),
            "devices": list(catalog.get("devices") or []),
            "devices_scanned_at": _safe_int(catalog.get("scanned_at"), 0, 0),
            "devices_error": _single_line(catalog.get("error"), 180),
            "boundary": "仅按明确任务读取单帧；可能发送给已配置视觉模型；不持续录像、不做人脸识别或情绪读脸；插件默认不保存原图。",
        }

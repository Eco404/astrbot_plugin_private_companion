# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.message_components import Plain, Record
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from astrbot_plugin_private_companion.tts_enhancement import TtsEnhancementMixin


class _OfficialTtsHarness(TtsEnhancementMixin):
    def __init__(self, *, enabled: bool = True, probability: object = 1) -> None:
        self.context = SimpleNamespace(
            get_config=lambda _umo: {
                "provider_tts_settings": {
                    "enable": enabled,
                    "trigger_probability": probability,
                }
            },
            get_using_tts_provider=lambda _umo: object(),
        )


def _llm_result(*components: object) -> MessageEventResult:
    result = MessageEventResult(chain=list(components))
    result.set_result_content_type(ResultContentType.LLM_RESULT)
    return result


class OfficialTtsSegmentingCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_reply_defers_remaining_bubbles_until_first_send(self) -> None:
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        plugin = object.__new__(PrivateCompanionPlugin)
        plugin.enabled = True
        plugin.segmented_proactive_scope = "all_llm"
        plugin.enable_framework_error_leak_guard = False
        plugin.enable_daily_case_review_experiment = False
        plugin._proactive_only_blocks_passive_event = lambda *_args: False
        plugin._feature_enabled_or_temp_unlocked = lambda key: key == "enable_segmented_proactive_reply"
        plugin._segmented_scope_allows_event = lambda _event: True
        plugin._restore_response_review_meta_leak_before_send = lambda *_args: False
        plugin._should_defer_segmenting_to_astrbot_tts = AsyncMock(return_value=False)
        plugin._platform_supports = lambda *_args, **_kwargs: True
        chunks = [[Plain("第一段。")], [Plain("第二段。")]]
        plugin._segment_llm_reply_chain = Mock(
            return_value=(chunks, True, "第一段。第二段。")
        )
        plugin._limit_private_routine_check_segments = lambda _text, value: value
        plugin._plain_text_segments_from_chunks = lambda _chunks: []
        plugin._build_result_from_chain = lambda chain: MessageEventResult(chain=list(chain))
        plugin._segmented_chunk_log_text = lambda chunk: chunk[0].text
        plugin._event_inbound_activity_ts = lambda _event: 10.0
        plugin._create_lifecycle_background_task = Mock()
        result = _llm_result(Plain("第一段。第二段。"))

        class Event:
            unified_msg_origin = "default:FriendMessage:10001"
            message_str = "普通聊天"
            _private_companion_reaction_expression_intent = {"query": "开心"}

            def __init__(self) -> None:
                self.result = result

            def get_result(self) -> MessageEventResult:
                return self.result

            def set_result(self, value: MessageEventResult) -> None:
                self.result = value

        event = Event()
        await PrivateCompanionPlugin.apply_segmented_llm_reply_scope(plugin, event)

        self.assertEqual(["第一段。"], [item.text for item in event.result.chain])
        self.assertEqual(
            chunks,
            event._private_companion_reaction_expression_expected_primary_chunks,
        )
        self.assertEqual(
            [chunks[1]],
            event._private_companion_reaction_expression_segmented_remainder["chunks"],
        )
        plugin._create_lifecycle_background_task.assert_not_called()

    async def test_deferred_reaction_tts_text_still_enters_segmenting(self) -> None:
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        plugin = object.__new__(PrivateCompanionPlugin)
        plugin.enabled = True
        plugin.segmented_proactive_scope = "all_llm"
        plugin.enable_framework_error_leak_guard = False
        plugin.enable_daily_case_review_experiment = False
        plugin._proactive_only_blocks_passive_event = lambda *_args: False
        plugin._feature_enabled_or_temp_unlocked = (
            lambda key: key == "enable_segmented_proactive_reply"
        )
        plugin._segmented_scope_allows_event = lambda _event: True
        plugin._restore_response_review_meta_leak_before_send = lambda *_args: False
        plugin._platform_supports = lambda *_args, **_kwargs: True
        chunks = [[Plain("第一段。")], [Plain("第二段。")]]
        plugin._segment_llm_reply_chain = Mock(
            return_value=(chunks, True, "第一段。第二段。")
        )
        plugin._limit_private_routine_check_segments = lambda _text, value: value
        plugin._plain_text_segments_from_chunks = lambda _chunks: []
        plugin._build_result_from_chain = lambda chain: MessageEventResult(
            chain=list(chain)
        )
        plugin._segmented_chunk_log_text = lambda chunk: chunk[0].text
        plugin._create_lifecycle_background_task = Mock()
        result = MessageEventResult(chain=[Plain("第一段。第二段。")])

        class Event:
            unified_msg_origin = "default:FriendMessage:10001"
            message_str = "普通聊天"
            _private_companion_reaction_expression_intent = {"query": "开心"}
            _private_companion_deferred_reaction_tts = {
                "normalized": "第一段。第二段。",
                "fallback_plain": "第一段。第二段。",
                "started_at": 10.0,
            }

            def __init__(self) -> None:
                self.result = result

            def get_result(self) -> MessageEventResult:
                return self.result

            def set_result(self, value: MessageEventResult) -> None:
                self.result = value

        event = Event()
        await PrivateCompanionPlugin.apply_segmented_llm_reply_scope(plugin, event)

        self.assertEqual(["第一段。"], [item.text for item in event.result.chain])
        self.assertEqual(
            chunks,
            event._private_companion_reaction_expression_expected_primary_chunks,
        )
        self.assertEqual(
            [chunks[1]],
            event._private_companion_reaction_expression_segmented_remainder[
                "chunks"
            ],
        )
        plugin._create_lifecycle_background_task.assert_not_called()

    async def test_logged_reaction_reply_uses_active_segment_rules_after_tts(self) -> None:
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        plugin = object.__new__(PrivateCompanionPlugin)
        plugin.enabled = True
        plugin.enable_segmented_proactive_reply = True
        plugin.segmented_proactive_scope = "all_llm"
        plugin.enable_framework_error_leak_guard = False
        plugin.enable_daily_case_review_experiment = False
        plugin.enable_proactive_quote_trigger_message = False
        plugin.enable_segmented_proactive_content_replacement = False
        plugin.segmented_proactive_content_replacements = []
        plugin.segmented_proactive_threshold = 500
        plugin.segmented_proactive_min_segment_chars = 5
        plugin.segmented_proactive_max_segments = 5
        plugin.segmented_proactive_split_mode = "words"
        plugin.segmented_proactive_split_words = [
            "。",
            "？",
            "！",
            "~",
            "?",
            ".",
            "!",
            ";",
            "；",
            "……",
            "（",
            "“",
            "，",
            "…",
        ]
        plugin.segmented_proactive_regex = r"(?<=[。！？!?…~～])\s*|\n+"
        plugin.enable_segmented_proactive_content_cleanup = True
        plugin.segmented_proactive_content_cleanup_rule = r"[\n。？！]"
        plugin.segmented_proactive_content_cleanup_scope = "all"
        plugin.segmented_proactive_content_cleanup_words = ["。", "，"]
        plugin._proactive_only_blocks_passive_event = lambda *_args: False
        plugin._feature_enabled_or_temp_unlocked = (
            lambda key: key == "enable_segmented_proactive_reply"
        )
        plugin._segmented_scope_allows_event = lambda _event: True
        plugin._restore_response_review_meta_leak_before_send = lambda *_args: False
        plugin._platform_supports = lambda *_args, **_kwargs: True
        plugin._plain_text_segments_from_chunks = lambda _chunks: []
        plugin._create_lifecycle_background_task = Mock()
        source = (
            "唔…外面还在下着雨呢，这么晚了还要例行检查呀？"
            "比折大人这次打算查点什么……还是说，其实只是睡不着想找我说话呀？"
        )
        result = MessageEventResult(chain=[Plain(source)])

        class Event:
            unified_msg_origin = "default:FriendMessage:10001"
            message_str = "例行检查"
            _private_companion_reaction_expression_intent = {"query": "回应"}
            _private_companion_deferred_reaction_tts = {
                "normalized": source,
                "fallback_plain": source,
                "started_at": 10.0,
            }

            def __init__(self) -> None:
                self.result = result

            def get_result(self) -> MessageEventResult:
                return self.result

            def set_result(self, value: MessageEventResult) -> None:
                self.result = value

        event = Event()
        await PrivateCompanionPlugin.apply_segmented_llm_reply_scope(plugin, event)

        expected = event._private_companion_reaction_expression_expected_primary_chunks
        self.assertEqual(2, len(expected))
        self.assertEqual(expected[0], event.result.chain)
        self.assertEqual(
            [expected[1]],
            event._private_companion_reaction_expression_segmented_remainder[
                "chunks"
            ],
        )
        self.assertNotEqual(source, "".join(item.text for item in expected[0]))
        plugin._create_lifecycle_background_task.assert_not_called()

    async def test_segmenting_hook_leaves_official_tts_llm_result_untouched(self) -> None:
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        plugin = object.__new__(PrivateCompanionPlugin)
        plugin.enabled = True
        plugin.segmented_proactive_scope = "all_llm"
        plugin.context = _OfficialTtsHarness().context
        plugin._proactive_only_blocks_passive_event = lambda *_args: False
        plugin._feature_enabled_or_temp_unlocked = lambda key: key == "enable_segmented_proactive_reply"
        plugin._segmented_scope_allows_event = lambda _event: True
        plugin._restore_response_review_meta_leak_before_send = lambda *_args: False
        plugin._segment_llm_reply_chain = Mock(
            side_effect=AssertionError("official TTS result must not enter plugin segmenting")
        )
        result = _llm_result(Plain("第一段。第二段。"))

        class Event:
            unified_msg_origin = "default:FriendMessage:10001"

            @staticmethod
            def get_result() -> MessageEventResult:
                return result

        with patch(
            "astrbot.core.star.session_llm_manager.SessionServiceManager.should_process_tts_request",
            new=AsyncMock(return_value=True),
        ):
            await PrivateCompanionPlugin.apply_segmented_llm_reply_scope(plugin, Event())

        plugin._segment_llm_reply_chain.assert_not_called()
        self.assertTrue(result.is_llm_result())

    async def test_enabled_official_tts_owns_unmodified_llm_text(self) -> None:
        harness = _OfficialTtsHarness()
        event = SimpleNamespace(unified_msg_origin="default:FriendMessage:10001")
        result = _llm_result(Plain("第一段。第二段。"))

        with patch(
            "astrbot.core.star.session_llm_manager.SessionServiceManager.should_process_tts_request",
            new=AsyncMock(return_value=True),
        ):
            should_defer = await harness._should_defer_segmenting_to_astrbot_tts(
                event,
                result,
                list(result.chain),
            )

        self.assertTrue(should_defer)
        self.assertTrue(result.is_llm_result())

    async def test_session_tts_override_is_respected(self) -> None:
        harness = _OfficialTtsHarness()
        event = SimpleNamespace(unified_msg_origin="default:FriendMessage:10001")
        result = _llm_result(Plain("保持文字。"))

        with patch(
            "astrbot.core.star.session_llm_manager.SessionServiceManager.should_process_tts_request",
            new=AsyncMock(return_value=False),
        ):
            should_defer = await harness._should_defer_segmenting_to_astrbot_tts(
                event,
                result,
                list(result.chain),
            )

        self.assertFalse(should_defer)

    async def test_disabled_or_zero_probability_official_tts_does_not_take_over(self) -> None:
        event = SimpleNamespace(unified_msg_origin="default:FriendMessage:10001")
        result = _llm_result(Plain("保持插件分段。"))

        for harness in (
            _OfficialTtsHarness(enabled=False),
            _OfficialTtsHarness(probability=0),
            _OfficialTtsHarness(probability="0"),
        ):
            with self.subTest(harness=harness):
                should_defer = await harness._should_defer_segmenting_to_astrbot_tts(
                    event,
                    result,
                    list(result.chain),
                )
                self.assertFalse(should_defer)

    async def test_empty_or_invalid_probability_matches_astrbot_default(self) -> None:
        event = SimpleNamespace(unified_msg_origin="default:FriendMessage:10001")
        result = _llm_result(Plain("交给官方 TTS。"))

        for probability in (None, "", "invalid"):
            with self.subTest(probability=probability):
                harness = _OfficialTtsHarness(probability=probability)
                with patch(
                    "astrbot.core.star.session_llm_manager.SessionServiceManager.should_process_tts_request",
                    new=AsyncMock(return_value=True),
                ):
                    should_defer = await harness._should_defer_segmenting_to_astrbot_tts(
                        event,
                        result,
                        list(result.chain),
                    )
                self.assertTrue(should_defer)

    async def test_plugin_owned_or_media_result_never_reenters_official_tts(self) -> None:
        harness = _OfficialTtsHarness()
        plugin_event = SimpleNamespace(
            unified_msg_origin="default:FriendMessage:10001",
            _private_companion_tts_request_applied=True,
        )
        plain_result = _llm_result(Plain("插件负责本轮语音。"))
        media_event = SimpleNamespace(unified_msg_origin="default:FriendMessage:10001")
        media_result = _llm_result(Record(file="voice.wav"), Plain("语音对应正文。"))

        self.assertFalse(
            await harness._should_defer_segmenting_to_astrbot_tts(
                plugin_event,
                plain_result,
                list(plain_result.chain),
            )
        )
        self.assertFalse(
            await harness._should_defer_segmenting_to_astrbot_tts(
                media_event,
                media_result,
                list(media_result.chain),
            )
        )

    async def test_plugin_owned_probability_miss_cannot_fall_through_to_official_tts(self) -> None:
        harness = _OfficialTtsHarness()
        harness.enabled = True
        event = SimpleNamespace(
            unified_msg_origin="default:FriendMessage:10001",
            _private_companion_tts_request_applied=True,
        )
        result = _llm_result(Plain("插件本轮决定保持文字。"))
        event.get_result = lambda: result

        await harness.finalize_outbound_tts_markup_guard(event)

        self.assertFalse(result.is_llm_result())
        self.assertEqual("插件本轮决定保持文字。", result.chain[0].text)

    async def test_unowned_llm_result_remains_available_to_official_tts(self) -> None:
        harness = _OfficialTtsHarness()
        harness.enabled = True
        event = SimpleNamespace(unified_msg_origin="default:FriendMessage:10001")
        result = _llm_result(Plain("交给 AstrBot 官方 TTS。"))
        event.get_result = lambda: result

        await harness.finalize_outbound_tts_markup_guard(event)

        self.assertTrue(result.is_llm_result())

    async def test_missing_official_provider_keeps_plugin_segmenting(self) -> None:
        harness = _OfficialTtsHarness()
        harness.context.get_using_tts_provider = lambda _umo: None
        event = SimpleNamespace(unified_msg_origin="default:FriendMessage:10001")
        result = _llm_result(Plain("保持插件分段。"))

        should_defer = await harness._should_defer_segmenting_to_astrbot_tts(
            event,
            result,
            list(result.chain),
        )

        self.assertFalse(should_defer)


if __name__ == "__main__":
    unittest.main()

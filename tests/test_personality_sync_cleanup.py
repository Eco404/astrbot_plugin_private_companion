# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import sys
import types
import unittest

try:
    from astrbot.api import logger as _astrbot_logger  # noqa: F401
except ModuleNotFoundError:
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logging.getLogger("personality-sync-cleanup-test")
    astrbot_module.api = astrbot_api_module
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", astrbot_api_module)

from astrbot_plugin_private_companion.helpers import (
    _strip_internal_message_blocks,
    _strip_outbound_control_blocks,
)


class PersonalitySyncCleanupTests(unittest.TestCase):
    def test_complete_block_and_comment_are_removed(self) -> None:
        raw = """算啦，原谅你了。
<!-- private_companion_personality_sync_v1 -->
<personality_sync>
{"last_interaction_mood": "warm_tease"}
</personality_sync>"""

        self.assertEqual("算啦，原谅你了。", _strip_internal_message_blocks(raw))
        self.assertEqual("算啦，原谅你了。", _strip_outbound_control_blocks(raw))

    def test_truncated_block_is_removed(self) -> None:
        raw = (
            "我还记得呢。\n"
            "<personality_sync>\n"
            '{"last_interaction_mood": "warm_tease"'
        )

        self.assertEqual("我还记得呢。", _strip_internal_message_blocks(raw))
        self.assertEqual("我还记得呢。", _strip_outbound_control_blocks(raw))

    def test_case_and_spacing_variants_are_removed(self) -> None:
        raw = "可见正文< PERSONALITY_SYNC >secret< / PERSONALITY_SYNC >"

        self.assertEqual("可见正文", _strip_outbound_control_blocks(raw))


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Regression tests for internal ``personality_sync`` metadata cleanup.

Purpose:
    Ensure internal personality synchronization comments, complete blocks, and
    truncated blocks never appear in user-visible outbound chat text, while
    preserving the surrounding visible reply.

Run from the parent directory of this plugin package:
    python -m pytest -q \
        astrbot_plugin_private_companion/tests/test_personality_sync_cleanup.py

How it works:
    Each test passes representative model output directly through the same two
    cleanup functions used by internal-history and outbound-message pipelines,
    then compares the result with the exact visible text expected by users.
    This makes future regressions fail during the test suite before release.
"""

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

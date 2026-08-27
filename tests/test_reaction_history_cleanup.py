# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace

from astrbot_plugin_private_companion.main import PrivateCompanionPlugin


def test_neutralize_stale_reaction_feedback_preserves_surrounding_feedback() -> None:
    plugin = object.__new__(PrivateCompanionPlugin)
    request = SimpleNamespace(
        contexts=[
            {
                "role": "assistant",
                "content": "收到你的反馈\n<pc-reaction-expression kind='smile'>\n内部标签\n</pc-reaction-expression>\n谢谢",
            },
            {
                "role": "user",
                "content": [{"text": "&lt;PC_REACTION_EXPRESSION&gt;hidden&lt;/PC_REACTION_EXPRESSION&gt;"}],
            },
        ]
    )

    plugin._neutralize_stale_reaction_feedback_in_history(
        SimpleNamespace(unified_msg_origin="test"), request
    )

    assert request.contexts[0]["content"] == "收到你的反馈\n\n谢谢"
    assert request.contexts[1]["content"][0]["text"] == ""

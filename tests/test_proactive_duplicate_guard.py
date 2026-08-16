from astrbot_plugin_private_companion.daily_state import DailyStateMixin


class _DuplicateGuardHarness(DailyStateMixin):
    pass


def test_current_proactive_candidate_is_not_treated_as_sent_history():
    harness = _DuplicateGuardHarness()
    now = 1_000_100.0
    text = "揉了下眼睛，忽然想起刚刷到的有趣视频。"
    user = {
        "last_companion_message": text,
        "last_companion_message_at": now - 5,
        "proactive_sending": True,
        "proactive_sending_started_at": now - 30,
        "recent_proactive_topics": [],
    }

    reason = harness._recent_proactive_text_duplicate_reason(user, text=text, now=now)

    assert reason == ""


def test_confirmed_message_before_current_proactive_still_blocks_duplicate():
    harness = _DuplicateGuardHarness()
    now = 1_000_100.0
    text = "揉了下眼睛，忽然想起刚刷到的有趣视频。"
    user = {
        "last_companion_message": text,
        "last_companion_message_at": now - 120,
        "proactive_sending": True,
        "proactive_sending_started_at": now - 30,
        "recent_proactive_topics": [],
    }

    reason = harness._recent_proactive_text_duplicate_reason(user, text=text, now=now)

    assert "聊天里已经说过相似内容" in reason


def test_inbound_reply_time_does_not_refresh_old_companion_message():
    harness = _DuplicateGuardHarness()
    now = 1_000_100.0
    text = "揉了下眼睛，忽然想起刚刷到的有趣视频。"
    user = {
        "last_companion_message": text,
        "last_companion_message_at": 0,
        "last_reply_at": now - 5,
        "proactive_sending": True,
        "proactive_sending_started_at": now - 30,
        "recent_proactive_topics": [],
    }

    reason = harness._recent_proactive_text_duplicate_reason(user, text=text, now=now)

    assert reason == ""


def test_ordinary_weather_variants_share_one_long_lived_topic():
    harness = _DuplicateGuardHarness()
    now = 1_000_100.0
    user = {
        "recent_proactive_topics": [
            {
                "ts": now - 12 * 3600,
                "signature": "ordinary_weather_topic",
                "text": "外面开始下雨了。",
            }
        ]
    }

    signature = harness._proactive_topic_signature("今天气温降下来了，你那边冷不冷？")

    assert signature == "ordinary_weather_topic"
    assert harness._recent_proactive_topic_repeated(user, signature, now=now)


def test_non_weather_outdoor_topic_is_not_collapsed_into_weather():
    harness = _DuplicateGuardHarness()

    signature = harness._proactive_topic_signature("我在外面吃饭，刚碰到一家小店。")

    assert signature != "ordinary_weather_topic"


def test_legacy_weather_topic_is_migrated_during_cleanup():
    harness = _DuplicateGuardHarness()
    now = 1_000_100.0
    user = {
        "recent_proactive_topics": [
            {
                "ts": now - 8 * 3600,
                "signature": "morning_weather_check",
                "text": "早呀，外面天阴阴的，好想赖床。",
            }
        ]
    }

    recent = harness._cleanup_recent_proactive_topics(user, now=now)

    assert len(recent) == 1
    assert recent[0]["signature"] == "ordinary_weather_topic"


def test_shared_recipient_address_does_not_make_unrelated_topics_duplicates():
    harness = _DuplicateGuardHarness()
    now = 1_000_100.0
    old_text = "比折大人，刚泡的蜂蜜茶还冒着热气。"
    candidate = "比折大人，桌腿边的球拍被斜光照亮了。"
    user = {
        "recent_proactive_topics": [
            {
                "ts": now - 60,
                "signature": harness._proactive_topic_signature(old_text),
                "text": old_text,
            }
        ]
    }

    reason = harness._recent_proactive_text_duplicate_reason(user, text=candidate, now=now)

    assert reason == ""


def test_same_scene_rephrased_with_shared_recipient_address_is_still_duplicate():
    harness = _DuplicateGuardHarness()
    now = 1_000_100.0
    old_text = "比折大人，桌腿边的球拍被斜光照亮了。"
    candidate = "比折大人，斜光正落在桌腿边的球拍上。"
    user = {
        "recent_proactive_topics": [
            {
                "ts": now - 60,
                "signature": harness._proactive_topic_signature(old_text),
                "text": old_text,
            }
        ]
    }

    reason = harness._recent_proactive_text_duplicate_reason(user, text=candidate, now=now)

    assert "已发送相似主动" in reason


def test_dedup_disabled_returns_no_reason():
    harness = _DuplicateGuardHarness()
    harness.proactive_dedup_enabled = False
    now = 1_000_100.0
    text = "揉了下眼睛，忽然想起刚刷到的有趣视频。"
    user = {
        "last_companion_message": text,
        "last_companion_message_at": now - 120,
        "proactive_sending": True,
        "proactive_sending_started_at": now - 30,
        "recent_proactive_topics": [],
    }

    reason = harness._recent_proactive_text_duplicate_reason(user, text=text, now=now)

    assert reason == ""


def test_dedup_last_message_window_config():
    harness = _DuplicateGuardHarness()
    harness.proactive_dedup_last_message_window_minutes = 5
    now = 1_000_100.0
    text = "揉了下眼睛，忽然想起刚刷到的有趣视频。"
    user = {
        "last_companion_message": text,
        "last_companion_message_at": now - 600,
        "proactive_sending": True,
        "proactive_sending_started_at": now - 30,
        "recent_proactive_topics": [],
    }

    reason = harness._recent_proactive_text_duplicate_reason(user, text=text, now=now)

    # 10 分钟前最后一条消息，超出 5 分钟配置窗口 → 不判重复
    assert reason == ""


def test_dedup_last_message_enabled_off():
    harness = _DuplicateGuardHarness()
    harness.proactive_dedup_last_message_enabled = False
    now = 1_000_100.0
    text = "揉了下眼睛，忽然想起刚刷到的有趣视频。"
    user = {
        "last_companion_message": text,
        "last_companion_message_at": now - 120,
        "proactive_sending": True,
        "proactive_sending_started_at": now - 30,
        "recent_proactive_topics": [],
    }

    reason = harness._recent_proactive_text_duplicate_reason(user, text=text, now=now)

    # Layer-2 关闭后不再对比最后一条消息（默认窗口内本应判重）
    assert reason == ""


def test_dedup_min_shared_tokens_config():
    harness = _DuplicateGuardHarness()
    harness.proactive_dedup_min_shared_tokens = 2

    assert harness._topic_signature_similar("冲浪|休息", "冲浪|做饭") is False

    harness.proactive_dedup_min_shared_tokens = 1
    assert harness._topic_signature_similar("冲浪|休息", "冲浪|做饭") is True


def test_dedup_policies_exclude_semantic():
    harness = _DuplicateGuardHarness()
    harness.proactive_dedup_policies = "content_fingerprint,life_event"
    policies = harness._proactive_dedup_enabled_policies()

    assert "semantic" not in policies
    # semantic 被移除 → 关系关怀不再参与去重
    assert harness._proactive_similarity_guard_enabled(
        {"planned_proactive_burst": False},
        is_troubleshooting=False,
        action="message",
        timeliness="routine",
        duplicate_policy="semantic",
        enabled_policies=policies,
    ) is False
    # content_fingerprint 仍在列表 → 内容去重保留
    assert harness._proactive_similarity_guard_enabled(
        {"planned_proactive_burst": False},
        is_troubleshooting=False,
        action="message",
        timeliness="routine",
        duplicate_policy="content_fingerprint",
        enabled_policies=policies,
    ) is True

from __future__ import annotations

from person_context_contract import build_identity_key, ensure_person_store
from unified_person_registry import UnifiedPersonRegistry


def identity(platform="qq", subject="u-1", companion="c-1", bot="b-1", adapter="a-1"):
    return {
        "companion_instance_id": companion,
        "bot_account_id": bot,
        "adapter_instance_id": adapter,
        "subject_namespace": platform,
        "platform_subject_id": subject,
    }


def test_five_dimension_identity_and_stable_id():
    a, b = {}, {}
    left, right = UnifiedPersonRegistry(a), UnifiedPersonRegistry(b)
    one = left.create_or_link(identity(), {"display_name": "小雪"}, operation_id="create-1")
    two = right.create_or_link(identity(), {"display_name": "小雪"}, operation_id="create-2")
    assert one["person_id"] == two["person_id"]
    assert left.resolve(identity())["state"] == "resolved"
    assert left.resolve(identity("qq", "u-1", "c-1", "b-2", "a-1"))["state"] == "pending"


def test_duplicate_create_is_idempotent_and_projection_valid():
    store = {}
    registry = UnifiedPersonRegistry(store)
    first = registry.create_or_link(identity(), operation_id="create-1")
    second = registry.create_or_link(identity(), {"display_name": "other"}, operation_id="create-2")
    assert first["person_id"] == second["person_id"]
    assert second["changed"] is False
    projection = registry.read_projection(first["person_id"])
    assert projection and projection["person_id"] == first["person_id"]


def test_link_projection_and_pending_without_explicit_operation():
    store = {}
    registry = UnifiedPersonRegistry(store)
    assert registry.create_or_link(identity(), operation_id="create")["state"] == "resolved"
    person_id = registry.resolve(identity())["person_id"]
    result = registry.link_identity(person_id, identity("telegram", "t-1"), operation_id="link-1")
    assert result["state"] == "resolved"
    assert registry.resolve(identity("telegram", "t-1"))["person_id"] == person_id
    assert registry.create_or_link(identity("qq", "new"))["state"] == "pending"


def test_group_overlay_isolated_and_safe():
    store = {}
    registry = UnifiedPersonRegistry(store)
    person_id = registry.create_or_link(identity(), operation_id="create")["person_id"]
    first = registry.upsert_group_overlay(person_id, "qq:group-1", {"nickname": "甲", "raw_prompt": "secret", "chat_text": "private"}, operation_id="g1")
    second = registry.upsert_group_overlay(person_id, "qq:group-2", {"nickname": "乙"}, operation_id="g2")
    assert first["state"] == second["state"] == "resolved"
    assert registry.read_group_overlay(person_id, "qq:group-1")["overlay"] == {"nickname": "甲"}
    assert registry.read_group_overlay(person_id, "qq:group-2")["overlay"] == {"nickname": "乙"}
    assert registry.read_group_overlay(person_id, "qq:group-3") is None
    assert "chat_text" not in str(store)
    assert "raw_prompt" not in str(store)


def test_invalid_identity_and_corrupt_projection_are_explicit():
    store = {}
    registry = UnifiedPersonRegistry(store)
    assert registry.resolve({})["state"] == "invalid"
    ensure_person_store(store)
    store["unified_person"]["identity_links"]["bad"] = {"person_id": "person_bad"}
    assert registry.status()["state"] in {"pending", "invalid"}

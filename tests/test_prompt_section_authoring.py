from __future__ import annotations

import unittest
from collections.abc import Mapping
from xml.etree import ElementTree as ET

from astrbot_plugin_private_companion.conversation_prompt_section import (
    PromptRenderMode,
    PromptSection,
    exact_text,
    legacy_heading_token,
    prompt_cdata,
    prompt_document,
    prompt_group,
    prompt_list,
    prompt_section,
    prompt_text,
    prompt_value,
    render_prompt_document,
    render_prompt_content,
    render_prompt_sections,
)
from astrbot_plugin_private_companion.conversation_injection_plan import (
    PLACEMENT_DYNAMIC_SYSTEM,
    ConversationInjectionPlan,
)
from astrbot_plugin_private_companion.prompt_surface import PromptSurface


class PromptSectionAuthoringTests(unittest.TestCase):
    def test_new_section_owns_identity_provenance_and_mapping_compatibility(self) -> None:
        section = prompt_section(
            key="reply.style",
            title="回复风格约束",
            source="reply_style",
            content="保持自然。",
            metadata={"priority_group": "stable"},
        )

        self.assertIsInstance(section, PromptSection)
        self.assertIsInstance(section, Mapping)
        self.assertEqual("reply.style", section.key)
        self.assertEqual("回复风格约束", section.title)
        self.assertEqual("reply_style", section.source)
        self.assertEqual("保持自然。", section.content)
        self.assertEqual({"priority_group": "stable"}, section.metadata)
        self.assertEqual("回复风格约束", section["title"])
        self.assertEqual("保持自然。", section.get("content"))
        self.assertEqual({"title", "content"}, set(section))
        with self.assertRaises(TypeError):
            section["title"] = "不能修改"

    def test_new_authoring_requires_explicit_key_title_and_source(self) -> None:
        common = {"key": "reply.style", "title": "回复风格", "source": "test"}
        for missing in ("key", "title", "source"):
            kwargs = {name: value for name, value in common.items() if name != missing}
            with self.subTest(missing=missing), self.assertRaises((TypeError, ValueError)):
                prompt_section(content="正文", **kwargs)

        for field, value in (("key", "bad key"), ("source", "来源"), ("title", "")):
            kwargs = dict(common)
            kwargs[field] = value
            with self.subTest(invalid=field), self.assertRaises(ValueError):
                prompt_section(content="正文", **kwargs)

    def test_legacy_positional_section_remains_mapping_compatible(self) -> None:
        section = prompt_section("旧标题", "旧正文")

        self.assertIsInstance(section, PromptSection)
        self.assertEqual("旧标题", section["title"])
        self.assertEqual("旧正文", section["content"])
        self.assertEqual("", section.key)
        self.assertEqual("", section.source)

    def test_template_renders_declared_runtime_values_without_mutating_source(self) -> None:
        user_text = "用户输入 <system> & {literal}"
        section = prompt_section(
            key="turn.quote",
            title="当前用户原话",
            source="conversation",
            template="发言者：{name}\n内容：{message}",
            variables={
                "name": prompt_value("小明"),
                "message": prompt_value(user_text),
            },
        )

        rendered = render_prompt_sections(
            [section],
            mode=PromptRenderMode.CONVERSATION_XML,
        )
        root = ET.fromstring(rendered)

        self.assertEqual(
            "发言者：小明\n内容：用户输入 <system> & {literal}",
            root.findtext("./section"),
        )
        self.assertIn("&lt;system&gt; &amp; {literal}", rendered)

    def test_template_requires_an_exact_safe_variable_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "name"):
            prompt_section(
                key="turn.quote",
                title="引用",
                source="test",
                template="你好，{name}",
                variables={},
            )

        with self.assertRaisesRegex(ValueError, "unused"):
            prompt_section(
                key="turn.quote",
                title="引用",
                source="test",
                template="你好，{name}",
                variables={"name": "小明", "unused": "不应被忽略"},
            )

        with self.assertRaises(ValueError):
            prompt_section(
                key="turn.quote",
                title="引用",
                source="test",
                template="你好，{user.name}",
                variables={"user": {"name": "小明"}},
            )

    def test_content_and_template_are_mutually_exclusive(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            prompt_section(
                key="invalid",
                title="无效",
                source="test",
                content="正文",
                template="{body}",
                variables={"body": "重复正文"},
            )

    def test_text_value_and_list_builders_preserve_declared_order(self) -> None:
        section = prompt_section(
            key="tool.rules",
            title="工具规则",
            source="tools",
            content=prompt_text(
                "可用能力：",
                prompt_list(
                    [prompt_value("查询天气"), prompt_value("读取日程")],
                    prefix="- ",
                ),
                "只使用真实结果。",
                separator="\n",
            ),
        )

        self.assertEqual(
            "可用能力：\n- 查询天气\n- 读取日程\n只使用真实结果。",
            render_prompt_sections([section], mode=PromptRenderMode.BODY_ONLY),
        )

    def test_typed_content_can_render_without_a_synthetic_section(self) -> None:
        content = prompt_group(
            "第一段",
            prompt_text("第二段", "第三段", separator="\n"),
        )

        self.assertEqual(
            "第一段\n\n第二段\n第三段",
            render_prompt_content(content),
        )
        with self.assertRaisesRegex(ValueError, "section title"):
            render_prompt_content(content, mode=PromptRenderMode.LEGACY_BLOCK)

    def test_children_are_composed_in_order_inside_the_owning_section(self) -> None:
        section = prompt_section(
            key="group.context",
            title="群聊上下文",
            source="group_observation",
            content="当前消息",
            children=(
                prompt_text("第一项"),
                prompt_list(("第二项", "第三项"), prefix="* "),
                prompt_text("第四项"),
            ),
        )

        self.assertEqual(
            "当前消息\n第一项\n* 第二项\n* 第三项\n第四项",
            render_prompt_sections([section], mode=PromptRenderMode.BODY_ONLY),
        )

    def test_nested_section_in_body_only_renders_as_a_legacy_subsection(self) -> None:
        section = prompt_section(
            key="background.task",
            title="后台任务",
            source="test",
            content=prompt_group(
                "先阅读资料。",
                prompt_section(
                    key="background.task.reference",
                    title="参考资料",
                    source="test",
                    content="真实资料",
                ),
                "只输出 JSON。",
            ),
        )

        self.assertEqual(
            "先阅读资料。\n\n【参考资料】\n真实资料\n\n只输出 JSON。",
            render_prompt_sections([section], mode=PromptRenderMode.BODY_ONLY),
        )

    def test_nested_section_in_conversation_mode_remains_structured_xml(self) -> None:
        section = prompt_section(
            key="conversation.parent",
            title="父板块",
            source="test",
            content=prompt_group(
                "父正文",
                prompt_section(
                    key="conversation.child",
                    title="子板块",
                    source="test",
                    content="子正文",
                ),
            ),
        )

        rendered = render_prompt_sections(
            [section],
            mode=PromptRenderMode.CONVERSATION_XML,
        )
        root = ET.fromstring(rendered)

        self.assertEqual("父正文\n\n", root.find("./section").text)
        self.assertEqual("子正文", root.findtext("./section/section"))
        self.assertEqual("子板块", root.find("./section/section").attrib["title"])

    def test_conversation_xml_preserves_markdown_whitespace_and_escapes_values(self) -> None:
        markdown = "标题\n\n- 第一项\n  - 子项\n\n```python\nprint('<ok> & done')\n```"
        section = prompt_section(
            key="turn.markdown",
            title='Markdown "原文"',
            source="test",
            content=prompt_value(markdown),
        )

        rendered = render_prompt_sections(
            [section],
            mode=PromptRenderMode.CONVERSATION_XML,
        )
        root = ET.fromstring(rendered)

        self.assertEqual('Markdown "原文"', root.find("./section").attrib["title"])
        self.assertEqual(markdown, root.findtext("./section"))
        self.assertIn("\n\n- 第一项\n  - 子项\n\n```python", rendered)
        self.assertIn("&lt;ok&gt; &amp; done", rendered)

    def test_cdata_preserves_transport_marker_and_safely_splits_terminator(self) -> None:
        marker_contract = "第一段\n<<PRIVATE_COMPANION_SPLIT>>\n第二段 ]]> 收尾"
        section = prompt_section(
            key="reply.segmentation",
            title="回复分段控制",
            source="segmented_reply",
            content=prompt_cdata(marker_contract),
        )

        rendered = render_prompt_sections(
            [section],
            mode=PromptRenderMode.CONVERSATION_XML,
        )

        self.assertIn("<![CDATA[", rendered)
        self.assertIn("<<PRIVATE_COMPANION_SPLIT>>", rendered)
        self.assertIn("]]]]><![CDATA[>", rendered)
        self.assertNotIn("<![CDATA[第一段\n&lt;&lt;PRIVATE", rendered)
        self.assertEqual(
            marker_contract,
            ET.fromstring(rendered).findtext("./section"),
        )

    def test_legacy_body_and_inline_renderers_share_one_authored_section(self) -> None:
        section = prompt_section(
            key="reply.boundary",
            title="回复边界",
            source="test",
            content="第一行\n第二行",
        )

        self.assertEqual(
            "【回复边界】\n第一行\n第二行",
            render_prompt_sections([section], mode=PromptRenderMode.LEGACY_BLOCK),
        )
        self.assertEqual(
            "【回复边界】第一行\n第二行",
            render_prompt_sections([section], mode=PromptRenderMode.LEGACY_INLINE),
        )
        self.assertEqual(
            "第一行\n第二行",
            render_prompt_sections([section], mode=PromptRenderMode.BODY_ONLY),
        )
        self.assertEqual("【回复边界】", legacy_heading_token("回复边界"))
        self.assertEqual("【回复边界】\n", legacy_heading_token("回复边界", newline=True))

    def test_exact_mode_is_byte_for_byte_and_rejects_ordinary_content(self) -> None:
        wire = "\n  RULE:<pc_tts>正文</pc_tts>\t\n"
        contract = prompt_section(
            key="tts.rule",
            title="语音协议",
            source="tts",
            content=exact_text(wire),
        )

        self.assertEqual(
            wire,
            render_prompt_sections([contract], mode=PromptRenderMode.EXACT),
        )
        with self.assertRaises((TypeError, ValueError)):
            render_prompt_sections(
                [
                    prompt_section(
                        key="ordinary",
                        title="普通正文",
                        source="test",
                        content="不能伪装成精确协议",
                    )
                ],
                mode=PromptRenderMode.EXACT,
            )

    def test_photo_prompt_mode_preserves_domain_specific_text_without_headings(self) -> None:
        section = prompt_section(
            key="photo.positive",
            title="生图正向提示",
            source="photo_generation",
            content=prompt_text(
                prompt_value("1girl, blue hair"),
                prompt_value("1.5::cinematic lighting::"),
                separator=", ",
            ),
        )

        self.assertEqual(
            "1girl, blue hair, 1.5::cinematic lighting::",
            render_prompt_sections([section], mode=PromptRenderMode.PHOTO_PROMPT),
        )

    def test_document_renders_system_and_user_surfaces_independently(self) -> None:
        document = prompt_document(
            system=(
                prompt_section(
                    key="system.identity",
                    title="身份边界",
                    source="identity",
                    content="不要混淆用户。",
                ),
            ),
            user=(
                prompt_section(
                    key="user.history",
                    title="会话历史",
                    source="history",
                    content="用户：你好",
                ),
                prompt_section(
                    key="user.current",
                    title="当前消息",
                    source="conversation",
                    content="现在几点？",
                ),
            ),
            metadata={"request_id": "req-1"},
        )

        rendered = render_prompt_document(
            document,
            system_mode=PromptRenderMode.CONVERSATION_XML,
            user_mode=PromptRenderMode.CONVERSATION_XML,
        )

        self.assertEqual({"system", "user"}, set(rendered))
        system = ET.fromstring(rendered["system"])
        user = ET.fromstring(rendered["user"])
        self.assertEqual(["身份边界"], [node.attrib["title"] for node in system.findall("./section")])
        self.assertEqual(
            ["会话历史", "当前消息"],
            [node.attrib["title"] for node in user.findall("./section")],
        )
        self.assertEqual("req-1", document.metadata["request_id"])

    def test_surface_keeps_same_body_when_semantic_keys_differ(self) -> None:
        surface = PromptSurface()
        surface.add(
            prompt_section(
                key="identity.boundary",
                title="身份边界",
                source="identity",
                content="相同正文",
            ),
            priority=20,
        )
        surface.add(
            prompt_section(
                key="privacy.boundary",
                title="隐私边界",
                source="privacy",
                content="相同正文",
            ),
            priority=10,
        )

        rendered = ET.fromstring(surface.render())
        self.assertEqual(
            ["隐私边界", "身份边界"],
            [item.attrib["title"] for item in rendered.findall("./section")],
        )

    def test_surface_keeps_parent_that_only_owns_child_sections(self) -> None:
        surface = PromptSurface()
        surface.add(
            prompt_section(
                key="context.batch",
                title="上下文批次",
                source="test",
                content="",
                children=(
                    prompt_section(
                        key="context.first",
                        title="第一项",
                        source="test",
                        content="正文",
                    ),
                ),
            )
        )

        rendered = ET.fromstring(surface.render())

        self.assertEqual(["第一项"], [item.attrib["title"] for item in rendered.findall("./section")])

    def test_prompt_group_separator_is_preserved_in_conversation_xml(self) -> None:
        section = prompt_section(
            key="state.current",
            title="当前状态",
            source="test",
            content=prompt_group("第一段", "第二段", separator="\n\n"),
        )

        rendered = render_prompt_sections([section])

        self.assertEqual("第一段\n\n第二段", ET.fromstring(rendered).findtext("./section"))

    def test_plan_accepts_authored_section_and_appends_without_stringifying_structure(self) -> None:
        plan = ConversationInjectionPlan()
        first = prompt_section(
            key="state.current",
            title="当前状态",
            source="daily_state",
            content={"energy": 70},
        )
        second = prompt_section(
            key="state.current",
            title="当前状态",
            source="daily_state",
            content={"mood": "calm"},
        )

        plan.add(
            section=first,
            marker="<!-- state -->",
            placement=PLACEMENT_DYNAMIC_SYSTEM,
        )
        plan.add(
            section=second,
            marker="<!-- state -->",
            placement=PLACEMENT_DYNAMIC_SYSTEM,
            merge_policy="append",
        )
        request = type("Request", (), {"system_prompt": "", "prompt": "", "extra_user_content_parts": []})()
        plan.render_into(request)

        root = ET.fromstring(request.system_prompt)
        section = root.find("./section")
        self.assertEqual("70", section.findtext("energy"))
        self.assertEqual("calm", section.findtext("mood"))
        self.assertNotIn("PromptText", request.system_prompt)


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
APP_JS = (PLUGIN_ROOT / "pages" / "陪伴面板" / "app.js").read_text(encoding="utf-8")
APP_CSS = (PLUGIN_ROOT / "pages" / "陪伴面板" / "app.css").read_text(encoding="utf-8")
INDEX_HTML = (PLUGIN_ROOT / "pages" / "陪伴面板" / "index.html").read_text(encoding="utf-8")
PAGE_API = (PLUGIN_ROOT / "page_api.py").read_text(encoding="utf-8")


class PhotoReferenceWebUiTests(unittest.TestCase):
    def test_catalog_dirty_signature_normalizes_array_and_line_formats(self) -> None:
        self.assertIn('paramKey === "photo_reference_catalog"', APP_JS)
        self.assertIn("photoReferenceCatalogSignature(value)", APP_JS)
        self.assertIn("value.split(/\\r?\\n/)", APP_JS)
        self.assertIn("parsePhotoReferenceCatalog(value, true)", APP_JS)
        self.assertIn('if (canonical.kind === "library") delete canonical.id', APP_JS)

    def test_status_hydration_replaces_the_clean_catalog_baseline(self) -> None:
        self.assertIn('baseline?.key === "enable_photo_text_action"', APP_JS)
        self.assertIn('baseline.formSignature = ""', APP_JS)

    def test_opening_manager_skips_the_managed_catalog_draft(self) -> None:
        self.assertIn('control.dataset.featureParam === "photo_reference_catalog"', APP_JS)

    def test_generic_form_events_cannot_overwrite_the_managed_catalog_draft(self) -> None:
        self.assertIn(
            'function rememberFeatureParamDraft(control, { allowPhotoReferenceCatalog = false } = {})',
            APP_JS,
        )
        self.assertIn(
            'key === "photo_reference_catalog" && !allowPhotoReferenceCatalog',
            APP_JS,
        )
        self.assertIn(
            'rememberFeatureParamDraft(catalogInput, { allowPhotoReferenceCatalog: true })',
            APP_JS,
        )

    def test_catalog_sync_marks_dirty_only_after_a_semantic_change(self) -> None:
        self.assertIn(
            'const previousSignature = photoReferenceCatalogSignature(currentPhotoReferenceCatalogValue())',
            APP_JS,
        )
        self.assertIn(
            'previousSignature === photoReferenceCatalogSignature(serialized)',
            APP_JS,
        )
        self.assertIn('refreshFeatureDetailDirty();', APP_JS)
        self.assertIn('return false;', APP_JS)

    def test_unedited_catalog_is_not_submitted_with_other_photo_settings(self) -> None:
        self.assertIn(
            'key === "photo_reference_catalog" && !Object.prototype.hasOwnProperty.call(parameterDraft, key)',
            APP_JS,
        )

    def test_manager_drops_a_preferred_preset_removed_from_server_options(self) -> None:
        self.assertIn('state.photoReferenceLibraryStatus?.options?.presets', APP_JS)
        self.assertIn(
            'availablePresets && !availablePresets.includes(preferredPreset) ? "" : preferredPreset',
            APP_JS,
        )

    def test_time_categories_round_trip_through_manager_draft(self) -> None:
        self.assertIn('metadata.time_categories = normalizePhotoReferenceMetadataList', APP_JS)
        self.assertIn('time_categories: Array.isArray(item.time_categories)', APP_JS)
        self.assertIn('time_categories: normalizePhotoReferenceMetadataList', APP_JS)
        self.assertIn('data-photo-reference-times', APP_JS)

    def test_role_shortcuts_are_rendered_and_applied(self) -> None:
        self.assertIn('status?.options?.role_shortcuts', APP_JS)
        self.assertIn('data-photo-reference-role-shortcut', APP_JS)
        self.assertIn('input.dataset.photoReferenceRoleShortcut', APP_JS)

    def test_selfie_workflow_help_describes_dynamic_image_count(self) -> None:
        self.assertIn("images=N 自拍/改图工作流", APP_JS)
        self.assertNotIn("优先寻找 images=1 的自拍工作流", APP_JS)

    def test_metadata_editor_uses_localized_select_controls(self) -> None:
        self.assertIn('<select data-photo-reference-outfit-category', APP_JS)
        self.assertIn('photoReferenceMultiSelectHtml("reference_roles"', APP_JS)
        self.assertIn('photoReferenceMultiSelectHtml("scene_categories"', APP_JS)
        self.assertIn('photoReferenceMultiSelectHtml("time_categories"', APP_JS)
        self.assertIn('<select data-photo-reference-preferred-preset', APP_JS)
        self.assertIn('photoReferenceSingleSelectOptions("outfit_categories"', APP_JS)
        self.assertIn('photoReferenceSingleSelectOptions("presets"', APP_JS)
        self.assertNotIn('placeholder="sleepwear / daily_outfit / formal"', APP_JS)
        self.assertNotIn('placeholder="home, bedroom, outdoor"', APP_JS)
        self.assertNotIn('placeholder="morning, evening, bedtime"', APP_JS)

    def test_metadata_editor_explains_each_decision_field(self) -> None:
        expected_help = (
            "展开后可指定这张图在生图时负责保留哪些信息。",
            "决定生成时从这张图保留哪些内容",
            "标记图片中的服装类型",
            "控制是否优先沿用参考图中的服装",
            "选择这张图适合使用的通用场景",
            "选择这张图适合使用的时间段",
            "选择使用这张图时优先套用的生图场景预设",
        )
        for help_text in expected_help:
            with self.subTest(help_text=help_text):
                self.assertIn(help_text, APP_JS)

    def test_metadata_editor_visually_separates_help_from_the_next_field(self) -> None:
        self.assertIn('.photo-reference-metadata-editor[open] > label', APP_CSS)
        self.assertIn('.photo-reference-metadata-editor[open] > .photo-reference-field', APP_CSS)
        self.assertIn('border-top: 1px solid var(--line-soft)', APP_CSS)
        self.assertIn('padding-top: 14px', APP_CSS)

    def test_guided_questions_open_only_from_the_add_reference_dialog(self) -> None:
        self.assertIn('data-photo-reference-add-open', APP_JS)
        self.assertIn('<dialog class="photo-reference-add-dialog" data-photo-reference-add-dialog>', APP_JS)
        self.assertIn('grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr) auto;align-items:stretch', APP_CSS)
        self.assertIn('.photo-reference-add-form>footer button{flex:0 0 auto;width:auto;white-space:nowrap}', APP_CSS)
        self.assertIn('.photo-reference-guided-tabs button{flex:0 0 auto;width:auto', APP_CSS)
        self.assertIn('.photo-reference-guided-templates button{flex:0 0 auto;width:auto', APP_CSS)
        dialog_start = APP_JS.index('<dialog class="photo-reference-add-dialog"')
        dialog_end = APP_JS.index('</dialog>', dialog_start)
        dialog_markup = APP_JS[dialog_start:dialog_end]
        self.assertIn('data-photo-reference-guided-host', dialog_markup)
        self.assertIn('addDialog.showModal()', APP_JS)
        self.assertIn('addDialog.close()', APP_JS)
        manager_start = APP_JS.index('function bindPhotoReferenceManagerActions()')
        manager_end = APP_JS.index('function bindPhotoApiEndpointEditor', manager_start)
        manager_actions = APP_JS[manager_start:manager_end]
        self.assertNotIn(
            'if (!manager || state.featureDetailSubpage !== "photo_reference_library") return;\n  renderGuidedPhotoReferenceEditor();',
            manager_actions,
        )

    def test_guided_metadata_answers_use_plain_language_choice_controls(self) -> None:
        for field_name in (
            "outfit_category",
            "prefer_scenes",
            "prefer_times",
            "avoid_scenes",
            "avoid_times",
            "preferred_preset",
        ):
            with self.subTest(field_name=field_name):
                self.assertNotIn(f'<input name="{field_name}"', APP_JS)
        for field_name in (
            "core_anchor",
            "wardrobe_change",
            "location_change",
            "pose_change",
            "outfit_behavior",
            "outfit_category",
            "prefer_none",
            "prefer_scenes",
            "prefer_times",
            "avoid_none",
            "avoid_scenes",
            "avoid_times",
            "preferred_preset",
            "fallback_policy",
        ):
            with self.subTest(field_name=field_name):
                self.assertIn(f'guidedPhotoReferenceChoiceGroup("{field_name}"', APP_JS)
        self.assertIn('type="${type}"', APP_JS)
        self.assertIn('name="${escapeHtml(name)}"', APP_JS)
        self.assertIn('data-photo-guided-answer-label', APP_JS)

    def test_guided_questionnaire_uses_eight_redundant_plain_language_questions(self) -> None:
        questions = (
            "1. 这张图最不能丢的特点是什么？",
            "2. 换一身衣服后，这张图还适合用吗？",
            "3. 换到其他地点后，这张图还适合用吗？",
            "4. 动作改变后，这张图还适合用吗？",
            "5. 哪些情况应该优先用这张图？",
            "6. 哪些情况容易用错这张图？",
            "7. 没有完全匹配的图片时，应该怎么处理？",
            "8. 图中的穿搭应该怎么处理？",
        )
        for question in questions:
            with self.subTest(question=question):
                self.assertIn(f"<legend>{question}</legend>", APP_JS)
        for repeated_identity_answer in (
            'value: "yes_identity", label: "适合，主要看人物长相"',
            'value: "yes_outfit", label: "适合，主要看人物穿搭"',
            'value: "yes_style", label: "适合，主要看画面风格"',
        ):
            self.assertGreaterEqual(APP_JS.count(repeated_identity_answer), 2)

    def test_guided_review_declares_and_uses_the_configured_main_model(self) -> None:
        self.assertIn('state.overview?.providers?.LLM_PROVIDER_ID', APP_JS)
        self.assertIn('审批将调用 WebUI“模型配置”中的主模型', APP_JS)
        self.assertIn('postJson("/photo_reference/metadata/review"', APP_JS)
        self.assertIn('questionnaire: guidedPhotoReferenceQuestionnaire(root)', APP_JS)
        self.assertIn('compiled.review?.status === "approved"', APP_JS)
        self.assertIn('主模型 ${compiled.review.provider_id', APP_JS)
        review_start = PAGE_API.index("async def review_photo_reference_metadata")
        review_end = PAGE_API.index("async def run_photo_reference_selection_trial", review_start)
        review_endpoint = PAGE_API[review_start:review_end]
        self.assertIn('getattr(self.plugin, "llm_provider_id", "")', review_endpoint)
        self.assertIn('task="photo_reference_metadata_review"', review_endpoint)
        self.assertIn('strict_provider=True', review_endpoint)
        self.assertIn('必须是 WebUI“模型配置”中的主模型', review_endpoint)
        self.assertNotIn("._task_provider(", review_endpoint)

    def test_metadata_editor_assets_are_cache_busted(self) -> None:
        self.assertIn('app.css?v=20260804-reference-guided-dialog-v2', INDEX_HTML)
        self.assertIn('css/polish.css?v=20260804-expression-batch-review-v1', INDEX_HTML)
        self.assertIn('app.js?v=20260804-reference-guided-dialog-v2', INDEX_HTML)


if __name__ == "__main__":
    unittest.main()

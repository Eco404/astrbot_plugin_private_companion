# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .conversation_prompt_section import (
    PromptRenderMode,
    PromptSection,
    render_prompt_sections,
)
from .helpers import _single_line


@dataclass(frozen=True, slots=True)
class PromptFragment:
    """One authored section plus surface-local ordering metadata."""

    section: PromptSection
    priority: int = 100
    index: int = 0

    @property
    def key(self) -> str:
        return self.section.key

    @property
    def content(self) -> Any:
        return self.section.content

    @property
    def title(self) -> str:
        return self.section.title

    @property
    def source(self) -> str:
        return self.section.source

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self.section.metadata)

    def normalized_key(self) -> str:
        return _single_line(self.section.key or self.section.source or "fragment", 80)


class PromptSurface:
    """Collect authored sections before request-level placement and rendering."""

    def __init__(self) -> None:
        self._fragments: list[PromptFragment] = []
        self._next_index = 0

    def add(
        self,
        section: PromptSection,
        *,
        priority: int = 100,
    ) -> None:
        """Add one fully authored section."""

        if not isinstance(section, PromptSection):
            raise TypeError("PromptSurface.add requires PromptSection")
        if not section.key or not section.source:
            raise ValueError("PromptSurface requires a section with key and source")
        if (
            section.content is None
            or (isinstance(section.content, str) and not section.content.strip())
        ) and not section.children:
            return
        self._fragments.append(
            PromptFragment(
                section=section,
                priority=int(priority),
                index=self._next_index,
            )
        )
        self._next_index += 1

    def extend(self, fragments: Iterable[PromptFragment | PromptSection]) -> None:
        for fragment in fragments:
            if isinstance(fragment, PromptFragment):
                self.add(fragment.section, priority=fragment.priority)
            elif isinstance(fragment, PromptSection):
                self.add(fragment)

    def _rendered_fragments(self) -> list[PromptFragment]:
        seen_keys: set[str] = set()
        rendered: list[PromptFragment] = []
        for fragment in sorted(self._fragments, key=lambda item: (item.priority, item.index)):
            key = fragment.normalized_key()
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            rendered.append(fragment)
        return rendered

    @staticmethod
    def _manifest_item(fragment: PromptFragment) -> dict[str, object]:
        content = render_prompt_sections(
            [fragment.section],
            mode=PromptRenderMode.BODY_ONLY,
        )
        item: dict[str, object] = {
            "key": fragment.normalized_key(),
            "title": fragment.title,
            "source": _single_line(fragment.source, 80),
            "priority": int(fragment.priority),
            "content": content,
            "chars": len(str(content)),
        }
        if fragment.metadata:
            item["metadata"] = fragment.metadata
        return item

    def rendered_fragments(self) -> list[dict[str, object]]:
        return [self._manifest_item(fragment) for fragment in self._rendered_fragments()]

    def rendered_sections(self) -> tuple[PromptSection, ...]:
        """Expose typed sections so downstream plans do not need pre-rendered XML."""

        return tuple(fragment.section for fragment in self._rendered_fragments())

    def render(self) -> str:
        return self._render_sections(self._rendered_fragments())

    @staticmethod
    def _render_sections(fragments: Iterable[PromptFragment]) -> str:
        return render_prompt_sections(fragment.section for fragment in fragments)

    def render_partition(self, predicate: Callable[[PromptFragment], bool]) -> tuple[str, str]:
        matched, rest, _matched_fragments, _rest_fragments = self.render_partition_with_fragments(predicate)
        return matched, rest

    def partition_sections(
        self,
        predicate: Callable[[PromptFragment], bool],
    ) -> tuple[tuple[PromptSection, ...], tuple[PromptSection, ...]]:
        matched: list[PromptSection] = []
        rest: list[PromptSection] = []
        for fragment in self._rendered_fragments():
            (matched if predicate(fragment) else rest).append(fragment.section)
        return tuple(matched), tuple(rest)

    def render_partition_with_fragments(
        self,
        predicate: Callable[[PromptFragment], bool],
    ) -> tuple[str, str, list[dict[str, object]], list[dict[str, object]]]:
        """Compatibility view for callers that have not moved to typed partitions."""

        matched: list[PromptFragment] = []
        rest: list[PromptFragment] = []
        for fragment in self._rendered_fragments():
            content = fragment.content
            if content is None or (isinstance(content, str) and not content.strip()):
                continue
            (matched if predicate(fragment) else rest).append(fragment)
        return (
            self._render_sections(matched),
            self._render_sections(rest),
            [self._manifest_item(fragment) for fragment in matched],
            [self._manifest_item(fragment) for fragment in rest],
        )

    def __len__(self) -> int:
        return len(self._fragments)

"""Typed prompt authoring and rendering for Private Companion."""

from __future__ import annotations

import copy
import json
import re
import string
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from types import MappingProxyType
from xml.sax.saxutils import escape


class PromptRenderMode(str, Enum):
    """Wire formats supported by the canonical prompt renderer."""

    CONVERSATION_XML = "conversation_xml"
    LEGACY_BLOCK = "legacy_block"
    LEGACY_INLINE = "legacy_inline"
    BODY_ONLY = "body_only"
    EXACT = "exact"
    PHOTO_PROMPT = "photo_prompt"


@dataclass(frozen=True, slots=True)
class PromptValue:
    """One dynamic value and its provenance/trust annotation."""

    value: Any
    trust: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class PromptText:
    """Ordered text fragments joined without implicit whitespace changes."""

    parts: tuple[Any, ...]
    separator: str = ""


@dataclass(frozen=True, slots=True)
class PromptGroup:
    """Ordered structured content rendered without flattening child types."""

    parts: tuple[Any, ...]
    separator: str = "\n\n"


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """A validated template whose variables remain typed until rendering."""

    template: str
    variables: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        template = str(self.template)
        variables = dict(self.variables)
        referenced: set[str] = set()
        try:
            parsed = tuple(string.Formatter().parse(template))
        except ValueError as exc:
            raise ValueError(f"invalid prompt template: {exc}") from exc
        for _literal, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if not field_name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field_name):
                raise ValueError(f"invalid prompt template variable: {field_name!r}")
            if format_spec or conversion:
                raise ValueError("prompt template variables do not support format specs or conversions")
            referenced.add(field_name)
        supplied = set(variables)
        missing = sorted(referenced - supplied)
        unused = sorted(supplied - referenced)
        if missing:
            raise ValueError(f"missing prompt template variables: {', '.join(missing)}")
        if unused:
            raise ValueError(f"unused prompt template variables: {', '.join(unused)}")
        object.__setattr__(self, "template", template)
        object.__setattr__(self, "variables", variables)


@dataclass(frozen=True, slots=True)
class PromptCData:
    """Content that must remain visibly literal inside conversation XML."""

    content: Any


@dataclass(frozen=True, slots=True)
class ExactText:
    """A byte-sensitive text contract that must not be normalized or escaped."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("exact prompt text must be str")


@dataclass(frozen=True, slots=True)
class PromptField:
    """One named structured field."""

    name: str
    value: Any

    def __post_init__(self) -> None:
        _validate_xml_name(self.name, kind="prompt field")


@dataclass(frozen=True, slots=True)
class PromptList:
    """An explicitly named ordered collection."""

    items: tuple[Any, ...]
    tag: str = "items"
    item_tag: str = "item"
    prefix: str = ""
    separator: str = "\n"

    def __post_init__(self) -> None:
        _validate_xml_name(self.tag, kind="prompt list")
        _validate_xml_name(self.item_tag, kind="prompt list item")


@dataclass(frozen=True, slots=True)
class XmlElement:
    """Typed XML node; callers provide data, never pre-rendered XML."""

    tag: str
    attrs: Mapping[str, Any] = field(default_factory=dict)
    text: Any = None
    children: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        _validate_xml_name(self.tag, kind="XML element")
        if not isinstance(self.attrs, Mapping):
            raise TypeError("XML attributes must be a mapping")
        normalized_attrs: dict[str, Any] = {}
        for key, value in self.attrs.items():
            normalized_key = str(key or "")
            _validate_xml_name(normalized_key, kind="XML attribute")
            if isinstance(
                value,
                (
                    Mapping,
                    list,
                    tuple,
                    set,
                    XmlElement,
                    PromptGroup,
                    PromptText,
                    PromptTemplate,
                    PromptCData,
                ),
            ):
                raise TypeError(f"XML attribute {key!r} must be scalar")
            normalized_attrs[normalized_key] = value.value if isinstance(value, PromptValue) else value
        normalized_children = tuple(self.children)
        allowed_children = (
            XmlElement,
            PromptGroup,
            PromptText,
            PromptTemplate,
            PromptCData,
            PromptValue,
            ExactText,
            str,
        )
        if not all(isinstance(child, allowed_children) for child in normalized_children):
            raise TypeError("XML children must be typed prompt content")
        object.__setattr__(self, "tag", str(self.tag))
        object.__setattr__(self, "attrs", normalized_attrs)
        object.__setattr__(self, "children", normalized_children)


class PromptSection(dict[str, Any]):
    """Canonical authored prompt unit.

    ``title`` and ``content`` remain first for compatibility with the original
    direct ``PromptSection(title, content)`` constructor. New production code
    should use :func:`prompt_section` with explicit ``key`` and ``source``.
    """

    def __init__(
        self,
        title: Any,
        content: Any,
        key: Any = "",
        source: Any = "",
        children: Iterable[Any] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("prompt section metadata must be a mapping")
        dict.__init__(self, title=_normalize_title(title), content=content)
        self._key = _normalize_identity(key, limit=160)
        self._source = _normalize_identity(source, limit=80)
        self._children = tuple(children)
        self._metadata = MappingProxyType(dict(metadata or {}))

    @property
    def title(self) -> str:
        return dict.__getitem__(self, "title")

    @property
    def content(self) -> Any:
        return dict.__getitem__(self, "content")

    @property
    def key(self) -> str:
        return self._key

    @property
    def source(self) -> str:
        return self._source

    @property
    def children(self) -> tuple[Any, ...]:
        return self._children

    @property
    def metadata(self) -> Mapping[str, Any]:
        return self._metadata

    def __copy__(self) -> "PromptSection":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "PromptSection":
        return PromptSection(
            title=self.title,
            content=copy.deepcopy(self.content, memo),
            key=self.key,
            source=self.source,
            children=copy.deepcopy(self.children, memo),
            metadata=copy.deepcopy(dict(self.metadata), memo),
        )

    def __repr__(self) -> str:
        return (
            "PromptSection("
            f"key={self.key!r}, title={self.title!r}, source={self.source!r}, "
            f"content={self.content!r}, children={self.children!r}, metadata={dict(self.metadata)!r})"
        )

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("PromptSection is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


@dataclass(frozen=True, slots=True)
class PromptDocument:
    """A channel-aware collection; all authored content remains sections."""

    system: tuple[PromptSection, ...] = ()
    user: tuple[PromptSection, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        system_sections = tuple(self.system)
        user_sections = tuple(self.user)
        if not all(isinstance(item, PromptSection) for item in (*system_sections, *user_sections)):
            raise TypeError("prompt document channels must contain PromptSection instances")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("prompt document metadata must be a mapping")
        object.__setattr__(self, "system", system_sections)
        object.__setattr__(self, "user", user_sections)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


def _validate_xml_name(value: Any, *, kind: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", text):
        raise ValueError(f"invalid {kind} name: {value!r}")
    return text


def _normalize_identity(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _validate_prompt_identity(value: str, *, kind: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise ValueError(f"invalid prompt {kind}: {value!r}")
    return value


def _normalize_title(value: Any) -> str:
    normalized = _normalize_identity(value, limit=80)
    return normalized or "提示词片段"


def prompt_value(value: Any, *, trust: str = "", source: str = "") -> PromptValue:
    return PromptValue(value=value, trust=_normalize_identity(trust, limit=80), source=_normalize_identity(source, limit=80))


def prompt_text(*parts: Any, separator: str = "") -> PromptText:
    return PromptText(parts=tuple(parts), separator=str(separator))


def prompt_group(*parts: Any, separator: str = "\n\n") -> PromptGroup:
    return PromptGroup(parts=tuple(parts), separator=str(separator))


def prompt_cdata(content: Any) -> PromptCData:
    return PromptCData(content=content)


def exact_text(text: str) -> ExactText:
    return ExactText(text=text)


def legacy_heading_token(title: Any, *, newline: bool = False) -> str:
    """Render one legacy heading token for protocols that still parse it."""

    normalized = _normalize_identity(title, limit=80)
    if not normalized:
        raise ValueError("legacy heading token requires a non-empty title")
    return f"【{normalized}】" + ("\n" if newline else "")


def prompt_field(name: str, value: Any) -> PromptField:
    return PromptField(name=name, value=value)


def prompt_list(
    items: Iterable[Any],
    *,
    tag: str = "items",
    item_tag: str = "item",
    prefix: str = "",
    separator: str = "\n",
) -> PromptList:
    return PromptList(
        items=tuple(items),
        tag=tag,
        item_tag=item_tag,
        prefix=str(prefix),
        separator=str(separator),
    )


_MISSING = object()


def prompt_section(
    *args: Any,
    key: Any = "",
    title: Any = _MISSING,
    source: Any = "",
    content: Any = _MISSING,
    template: str | None = None,
    variables: Mapping[str, Any] | None = None,
    children: Iterable[Any] = (),
    metadata: Mapping[str, Any] | None = None,
) -> PromptSection:
    """Create one canonical prompt section.

    ``prompt_section(title, content)`` remains a migration adapter. New code
    must use keyword arguments and provide stable ``key`` and ``source``.
    """

    legacy_call = bool(args)
    if legacy_call:
        if len(args) != 2 or title is not _MISSING or content is not _MISSING:
            raise TypeError("legacy prompt_section accepts exactly title and content")
        title, content = args
    if title is _MISSING:
        raise TypeError("prompt_section requires title")
    if template is not None and content is not _MISSING:
        raise TypeError("prompt_section accepts either content or template, not both")
    if template is not None:
        content = PromptTemplate(template=str(template), variables=dict(variables or {}))
    elif variables:
        raise TypeError("prompt_section variables require template")
    elif content is _MISSING:
        raise TypeError("prompt_section requires content or template")

    normalized_key = _normalize_identity(key, limit=160)
    normalized_source = _normalize_identity(source, limit=80)
    if not legacy_call and (not normalized_key or not normalized_source):
        raise ValueError("new prompt sections require key and source")
    if bool(normalized_key) != bool(normalized_source):
        raise ValueError("new prompt sections require both key and source")
    if not legacy_call:
        if not _normalize_identity(title, limit=80):
            raise ValueError("new prompt sections require a non-empty title")
        _validate_prompt_identity(normalized_key, kind="key")
        _validate_prompt_identity(normalized_source, kind="source")

    normalized_children: list[Any] = []
    for child in children:
        section = coerce_prompt_section(child)
        if section is not None:
            normalized_children.append(section)
            continue
        if isinstance(
            child,
            (
                str,
                PromptGroup,
                PromptText,
                PromptTemplate,
                PromptValue,
                PromptList,
                PromptField,
                XmlElement,
                PromptCData,
            ),
        ):
            normalized_children.append(child)
            continue
        raise TypeError("prompt section children must be prompt content or PromptSection values")
    return PromptSection(
        title=_normalize_title(title),
        content=content,
        key=normalized_key,
        source=normalized_source,
        children=tuple(normalized_children),
        metadata=dict(metadata or {}),
    )


def prompt_document(
    *,
    system: Iterable[PromptSection | Mapping[str, Any]] = (),
    user: Iterable[PromptSection | Mapping[str, Any]] = (),
    metadata: Mapping[str, Any] | None = None,
) -> PromptDocument:
    def normalize(items: Iterable[PromptSection | Mapping[str, Any]]) -> tuple[PromptSection, ...]:
        result: list[PromptSection] = []
        for item in items:
            section = coerce_prompt_section(item)
            if section is None:
                raise TypeError("prompt document channels must contain prompt sections")
            result.append(section)
        return tuple(result)

    return PromptDocument(system=normalize(system), user=normalize(user), metadata=dict(metadata or {}))


def xml_element(
    tag: str,
    *,
    attrs: Mapping[str, Any] | None = None,
    text: Any = None,
    children: Iterable[Any] = (),
) -> XmlElement:
    return XmlElement(tag=str(tag or "").strip(), attrs=dict(attrs or {}), text=text, children=tuple(children))


def coerce_prompt_section(value: Any) -> PromptSection | None:
    """Normalize a PromptSection or a legacy ``{title, content}`` mapping."""

    if isinstance(value, PromptSection):
        return value
    if isinstance(value, Mapping) and "title" in value and "content" in value:
        children: list[PromptSection] = []
        for child in value.get("children") or ():
            normalized = coerce_prompt_section(child)
            if normalized is not None:
                children.append(normalized)
        return PromptSection(
            title=_normalize_title(value.get("title")),
            content=value.get("content"),
            key=_normalize_identity(value.get("key"), limit=160),
            source=_normalize_identity(value.get("source"), limit=80),
            children=tuple(children),
            metadata=dict(value.get("metadata") or {}),
        )
    return None


def title_for_prompt_key(key: Any, explicit_title: Any = "") -> str:
    del key
    return _normalize_title(explicit_title)


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, PromptValue):
        return _has_content(value.value)
    if isinstance(value, PromptCData):
        return _has_content(value.content)
    if isinstance(value, ExactText):
        return bool(value.text)
    if isinstance(value, PromptTemplate):
        return bool(value.template)
    if isinstance(value, PromptGroup):
        return any(_has_content(item) for item in value.parts)
    if isinstance(value, PromptText):
        return any(_has_content(item) for item in value.parts)
    if isinstance(value, PromptField):
        return _has_content(value.value)
    if isinstance(value, PromptList):
        return bool(value.items)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple)):
        return bool(value)
    return True


def _xml_string(value: Any) -> str:
    if isinstance(value, PromptValue):
        value = value.value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = "".join(
        char
        for char in str(value)
        if (
            char in "\t\n\r"
            or 0x20 <= ord(char) <= 0xD7FF
            or 0xE000 <= ord(char) <= 0xFFFD
            or 0x10000 <= ord(char) <= 0x10FFFF
        )
    )
    return text


def _xml_text(value: Any) -> str:
    return escape(_xml_string(value))


def _xml_attribute(value: Any) -> str:
    return escape(_xml_string(value), {'"': "&quot;", "'": "&apos;"})


def _legacy_mapping_text(value: Mapping[Any, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _plain_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, PromptValue):
        return _plain_content(value.value)
    if isinstance(value, ExactText):
        return value.text
    if isinstance(value, PromptCData):
        return _plain_content(value.content)
    if isinstance(value, PromptTemplate):
        parts: list[str] = []
        for literal, field_name, _format_spec, _conversion in string.Formatter().parse(value.template):
            parts.append(literal)
            if field_name is not None:
                parts.append(_plain_content(value.variables[field_name]))
        return "".join(parts)
    if isinstance(value, PromptSection):
        return _render_legacy([value], inline=False)
    if isinstance(value, PromptGroup):
        return value.separator.join(_plain_content(part) for part in value.parts)
    if isinstance(value, PromptText):
        return value.separator.join(_plain_content(part) for part in value.parts)
    if isinstance(value, PromptField):
        return f"{value.name}: {_plain_content(value.value)}"
    if isinstance(value, PromptList):
        return value.separator.join(f"{value.prefix}{_plain_content(item)}" for item in value.items)
    if isinstance(value, XmlElement):
        return _render_xml_element(value)
    if isinstance(value, Mapping):
        return _legacy_mapping_text(value)
    if isinstance(value, (list, tuple)):
        return "\n".join(_plain_content(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _cdata_text(value: Any) -> str:
    return _xml_string(_plain_content(value)).replace("]]>", "]]]]><![CDATA[>")


def _xml_tag(value: Any, fallback: str = "item") -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "")).strip("_.-")
    if not text or not re.match(r"^[A-Za-z_]", text):
        return fallback
    return text


def _list_item_tag(parent: str) -> str:
    return {
        "history": "message",
        "constraints": "constraint",
        "items": "item",
        "evidence": "item",
    }.get(parent, "item")


def _render_xml_value(tag: str, value: Any) -> str:
    safe_tag = _xml_tag(tag)
    if isinstance(value, PromptField):
        return _render_xml_value(value.name, value.value)
    if isinstance(value, PromptList):
        body = "".join(_render_xml_value(value.item_tag, item) for item in value.items)
        return f"<{value.tag}>{body}</{value.tag}>"
    if isinstance(value, XmlElement):
        return f"<{safe_tag}>{_render_xml_element(value)}</{safe_tag}>"
    if isinstance(value, Mapping):
        body = "".join(_render_xml_value(str(key), item) for key, item in value.items())
        return f"<{safe_tag}>{body}</{safe_tag}>"
    if isinstance(value, (list, tuple)):
        item_tag = _list_item_tag(safe_tag)
        body = "".join(_render_xml_value(item_tag, item) for item in value)
        return f"<{safe_tag}>{body}</{safe_tag}>"
    if isinstance(value, PromptCData):
        return f"<{safe_tag}><![CDATA[{_cdata_text(value.content)}]]></{safe_tag}>"
    if isinstance(value, ExactText):
        raise ValueError("ExactText cannot be embedded in conversation XML")
    return f"<{safe_tag}>{_xml_text(_plain_content(value))}</{safe_tag}>"


def _render_xml_child(value: Any) -> str:
    if isinstance(value, PromptSection):
        return _render_xml_section(value)
    if isinstance(value, XmlElement):
        return _render_xml_element(value)
    if isinstance(value, PromptCData):
        return f"<![CDATA[{_cdata_text(value.content)}]]>"
    if isinstance(value, ExactText):
        raise ValueError("ExactText cannot be embedded in conversation XML")
    return _xml_text(_plain_content(value))


def _render_xml_element(element: XmlElement) -> str:
    attrs = "".join(
        f' {key}="{_xml_attribute(value)}"'
        for key, value in element.attrs.items()
        if value is not None
    )
    body = _render_xml_child(element.text) if element.text is not None else ""
    body += "".join(_render_xml_child(child) for child in element.children)
    if not body:
        return f"<{element.tag}{attrs}/>"
    return f"<{element.tag}{attrs}>{body}</{element.tag}>"


def _render_xml_content(value: Any) -> str:
    if isinstance(value, PromptCData):
        return f"<![CDATA[{_cdata_text(value.content)}]]>"
    if isinstance(value, ExactText):
        raise ValueError("ExactText requires the exact render mode")
    if isinstance(value, PromptGroup):
        separator = _xml_text(value.separator)
        return separator.join(_render_xml_content(item) for item in value.parts)
    if isinstance(value, PromptSection):
        return _render_xml_section(value)
    if isinstance(value, XmlElement):
        return _render_xml_element(value)
    if isinstance(value, PromptField):
        return _render_xml_value(value.name, value.value)
    if isinstance(value, PromptList):
        return _render_xml_value(value.tag, value)
    if isinstance(value, Mapping):
        return "".join(_render_xml_value(str(key), item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return "".join(_render_xml_value("item", item) for item in value)
    return _xml_text(_plain_content(value))


def _flatten_sections(sections: Iterable[PromptSection | Mapping[str, Any]]) -> list[PromptSection]:
    result: list[PromptSection] = []

    def append(raw: PromptSection | Mapping[str, Any]) -> None:
        section = coerce_prompt_section(raw)
        if section is None:
            return
        content_children = tuple(
            child for child in section.children if not isinstance(child, PromptSection)
        )
        section_children = tuple(
            child for child in section.children if isinstance(child, PromptSection)
        )
        content = section.content
        if content_children:
            content = prompt_text(content, *content_children, separator="\n")
        if _has_content(content):
            result.append(
                PromptSection(
                    title=section.title,
                    content=content,
                    key=section.key,
                    source=section.source,
                    metadata=section.metadata,
                )
            )
        for child in section_children:
            append(child)

    for raw in sections:
        append(raw)
    return result


def _coerce_render_mode(value: PromptRenderMode | str) -> PromptRenderMode:
    if isinstance(value, PromptRenderMode):
        return value
    normalized = str(value or "").strip().lower()
    try:
        return PromptRenderMode(normalized)
    except ValueError as exc:
        raise ValueError(f"unsupported prompt render mode: {value!r}") from exc


def _render_conversation_xml(sections: Sequence[PromptSection]) -> str:
    if not sections:
        return ""
    body = "".join(_render_xml_section(section) for section in sections)
    return f"<private_companion_context>{body}</private_companion_context>"


def _render_xml_section(section: PromptSection) -> str:
    body = _render_xml_content(section.content)
    body += "".join(
        _render_xml_section(child)
        if isinstance(child, PromptSection)
        else _render_xml_content(child)
        for child in section.children
    )
    return f'<section title="{_xml_attribute(section.title)}">{body}</section>'


def _render_legacy(sections: Sequence[PromptSection], *, inline: bool) -> str:
    separator = "" if inline else "\n"
    return "\n\n".join(
        f"【{section.title}】{separator}{_plain_content(section.content)}"
        for section in sections
    )


def _render_body_only(sections: Sequence[PromptSection]) -> str:
    return "\n\n".join(_plain_content(section.content) for section in sections)


def _render_exact(sections: Sequence[PromptSection]) -> str:
    bodies: list[str] = []
    for section in sections:
        if not isinstance(section.content, ExactText):
            raise TypeError("exact render mode requires ExactText content")
        bodies.append(section.content.text)
    return "".join(bodies)


def _render_photo_prompt(sections: Sequence[PromptSection]) -> str:
    """Render an already-authoritative photo payload without XML semantics.

    The photo pipeline owns positive/negative ordering and NAI weights. During
    migration it supplies those bytes as ExactText; a later adapter can carry
    the richer PhotoPromptSection payload without changing this public mode.
    """

    if all(isinstance(section.content, ExactText) for section in sections):
        return _render_exact(sections)
    return _render_body_only(sections)


def render_prompt_content(
    content: Any,
    *,
    mode: PromptRenderMode | str = PromptRenderMode.BODY_ONLY,
) -> str:
    """Render one typed content node without inventing a section identity."""

    render_mode = _coerce_render_mode(mode)
    if render_mode is PromptRenderMode.BODY_ONLY:
        return _plain_content(content)
    if render_mode is PromptRenderMode.CONVERSATION_XML:
        return _render_xml_content(content)
    if render_mode is PromptRenderMode.EXACT:
        if not isinstance(content, ExactText):
            raise TypeError("exact render mode requires ExactText content")
        return content.text
    if render_mode is PromptRenderMode.PHOTO_PROMPT:
        return content.text if isinstance(content, ExactText) else _plain_content(content)
    raise ValueError("legacy prompt content requires a section title")


def render_prompt_section(
    title_or_section: Any,
    content: Any = _MISSING,
    *,
    mode: PromptRenderMode | str = PromptRenderMode.CONVERSATION_XML,
) -> str:
    section = coerce_prompt_section(title_or_section)
    if section is None:
        if content is _MISSING:
            raise TypeError("render_prompt_section requires a section or title and content")
        section = PromptSection(_normalize_title(title_or_section), content)
    return render_prompt_sections([section], mode=mode)


def render_prompt_sections(
    sections: Iterable[PromptSection | Mapping[str, Any]],
    *,
    mode: PromptRenderMode | str = PromptRenderMode.CONVERSATION_XML,
) -> str:
    """Render authored sections without changing business-content spacing."""

    payload = _flatten_sections(sections)
    render_mode = _coerce_render_mode(mode)
    if render_mode is PromptRenderMode.CONVERSATION_XML:
        return _render_conversation_xml(payload)
    if render_mode is PromptRenderMode.LEGACY_BLOCK:
        return _render_legacy(payload, inline=False)
    if render_mode is PromptRenderMode.LEGACY_INLINE:
        return _render_legacy(payload, inline=True)
    if render_mode is PromptRenderMode.BODY_ONLY:
        return _render_body_only(payload)
    if render_mode is PromptRenderMode.EXACT:
        return _render_exact(payload)
    if render_mode is PromptRenderMode.PHOTO_PROMPT:
        return _render_photo_prompt(payload)
    raise AssertionError(f"unhandled prompt render mode: {render_mode}")


def render_prompt_document(
    document: PromptDocument,
    *,
    mode: PromptRenderMode | str | None = None,
    system_mode: PromptRenderMode | str = PromptRenderMode.LEGACY_BLOCK,
    user_mode: PromptRenderMode | str = PromptRenderMode.LEGACY_BLOCK,
) -> dict[str, str]:
    if not isinstance(document, PromptDocument):
        raise TypeError("render_prompt_document requires PromptDocument")
    if mode is not None:
        system_mode = mode
        user_mode = mode
    return {
        "system": render_prompt_sections(document.system, mode=system_mode),
        "user": render_prompt_sections(document.user, mode=user_mode),
    }


__all__ = [
    "ExactText",
    "PromptCData",
    "PromptDocument",
    "PromptField",
    "PromptGroup",
    "PromptList",
    "PromptRenderMode",
    "PromptSection",
    "PromptTemplate",
    "PromptText",
    "PromptValue",
    "XmlElement",
    "coerce_prompt_section",
    "exact_text",
    "legacy_heading_token",
    "prompt_cdata",
    "prompt_document",
    "prompt_field",
    "prompt_group",
    "prompt_list",
    "prompt_section",
    "prompt_text",
    "prompt_value",
    "render_prompt_document",
    "render_prompt_content",
    "render_prompt_section",
    "render_prompt_sections",
    "title_for_prompt_key",
    "xml_element",
]

"""Lossless text edits to pom.xml files (spec section 8.4).

The document is never re-serialised. A small tokenizer records where every element's tags start
and end, and edits splice new text in at those offsets, so comments, whitespace, attribute
quoting and line endings everywhere else stay byte-for-byte identical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NAME = r"[A-Za-z_][\w.\-:]*"
_START_TAG = re.compile(rf"<({_NAME})((?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(/?)>")
_END_TAG = re.compile(rf"</({_NAME})\s*>")
_SKIPPED = (("<!--", "-->"), ("<![CDATA[", "]]>"), ("<?", "?>"), ("<!", ">"))


class PomEditError(ValueError):
    """The POM text cannot be tokenized or the requested edit does not apply."""


@dataclass
class Element:
    name: str  # local name, namespace prefix removed
    start: int  # offset of '<' of the start tag
    start_end: int  # offset just after the start tag's '>'
    end_start: int  # offset of '<' of the end tag (== start_end for a self-closing element)
    end: int  # offset just after the end tag's '>'
    self_closing: bool
    children: list[Element] = field(default_factory=list)

    def child(self, name: str) -> Element | None:
        return next((c for c in self.children if c.name == name), None)

    def all(self, name: str) -> list[Element]:
        return [c for c in self.children if c.name == name]


def _local(name: str) -> str:
    return name.rsplit(":", 1)[-1]


def parse(text: str) -> Element:
    """The root element with offsets for every descendant. Raises PomEditError."""
    stack: list[Element] = []
    root: Element | None = None
    pos = 0
    while (lt := text.find("<", pos)) != -1:
        for opener, closer in _SKIPPED:
            if text.startswith(opener, lt):
                close = text.find(closer, lt + len(opener))
                if close == -1:
                    raise PomEditError(f"unterminated {opener} at offset {lt}")
                pos = close + len(closer)
                break
        else:
            if end := _END_TAG.match(text, lt):
                if not stack or stack[-1].name != _local(end.group(1)):
                    raise PomEditError(f"unexpected </{end.group(1)}> at offset {lt}")
                element = stack.pop()
                element.end_start, element.end = lt, end.end()
                pos = end.end()
            elif start := _START_TAG.match(text, lt):
                closing = start.group(3) == "/"
                element = Element(_local(start.group(1)), lt, start.end(), start.end(), start.end(), closing)
                if stack:
                    stack[-1].children.append(element)
                elif root is None:
                    root = element
                else:
                    raise PomEditError(f"second root element <{start.group(1)}> at offset {lt}")
                if not closing:
                    stack.append(element)
                pos = start.end()
            else:
                raise PomEditError(f"malformed markup at offset {lt}")
    if stack:
        raise PomEditError(f"<{stack[-1].name}> is never closed")
    if root is None:
        raise PomEditError("no root element")
    return root


def text_of(text: str, element: Element | None) -> str | None:
    """The stripped text content of a leaf element."""
    if element is None or element.self_closing or element.children:
        return None
    return text[element.start_end : element.end_start].strip()


def newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _line_indent(text: str, offset: int) -> str:
    line_start = text.rfind("\n", 0, offset) + 1
    prefix = text[line_start:offset]
    return prefix if prefix.strip() == "" else prefix[: len(prefix) - len(prefix.lstrip())]


def indent_unit(text: str, root: Element) -> str:
    """One level of indentation as the document uses it (default four spaces)."""
    base = _line_indent(text, root.start)
    for child in root.children:
        inner = _line_indent(text, child.start)
        if inner.startswith(base) and len(inner) > len(base):
            return inner[len(base) :]
    return "    "


def append_child(text: str, root: Element, parent: Element, block: str) -> str:
    """Insert ``block`` as the last child of ``parent`` and return the new text.

    ``block`` uses one tab per nesting level (relative to the new child) and "\\n" line breaks;
    both are converted to the document's own indentation and line ending.
    """
    nl, unit = newline(text), indent_unit(text, root)
    parent_indent = _line_indent(text, parent.start)
    child_indent = parent_indent + unit
    lines = [child_indent + line.replace("\t", unit) if line else "" for line in block.strip("\n").split("\n")]
    body = nl.join(lines)
    if parent.self_closing:
        tag = text[parent.start : parent.start_end]
        opened = tag[:-2].rstrip() + ">"
        name = tag[1:].split(None, 1)[0].rstrip("/>")
        replacement = f"{opened}{nl}{body}{nl}{parent_indent}</{name}>"
        return text[: parent.start] + replacement + text[parent.end :]
    line_start = text.rfind("\n", 0, parent.end_start) + 1
    if text[line_start : parent.end_start].strip() == "" and line_start > parent.start_end:
        # The end tag sits on its own line: insert whole lines just before it.
        return text[:line_start] + body + nl + text[line_start:]
    return text[: parent.end_start] + nl + body + nl + parent_indent + text[parent.end_start :]


def ensure_path(text: str, names: list[str]) -> tuple[str, list[str]]:
    """Make sure the element path below the root exists; returns the new text and the names of
    elements created (for reporting). Existing elements are reused."""
    created: list[str] = []
    for depth in range(1, len(names) + 1):
        root = parse(text)
        parent = find(root, names[: depth - 1])
        if parent.child(names[depth - 1]) is None:
            text = append_child(text, root, parent, f"<{names[depth - 1]}>\n</{names[depth - 1]}>")
            created.append("/".join(names[:depth]))
    return text, created


def find(root: Element, names: list[str]) -> Element:
    element = root
    for name in names:
        found = element.child(name)
        if found is None:
            raise PomEditError(f"<{'/'.join([root.name, *names])}> does not exist")
        element = found
    return element


def find_plugin(text: str, root: Element, group_id: str, artifact_id: str) -> Element | None:
    """A plugin declared in build/plugins or build/pluginManagement/plugins (groupId defaults to
    org.apache.maven.plugins, as in Maven)."""
    build = root.child("build")
    if build is None:
        return None
    containers = [build.child("plugins")]
    management = build.child("pluginManagement")
    if management is not None:
        containers.append(management.child("plugins"))
    for container in filter(None, containers):
        for plugin in container.all("plugin"):
            group = text_of(text, plugin.child("groupId")) or "org.apache.maven.plugins"
            if group == group_id and text_of(text, plugin.child("artifactId")) == artifact_id:
                return plugin
    return None

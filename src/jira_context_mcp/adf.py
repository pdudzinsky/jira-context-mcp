"""Atlassian Document Format (ADF) to markdown conversion.

Minimal walker that handles the node types commonly used in Jira issue
descriptions and comments: ``paragraph``, ``heading``, ``bulletList``,
``orderedList``, ``listItem``, ``codeBlock``, ``blockquote``, ``text``
(with ``strong``, ``em``, ``code``, ``strike``, ``link`` marks),
``hardBreak``, ``mention``, and ``emoji``. Unknown block or inline nodes are
preserved as ``[unsupported: <type>]`` markers so that missing content is
visible rather than silently dropped; unknown marks are silently ignored
(the underlying text is still rendered).

Pure stdlib — no runtime dependencies.
"""

from __future__ import annotations

from typing import Any

_AdfNode = dict[str, Any]


def adf_to_markdown(adf: Any) -> str | None:
    """Render an ADF document tree as markdown.

    Returns ``None`` when ``adf`` is not an ADF ``doc`` node, when its content
    is empty, or when rendering produces only whitespace.
    """
    if not isinstance(adf, dict) or adf.get("type") != "doc":
        return None
    blocks = [b for b in _render_blocks(adf.get("content") or []) if b]
    result = "\n\n".join(blocks).strip()
    return result or None


def _render_blocks(nodes: list[_AdfNode]) -> list[str]:
    rendered: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        text = _render_block(node)
        if text:
            rendered.append(text)
    return rendered


def _render_block(node: _AdfNode) -> str:
    node_type = node.get("type")
    content = node.get("content") or []
    attrs = node.get("attrs") or {}

    if node_type == "paragraph":
        return _render_inline(content)
    if node_type == "heading":
        level = attrs.get("level", 1)
        if not isinstance(level, int) or not 1 <= level <= 6:
            level = 1
        inline = _render_inline(content)
        return f"{'#' * level} {inline}" if inline else ""
    if node_type == "bulletList":
        return _render_list(content, bullet=True)
    if node_type == "orderedList":
        return _render_list(content, bullet=False)
    if node_type == "codeBlock":
        lang = attrs.get("language", "") or ""
        text = "".join(
            child.get("text", "")
            for child in content
            if isinstance(child, dict) and child.get("type") == "text"
        )
        return f"```{lang}\n{text}\n```"
    if node_type == "blockquote":
        inner = _render_blocks(content)
        if not inner:
            return ""
        body = "\n\n".join(inner)
        return "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
    if node_type == "listItem":
        # listItem is normally rendered by _render_list; reaching it here means
        # it appeared at the top level, which we render as its block children.
        return "\n\n".join(_render_blocks(content))
    return f"[unsupported: {node_type}]"


def _render_list(items: list[_AdfNode], *, bullet: bool) -> str:
    """Render a bulletList or orderedList.

    ``attrs.order`` on ``orderedList`` is intentionally ignored: markdown
    renderers renumber regardless, and lists beginning at a non-1 start are
    rare enough that the complexity is not worth it in v0.1.
    """
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or item.get("type") != "listItem":
            continue
        prefix = "- " if bullet else f"{index}. "
        item_content = item.get("content") or []
        rendered_blocks = _render_blocks(item_content)
        if not rendered_blocks:
            lines.append(prefix.rstrip())
            continue
        body = "\n\n".join(rendered_blocks)
        first, *rest = body.splitlines()
        lines.append(f"{prefix}{first}")
        for line in rest:
            lines.append(f"  {line}" if line else "")
    return "\n".join(lines)


def _render_inline(content: list[_AdfNode]) -> str:
    parts: list[str] = []
    for node in content:
        if not isinstance(node, dict):
            continue
        node_type = node.get("type")
        if node_type == "text":
            text = node.get("text", "")
            parts.append(_apply_marks(text, node.get("marks") or []))
        elif node_type == "hardBreak":
            parts.append("  \n")
        elif node_type == "mention":
            attrs = node.get("attrs") or {}
            text = attrs.get("text")
            if text:
                parts.append(text)
            else:
                user_id = attrs.get("id", "")
                parts.append(f"@user:{user_id}" if user_id else "@user")
        elif node_type == "emoji":
            attrs = node.get("attrs") or {}
            parts.append(attrs.get("text") or attrs.get("shortName") or "")
        else:
            parts.append(f"[unsupported: {node_type}]")
    return "".join(parts)


def _apply_marks(text: str, marks: list[_AdfNode]) -> str:
    for mark in marks:
        if not isinstance(mark, dict):
            continue
        mark_type = mark.get("type")
        if mark_type == "strong":
            text = f"**{text}**"
        elif mark_type == "em":
            text = f"*{text}*"
        elif mark_type == "code":
            text = f"`{text}`"
        elif mark_type == "strike":
            text = f"~~{text}~~"
        elif mark_type == "link":
            href = (mark.get("attrs") or {}).get("href", "")
            text = f"[{text}]({href})"
        # Unknown marks: keep underlying text, drop the decoration.
    return text

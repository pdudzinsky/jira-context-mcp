"""Render a :class:`TicketContext` into the markdown string returned by the MCP tool.

The layout is top-down: a header with a path breadcrumb, a meta-note about
tool arguments, an optional truncation note, an ASCII ``Issue tree`` showing
the full hierarchy at a glance, and then one detail section per node in
root → entry order. The entry ticket is marked with a target emoji plus an
``ENTRY`` text marker (belt-and-braces — the emoji guides the eye on scroll,
the text marker is unambiguous for LLMs that ignore emoji).

Path nodes in the tree are additionally prefixed with ``→`` so readers can
trace the walk visually; leaves (siblings of path nodes whose own children
were not fetched) render without any marker.
"""

from __future__ import annotations

from .models import Checklist, ChecklistItem, Comment, Ticket, TicketContext, TreeNode

_STATUS_MARKER: dict[str, str] = {
    "open": "[ ]",
    "done": "[x]",
    "in_progress": "[-]",
    "skipped": "[~]",
}


def render_checklist_items(items: list[ChecklistItem]) -> str:
    """Render a flat list of Smart Checklist items as a markdown task list.

    Public helper so other tools (e.g. the standalone ``get_smart_checklist``
    MCP tool) can reuse the canonical marker mapping without duplicating it.
    """
    return "\n".join(f"- {_STATUS_MARKER[item.status]} {item.name}" for item in items)


def render_checklist(checklist: Checklist, *, heading_level: int = 2) -> str:
    """Render a :class:`Checklist` as markdown, preserving section grouping.

    Each non-empty section is emitted as a header at ``heading_level`` (e.g.
    ``##`` for level 2, ``####`` for level 4 — pick a level that nests under
    the surrounding document structure) followed by its items as a markdown
    task list. Sections that contain no items, and an unnamed leading
    section without items, are skipped — empty headers carry no checklist
    information for the LLM and only add visual noise.
    """
    prefix = "#" * max(1, min(heading_level, 6))
    parts: list[str] = []
    for section in checklist.sections:
        if not section.items:
            continue
        if section.title:
            parts.append(f"{prefix} {section.title}")
        parts.append(render_checklist_items(section.items))
    return "\n\n".join(parts)


def render_ticket_context(
    ctx: TicketContext,
    *,
    include_comments: bool,
    max_depth: int,
) -> str:
    """Serialize a :class:`TicketContext` as markdown for the MCP tool response."""
    sections: list[str] = [
        f"# Ticket context: {ctx.entry_key}",
        _render_path_line(ctx),
        f"_(Generated with: include_comments={include_comments}, max_depth={max_depth})_",
    ]
    if ctx.truncated:
        sections.append(
            f"_(Note: hierarchy truncated at max_depth={max_depth}. "
            f"Root may have further ancestors.)_"
        )
    sections.append(_render_tree(ctx))
    sections.append("---")

    for node in ctx.path:
        sections.append(_render_node(node, include_comments=include_comments))
        sections.append("---")

    # Drop the trailing separator after the last node.
    if sections and sections[-1] == "---":
        sections.pop()

    return "\n\n".join(sections).rstrip() + "\n"


def _render_path_line(ctx: TicketContext) -> str:
    parts = [
        f"**{node.ticket.key}**" if node.is_entry else node.ticket.key
        for node in ctx.path
    ]
    return "Path: " + " → ".join(parts)


def _render_node(node: TreeNode, *, include_comments: bool) -> str:
    ticket = node.ticket
    lines: list[str] = []

    if node.is_entry:
        lines.append(
            f"## 🎯 {ticket.key} · [{ticket.issue_type}] {ticket.summary} ⬅️ ENTRY"
        )
    else:
        lines.append(f"## {ticket.key} · [{ticket.issue_type}] {ticket.summary}")

    lines.append(
        f"**Status:** {ticket.status} · "
        f"**Assignee:** {ticket.assignee or 'unassigned'} · "
        f"**URL:** {ticket.url}"
    )

    lines.extend(["", "### Description", ticket.description_md or "_(no description)_"])
    checklist_lines = _render_checklist(node)
    if checklist_lines:
        lines.extend(["", *checklist_lines])

    if include_comments:
        lines.extend(["", *_render_comments(node)])

    return "\n".join(lines)


def _render_checklist(node: TreeNode) -> list[str]:
    """Render the Smart Checklist for a node, or an empty list to skip the section.

    Returns ``[]`` (caller drops the section entirely) when the ticket has no
    Smart Checklist data — either the plugin isn't installed (``checklist is
    None``) or the property exists but has no items. Both cases carry no
    actionable information for the LLM, so omitting them keeps the output
    focused on tickets that actually have ACCs to consider.
    """
    if node.checklist is None or not node.checklist.items:
        return []
    # heading_level=4 keeps section headers (#### ...) one level below the
    # node's own ### Smart Checklist heading, preserving markdown hierarchy.
    count = _format_item_count(node.checklist.items)
    return [
        f"### Smart Checklist ({count})",
        render_checklist(node.checklist, heading_level=4),
    ]


def _format_item_count(items: list[ChecklistItem]) -> str:
    """Render the count summary used in Smart Checklist headers.

    Falls back to the total count when every item is open (the common case
    for the modern bullet-list format where statuses live in sibling Jira
    properties); otherwise reports done-vs-total so the reader sees progress.
    """
    total = len(items)
    done = sum(1 for item in items if item.status == "done")
    if done == 0:
        return f"{total} item{'s' if total != 1 else ''}"
    return f"{done}/{total} done"


def _render_tree(ctx: TicketContext) -> str:
    """Render an ASCII tree of the traversed hierarchy rooted at ``ctx.path[0]``.

    Path nodes are expanded (their children were fetched); their non-path
    siblings render as leaves since their own children are unknown. The entry
    ticket gets the 🎯 + ⬅️ ENTRY markers; other path nodes get a ``→``
    prefix so readers can trace the walk without re-reading the breadcrumb.
    """
    path_keys = {node.ticket.key for node in ctx.path}

    # parent_key -> list of children (preserves JQL order). A ticket's entry
    # in this map exists only if we fetched its children — i.e. it is a
    # non-entry path node. Entry is a leaf in the tree even if it has real
    # subtasks in Jira, because we deliberately stop walking down.
    parent_to_children: dict[str, list[Ticket]] = {}
    for i in range(1, len(ctx.path)):
        parent_key = ctx.path[i - 1].ticket.key
        parent_to_children[parent_key] = list(ctx.path[i].children_of_parent)

    root = ctx.path[0].ticket
    # Root is already visually distinct as the top of the tree — no marker.
    lines = [
        "## Issue tree",
        "",
        "```",
        f"{root.key} · [{root.issue_type}] {root.summary} · {root.status}",
    ]
    lines.extend(
        _render_tree_children(root, "", parent_to_children, path_keys, ctx.entry_key)
    )
    lines.append("```")
    return "\n".join(lines)


def _render_tree_children(
    ticket: Ticket,
    prefix: str,
    parent_to_children: dict[str, list[Ticket]],
    path_keys: set[str],
    entry_key: str,
) -> list[str]:
    children = parent_to_children.get(ticket.key)
    if not children:
        return []
    lines: list[str] = []
    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        branch = "└── " if is_last else "├── "
        lines.append(prefix + branch + _format_tree_line(child, entry_key, path_keys))
        sub_prefix = prefix + ("    " if is_last else "│   ")
        lines.extend(
            _render_tree_children(child, sub_prefix, parent_to_children, path_keys, entry_key)
        )
    return lines


def _format_tree_line(ticket: Ticket, entry_key: str, path_keys: set[str]) -> str:
    if ticket.key == entry_key:
        marker, suffix = "🎯 ", " ⬅️ ENTRY"
    elif ticket.key in path_keys:
        marker, suffix = "→ ", ""
    else:
        marker, suffix = "", ""
    return f"{marker}{ticket.key} · [{ticket.issue_type}] {ticket.summary} · {ticket.status}{suffix}"


def _render_comments(node: TreeNode) -> list[str]:
    if not node.comments:
        return ["### Comments", "_(no comments)_"]
    lines: list[str] = ["### Comments"]
    for comment in node.comments:
        lines.extend(_render_comment(comment))
    return lines


def _render_comment(comment: Comment) -> list[str]:
    date_str = comment.created.strftime("%Y-%m-%d %H:%M")
    body = comment.body_md or "_(empty comment)_"
    quoted = [f"> {line}" if line else ">" for line in body.splitlines()] or ["> _(empty)_"]
    return [f"**{date_str}, {comment.author}:**", *quoted, ""]

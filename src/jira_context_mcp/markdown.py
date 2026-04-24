"""Render a :class:`TicketContext` into the markdown string returned by the MCP tool.

The layout is top-down: a header with a path breadcrumb, meta-note about
tool arguments, an optional truncation note, and then one section per node
in root → entry order. The entry ticket is marked with a target emoji plus
an ``ENTRY`` text marker (belt-and-braces — the emoji guides the eye on
scroll, the text marker is unambiguous for LLMs that ignore emoji).
"""

from __future__ import annotations

from .models import ChecklistItem, Comment, Ticket, TicketContext, TreeNode

_STATUS_MARKER: dict[str, str] = {
    "open": "[ ]",
    "done": "[x]",
    "in_progress": "[-]",
    "skipped": "[~]",
}


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
    lines.extend(["", *_render_checklist(node)])
    lines.extend(["", *_render_children(node)])

    if include_comments:
        lines.extend(["", *_render_comments(node)])

    return "\n".join(lines)


def _render_checklist(node: TreeNode) -> list[str]:
    if node.checklist is None:
        return ["### Smart Checklist", "_(no checklist)_"]
    if not node.checklist.items:
        return ["### Smart Checklist", "_(empty checklist)_"]
    return ["### Smart Checklist", *(_render_checklist_item(i) for i in node.checklist.items)]


def _render_checklist_item(item: ChecklistItem) -> str:
    return f"- {_STATUS_MARKER[item.status]} {item.name}"


def _render_children(node: TreeNode) -> list[str]:
    if node.ticket.parent_key is None:
        return [
            "### Children",
            "_(not fetched — this is the root of the traversed hierarchy)_",
        ]

    children = node.children_of_parent
    header = f"### Children ({len(children)})"
    return [header, *(_render_child_line(t, self_key=node.ticket.key) for t in children)]


def _render_child_line(ticket: Ticket, *, self_key: str) -> str:
    body = f"{ticket.key} · [{ticket.issue_type}] {ticket.summary} · {ticket.status}"
    if ticket.key == self_key:
        return f"- **→ {body}**"
    return f"- {body}"


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

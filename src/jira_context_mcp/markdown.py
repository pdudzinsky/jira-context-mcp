"""Markdown renderers for the three MCP tools.

Three public entry points:

- :func:`render_issue_tree` — formats a :class:`TreeNode` (built by
  :mod:`.tree`) as a fenced ASCII tree plus a short Overview aggregate;
  used by the ``get_issue_tree`` tool.
- :func:`render_ticket_content` — formats a single ticket's full content
  (description + Smart Checklist + optional comments); used by the
  ``get_ticket_content`` tool.
- :func:`render_checklist` / :func:`render_checklist_items` — used both by
  ``render_ticket_content`` and the standalone ``get_smart_checklist`` tool.

All renderers are pure functions of their inputs.
"""

from __future__ import annotations

from collections import Counter

from .models import Checklist, ChecklistItem, Comment, Ticket, TreeNode

_STATUS_MARKER: dict[str, str] = {
    "open": "[ ]",
    "done": "[x]",
    "in_progress": "[-]",
    "skipped": "[~]",
}


# ---------- Smart Checklist (shared by content + standalone tool) ----------


def render_checklist_items(items: list[ChecklistItem]) -> str:
    """Render a flat list of Smart Checklist items as a markdown task list."""
    return "\n".join(f"- {_STATUS_MARKER[item.status]} {item.name}" for item in items)


def render_checklist(checklist: Checklist, *, heading_level: int = 2) -> str:
    """Render a :class:`Checklist` preserving section grouping.

    Each non-empty section is emitted as a header at ``heading_level``
    (clamped to ``[1, 6]``) followed by its items as a task list. Empty
    sections — and an unnamed leading section without items — are skipped.
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


def _format_item_count(items: list[ChecklistItem]) -> str:
    total = len(items)
    done = sum(1 for item in items if item.status == "done")
    if done == 0:
        return f"{total} item{'s' if total != 1 else ''}"
    return f"{done}/{total} done"


# ---------- Issue tree ----------


def render_issue_tree(tree: TreeNode) -> str:
    """Render a :class:`TreeNode` as ``# Issue tree`` markdown.

    Output structure:

    .. code-block:: markdown

        # Issue tree: <focus_key>

        ## Overview
        Total: 27 tickets · By type: 1 Epic, 5 Story, 21 Subtask
        By status: 24 Done, 2 In Progress, 1 Rejected

        ## Tree

        ```
        ROOT-1 · [Epic] ... · In Progress
        ├── CHILD-1 · [Story] ... · In QA
        │   ├── 🎯 CHILD-1-1 · [Task] ... · Gotowe ⬅️ FOCUS
        │   └── ...
        └── CHILD-2 · ...
        ```
    """
    focus_key = _find_focus_key(tree) or tree.ticket.key
    stats = _collect_stats(tree)

    lines: list[str] = [f"# Issue tree: {focus_key}", "", "## Overview", ""]
    lines.append(_format_overview(stats))
    lines.extend(["", "## Tree", "", "```"])

    lines.append(_format_tree_line(tree, prefix="", branch="", is_root=True))
    lines.extend(_render_children(tree, prefix=""))
    lines.append("```")

    return "\n".join(lines) + "\n"


def _find_focus_key(node: TreeNode) -> str | None:
    if node.is_focus:
        return node.ticket.key
    for child in node.children:
        result = _find_focus_key(child)
        if result is not None:
            return result
    return None


def _collect_stats(node: TreeNode) -> dict[str, object]:
    """Collect aggregate counts across the whole tree (root + descendants)."""
    types: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    total = 0

    def _walk(n: TreeNode) -> None:
        nonlocal total
        total += 1
        types[n.ticket.issue_type] += 1
        statuses[n.ticket.status] += 1
        for child in n.children:
            _walk(child)

    _walk(node)
    return {"total": total, "types": types, "statuses": statuses}


def _format_overview(stats: dict[str, object]) -> str:
    types = stats["types"]
    statuses = stats["statuses"]
    assert isinstance(types, Counter)
    assert isinstance(statuses, Counter)

    type_str = ", ".join(f"{n} {t}" for t, n in types.most_common())
    status_str = ", ".join(f"{n} {s}" for s, n in statuses.most_common())

    return f"Total: {stats['total']} tickets · By type: {type_str}\nBy status: {status_str}"


def _render_children(node: TreeNode, prefix: str) -> list[str]:
    lines: list[str] = []
    for index, child in enumerate(node.children):
        is_last = index == len(node.children) - 1
        branch = "└── " if is_last else "├── "
        lines.append(_format_tree_line(child, prefix=prefix, branch=branch))
        sub_prefix = prefix + ("    " if is_last else "│   ")
        lines.extend(_render_children(child, sub_prefix))
    return lines


def _format_tree_line(node: TreeNode, *, prefix: str, branch: str, is_root: bool = False) -> str:
    ticket = node.ticket
    if node.is_focus:
        marker, suffix = "🎯 ", " ⬅️ FOCUS"
    else:
        marker, suffix = "", ""
    body = (
        f"{marker}{ticket.key} · [{ticket.issue_type}] {ticket.summary} · {ticket.status}{suffix}"
    )
    if is_root:
        return body
    return f"{prefix}{branch}{body}"


# ---------- Single-ticket full content ----------


def render_ticket_content(
    ticket: Ticket,
    *,
    checklist: Checklist | None,
    comments: list[Comment],
    include_comments: bool,
) -> str:
    """Render full content of a single ticket as markdown.

    Always shows the header (key, type, summary, status, assignee, URL) and
    the description block. Smart Checklist appears only when the ticket
    actually has items; comments only when ``include_comments`` is true.
    """
    lines: list[str] = [
        f"# {ticket.key} · [{ticket.issue_type}] {ticket.summary}",
        (
            f"**Status:** {ticket.status} · "
            f"**Assignee:** {ticket.assignee or 'unassigned'} · "
            f"**URL:** {ticket.url}"
        ),
        "",
        "## Description",
        ticket.description_md or "_(no description)_",
    ]

    if checklist is not None and checklist.items:
        count = _format_item_count(checklist.items)
        lines.extend(
            [
                "",
                f"## Smart Checklist ({count})",
                render_checklist(checklist, heading_level=3),
            ]
        )

    if include_comments:
        lines.extend(["", "## Comments", *_render_comments_body(comments)])

    return "\n".join(lines).rstrip() + "\n"


def _render_comments_body(comments: list[Comment]) -> list[str]:
    if not comments:
        return ["_(no comments)_"]
    lines: list[str] = []
    for comment in comments:
        lines.extend(_render_comment(comment))
    return lines


def _render_comment(comment: Comment) -> list[str]:
    date_str = comment.created.strftime("%Y-%m-%d %H:%M")
    body = comment.body_md or "_(empty comment)_"
    quoted = [f"> {line}" if line else ">" for line in body.splitlines()] or ["> _(empty)_"]
    return [f"**{date_str}, {comment.author}:**", *quoted, ""]

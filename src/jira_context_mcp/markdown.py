"""Markdown renderers for the three MCP tools.

Three public entry points:

- :func:`render_issue_tree` — formats a :class:`TreeNode` (built by
  :mod:`.tree`) as a fenced ASCII tree plus a short Overview aggregate;
  used by the ``get_issue_tree`` tool.
- :func:`render_ticket_content` — formats a single ticket's full content
  (description + Smart Checklist + attachment list + optional comments);
  used by the ``get_ticket_content`` tool.
- :func:`render_checklist` / :func:`format_checklist_count` — used both by
  ``render_ticket_content`` and the standalone ``get_smart_checklist`` tool.

All renderers are pure functions of their inputs.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Final

from .models import Attachment, Checklist, ChecklistItem, ChecklistText, Comment, Ticket, TreeNode

_STATUS_MARKER: dict[str, str] = {
    "open": "[ ]",
    "done": "[x]",
    "in_progress": "[-]",
    "skipped": "[~]",
}


# ---------- Smart Checklist (shared by content + standalone tool) ----------


def _render_item(item: ChecklistItem) -> str:
    head = f"- {_STATUS_MARKER[item.status]}"
    if item.mandatory:
        head += " (mandatory)"
    if item.custom_status:
        head += f" [{item.custom_status}]"
    details = (f"  > {detail}".rstrip() for detail in item.details)
    return "\n".join([f"{head} {item.name}", *details])


def _render_entries(entries: list[ChecklistItem | ChecklistText]) -> str:
    """Render one section's lines in their original order.

    A blank line separates items from text. Without it, markdown would read a
    text line as a continuation of the list item above.
    """
    lines: list[str] = []
    for index, entry in enumerate(entries):
        if index and isinstance(entry, ChecklistText) != isinstance(
            entries[index - 1], ChecklistText
        ):
            lines.append("")
        lines.append(entry.text if isinstance(entry, ChecklistText) else _render_item(entry))
    return "\n".join(lines)


def render_checklist(checklist: Checklist, *, heading_level: int = 2) -> str:
    """Render a :class:`Checklist` preserving section grouping and nesting.

    The shallowest header goes at ``heading_level`` (clamped to ``[1, 6]``) and
    deeper ones keep their distance from it, up to level 6. Every titled section
    is emitted, even with nothing under it: it can be the parent of the next one
    or a note in its own right.
    """
    base_level = max(1, min(heading_level, 6))
    top_level = min((s.level for s in checklist.sections if s.title), default=1)
    parts: list[str] = []
    for section in checklist.sections:
        if section.title:
            level = min(base_level + section.level - top_level, 6)
            parts.append(f"{'#' * level} {section.title}")
        if section.entries:
            parts.append(_render_entries(section.entries))
    return "\n\n".join(parts)


def format_checklist_count(checklist: Checklist) -> str:
    """Summarise a checklist for its heading, e.g. ``2/5 done, 1 in progress, 1 skipped``.

    Jira's "Smart Checklist Progress" field counts skipped items as completed, so
    its ``x/y`` is the done plus skipped figures here over the same total. Custom
    statuses are listed by name for items still marked as to do, in brackets as on
    their item lines: whether one counts as completed is configured in Jira and
    isn't in the markdown.
    """
    items = checklist.items
    total = len(items)
    if not total and any(section.entries for section in checklist.sections):
        return "0 items, text only"
    statuses = Counter(item.status for item in items)
    parts = [
        f"{statuses['done']}/{total} done"
        if statuses["done"]
        else f"{total} item{'s' if total != 1 else ''}"
    ]
    if statuses["in_progress"]:
        parts.append(f"{statuses['in_progress']} in progress")
    if statuses["skipped"]:
        parts.append(f"{statuses['skipped']} skipped")
    custom_statuses = Counter(
        item.custom_status for item in items if item.status == "open" and item.custom_status
    )
    parts.extend(f"{count} [{name}]" for name, count in custom_statuses.items())
    return ", ".join(parts)


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
        │   ├── 🎯 CHILD-1-1 · [Task] ... · Done ⬅️ FOCUS
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

# The section headings :func:`render_ticket_content` emits. ``heading_offset``
# moves real ADF headings to level 4 or deeper, and checklist headers start at
# level 3, so a description or checklist line matching this is literal text
# someone typed, not document structure. Whitespace covers what a client might
# strip or skip: ``\s``, plus the BOM that JavaScript's ``trim`` removes too.
_SECTION_HEADING_RE: Final = re.compile(
    r"^([\s\ufeff]*)"
    r"(##[\s\ufeff]+(?:Description|Smart[\s\ufeff]+Checklist|Attachments|Comments)\b)",
    re.IGNORECASE,
)


def render_ticket_content(
    ticket: Ticket,
    *,
    checklist: Checklist | None,
    comments: list[Comment],
    include_comments: bool,
) -> str:
    """Render full content of a single ticket as markdown.

    Always shows the header (key, type, summary, status, assignee, URL) and
    the description block. Smart Checklist appears only when the ticket has
    something in it, items or not; the attachment list only when it has files;
    comments only when ``include_comments`` is true.
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
        _escape_section_headings(ticket.description_md or "_(no description)_"),
    ]

    if checklist is not None and not checklist.is_empty:
        lines.extend(
            [
                "",
                f"## Smart Checklist ({format_checklist_count(checklist)})",
                _escape_section_headings(render_checklist(checklist, heading_level=3)),
            ]
        )

    if ticket.attachments:
        lines.extend(["", render_attachments(ticket.attachments)])

    if include_comments:
        lines.extend(["", "## Comments", *_render_comments_body(comments)])

    return "\n".join(lines).rstrip() + "\n"


def _escape_section_headings(text: str) -> str:
    """Stop ticket text from passing for one of this document's own sections.

    Clients parse this output and anyone who can edit a ticket controls its
    description and checklist, so a literal ``## Attachments`` line could
    otherwise introduce a forged attachment list. ``splitlines`` covers every
    line boundary a client might split on.
    """
    return "\n".join(_SECTION_HEADING_RE.sub(r"\1\\\2", line) for line in text.splitlines())


# ---------- Attachments ----------

# The only URL shape shown to readers: http(s), with no whitespace, userinfo,
# query or fragment. Jira's ``attachment.content`` fits it; anything else could
# carry a secret or break the line, so it is left out rather than cleaned up.
_SAFE_URL_RE: Final = re.compile(r"https?://[^\s/?#@]+(?:/[^\s?#]*)?")
# A ``·`` next to whitespace or a field edge would read as a field separator.
_LOOSE_SEPARATOR_RE: Final = re.compile(r"(?<!\S)·|·(?!\S)")


def render_attachments(attachments: tuple[Attachment, ...] | list[Attachment]) -> str:
    """Render a ticket's attachments as a ``## Attachments`` markdown section.

    Each file gets one line with everything the LLM needs to decide whether
    to fetch it: id (key for ``get_ticket_attachment``), filename, mime type,
    human-readable size, creation date, author, and an ``embedded`` /
    ``orphan`` marker indicating whether the file is referenced from the
    description prose (embedded — part of the narrative, priority) or just
    attached without mention (orphan — usually post-review revisions or
    supplementary material).

    The line ends with ``content_url``, exactly as Jira returned it, as plain
    text; opening it still requires Jira auth. A value outside
    :data:`_SAFE_URL_RE` is left out and the line stops at the marker, so the
    URL is optional but the five fields before it keep their positions.
    :func:`~.jira._parse_attachments` drops files with no ``content`` before
    they get here.

    Returns an empty string when ``attachments`` is empty so callers can
    treat the result as concatenable without a presence check.
    """
    if not attachments:
        return ""
    lines = ["## Attachments"]
    for a in attachments:
        date_str = a.created.strftime("%Y-%m-%d")
        status = "embedded" if a.embedded else "orphan"
        url_field = f" · {a.content_url}" if _SAFE_URL_RE.fullmatch(a.content_url) else ""
        lines.append(
            f"- [{a.id}] {_field(a.filename)} · {_field(a.mime_type)} · "
            f"{_format_size(a.size)} · {date_str} by {_field(a.author)} · {status}{url_field}"
        )
    return "\n".join(lines)


def _field(text: str) -> str:
    """Keep free text from Jira inside its own field of an attachment line.

    Clients parse these lines, so a line break must not be able to start a new
    entry and a free-standing ``·`` must not shift the fields after it.
    """
    return _LOOSE_SEPARATOR_RE.sub("-", " ".join(text.splitlines()))


def _format_size(size_bytes: int) -> str:
    """Format a byte count as B / KB / MB with at most one decimal place.

    Informational only — the renderer never compares this string against
    the size cap. Cap enforcement always works on raw bytes in the Jira
    client (``download_attachment``) and the server tool, both of which
    operate on the unrounded ``attachment.size`` integer.
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    # Compare the rounded value, or just under 1 MiB would read as ``1024 KB``.
    kilobytes = round(size_bytes / 1024)
    if kilobytes < 1024:
        return f"{kilobytes} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


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

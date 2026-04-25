"""Tests for the markdown renderer (Issue tree, per-node sections, checklist)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jira_context_mcp.markdown import (
    render_checklist,
    render_checklist_items,
    render_ticket_context,
)
from jira_context_mcp.models import (
    Checklist,
    ChecklistItem,
    ChecklistSection,
    Comment,
    Ticket,
    TicketContext,
    TreeNode,
)

FIXTURES = Path(__file__).parent / "fixtures" / "expected"


def make_ticket(
    key: str,
    *,
    summary: str | None = None,
    status: str = "Open",
    issue_type: str = "Story",
    parent_key: str | None = None,
    description_md: str | None = None,
    assignee: str | None = None,
) -> Ticket:
    return Ticket(
        key=key,
        summary=summary or f"summary of {key}",
        status=status,
        issue_type=issue_type,
        assignee=assignee,
        description_md=description_md,
        parent_key=parent_key,
        url=f"https://example.atlassian.net/browse/{key}",
    )


def node(
    ticket: Ticket,
    *,
    checklist: Checklist | None = None,
    comments: list[Comment] | None = None,
    children_of_parent: list[Ticket] | None = None,
    is_entry: bool = False,
) -> TreeNode:
    return TreeNode(
        ticket=ticket,
        checklist=checklist,
        comments=comments or [],
        children_of_parent=children_of_parent or [],
        is_entry=is_entry,
    )


# ---------- render_checklist_items ----------


def test_render_checklist_items_uses_canonical_markers() -> None:
    out = render_checklist_items(
        [
            ChecklistItem(name="a", status="open"),
            ChecklistItem(name="b", status="done"),
            ChecklistItem(name="c", status="in_progress"),
            ChecklistItem(name="d", status="skipped"),
        ]
    )
    assert out == "- [ ] a\n- [x] b\n- [-] c\n- [~] d"


# ---------- render_checklist (sectioned) ----------


def test_render_checklist_default_heading_level_two(simple_checklist: Checklist) -> None:
    out = render_checklist(simple_checklist)
    assert "## 1. First section" in out
    assert "## 2. Second section" in out
    assert "- [x] alpha" in out
    assert "- [-] gamma" in out


def test_render_checklist_heading_level_four(simple_checklist: Checklist) -> None:
    out = render_checklist(simple_checklist, heading_level=4)
    assert "#### 1. First section" in out
    assert "#### 2. Second section" in out


def test_render_checklist_clamps_excessive_heading_level(simple_checklist: Checklist) -> None:
    out = render_checklist(simple_checklist, heading_level=8)
    # Markdown spec caps headings at 6
    assert "###### 1. First section" in out
    assert "####### " not in out


def test_render_checklist_clamps_zero_or_negative_heading_level(
    simple_checklist: Checklist,
) -> None:
    out = render_checklist(simple_checklist, heading_level=0)
    assert "# 1. First section" in out
    out_neg = render_checklist(simple_checklist, heading_level=-3)
    assert "# 1. First section" in out_neg


def test_render_checklist_skips_empty_sections() -> None:
    cl = Checklist(
        sections=[
            ChecklistSection(title="Important Note", items=[]),
            ChecklistSection(title="Real", items=[ChecklistItem(name="x")]),
        ]
    )
    out = render_checklist(cl, heading_level=2)
    assert "Important Note" not in out
    assert "## Real" in out
    assert "- [ ] x" in out


def test_render_checklist_unsectioned_items_omit_header() -> None:
    cl = Checklist(
        sections=[ChecklistSection(title=None, items=[ChecklistItem(name="x")])]
    )
    out = render_checklist(cl)
    assert out == "- [ ] x"


# ---------- render_ticket_context: structural assertions ----------


def _ctx_3level_with_checklist() -> TicketContext:
    root = make_ticket("ROOT-1", summary="Root epic", issue_type="Epic", status="In Progress")
    middle = make_ticket(
        "MID-1",
        summary="Middle story",
        parent_key="ROOT-1",
        status="In Progress",
    )
    entry = make_ticket(
        "ENT-1",
        summary="Entry task",
        issue_type="Task",
        parent_key="MID-1",
        status="In Progress",
        description_md="Plain description.",
    )
    middle_siblings = [
        make_ticket("MID-2", summary="Middle peer A", parent_key="ROOT-1", status="Done"),
        middle,
        make_ticket("MID-3", summary="Middle peer B", parent_key="ROOT-1", status="To Do"),
    ]
    entry_siblings = [
        make_ticket("ENT-0", summary="Entry peer A", issue_type="Task",
                    parent_key="MID-1", status="Done"),
        entry,
        make_ticket("ENT-2", summary="Entry peer B", issue_type="Task",
                    parent_key="MID-1", status="To Do"),
    ]
    middle_checklist = Checklist(
        sections=[
            ChecklistSection(
                title="1. Things",
                items=[
                    ChecklistItem(name="alpha", status="done"),
                    ChecklistItem(name="beta", status="open"),
                ],
            )
        ]
    )
    return TicketContext(
        path=[
            node(root, children_of_parent=[]),
            node(middle, checklist=middle_checklist, children_of_parent=middle_siblings),
            node(entry, children_of_parent=entry_siblings, is_entry=True),
        ],
        entry_key="ENT-1",
    )


def test_render_top_header_and_path() -> None:
    ctx = _ctx_3level_with_checklist()
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    assert out.startswith("# Ticket context: ENT-1\n")
    assert "Path: ROOT-1 → MID-1 → **ENT-1**" in out


def test_render_meta_note_present() -> None:
    ctx = _ctx_3level_with_checklist()
    out = render_ticket_context(ctx, include_comments=True, max_depth=5)
    assert "_(Generated with: include_comments=True, max_depth=5)_" in out


def test_render_truncation_note_when_truncated() -> None:
    ctx = _ctx_3level_with_checklist()
    truncated = TicketContext(
        path=ctx.path, entry_key=ctx.entry_key, truncated=True,
    )
    out = render_ticket_context(truncated, include_comments=False, max_depth=2)
    assert "hierarchy truncated at max_depth=2" in out


def test_render_no_truncation_note_when_not_truncated() -> None:
    ctx = _ctx_3level_with_checklist()
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    assert "hierarchy truncated" not in out


# ---------- Issue tree ----------


def test_tree_wrapped_in_fenced_code_block() -> None:
    ctx = _ctx_3level_with_checklist()
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    # tree section header followed by ``` opening
    idx = out.index("## Issue tree")
    assert "```\n" in out[idx : idx + 200]


def test_tree_root_has_no_marker_path_nodes_have_arrow_entry_has_target() -> None:
    ctx = _ctx_3level_with_checklist()
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    tree_block = out.split("```")[1]
    # root: first line, no leading arrow or target
    first_line = tree_block.strip().splitlines()[0]
    assert first_line.startswith("ROOT-1")
    # path node middle gets the → marker
    assert "→ MID-1" in tree_block
    # entry gets 🎯 + ⬅️ ENTRY
    assert "🎯 ENT-1" in tree_block
    assert "⬅️ ENTRY" in tree_block


def test_tree_preserves_jql_response_order() -> None:
    """Children should appear in the order children_of_parent stores them,
    not alphabetical."""
    ctx = _ctx_3level_with_checklist()
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    tree_block = out.split("```")[1]
    mid_lines = [line for line in tree_block.splitlines() if " MID-" in line]
    # Order in our fixture: MID-2, MID-1 (path), MID-3
    assert mid_lines[0].strip().endswith("Middle peer A · Done")
    assert "→ MID-1" in mid_lines[1]
    assert mid_lines[2].strip().endswith("Middle peer B · To Do")


def test_tree_non_path_siblings_render_as_leaves() -> None:
    ctx = _ctx_3level_with_checklist()
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    tree_block = out.split("```")[1]
    # MID-2 is a non-path sibling; should not have a → or 🎯 marker
    mid2_line = next(line for line in tree_block.splitlines() if "MID-2" in line)
    assert "→" not in mid2_line
    assert "🎯" not in mid2_line


# ---------- per-node Smart Checklist behaviour ----------


def test_render_checklist_section_includes_item_count_when_none_done() -> None:
    ctx = _ctx_3level_with_checklist()
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    # 1 done + 1 open = "1/2 done" (mixed -> done count surfaces)
    assert "### Smart Checklist (1/2 done)" in out


def test_render_checklist_count_shows_total_when_all_open(
    sample_ticket: Ticket,
) -> None:
    cl = Checklist(
        sections=[
            ChecklistSection(
                title=None,
                items=[ChecklistItem(name="x"), ChecklistItem(name="y")],
            )
        ]
    )
    n = node(sample_ticket, checklist=cl, is_entry=True)
    ctx = TicketContext(path=[n], entry_key=sample_ticket.key)
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    assert "### Smart Checklist (2 items)" in out


def test_per_node_checklist_uses_heading_level_four(sample_ticket: Ticket) -> None:
    cl = Checklist(
        sections=[
            ChecklistSection(title="Group", items=[ChecklistItem(name="x")]),
        ]
    )
    n = node(sample_ticket, checklist=cl, is_entry=True)
    ctx = TicketContext(path=[n], entry_key=sample_ticket.key)
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    assert "#### Group" in out


def test_no_checklist_section_when_checklist_is_none(sample_ticket: Ticket) -> None:
    n = node(sample_ticket, checklist=None, is_entry=True)
    ctx = TicketContext(path=[n], entry_key=sample_ticket.key)
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    assert "Smart Checklist" not in out


def test_no_checklist_section_when_checklist_is_empty(sample_ticket: Ticket) -> None:
    n = node(sample_ticket, checklist=Checklist(sections=[]), is_entry=True)
    ctx = TicketContext(path=[n], entry_key=sample_ticket.key)
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    assert "Smart Checklist" not in out


# ---------- comments ----------


def test_comments_section_absent_when_include_comments_false(sample_ticket: Ticket) -> None:
    c = Comment(
        author="A", created=datetime(2026, 1, 1, tzinfo=UTC), body_md="x"
    )
    n = node(sample_ticket, comments=[c], is_entry=True)
    ctx = TicketContext(path=[n], entry_key=sample_ticket.key)
    out = render_ticket_context(ctx, include_comments=False, max_depth=10)
    assert "### Comments" not in out


def test_comments_section_shows_no_comments_placeholder_when_empty(
    sample_ticket: Ticket,
) -> None:
    n = node(sample_ticket, is_entry=True)
    ctx = TicketContext(path=[n], entry_key=sample_ticket.key)
    out = render_ticket_context(ctx, include_comments=True, max_depth=10)
    assert "### Comments" in out
    assert "_(no comments)_" in out


def test_comments_render_with_date_author_and_blockquoted_body(
    sample_ticket: Ticket,
) -> None:
    c = Comment(
        author="Bob",
        created=datetime(2026, 4, 22, 14, 5, tzinfo=UTC),
        body_md="line 1\n\nline 2",
    )
    n = node(sample_ticket, comments=[c], is_entry=True)
    ctx = TicketContext(path=[n], entry_key=sample_ticket.key)
    out = render_ticket_context(ctx, include_comments=True, max_depth=10)
    assert "**2026-04-22 14:05, Bob:**" in out
    # blank line between paragraphs continues the blockquote with ">"
    assert "> line 1\n>\n> line 2" in out


# ---------- golden file ----------


def test_render_3level_with_sections_matches_golden() -> None:
    ctx = _ctx_3level_with_checklist()
    actual = render_ticket_context(ctx, include_comments=False, max_depth=10)
    expected = (FIXTURES / "3level_with_sections.md").read_text()
    assert actual == expected

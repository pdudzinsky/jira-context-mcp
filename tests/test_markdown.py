"""Tests for the markdown renderers (issue tree, ticket content, checklist)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jira_context_mcp.markdown import (
    render_checklist,
    render_checklist_items,
    render_issue_tree,
    render_ticket_content,
)
from jira_context_mcp.models import (
    Checklist,
    ChecklistItem,
    ChecklistSection,
    Comment,
    Ticket,
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
    children: list[TreeNode] | None = None,
    is_focus: bool = False,
) -> TreeNode:
    return TreeNode(ticket=ticket, children=children or [], is_focus=is_focus)


# ===================================================================
# render_checklist_items / render_checklist
# ===================================================================


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
    assert "###### 1. First section" in out
    assert "####### " not in out


def test_render_checklist_clamps_zero_or_negative_heading_level(
    simple_checklist: Checklist,
) -> None:
    assert "# 1. First section" in render_checklist(simple_checklist, heading_level=0)
    assert "# 1. First section" in render_checklist(simple_checklist, heading_level=-3)


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
    cl = Checklist(sections=[ChecklistSection(title=None, items=[ChecklistItem(name="x")])])
    assert render_checklist(cl) == "- [ ] x"


# ===================================================================
# render_issue_tree
# ===================================================================


def _build_3level_tree() -> TreeNode:
    """Root Epic + 3 mid Stories (2 path-adjacent, 1 sibling) + focus leaf."""
    root_t = make_ticket("ROOT-1", summary="Root epic", issue_type="Epic", status="In Progress")
    mid1_t = make_ticket("MID-1", summary="Mid one", parent_key="ROOT-1", status="Done")
    mid2_t = make_ticket("MID-2", summary="Mid two", parent_key="ROOT-1", status="In Progress")
    mid3_t = make_ticket("MID-3", summary="Mid three", parent_key="ROOT-1", status="To Do")
    leaf_a = make_ticket(
        "LEAF-A", summary="Leaf a", issue_type="Task", parent_key="MID-2", status="Done"
    )
    leaf_b = make_ticket(
        "LEAF-B", summary="Leaf b", issue_type="Task", parent_key="MID-2", status="In Progress"
    )
    return node(
        root_t,
        children=[
            node(mid1_t),
            node(
                mid2_t,
                children=[
                    node(leaf_a),
                    node(leaf_b, is_focus=True),
                ],
            ),
            node(mid3_t),
        ],
    )


def test_render_issue_tree_top_header_uses_focus_key() -> None:
    out = render_issue_tree(_build_3level_tree())
    assert out.startswith("# Issue tree: LEAF-B\n")


def test_render_issue_tree_overview_lists_counts() -> None:
    out = render_issue_tree(_build_3level_tree())
    assert "Total: 6 tickets" in out
    # 1 Epic + 3 Story + 2 Task across the tree
    assert "Epic" in out
    assert "Story" in out
    assert "Task" in out


def test_render_issue_tree_wrapped_in_fenced_code_block() -> None:
    out = render_issue_tree(_build_3level_tree())
    assert "## Tree" in out
    # opening fence appears between "## Tree" and the root key
    tree_idx = out.index("## Tree")
    assert "```\nROOT-1" in out[tree_idx:]


def test_render_issue_tree_root_has_no_marker() -> None:
    out = render_issue_tree(_build_3level_tree())
    # the root line shouldn't start with the focus emoji
    tree_block = out.split("```")[1]
    first_line = tree_block.strip().splitlines()[0]
    assert first_line.startswith("ROOT-1")
    assert "🎯" not in first_line


def test_render_issue_tree_focus_marker_on_focus_node() -> None:
    out = render_issue_tree(_build_3level_tree())
    tree_block = out.split("```")[1]
    focus_line = next(line for line in tree_block.splitlines() if "LEAF-B" in line)
    assert "🎯" in focus_line
    assert "⬅️ FOCUS" in focus_line


def test_render_issue_tree_non_focus_nodes_have_no_marker() -> None:
    out = render_issue_tree(_build_3level_tree())
    tree_block = out.split("```")[1]
    mid1_line = next(line for line in tree_block.splitlines() if "MID-1" in line)
    assert "🎯" not in mid1_line
    assert "⬅️ FOCUS" not in mid1_line


def test_render_issue_tree_preserves_sibling_order() -> None:
    out = render_issue_tree(_build_3level_tree())
    tree_block = out.split("```")[1]
    mid_lines = [line for line in tree_block.splitlines() if "MID-" in line]
    # tree was built with [MID-1, MID-2, MID-3] in that order
    assert mid_lines[0].strip().startswith("├── MID-1")
    assert mid_lines[1].strip().startswith("├── MID-2")
    assert mid_lines[2].strip().startswith("└── MID-3")


def test_render_issue_tree_single_node_tree() -> None:
    """A focus that is itself the root (no children fetched) still renders."""
    just_root = node(
        make_ticket("EPIC-7", issue_type="Epic", status="In Progress"),
        is_focus=True,
    )
    out = render_issue_tree(just_root)
    assert "Total: 1 tickets" in out
    assert "🎯 EPIC-7" in out
    assert "⬅️ FOCUS" in out


# ===================================================================
# render_ticket_content
# ===================================================================


def test_render_ticket_content_minimal() -> None:
    t = make_ticket("FOO-1", summary="A task", description_md="Plain description.")
    out = render_ticket_content(t, checklist=None, comments=[], include_comments=False)
    assert "# FOO-1 · [Story] A task" in out
    assert "**Status:** Open · **Assignee:** unassigned" in out
    assert "## Description" in out
    assert "Plain description." in out
    assert "Smart Checklist" not in out
    assert "Comments" not in out


def test_render_ticket_content_no_description_uses_placeholder() -> None:
    t = make_ticket("FOO-1")
    out = render_ticket_content(t, checklist=None, comments=[], include_comments=False)
    assert "_(no description)_" in out


def test_render_ticket_content_renders_assignee_when_present() -> None:
    t = make_ticket("FOO-1", assignee="Alice")
    out = render_ticket_content(t, checklist=None, comments=[], include_comments=False)
    assert "**Assignee:** Alice" in out


def test_render_ticket_content_includes_checklist_with_count() -> None:
    t = make_ticket("FOO-1")
    cl = Checklist(
        sections=[
            ChecklistSection(
                title="A",
                items=[
                    ChecklistItem(name="x", status="done"),
                    ChecklistItem(name="y", status="open"),
                ],
            )
        ]
    )
    out = render_ticket_content(t, checklist=cl, comments=[], include_comments=False)
    assert "## Smart Checklist (1/2 done)" in out
    assert "### A" in out
    assert "- [x] x" in out
    assert "- [ ] y" in out


def test_render_ticket_content_count_uses_total_when_none_done() -> None:
    t = make_ticket("FOO-1")
    cl = Checklist(
        sections=[
            ChecklistSection(
                title=None,
                items=[ChecklistItem(name="x"), ChecklistItem(name="y")],
            )
        ]
    )
    out = render_ticket_content(t, checklist=cl, comments=[], include_comments=False)
    assert "## Smart Checklist (2 items)" in out


def test_render_ticket_content_omits_checklist_when_no_items() -> None:
    t = make_ticket("FOO-1")
    out = render_ticket_content(
        t, checklist=Checklist(sections=[]), comments=[], include_comments=False
    )
    assert "Smart Checklist" not in out


def test_render_ticket_content_omits_checklist_when_none() -> None:
    t = make_ticket("FOO-1")
    out = render_ticket_content(t, checklist=None, comments=[], include_comments=False)
    assert "Smart Checklist" not in out


def test_render_ticket_content_comments_section_absent_when_flag_false() -> None:
    t = make_ticket("FOO-1")
    c = Comment(author="A", created=datetime(2026, 1, 1, tzinfo=UTC), body_md="hi")
    out = render_ticket_content(t, checklist=None, comments=[c], include_comments=False)
    assert "Comments" not in out


def test_render_ticket_content_no_comments_placeholder_when_flag_true_and_empty() -> None:
    t = make_ticket("FOO-1")
    out = render_ticket_content(t, checklist=None, comments=[], include_comments=True)
    assert "## Comments" in out
    assert "_(no comments)_" in out


def test_render_ticket_content_renders_comment_with_blockquoted_body() -> None:
    t = make_ticket("FOO-1")
    c = Comment(
        author="Bob",
        created=datetime(2026, 4, 22, 14, 5, tzinfo=UTC),
        body_md="line 1\n\nline 2",
    )
    out = render_ticket_content(t, checklist=None, comments=[c], include_comments=True)
    assert "**2026-04-22 14:05, Bob:**" in out
    assert "> line 1\n>\n> line 2" in out


# ===================================================================
# golden file
# ===================================================================


def test_render_issue_tree_matches_golden() -> None:
    actual = render_issue_tree(_build_3level_tree())
    expected = (FIXTURES / "issue_tree_3level.md").read_text()
    assert actual == expected

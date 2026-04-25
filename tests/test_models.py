"""Unit tests for the frozen domain models and their invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from jira_context_mcp.models import (
    Checklist,
    ChecklistItem,
    ChecklistSection,
    Comment,
    Ticket,
    TreeNode,
)

# ---------- construction ----------


def test_ticket_constructs_with_required_fields() -> None:
    t = Ticket(
        key="A-1",
        summary="s",
        status="Open",
        issue_type="Story",
        assignee=None,
        description_md=None,
        parent_key=None,
        url="https://x/browse/A-1",
    )
    assert t.key == "A-1"
    assert t.parent_key is None


def test_checklist_item_default_status_open() -> None:
    item = ChecklistItem(name="x")
    assert item.status == "open"


def test_checklist_section_with_title_and_without() -> None:
    s1 = ChecklistSection(title="Section A", items=[ChecklistItem(name="x")])
    s2 = ChecklistSection(title=None, items=[])
    assert s1.title == "Section A"
    assert s2.title is None


def test_checklist_items_property_flattens_sections() -> None:
    cl = Checklist(
        sections=[
            ChecklistSection(title="A", items=[ChecklistItem(name="x"), ChecklistItem(name="y")]),
            ChecklistSection(title="B", items=[ChecklistItem(name="z")]),
        ]
    )
    assert [i.name for i in cl.items] == ["x", "y", "z"]


def test_checklist_with_no_sections_has_empty_items() -> None:
    cl = Checklist(sections=[])
    assert cl.items == []


def test_comment_with_tz_aware_created_constructs() -> None:
    c = Comment(
        author="bob",
        created=datetime(2026, 1, 1, tzinfo=UTC),
        body_md="body",
    )
    assert c.author == "bob"


# ---------- frozen ----------


def test_ticket_is_frozen() -> None:
    t = Ticket(
        key="A-1",
        summary="s",
        status="Open",
        issue_type="Story",
        assignee=None,
        description_md=None,
        parent_key=None,
        url="https://x/browse/A-1",
    )
    with pytest.raises(ValidationError):
        t.summary = "mutated"  # type: ignore[misc]


def test_checklist_is_frozen() -> None:
    cl = Checklist(sections=[])
    with pytest.raises(ValidationError):
        cl.sections = [ChecklistSection(title="x", items=[])]  # type: ignore[misc]


def test_tree_node_is_frozen(sample_ticket: Ticket) -> None:
    n = TreeNode(ticket=sample_ticket, children=[], is_focus=True)
    with pytest.raises(ValidationError):
        n.is_focus = False  # type: ignore[misc]


# ---------- validators ----------


def test_checklist_item_status_outside_literal_rejected() -> None:
    with pytest.raises(ValidationError):
        ChecklistItem(name="x", status="bogus")  # type: ignore[arg-type]


def test_comment_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        Comment(author="bob", created=datetime(2026, 1, 1), body_md="x")
    assert "timezone-aware" in str(exc.value)


def test_comment_with_non_utc_tz_accepted() -> None:
    tz = timezone(timedelta(hours=2))
    c = Comment(author="bob", created=datetime(2026, 1, 1, tzinfo=tz), body_md="x")
    assert c.created.tzinfo is not None


# ---------- TreeNode (recursive) ----------


def test_tree_node_minimal_construction(sample_ticket: Ticket) -> None:
    n = TreeNode(ticket=sample_ticket, children=[])
    assert n.ticket is sample_ticket
    assert n.children == []
    assert n.is_focus is False


def test_tree_node_nests_recursively(sample_ticket: Ticket) -> None:
    leaf = TreeNode(ticket=sample_ticket, children=[], is_focus=True)
    root = TreeNode(ticket=sample_ticket, children=[leaf])
    assert root.children[0] is leaf
    assert root.children[0].is_focus is True
    assert root.is_focus is False


def test_tree_node_is_focus_default_false(sample_ticket: Ticket) -> None:
    n = TreeNode(ticket=sample_ticket, children=[])
    assert n.is_focus is False

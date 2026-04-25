"""Unit tests for the frozen domain models and their invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest
from pydantic import ValidationError

from jira_context_mcp.models import (
    Checklist,
    ChecklistItem,
    ChecklistSection,
    Comment,
    Ticket,
    TicketContext,
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
        key="A-1", summary="s", status="Open", issue_type="Story",
        assignee=None, description_md=None, parent_key=None,
        url="https://x/browse/A-1",
    )
    with pytest.raises(ValidationError):
        t.summary = "mutated"  # type: ignore[misc]


def test_checklist_is_frozen() -> None:
    cl = Checklist(sections=[])
    with pytest.raises(ValidationError):
        cl.sections = [ChecklistSection(title="x", items=[])]  # type: ignore[misc]


def test_tree_node_is_frozen(sample_ticket: Ticket) -> None:
    n = TreeNode(
        ticket=sample_ticket, checklist=None, comments=[],
        children_of_parent=[], is_entry=True,
    )
    with pytest.raises(ValidationError):
        n.is_entry = False  # type: ignore[misc]


# ---------- validators ----------


def test_checklist_item_status_outside_literal_rejected() -> None:
    with pytest.raises(ValidationError):
        ChecklistItem(name="x", status="bogus")  # type: ignore[arg-type]


def test_comment_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        Comment(author="bob", created=datetime(2026, 1, 1), body_md="x")
    assert "timezone-aware" in str(exc.value)


def test_comment_with_non_utc_tz_accepted() -> None:
    from datetime import timedelta
    tz = timezone(timedelta(hours=2))
    c = Comment(author="bob", created=datetime(2026, 1, 1, tzinfo=tz), body_md="x")
    assert c.created.tzinfo is not None


# ---------- TicketContext invariants ----------


def _node(ticket: Ticket, *, is_entry: bool = False) -> TreeNode:
    return TreeNode(
        ticket=ticket, checklist=None, comments=[],
        children_of_parent=[], is_entry=is_entry,
    )


def test_ticket_context_empty_path_rejected(sample_ticket: Ticket) -> None:
    with pytest.raises(ValidationError) as exc:
        TicketContext(path=[], entry_key=sample_ticket.key)
    assert "must not be empty" in str(exc.value)


def test_ticket_context_two_entry_flags_rejected(sample_ticket: Ticket) -> None:
    n1 = _node(sample_ticket, is_entry=True)
    n2 = _node(sample_ticket, is_entry=True)
    with pytest.raises(ValidationError) as exc:
        TicketContext(path=[n1, n2], entry_key=sample_ticket.key)
    assert "exactly one TreeNode" in str(exc.value)


def test_ticket_context_entry_not_last_rejected() -> None:
    a = Ticket(
        key="A-1", summary="a", status="Open", issue_type="Story", assignee=None,
        description_md=None, parent_key=None, url="https://x/browse/A-1",
    )
    b = Ticket(
        key="B-1", summary="b", status="Open", issue_type="Story", assignee=None,
        description_md=None, parent_key="A-1", url="https://x/browse/B-1",
    )
    # entry flag on first instead of last
    n1 = _node(a, is_entry=True)
    n2 = _node(b, is_entry=False)
    with pytest.raises(ValidationError) as exc:
        TicketContext(path=[n1, n2], entry_key="A-1")
    assert "must be the last element" in str(exc.value)


def test_ticket_context_entry_key_mismatch_rejected(sample_ticket: Ticket) -> None:
    n = _node(sample_ticket, is_entry=True)
    with pytest.raises(ValidationError) as exc:
        TicketContext(path=[n], entry_key="OTHER-9")
    assert "entry_key" in str(exc.value).lower()


def test_ticket_context_entry_node_returns_last(sample_ticket: Ticket) -> None:
    n = _node(sample_ticket, is_entry=True)
    ctx = TicketContext(path=[n], entry_key=sample_ticket.key)
    assert ctx.entry_node is n


def test_ticket_context_truncated_defaults_false(sample_ticket: Ticket) -> None:
    n = _node(sample_ticket, is_entry=True)
    ctx = TicketContext(path=[n], entry_key=sample_ticket.key)
    assert ctx.truncated is False


def test_ticket_context_truncated_can_be_set(sample_ticket: Ticket) -> None:
    n = _node(sample_ticket, is_entry=True)
    ctx = TicketContext(path=[n], entry_key=sample_ticket.key, truncated=True)
    assert ctx.truncated is True

"""Tests for the orchestration in context.py with a mocked JiraClient."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import pytest

from jira_context_mcp.context import build_ticket_context
from jira_context_mcp.jira import JiraNotFoundError
from jira_context_mcp.models import Checklist, Comment, Ticket


def make_ticket(key: str, *, parent_key: str | None = None) -> Ticket:
    return Ticket(
        key=key,
        summary=f"summary {key}",
        status="Open",
        issue_type="Story",
        assignee=None,
        description_md=None,
        parent_key=parent_key,
        url=f"https://example.atlassian.net/browse/{key}",
    )


class FakeClient:
    """In-memory stand-in for JiraClient that records calls.

    Avoids both respx and the real httpx surface entirely so we can exercise
    the orchestration logic on its own without coupling to HTTP details.
    """

    def __init__(
        self,
        tickets: dict[str, Ticket] | None = None,
        children: dict[str, list[Ticket]] | None = None,
        comments: dict[str, list[Comment]] | None = None,
        checklists: dict[str, Checklist | None] | None = None,
    ) -> None:
        self._tickets = tickets or {}
        self._children = children or {}
        self._comments = comments or {}
        self._checklists = checklists or {}
        self.get_ticket_calls: list[str] = []
        self.get_checklist_calls: list[str] = []
        self.get_comments_calls: list[str] = []
        self.get_children_calls: list[Sequence[str]] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get_ticket(self, key: str) -> Ticket:
        self.get_ticket_calls.append(key)
        if key not in self._tickets:
            raise JiraNotFoundError(f"not found: {key}", key=key)
        return self._tickets[key]

    async def get_checklist(self, key: str) -> Checklist | None:
        self.get_checklist_calls.append(key)
        return self._checklists.get(key, Checklist(sections=[]))

    async def get_comments(self, key: str) -> list[Comment]:
        self.get_comments_calls.append(key)
        return self._comments.get(key, [])

    async def get_children_of(self, parent_keys: list[str]) -> dict[str, list[Ticket]]:
        self.get_children_calls.append(tuple(parent_keys))
        return {k: self._children.get(k, []) for k in parent_keys}


# ===================================================================
# happy paths
# ===================================================================


async def test_3level_walk_assembles_ordered_path() -> None:
    root = make_ticket("ROOT", parent_key=None)
    middle = make_ticket("MID", parent_key="ROOT")
    entry = make_ticket("ENT", parent_key="MID")
    fake = FakeClient(
        tickets={"ROOT": root, "MID": middle, "ENT": entry},
        children={"ROOT": [middle], "MID": [entry]},
    )
    ctx = await build_ticket_context(fake, "ENT")  # type: ignore[arg-type]
    assert [n.ticket.key for n in ctx.path] == ["ROOT", "MID", "ENT"]
    assert ctx.entry_key == "ENT"
    assert ctx.entry_node.is_entry is True
    assert ctx.truncated is False


async def test_single_node_entry_when_parent_key_is_none() -> None:
    root = make_ticket("EPIC-7", parent_key=None)
    fake = FakeClient(tickets={"EPIC-7": root})
    ctx = await build_ticket_context(fake, "EPIC-7")  # type: ignore[arg-type]
    assert [n.ticket.key for n in ctx.path] == ["EPIC-7"]
    assert ctx.path[0].is_entry is True
    assert ctx.truncated is False


# ===================================================================
# guards
# ===================================================================


async def test_max_depth_zero_rejected() -> None:
    fake = FakeClient(tickets={"ENT": make_ticket("ENT")})
    with pytest.raises(ValueError, match="max_depth"):
        await build_ticket_context(fake, "ENT", max_depth=0)  # type: ignore[arg-type]


async def test_max_depth_truncation_sets_flag_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 5-level chain: A -> B -> C -> D -> E (E is the root)
    chain = ["A", "B", "C", "D", "E"]
    tickets = {
        chain[i]: make_ticket(chain[i], parent_key=chain[i + 1] if i + 1 < len(chain) else None)
        for i in range(len(chain))
    }
    fake = FakeClient(tickets=tickets)
    with caplog.at_level(logging.WARNING, logger="jira_context_mcp.context"):
        ctx = await build_ticket_context(fake, "A", max_depth=2)  # type: ignore[arg-type]
    assert [n.ticket.key for n in ctx.path] == ["B", "A"]
    assert ctx.truncated is True
    assert any("max_depth=2" in r.message for r in caplog.records)


async def test_cycle_detection_raises_value_error() -> None:
    a = make_ticket("A", parent_key="B")
    b = make_ticket("B", parent_key="A")
    fake = FakeClient(tickets={"A": a, "B": b})
    with pytest.raises(ValueError, match="cycle"):
        await build_ticket_context(fake, "A")  # type: ignore[arg-type]


# ===================================================================
# include_comments behaviour
# ===================================================================


async def test_include_comments_true_calls_get_comments_per_ticket() -> None:
    root = make_ticket("ROOT", parent_key=None)
    entry = make_ticket("ENT", parent_key="ROOT")
    fake = FakeClient(
        tickets={"ROOT": root, "ENT": entry},
        children={"ROOT": [entry]},
    )
    await build_ticket_context(fake, "ENT", include_comments=True)  # type: ignore[arg-type]
    assert sorted(fake.get_comments_calls) == ["ENT", "ROOT"]


async def test_include_comments_false_skips_get_comments() -> None:
    root = make_ticket("ROOT", parent_key=None)
    entry = make_ticket("ENT", parent_key="ROOT")
    fake = FakeClient(
        tickets={"ROOT": root, "ENT": entry},
        children={"ROOT": [entry]},
    )
    await build_ticket_context(fake, "ENT", include_comments=False)  # type: ignore[arg-type]
    assert fake.get_comments_calls == []


# ===================================================================
# children fetch + semantics
# ===================================================================


async def test_children_of_parent_includes_self() -> None:
    """After the recent semantics change the per-node list keeps the entry
    ticket itself; we no longer filter it out."""
    root = make_ticket("ROOT", parent_key=None)
    entry = make_ticket("ENT", parent_key="ROOT")
    sibling = make_ticket("SIB", parent_key="ROOT")
    # JQL response for parent ROOT contains both entry and sibling
    fake = FakeClient(
        tickets={"ROOT": root, "ENT": entry},
        children={"ROOT": [entry, sibling]},
    )
    ctx = await build_ticket_context(fake, "ENT")  # type: ignore[arg-type]
    keys = [t.key for t in ctx.path[-1].children_of_parent]
    assert keys == ["ENT", "SIB"]


async def test_get_children_of_called_once_with_unique_parents() -> None:
    """Phase 2 should batch the JQL into a single call."""
    root = make_ticket("ROOT", parent_key=None)
    middle = make_ticket("MID", parent_key="ROOT")
    entry = make_ticket("ENT", parent_key="MID")
    fake = FakeClient(
        tickets={"ROOT": root, "MID": middle, "ENT": entry},
        children={"ROOT": [middle], "MID": [entry]},
    )
    await build_ticket_context(fake, "ENT")  # type: ignore[arg-type]
    assert len(fake.get_children_calls) == 1
    assert sorted(fake.get_children_calls[0]) == ["MID", "ROOT"]


# ===================================================================
# error propagation through TaskGroup
# ===================================================================


class FailingChecklistClient(FakeClient):
    """Variant that blows up specifically inside Phase 2's checklist task."""

    async def get_checklist(self, key: str) -> Checklist | None:
        raise RuntimeError(f"boom on {key}")


async def test_taskgroup_propagates_exception_as_exception_group() -> None:
    root = make_ticket("ROOT", parent_key=None)
    entry = make_ticket("ENT", parent_key="ROOT")
    fake = FailingChecklistClient(
        tickets={"ROOT": root, "ENT": entry},
        children={"ROOT": [entry]},
    )
    with pytest.raises(BaseExceptionGroup) as exc:
        await build_ticket_context(fake, "ENT")  # type: ignore[arg-type]
    # all sub-exceptions are the RuntimeError we raised in the fake
    matched, unmatched = exc.value.split(RuntimeError)
    assert matched is not None
    assert unmatched is None

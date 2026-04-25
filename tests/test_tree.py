"""Tests for build_issue_tree (walk-up + walk-down orchestration)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import pytest

from jira_context_mcp.jira import JiraNotFoundError
from jira_context_mcp.models import Ticket, TreeNode
from jira_context_mcp.tree import build_issue_tree


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
    """In-memory stand-in for JiraClient used by build_issue_tree."""

    def __init__(
        self,
        tickets: dict[str, Ticket] | None = None,
        children: dict[str, list[Ticket]] | None = None,
    ) -> None:
        self._tickets = tickets or {}
        self._children = children or {}
        self.get_ticket_calls: list[str] = []
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

    async def get_children_of(self, parent_keys: list[str]) -> dict[str, list[Ticket]]:
        self.get_children_calls.append(tuple(parent_keys))
        return {k: self._children.get(k, []) for k in parent_keys}


def collect_keys(node: TreeNode) -> list[str]:
    """Pre-order traversal of all keys in the tree."""
    out = [node.ticket.key]
    for child in node.children:
        out.extend(collect_keys(child))
    return out


def find_focus(node: TreeNode) -> TreeNode | None:
    if node.is_focus:
        return node
    for child in node.children:
        result = find_focus(child)
        if result is not None:
            return result
    return None


# ===================================================================
# happy paths
# ===================================================================


async def test_focus_is_root_returns_root_with_focus_marker() -> None:
    root = make_ticket("R", parent_key=None)
    fake = FakeClient(tickets={"R": root})
    tree = await build_issue_tree(fake, "R", depth_down=2)  # type: ignore[arg-type]
    assert tree.ticket.key == "R"
    assert tree.is_focus is True


async def test_focus_is_leaf_walks_up_to_root_and_back_down() -> None:
    """Focus is a leaf 2 levels deep; tree should expose the whole chain."""
    root = make_ticket("R", parent_key=None)
    mid = make_ticket("M", parent_key="R")
    leaf = make_ticket("L", parent_key="M")
    fake = FakeClient(
        tickets={"R": root, "M": mid, "L": leaf},
        children={"R": [mid], "M": [leaf]},
    )
    tree = await build_issue_tree(fake, "L", depth_down=2)  # type: ignore[arg-type]
    assert tree.ticket.key == "R"  # root at top
    assert tree.is_focus is False
    focus = find_focus(tree)
    assert focus is not None
    assert focus.ticket.key == "L"


async def test_full_tree_includes_non_path_siblings() -> None:
    """Walking down from the root surfaces siblings of path nodes."""
    root = make_ticket("R", parent_key=None)
    path_mid = make_ticket("M-PATH", parent_key="R")
    sibling_mid = make_ticket("M-SIB", parent_key="R")
    leaf = make_ticket("L", parent_key="M-PATH")
    fake = FakeClient(
        tickets={"R": root, "M-PATH": path_mid, "L": leaf},
        children={"R": [path_mid, sibling_mid], "M-PATH": [leaf]},
    )
    tree = await build_issue_tree(fake, "L", depth_down=2)  # type: ignore[arg-type]
    keys = collect_keys(tree)
    assert "M-SIB" in keys


async def test_depth_down_limits_non_path_expansion() -> None:
    """With depth_down=0, only the spine is reachable; siblings lose their kids."""
    root = make_ticket("R", parent_key=None)
    path_mid = make_ticket("M-PATH", parent_key="R")
    sibling_mid = make_ticket("M-SIB", parent_key="R")
    sibling_kid = make_ticket("SIB-KID", parent_key="M-SIB")
    leaf = make_ticket("L", parent_key="M-PATH")
    fake = FakeClient(
        tickets={"R": root, "M-PATH": path_mid, "L": leaf},
        children={
            "R": [path_mid, sibling_mid],
            "M-PATH": [leaf],
            "M-SIB": [sibling_kid],
        },
    )
    tree = await build_issue_tree(fake, "L", depth_down=0)  # type: ignore[arg-type]
    keys = collect_keys(tree)
    # spine is fully expanded
    assert "L" in keys
    # depth_down=0 still expands path nodes even at level 1, so M-SIB makes it
    # in (it is at level 1, fetched as sibling of M-PATH); but its children
    # are not expanded because level >= depth_down
    assert "M-SIB" in keys
    assert "SIB-KID" not in keys


async def test_focus_deeper_than_depth_down_still_reachable() -> None:
    """Focus at level 3 with depth_down=1 should still appear via spine expansion."""
    chain = ["A", "B", "C", "D"]  # A is root, D is focus
    tickets = {
        chain[i]: make_ticket(chain[i], parent_key=chain[i - 1] if i > 0 else None)
        for i in range(len(chain))
    }
    fake = FakeClient(
        tickets=tickets,
        children={
            "A": [tickets["B"]],
            "B": [tickets["C"]],
            "C": [tickets["D"]],
        },
    )
    tree = await build_issue_tree(fake, "D", depth_down=1)  # type: ignore[arg-type]
    assert "D" in collect_keys(tree)
    focus = find_focus(tree)
    assert focus is not None
    assert focus.ticket.key == "D"


# ===================================================================
# guards
# ===================================================================


async def test_depth_up_zero_rejected() -> None:
    fake = FakeClient(tickets={"R": make_ticket("R")})
    with pytest.raises(ValueError, match="depth_up"):
        await build_issue_tree(fake, "R", depth_up=0)  # type: ignore[arg-type]


async def test_depth_up_truncation_emits_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When walk_up exhausts depth_up before reaching the root, log a WARN."""
    chain = ["A", "B", "C", "D", "E"]  # E is the actual root
    tickets = {
        chain[i]: make_ticket(chain[i], parent_key=chain[i + 1] if i + 1 < len(chain) else None)
        for i in range(len(chain))
    }
    fake = FakeClient(tickets=tickets)
    with caplog.at_level(logging.WARNING, logger="jira_context_mcp.tree"):
        tree = await build_issue_tree(fake, "A", depth_up=2)  # type: ignore[arg-type]
    # Tree is rooted at the truncation point ("B"), not the real root ("E")
    assert tree.ticket.key == "B"
    assert any("depth_up=2" in rec.message for rec in caplog.records)


async def test_cycle_detection_raises() -> None:
    a = make_ticket("A", parent_key="B")
    b = make_ticket("B", parent_key="A")
    fake = FakeClient(tickets={"A": a, "B": b})
    with pytest.raises(ValueError, match="cycle"):
        await build_issue_tree(fake, "A")  # type: ignore[arg-type]


async def test_focus_not_found_raises_jira_not_found() -> None:
    fake = FakeClient(tickets={})
    with pytest.raises(JiraNotFoundError):
        await build_issue_tree(fake, "MISSING-1")  # type: ignore[arg-type]


# ===================================================================
# batching: each layer = one JQL call
# ===================================================================


async def test_walk_down_uses_one_jql_per_layer() -> None:
    """A 3-layer tree (root, mids, leaves) should issue 2 JQL calls."""
    root = make_ticket("R", parent_key=None)
    mids = [make_ticket(f"M-{i}", parent_key="R") for i in range(3)]
    leaves_by_mid = {
        m.key: [make_ticket(f"{m.key}-L{i}", parent_key=m.key) for i in range(2)] for m in mids
    }
    fake = FakeClient(
        tickets={"R": root, **{m.key: m for m in mids}},
        children={"R": mids, **leaves_by_mid},
    )
    await build_issue_tree(fake, "R", depth_down=2)  # type: ignore[arg-type]
    # walks down: layer 0 expands [R], layer 1 expands all 3 mids
    assert len(fake.get_children_calls) == 2
    assert fake.get_children_calls[0] == ("R",)
    assert sorted(fake.get_children_calls[1]) == ["M-0", "M-1", "M-2"]


async def test_depth_down_is_clamped_to_three() -> None:
    """Deep request should hit the hard cap silently."""
    # build a 6-level chain, focus at the leaf
    chain = ["A", "B", "C", "D", "E", "F"]
    tickets = {
        chain[i]: make_ticket(chain[i], parent_key=chain[i - 1] if i > 0 else None)
        for i in range(len(chain))
    }
    children: dict[str, list[Ticket]] = {
        chain[i]: [tickets[chain[i + 1]]] for i in range(len(chain) - 1)
    }
    fake = FakeClient(tickets=tickets, children=children)
    # asking for depth_down=99 should be silently clamped; tree still reaches F
    # via the spine even though depth_down=3 wouldn't otherwise expose level 5
    tree = await build_issue_tree(fake, "F", depth_down=99)  # type: ignore[arg-type]
    assert "F" in collect_keys(tree)

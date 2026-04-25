"""Domain models for Jira tickets, comments, checklists, and traversal context.

All models are frozen pydantic ``BaseModel`` subclasses — instances are DTOs
produced by the Jira client and the context walker, then handed off to the
markdown renderer. Mutation after construction is a bug and is rejected by
pydantic at assignment time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

ChecklistStatus = Literal["open", "done", "in_progress", "skipped"]
"""Canonical Smart Checklist item states. The parser in ``jira`` maps any
upstream value outside this set to ``"open"`` before constructing the model."""


class _Frozen(BaseModel):
    """Immutable base for all DTOs in this module."""

    model_config = ConfigDict(frozen=True)


class ChecklistItem(_Frozen):
    """A single Smart Checklist entry."""

    name: str
    status: ChecklistStatus = "open"


class ChecklistSection(_Frozen):
    """A group of items under a single Smart Checklist header.

    ``title`` is the markdown header text minus the leading ``#`` characters,
    or ``None`` for items appearing before the first header (rare — modern
    Smart Checklist always starts with a section header).
    """

    title: str | None
    items: list[ChecklistItem]


class Checklist(_Frozen):
    """Smart Checklist attached to a Jira issue.

    Stored as an ordered list of sections preserving the grouping that
    Smart Checklist exposes in the Jira UI. The flat ``items`` accessor is
    kept as a property for callers that need the full list without caring
    about sections (e.g. counting, the simple "no items" check).
    """

    sections: list[ChecklistSection]

    @property
    def items(self) -> list[ChecklistItem]:
        return [item for section in self.sections for item in section.items]


class Comment(_Frozen):
    """A Jira comment with its body already converted from ADF to markdown."""

    author: str
    created: datetime
    body_md: str

    @field_validator("created")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Comment.created must be timezone-aware")
        return value


class Ticket(_Frozen):
    """A single Jira issue, with fields sufficient to render its context.

    ``description_md`` is the markdown form of the ADF body (``None`` when the
    Jira field is empty). ``parent_key`` drives the root-ward traversal, and
    ``url`` is precomputed to keep the renderer concatenation-free.
    """

    key: str
    summary: str
    status: str
    issue_type: str
    assignee: str | None
    description_md: str | None
    parent_key: str | None
    url: str


class TreeNode(_Frozen):
    """One node on the root → entry path produced by the context walker.

    ``children_of_parent`` lists every issue that shares this node's parent,
    preserved in the order Jira returned from the JQL search (which roughly
    matches rank / the order a user sees in the Jira UI). It **includes**
    ``ticket`` itself when the node has a parent; readers tell self apart by
    comparing ``ticket.key``. For a root node (``parent_key is None``) the
    list is empty — the parent of the traversal root is outside the fetched
    context.

    ``comments`` is empty when the caller requested ``include_comments=False``,
    and ``checklist`` is ``None`` when the ticket has no Smart Checklist
    property (or the plugin is absent).
    """

    ticket: Ticket
    checklist: Checklist | None
    comments: list[Comment]
    children_of_parent: list[Ticket]
    is_entry: bool


class TicketContext(_Frozen):
    """Full traversal result handed from ``context`` to ``markdown``.

    ``path`` is ordered from the root ancestor down to the entry ticket, which
    is always the last element. Exactly one node in the path has
    ``is_entry=True`` and it is that last element; invariants are checked on
    construction. ``truncated`` is ``True`` when the upward walk hit
    ``max_depth`` before reaching the root — the renderer surfaces this so
    the LLM knows the hierarchy is incomplete.
    """

    path: list[TreeNode]
    entry_key: str
    truncated: bool = False

    @property
    def entry_node(self) -> TreeNode:
        return self.path[-1]

    @model_validator(mode="after")
    def _check_path_invariants(self) -> TicketContext:
        if not self.path:
            raise ValueError("TicketContext.path must not be empty")
        entry_indices = [i for i, node in enumerate(self.path) if node.is_entry]
        last_index = len(self.path) - 1
        if entry_indices != [last_index]:
            raise ValueError(
                "exactly one TreeNode must have is_entry=True and it must be the "
                f"last element; got is_entry at indices {entry_indices} "
                f"in path of length {len(self.path)}"
            )
        tail_key = self.path[-1].ticket.key
        if tail_key != self.entry_key:
            raise ValueError(
                f"entry_key {self.entry_key!r} does not match "
                f"path[-1].ticket.key {tail_key!r}"
            )
        return self

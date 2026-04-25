"""Domain models for Jira tickets, comments, checklists, and the issue tree.

All models are frozen pydantic ``BaseModel`` subclasses — instances are DTOs
produced by the Jira client and the tree walker, then handed off to the
markdown renderer. Mutation after construction is a bug and is rejected by
pydantic at assignment time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

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
    """A single Jira issue with the fields needed by every renderer.

    ``description_md`` is the markdown form of the ADF body (``None`` when
    the Jira field is empty). ``parent_key`` drives the root-ward traversal
    of :class:`build_issue_tree`; ``url`` is precomputed so renderers stay
    concatenation-free.
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
    """A node in the issue tree produced by :func:`build_issue_tree`.

    The tree is rooted at the topmost ancestor reachable from the focus
    ticket; ``children`` recursively contain the descendants that the
    walker decided to expand (everything up to ``depth_down`` levels, plus
    the path leading to the focus ticket whatever its actual depth).

    ``is_focus`` flags the ticket the user originally asked about. Exactly
    one node in any tree has ``is_focus=True``; the renderer uses it to
    place the 🎯 / ⬅️ FOCUS markers regardless of where the focus sits in
    the hierarchy.
    """

    ticket: Ticket
    children: list[TreeNode]
    is_focus: bool = False

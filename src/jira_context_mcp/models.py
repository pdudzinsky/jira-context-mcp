"""Domain models for Jira tickets, comments, checklists, and the issue tree.

All models are frozen pydantic ``BaseModel`` subclasses — instances are DTOs
produced by the Jira client and the tree walker, then handed off to the
markdown renderer. Mutation after construction is a bug and is rejected by
pydantic at assignment time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

ChecklistStatus = Literal["open", "done", "in_progress", "skipped"]
"""Smart Checklist's default statuses: To Do, Done, In Progress and Skipped.

The plugin writes them inline, as the line markers ``-``, ``+``, ``~`` and ``x``.
Custom statuses are not in this set: see :class:`ChecklistItem`.
"""


class _Frozen(BaseModel):
    """Immutable base for all DTOs in this module.

    A rejected value stays out of the validation message, which ``_reading_body`` logs.
    """

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)


class ChecklistItem(_Frozen):
    """A checklist line that carries a status.

    ``name`` is the text after the marker as written, so inline markdown and
    the ``[priority="…"]`` / ``[dueDate="…"]`` / ``[assignee="…"]`` attributes
    stay in it.

    ``status`` reflects the line marker alone. ``custom_status`` is the
    ``[name]`` right after it (``- [FAILED] text``). Jira resolves that name
    against the statuses configured on the instance, which the markdown does
    not show, so it can't say whether the status counts as checked, or whether
    the brackets are a status at all rather than plain text (``- [FE] text``).

    ``details`` are the ``>`` lines below the item, minus that first ``>``.
    """

    name: str
    status: ChecklistStatus = "open"
    custom_status: str | None = None
    mandatory: bool = False
    details: list[str] = Field(default_factory=list)


class ChecklistText(_Frozen):
    """A checklist line that is not an item, kept verbatim where it was found.

    Jira doesn't count these lines as items, so they have no status. They hold
    real content, such as sub-headings written in bold or notes between items.
    """

    text: str


class ChecklistSection(_Frozen):
    """What sits under one Smart Checklist header, in document order.

    ``title`` is the header text minus the leading ``#`` characters. It is
    ``None`` for content before the first header, which is common because a
    checklist doesn't need headers at all, and for a header with no text.
    ``level`` is the number of those characters.
    Nesting is expressed by level alone: a parent header with only a nested
    header below it is a section with no entries of its own.

    ``entries`` also accepts ``items=`` so that a section holding nothing but
    items reads naturally at the call site.
    """

    title: str | None
    level: int = Field(default=1, ge=1, le=6)
    entries: list[ChecklistItem | ChecklistText] = Field(
        default_factory=list,
        validation_alias=AliasChoices("entries", "items"),
    )

    @property
    def items(self) -> list[ChecklistItem]:
        return [entry for entry in self.entries if isinstance(entry, ChecklistItem)]


class Checklist(_Frozen):
    """Smart Checklist attached to a Jira issue.

    Stored as an ordered list of sections preserving the grouping that
    Smart Checklist exposes in the Jira UI. The flat ``items`` accessor is
    kept as a property for callers that need the full list without caring
    about sections (e.g. counting).
    """

    sections: list[ChecklistSection]

    @property
    def items(self) -> list[ChecklistItem]:
        return [item for section in self.sections for item in section.items]

    @property
    def is_empty(self) -> bool:
        """``True`` when there is nothing to show: no items, text or titled header."""
        return not any(section.title or section.entries for section in self.sections)


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


class Attachment(_Frozen):
    """A file attached to a Jira issue.

    ``content_url`` is the authenticated download URL Jira returns in the
    ``attachment.content`` field. Fetching it requires the same BasicAuth
    used by :class:`JiraClient`, so it is not a public link: the renderer
    shows it as plain text at the end of the attachment line, for readers
    with Jira access. It is never empty, because ``download_attachment``
    would resolve an empty URL to the Jira home page and send credentials.

    ``embedded`` is ``True`` when this file is referenced by a media node
    inside the surrounding ticket's description (i.e. someone pasted it
    into the body). When ``False`` the file is "orphan" — uploaded but
    not mentioned anywhere in the prose. The renderer surfaces this so
    the LLM can prioritise: embedded files are part of the narrative,
    orphans are usually post-review revisions or supplementary material.
    """

    id: str
    filename: str
    mime_type: str
    size: int
    created: datetime
    author: str
    content_url: str = Field(min_length=1)
    embedded: bool = False

    @field_validator("created")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Attachment.created must be timezone-aware")
        return value


class Ticket(_Frozen):
    """A single Jira issue with the fields needed by every renderer.

    ``description_md`` is the markdown form of the ADF body (``None`` when
    the Jira field is empty). ``parent_key`` drives the root-ward traversal
    of :class:`build_issue_tree`; ``url`` is precomputed so renderers stay
    concatenation-free. ``attachments`` is a tuple (immutable, frozen-safe)
    that defaults to empty so existing call sites stay source-compatible.
    """

    key: str
    summary: str
    status: str
    issue_type: str
    assignee: str | None
    description_md: str | None
    parent_key: str | None
    url: str
    attachments: tuple[Attachment, ...] = ()


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

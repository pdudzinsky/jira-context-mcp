"""FastMCP server exposing four composable Jira tools.

- :func:`get_issue_tree` — structural overview of the hierarchy around a
  ticket (lite info per ticket, focus marker, root → leaves layout).
- :func:`get_ticket_content` — full content (description + Smart Checklist
  + attachment list + optional comments) for a single ticket, no hierarchy.
- :func:`get_smart_checklist` — just the Smart Checklist of one ticket,
  the leanest option when only ACCs are needed.
- :func:`get_ticket_attachment` — fetch the binary content of a single
  attachment from a ticket as a native MCP image / file / text payload.

A typical workflow has the LLM call :func:`get_issue_tree` first to discover
structure, then :func:`get_ticket_content` on the path nodes that interest
it (focus + ancestors usually carry the actionable detail). Attachments are
listed there with their ids; the model decides which (if any) to pull via
:func:`get_ticket_attachment`. The four tools are designed to be composable
— each does one thing and returns content the LLM can quote or further
explore.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP
from fastmcp.utilities.types import File, Image
from pydantic import ValidationError

from .config import get_settings
from .jira import (
    JiraAttachmentTooLargeError,
    JiraAuthError,
    JiraClient,
    JiraError,
    JiraNotFoundError,
    JiraRateLimitError,
)
from .markdown import (
    render_checklist,
    render_issue_tree,
    render_ticket_content,
)
from .tree import build_issue_tree

# Format string per image mime type — FastMCP's ``Image`` helper prepends
# ``"image/"`` to whatever ``format`` value we pass, so the value must be
# the mime's exact subtype (e.g. ``"svg+xml"`` -> ``"image/svg+xml"``, not
# ``"svg"`` which would produce the non-canonical ``"image/svg"`` that
# MCP clients won't render). Mimes not on this list fall back to the
# subtype after the slash, which is correct for ``image/avif``,
# ``image/heic`` and similar single-token IANA mimes.
_IMAGE_FORMATS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg+xml",
}

mcp: FastMCP = FastMCP("jira-context-mcp")


# ---------------------------------------------------------------------------
# Tool 1: get_issue_tree
# ---------------------------------------------------------------------------


@mcp.tool
async def get_issue_tree(
    issue_key: str,
    depth_up: int = 10,
    depth_down: int = 2,
) -> str:
    """Return the hierarchy tree around a Jira ticket as structured markdown.

    Walks upward from ``issue_key`` to the topmost ancestor reachable within
    ``depth_up``, then walks downward from that root expanding every node up
    to ``depth_down`` levels (with the focus ticket and its direct ancestors
    always reachable, even if the focus sits deeper). Each ticket in the
    tree is rendered with key, type, summary, status — no descriptions, no
    checklists, no comments.

    Use this tool when:
    - You want a quick overview of an epic, initiative, or large story
    - You need to see what's around a ticket before deciding which siblings
      or descendants to explore
    - You're scanning sprint progress or release scope
    - You want the structural map first, then full content of selected
      tickets via ``get_ticket_content``

    Args:
        issue_key: Jira issue key, e.g. ``"PROJ-1234"``. Can be a leaf, a
            mid-tier story, or a top-level epic — the output always shows
            the full reachable hierarchy with the focus marker on this key.
        depth_up: Max levels to walk upward toward the root. Default 10
            covers typical hierarchies (Epic → Story → Subtask is 3).
        depth_down: Max levels to expand below the root. Default 2 is a
            good balance for epic overviews. Hard-capped at 3 to prevent
            runaway expansion on epics with hundreds of descendants.

    Returns:
        Markdown with an Overview aggregate (counts by type and status)
        and a fenced ASCII tree, or an ``Error: ...`` line on failure.
    """
    try:
        async with JiraClient.from_settings(get_settings()) as client:
            tree = await build_issue_tree(
                client,
                issue_key,
                depth_up=depth_up,
                depth_down=depth_down,
            )
        return render_issue_tree(tree)
    except* (JiraError, ValidationError, ValueError) as eg:
        error = _format_jira_errors(eg)
    return error


# ---------------------------------------------------------------------------
# Tool 2: get_ticket_content
# ---------------------------------------------------------------------------


@mcp.tool
async def get_ticket_content(
    issue_key: str,
    include_comments: bool = False,
) -> str:
    """Return the full content of a single Jira ticket as markdown.

    Fetches description (ADF → markdown), Smart Checklist (if present), and
    optionally the comments thread. Does **not** walk the parent or child
    hierarchy — pair this with ``get_issue_tree`` when context from siblings
    or ancestors matters.

    Use this tool when:
    - You already know which ticket you want full detail on
    - You're following up after ``get_issue_tree`` to inspect a specific
      ancestor or sibling that carried actual ACCs
    - The user asks for one ticket's description, ACCs, or comment history

    Args:
        issue_key: Jira issue key, e.g. ``"PROJ-1234"``.
        include_comments: When True, fetches and renders the comments
            thread (capped at 100 — see CHANGELOG). Default False because
            comments are noisy and token-heavy.

    Returns:
        Markdown with ``# KEY · [Type] Summary``, status/assignee/URL line,
        ``## Description``, optional ``## Smart Checklist``, optional
        ``## Comments``. ``Error: ...`` line on failure.
    """
    try:
        async with (
            JiraClient.from_settings(get_settings()) as client,
            asyncio.TaskGroup() as tg,
        ):
            ticket_task = tg.create_task(client.get_ticket(issue_key))
            checklist_task = tg.create_task(client.get_checklist(issue_key))
            comments_task = (
                tg.create_task(client.get_comments(issue_key)) if include_comments else None
            )
        ticket = ticket_task.result()
        checklist = checklist_task.result()
        comments = comments_task.result() if comments_task else []
        return render_ticket_content(
            ticket,
            checklist=checklist,
            comments=comments,
            include_comments=include_comments,
        )
    except* (JiraError, ValidationError) as eg:
        error = _format_jira_errors(eg)
    return error


# ---------------------------------------------------------------------------
# Tool 3: get_smart_checklist
# ---------------------------------------------------------------------------


@mcp.tool
async def get_smart_checklist(issue_key: str) -> str:
    """Return only the Smart Checklist (Acceptance Criteria) of one ticket.

    The Railsware Smart Checklist plugin stores ACCs in a Jira issue
    property (``com.railsware.SmartChecklist.checklist``) that the standard
    Jira API endpoint does not expose. Many Atlassian Cloud teams use it as
    the canonical location for ACCs/DoD, leaving the description minimal.

    Use this tool when:
    - You only need the acceptance criteria of one ticket, no other context
    - You want a token-efficient alternative to ``get_ticket_content`` for
      ACC review

    Args:
        issue_key: Jira issue key, e.g. ``"PROJ-1234"``.

    Returns:
        Markdown task list with section headers, an explanatory message if
        the plugin/property is absent or empty, or an ``Error: ...`` line.
    """
    try:
        async with JiraClient.from_settings(get_settings()) as client:
            checklist = await client.get_checklist(issue_key)
        if checklist is None:
            return (
                f"Smart Checklist on {issue_key}: not present "
                "(plugin not installed, or this ticket doesn't use it)."
            )
        if not checklist.items:
            return f"Smart Checklist on {issue_key}: empty (plugin active but no items recorded)."
        total = len(checklist.items)
        done = sum(1 for item in checklist.items if item.status == "done")
        count = f"{total} item{'s' if total != 1 else ''}" if done == 0 else f"{done}/{total} done"
        return (
            f"# Smart Checklist: {issue_key} ({count})\n\n"
            f"{render_checklist(checklist, heading_level=2)}\n"
        )
    except* (JiraError, ValidationError) as eg:
        error = _format_jira_errors(eg)
    return error


# ---------------------------------------------------------------------------
# Tool 4: get_ticket_attachment
# ---------------------------------------------------------------------------


@mcp.tool
async def get_ticket_attachment(
    issue_key: str,
    attachment_id: str,
) -> Image | File | str:
    """Fetch the binary content of a single attachment as a native MCP payload.

    First lists the ticket's attachments to find ``attachment_id`` and learn
    its mime type and size, then downloads the bytes (subject to the
    ``JIRA_ATTACHMENT_MAX_MB`` cap) and returns them in a format the LLM can
    natively consume:

    - ``image/*`` → :class:`fastmcp.utilities.types.Image` (rendered as an
      MCP ``ImageContent`` block — Claude reads it as an image).
    - ``application/pdf`` → :class:`fastmcp.utilities.types.File` (embedded
      resource — Claude reads it as a PDF document).
    - ``text/*`` → a markdown string with a small header so the file is
      clearly delimited from any surrounding ticket prose.
    - Anything else → an ``Error: unsupported mime …`` string pointing at
      the original ``content_url`` for manual download.

    Use this tool when:
    - ``get_ticket_content`` revealed an attachment (mockup, PDF spec,
      log file) that's needed to fully understand the ticket.
    - The model is reviewing a design / requirements doc that was uploaded
      to the issue rather than written in the description.

    Args:
        issue_key: Jira issue key the attachment belongs to.
        attachment_id: The numeric id from the ``## Attachments`` section
            of ``get_ticket_content`` (e.g. ``"12345"``).

    Returns:
        ``Image`` / ``File`` / text payload for supported mime types; an
        ``Error: …`` string when the attachment is missing, too large, or
        of an unsupported type.
    """
    try:
        settings = get_settings()
        max_bytes = settings.jira_attachment_max_mb * 1024 * 1024
        async with JiraClient.from_settings(settings) as client:
            ticket = await client.get_ticket(issue_key)
            attachment = next(
                (a for a in ticket.attachments if a.id == attachment_id),
                None,
            )
            if attachment is None:
                available = ", ".join(a.id for a in ticket.attachments) or "<none>"
                return (
                    f"Error: attachment '{attachment_id}' not found on {issue_key}. "
                    f"Available ids: {available}. "
                    f"Call get_ticket_content to see attachment metadata."
                )
            if attachment.size > max_bytes:
                return (
                    f"Error: attachment too large "
                    f"({attachment.size / (1024 * 1024):.1f} MB > "
                    f"limit {settings.jira_attachment_max_mb} MB). "
                    f"Download manually from {attachment.content_url}"
                )

            mime = attachment.mime_type.lower().strip()
            is_image = mime.startswith("image/")
            is_pdf = mime == "application/pdf"
            is_text = mime.startswith("text/")
            # Reject unsupported mimes BEFORE downloading — saves bandwidth
            # and avoids the asymmetry of "download then realise we can't
            # consume it".
            if not (is_image or is_pdf or is_text):
                return (
                    f"Error: unsupported mime type '{attachment.mime_type}'. "
                    f"Tool supports image/*, application/pdf, text/*. "
                    f"Download manually from {attachment.content_url}"
                )

            data, _ = await client.download_attachment(attachment.content_url, max_bytes=max_bytes)

            if is_image:
                fmt = _IMAGE_FORMATS.get(mime, mime.split("/", 1)[1])
                return Image(data=data, format=fmt)
            if is_pdf:
                return File(data=data, format="pdf", name=attachment.filename)
            # text/* attachments are typically utf-8 (logs, csv, source);
            # fall back to replacement for non-utf-8 so we still surface
            # readable content rather than failing the whole request.
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
            return f"# Attachment: {attachment.filename} ({attachment.mime_type})\n\n{text}"
    except* (JiraError, ValidationError) as eg:
        error = _format_jira_errors(eg)
    return error


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _format_validation_error(eg: BaseExceptionGroup[ValidationError]) -> str:
    missing = sorted(
        {
            str(err["loc"][0]).upper()
            for e in eg.exceptions
            if isinstance(e, ValidationError)
            for err in e.errors()
            if err.get("type") == "missing" and err.get("loc")
        }
    )
    if missing:
        return (
            f"Error: missing required environment variable(s): {', '.join(missing)}. "
            "Set them in the MCP server config (env) or a .env file."
        )
    return f"Error: invalid Jira configuration — {eg.exceptions[0]}"


def _format_jira_errors(eg: BaseExceptionGroup[Exception]) -> str:
    """Map an ExceptionGroup raised inside a tool into a user-facing error.

    Centralises the four tools' error-handling so wording stays in lock-step.
    Each tool catches the union of ``JiraError`` / ``ValidationError`` /
    ``ValueError`` with one ``except*`` clause and hands the group here; we
    pick the most specific subtype present and format accordingly. Order
    inside this function matters: more specific subclasses first.

    Falls through to re-raising the group when none of the known types are
    present, so unexpected exceptions aren't silently swallowed.
    """
    excs = list(eg.exceptions)

    too_large = [e for e in excs if isinstance(e, JiraAttachmentTooLargeError)]
    if too_large:
        msgs = "; ".join(str(e) for e in too_large)
        return f"Error: attachment exceeds size cap — {msgs}"

    auths = [e for e in excs if isinstance(e, JiraAuthError)]
    if auths:
        msgs = "; ".join(str(e) for e in auths)
        return f"Error: Jira authentication failed. Check JIRA_EMAIL and JIRA_API_TOKEN. ({msgs})"

    not_founds = [e for e in excs if isinstance(e, JiraNotFoundError)]
    if not_founds:
        keys = sorted({e.key for e in not_founds if e.key})
        if keys:
            return f"Error: ticket(s) not found in Jira: {', '.join(keys)}"
        msgs = "; ".join(str(e) for e in not_founds)
        return f"Error: Jira returned 404 — {msgs}"

    rate_limits = [e for e in excs if isinstance(e, JiraRateLimitError)]
    if rate_limits:
        msgs = "; ".join(str(e) for e in rate_limits)
        return f"Error: Jira rate limit exceeded after retries. Try again shortly. ({msgs})"

    # ValidationError must be checked before ValueError — pydantic's
    # ValidationError is a subclass of ValueError, so the order avoids
    # mis-routing missing-env-var failures into the depth/cycle branch.
    validations = [e for e in excs if isinstance(e, ValidationError)]
    if validations:
        return _format_validation_error(BaseExceptionGroup("validation", validations))

    value_errors = [
        e for e in excs if isinstance(e, ValueError) and not isinstance(e, ValidationError)
    ]
    if value_errors:
        first = str(value_errors[0])
        if "depth_up" in first or "depth_down" in first:
            return f"Error: invalid depth parameter. {first}"
        return f"Error: hierarchy cycle detected. {first}"

    jira_errors = [e for e in excs if isinstance(e, JiraError)]
    if jira_errors:
        msgs = "; ".join(str(e) for e in jira_errors)
        return f"Error: Jira request failed — {msgs}"

    # Defensive — unknown exception shape, re-raise so the caller sees it.
    raise eg

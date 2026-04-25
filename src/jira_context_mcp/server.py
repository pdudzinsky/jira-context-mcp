"""FastMCP server exposing three composable Jira tools.

- :func:`get_issue_tree` — structural overview of the hierarchy around a
  ticket (lite info per ticket, focus marker, root → leaves layout).
- :func:`get_ticket_content` — full content (description + Smart Checklist
  + optional comments) for a single ticket, no hierarchy.
- :func:`get_smart_checklist` — just the Smart Checklist of one ticket,
  the leanest option when only ACCs are needed.

A typical workflow has the LLM call :func:`get_issue_tree` first to discover
structure, then :func:`get_ticket_content` on the path nodes that interest
it (focus + ancestors usually carry the actionable detail). The three tools
are designed to be composable — each does one thing and returns text the
LLM can quote or further explore.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP
from pydantic import ValidationError

from .config import get_settings
from .jira import (
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
    error: str | None = None
    try:
        async with JiraClient.from_settings(get_settings()) as client:
            tree = await build_issue_tree(
                client,
                issue_key,
                depth_up=depth_up,
                depth_down=depth_down,
            )
        return render_issue_tree(tree)
    except* JiraAuthError as eg:
        msgs = "; ".join(str(e) for e in eg.exceptions)
        error = (
            "Error: Jira authentication failed. "
            f"Check JIRA_EMAIL and JIRA_API_TOKEN. ({msgs})"
        )
    except* JiraNotFoundError as eg:
        keys = sorted(
            {e.key for e in eg.exceptions if isinstance(e, JiraNotFoundError) and e.key}
        )
        if keys:
            error = f"Error: ticket(s) not found in Jira: {', '.join(keys)}"
        else:
            msgs = "; ".join(str(e) for e in eg.exceptions)
            error = f"Error: Jira returned 404 — {msgs}"
    except* JiraRateLimitError as eg:
        msgs = "; ".join(str(e) for e in eg.exceptions)
        error = (
            "Error: Jira rate limit exceeded after retries. "
            f"Try again shortly. ({msgs})"
        )
    except* JiraError as eg:
        msgs = "; ".join(str(e) for e in eg.exceptions)
        error = f"Error: Jira request failed — {msgs}"
    except* ValidationError as eg:
        error = _format_validation_error(eg)
    except* ValueError as eg:
        first = str(eg.exceptions[0]) if eg.exceptions else ""
        if "depth_up" in first or "depth_down" in first:
            error = f"Error: invalid depth parameter. {first}"
        else:
            error = f"Error: hierarchy cycle detected. {first}"
    assert error is not None
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
    error: str | None = None
    try:
        async with JiraClient.from_settings(get_settings()) as client:
            async with asyncio.TaskGroup() as tg:
                ticket_task = tg.create_task(client.get_ticket(issue_key))
                checklist_task = tg.create_task(client.get_checklist(issue_key))
                comments_task = (
                    tg.create_task(client.get_comments(issue_key))
                    if include_comments
                    else None
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
    except* JiraAuthError as eg:
        msgs = "; ".join(str(e) for e in eg.exceptions)
        error = (
            "Error: Jira authentication failed. "
            f"Check JIRA_EMAIL and JIRA_API_TOKEN. ({msgs})"
        )
    except* JiraNotFoundError as eg:
        keys = sorted(
            {e.key for e in eg.exceptions if isinstance(e, JiraNotFoundError) and e.key}
        )
        if keys:
            error = f"Error: ticket(s) not found in Jira: {', '.join(keys)}"
        else:
            msgs = "; ".join(str(e) for e in eg.exceptions)
            error = f"Error: Jira returned 404 — {msgs}"
    except* JiraRateLimitError as eg:
        msgs = "; ".join(str(e) for e in eg.exceptions)
        error = (
            "Error: Jira rate limit exceeded after retries. "
            f"Try again shortly. ({msgs})"
        )
    except* JiraError as eg:
        msgs = "; ".join(str(e) for e in eg.exceptions)
        error = f"Error: Jira request failed — {msgs}"
    except* ValidationError as eg:
        error = _format_validation_error(eg)
    assert error is not None
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
    error: str | None = None
    try:
        async with JiraClient.from_settings(get_settings()) as client:
            checklist = await client.get_checklist(issue_key)
        if checklist is None:
            return (
                f"Smart Checklist on {issue_key}: not present "
                "(plugin not installed, or this ticket doesn't use it)."
            )
        if not checklist.items:
            return (
                f"Smart Checklist on {issue_key}: empty "
                "(plugin active but no items recorded)."
            )
        total = len(checklist.items)
        done = sum(1 for item in checklist.items if item.status == "done")
        count = (
            f"{total} item{'s' if total != 1 else ''}"
            if done == 0
            else f"{done}/{total} done"
        )
        return (
            f"# Smart Checklist: {issue_key} ({count})\n\n"
            f"{render_checklist(checklist, heading_level=2)}\n"
        )
    except* JiraAuthError as eg:
        msgs = "; ".join(str(e) for e in eg.exceptions)
        error = (
            "Error: Jira authentication failed. "
            f"Check JIRA_EMAIL and JIRA_API_TOKEN. ({msgs})"
        )
    except* JiraRateLimitError as eg:
        msgs = "; ".join(str(e) for e in eg.exceptions)
        error = (
            "Error: Jira rate limit exceeded after retries. "
            f"Try again shortly. ({msgs})"
        )
    except* JiraError as eg:
        msgs = "; ".join(str(e) for e in eg.exceptions)
        error = f"Error: Jira request failed — {msgs}"
    except* ValidationError as eg:
        error = _format_validation_error(eg)
    assert error is not None
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

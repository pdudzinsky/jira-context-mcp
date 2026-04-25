"""FastMCP server exposing the ``get_ticket_context`` tool.

The server creates a fresh :class:`JiraClient` per tool invocation (so the
underlying ``httpx.AsyncClient`` is bound to the tool call's event loop and
cleanly torn down afterwards). Errors from the Jira client and the context
walker are converted to readable strings and returned to the caller, rather
than re-raised — MCP tools return text, so communicating failure as text is
what lets the LLM act on it.
"""

from __future__ import annotations

from fastmcp import FastMCP
from pydantic import ValidationError

from .config import get_settings
from .context import build_ticket_context
from .jira import (
    JiraAuthError,
    JiraClient,
    JiraError,
    JiraNotFoundError,
    JiraRateLimitError,
)
from .markdown import render_checklist, render_ticket_context

mcp: FastMCP = FastMCP("jira-context-mcp")


@mcp.tool
async def get_ticket_context(
    issue_key: str,
    include_comments: bool = False,
    max_depth: int = 10,
) -> str:
    """Pull full context of a Jira ticket and its parent hierarchy as structured markdown.

    Walks up from the given ticket through ``fields.parent.key`` to the root of its
    hierarchy (Epic, Initiative, etc.), collecting at each level: description
    (converted from ADF to markdown), Smart Checklist (if the Railsware plugin is
    installed and the ticket uses it), and children of the parent (siblings + self,
    with the current path element highlighted).

    Optionally includes comments from all tickets in the hierarchy.

    Use this tool when:
    - You need full context of a ticket for development work, not just its description
    - A ticket references "the epic" or "the parent" and you need to see them
    - You're estimating work and need to understand the surrounding scope
    - The ticket description is sparse and context likely lives in parent tickets

    Args:
        issue_key: Jira issue key, e.g. "PROJ-1234".
        include_comments: If True, fetches comments for all tickets in the
            hierarchy. Default False (comments can be noisy and costly in tokens).
        max_depth: Safety limit for hierarchy traversal. Default 10. Rarely needed
            to change — real hierarchies are 2-4 levels deep.

    Returns:
        Structured markdown with the full hierarchy, from root down to the entry
        ticket. The entry ticket is marked with a ⬅️ ENTRY indicator. On failure
        returns a line starting with ``Error:`` describing the cause.
    """
    # PEP 654 forbids ``return`` inside ``except*``; collect the error message
    # into a local and return it after the try/except block.
    error: str | None = None
    try:
        async with JiraClient.from_settings(get_settings()) as client:
            ctx = await build_ticket_context(
                client,
                issue_key,
                include_comments=include_comments,
                max_depth=max_depth,
            )
        return render_ticket_context(
            ctx, include_comments=include_comments, max_depth=max_depth
        )
    except* JiraAuthError as eg:
        msgs = "; ".join(str(e) for e in eg.exceptions)
        error = (
            "Error: Jira authentication failed. "
            f"Check JIRA_EMAIL and JIRA_API_TOKEN. ({msgs})"
        )
    except* JiraNotFoundError as eg:
        # eg.exceptions can in principle contain nested ExceptionGroups when
        # multiple tasks raise in the orchestrator's TaskGroup; isinstance
        # narrows the type for mypy and skips groups (which don't carry a key).
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
        # Most common cause: env vars missing at startup. Extract field names
        # for a targeted message; fall back to the full validation output.
        missing = sorted({
            str(err["loc"][0]).upper()
            for e in eg.exceptions
            if isinstance(e, ValidationError)
            for err in e.errors()
            if err.get("type") == "missing" and err.get("loc")
        })
        if missing:
            error = (
                f"Error: missing required environment variable(s): {', '.join(missing)}. "
                "Set them in the MCP server config (env) or a .env file."
            )
        else:
            error = f"Error: invalid Jira configuration — {eg.exceptions[0]}"
    except* ValueError as eg:
        first = str(eg.exceptions[0]) if eg.exceptions else ""
        if "max_depth" in first:
            error = f"Error: invalid max_depth parameter. {first}"
        else:
            error = f"Error: hierarchy cycle detected. {first}"
    assert error is not None
    return error


@mcp.tool
async def get_smart_checklist(issue_key: str) -> str:
    """Fetch the Smart Checklist (Acceptance Criteria / DoD) for a single Jira ticket.

    Smart Checklist is a Railsware/TitanApps Jira plugin that stores acceptance
    criteria in issue properties (``com.railsware.SmartChecklist.checklist``),
    not visible through the standard Jira issue endpoint. Many Atlassian Cloud
    teams use it as the canonical location for ACCs/DoD, leaving the Jira
    Description field minimal or with stubs like "See ACCs.".

    Use this tool when:
    - You only need the acceptance criteria of one ticket, no hierarchy
    - The Description references "See ACCs" and you want the actual list
    - You want a token-efficient alternative to ``get_ticket_context`` for ACC review

    For a ticket plus its parents, siblings, and other context, use
    ``get_ticket_context`` instead — it already includes the Smart Checklist
    for every ticket on the path.

    Args:
        issue_key: Jira issue key, e.g. "PROJ-1234".

    Returns:
        Markdown task list of checklist items, an explanatory message if the
        plugin/property is absent or the checklist is empty, or an
        ``Error: ...`` line on failure.
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
        missing = sorted({
            str(err["loc"][0]).upper()
            for e in eg.exceptions
            if isinstance(e, ValidationError)
            for err in e.errors()
            if err.get("type") == "missing" and err.get("loc")
        })
        if missing:
            error = (
                f"Error: missing required environment variable(s): {', '.join(missing)}. "
                "Set them in the MCP server config (env) or a .env file."
            )
        else:
            error = f"Error: invalid Jira configuration — {eg.exceptions[0]}"
    assert error is not None
    return error

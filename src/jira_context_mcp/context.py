"""Orchestration that assembles a :class:`TicketContext` for one Jira issue.

The build happens in two phases:

1. **Sequential walk up** from the entry ticket to its root via ``parent``
   links. Bounded by ``max_depth`` and guarded against loops by a seen-set.
2. **Parallel fetch** under a single :class:`asyncio.TaskGroup`:

   - one JQL query returns the children of every non-root ancestor so the
     whole level arrives in a single round trip;
   - one checklist call per ticket on the path;
   - one comment call per ticket when ``include_comments`` is true.

The resulting :class:`TicketContext` is ordered root → entry. Each node's
``children_of_parent`` list preserves the JQL response order and **includes
the ticket itself** — the renderer uses ``ticket.key`` matching to highlight
the current node among its peers.
"""

from __future__ import annotations

import asyncio
import logging

from .jira import JiraClient
from .models import Comment, Ticket, TicketContext, TreeNode

logger = logging.getLogger(__name__)


async def build_ticket_context(
    client: JiraClient,
    entry_key: str,
    *,
    include_comments: bool = False,
    max_depth: int = 10,
) -> TicketContext:
    """Fetch the full context for ``entry_key`` and assemble a TicketContext.

    The returned path starts at the ancestor-most ticket reachable from
    ``entry_key`` within ``max_depth`` and ends at ``entry_key`` itself.
    Each :class:`TreeNode` carries its (optional) checklist, comments, and
    the list of its parent's children (including the node itself).

    Raises:
        ValueError: if ``max_depth < 1`` or a parent-link cycle is detected.
    """
    if max_depth < 1:
        raise ValueError(f"max_depth must be >= 1, got {max_depth}")

    tickets, truncated = await _walk_up(client, entry_key, max_depth=max_depth)
    tickets.reverse()

    parent_keys_needed = [t.parent_key for t in tickets if t.parent_key]

    async with asyncio.TaskGroup() as tg:
        children_task = tg.create_task(client.get_children_of(parent_keys_needed))
        checklist_tasks = [tg.create_task(client.get_checklist(t.key)) for t in tickets]
        comment_tasks = (
            [tg.create_task(client.get_comments(t.key)) for t in tickets]
            if include_comments
            else []
        )

    children_by_parent = children_task.result()
    checklists = [task.result() for task in checklist_tasks]
    comments_per_ticket: list[list[Comment]] = (
        [task.result() for task in comment_tasks]
        if include_comments
        else [[] for _ in tickets]
    )

    nodes: list[TreeNode] = []
    last_index = len(tickets) - 1
    for index, ticket in enumerate(tickets):
        children = (
            children_by_parent.get(ticket.parent_key, [])
            if ticket.parent_key
            else []
        )
        nodes.append(
            TreeNode(
                ticket=ticket,
                checklist=checklists[index],
                comments=comments_per_ticket[index],
                children_of_parent=children,
                is_entry=index == last_index,
            )
        )

    return TicketContext(path=nodes, entry_key=entry_key, truncated=truncated)


async def _walk_up(
    client: JiraClient, entry_key: str, *, max_depth: int
) -> tuple[list[Ticket], bool]:
    """Walk parent links from ``entry_key``, fetching up to ``max_depth`` tickets.

    Returns ``(tickets_in_walk_order, truncated)``. ``truncated`` is ``True``
    when the loop exhausted ``max_depth`` before reaching the root. Raises
    :class:`ValueError` on a parent-link cycle and logs a WARN on truncation.
    """
    seen: set[str] = set()
    tickets: list[Ticket] = []
    current_key: str | None = entry_key
    truncated = False

    for _ in range(max_depth):
        if current_key is None:
            break
        if current_key in seen:
            raise ValueError(
                f"cycle detected at {current_key!r}; visited keys: {sorted(seen)}"
            )
        seen.add(current_key)
        ticket = await client.get_ticket(current_key)
        tickets.append(ticket)
        current_key = ticket.parent_key
    else:
        if current_key is not None:
            truncated = True
            logger.warning(
                "max_depth=%d reached while walking from %s; truncating at %s "
                "(parent %s not fetched)",
                max_depth,
                entry_key,
                tickets[-1].key,
                current_key,
            )

    return tickets, truncated

"""Build a :class:`TreeNode` hierarchy rooted at the topmost ancestor of a focus ticket.

The walker has two phases:

1. **Walk UP** sequentially from the focus ticket through ``parent`` links to
   the topmost ancestor reachable within ``depth_up``. The result is the
   "spine" of the tree from the user's perspective — they asked about the
   focus, and these are its direct ancestors.
2. **BFS DOWN** layer by layer from the root, batched into one JQL call per
   layer. Within ``depth_down`` levels every node is expanded; below that,
   only nodes on the spine are expanded so the focus ticket is reachable
   even if it sits deeper than ``depth_down`` from the root.

The resulting :class:`TreeNode` carries the focus marker and the recursive
``children`` list; the renderer in :mod:`.markdown` walks it and produces
markdown.
"""

from __future__ import annotations

import logging

from .jira import JiraClient
from .models import Ticket, TreeNode

logger = logging.getLogger(__name__)

_MAX_DEPTH_DOWN: int = 3
"""Hard upper bound on ``depth_down`` to prevent runaway expansion on epics
with hundreds of stories. Callers can request more, but the walker silently
clamps to this value."""


async def build_issue_tree(
    client: JiraClient,
    focus_key: str,
    *,
    depth_up: int = 10,
    depth_down: int = 2,
) -> TreeNode:
    """Construct the hierarchy tree centered on ``focus_key``.

    Args:
        client: Async Jira client (already inside its async context manager).
        focus_key: Ticket key the user is interested in.
        depth_up: Max levels to walk upward from the focus toward the root.
        depth_down: Max levels to expand under the root for non-spine nodes.
            Clamped to ``[0, _MAX_DEPTH_DOWN]`` silently.

    Raises:
        ValueError: when ``depth_up < 1``, when a parent-link cycle is
            detected, or when ``focus_key`` cannot be fetched.
    """
    if depth_up < 1:
        raise ValueError(f"depth_up must be >= 1, got {depth_up}")
    depth_down_eff = max(0, min(depth_down, _MAX_DEPTH_DOWN))

    spine = await _walk_up(client, focus_key, depth_up=depth_up)
    spine_keys = {ticket.key for ticket in spine}
    root = spine[0]

    children_map = await _walk_down(
        client,
        root=root,
        spine_keys=spine_keys,
        depth_down=depth_down_eff,
    )

    return _build_subtree(root, children_map=children_map, focus_key=focus_key)


async def _walk_up(
    client: JiraClient, focus_key: str, *, depth_up: int
) -> list[Ticket]:
    """Walk parent links from ``focus_key`` toward the root.

    Returns the spine in root-first order. Raises on parent-link cycles.
    """
    seen: set[str] = set()
    spine: list[Ticket] = []
    current_key: str | None = focus_key
    truncated = False

    for _ in range(depth_up):
        if current_key is None:
            break
        if current_key in seen:
            raise ValueError(
                f"cycle detected at {current_key!r}; visited keys: {sorted(seen)}"
            )
        seen.add(current_key)
        ticket = await client.get_ticket(current_key)
        spine.append(ticket)
        current_key = ticket.parent_key
    else:
        if current_key is not None:
            truncated = True
            logger.warning(
                "depth_up=%d reached while walking from %s; spine truncated at %s "
                "(parent %s not fetched)",
                depth_up,
                focus_key,
                spine[-1].key,
                current_key,
            )

    if not spine:
        raise ValueError(f"could not fetch focus ticket {focus_key!r}")

    spine.reverse()  # root first
    if truncated:
        # The walker still produces a usable tree; the warning above is the
        # only signal callers need. The tree just won't include ancestors
        # above the truncation point.
        pass
    return spine


async def _walk_down(
    client: JiraClient,
    *,
    root: Ticket,
    spine_keys: set[str],
    depth_down: int,
) -> dict[str, list[Ticket]]:
    """BFS layer-by-layer from ``root`` collecting children for each expanded node.

    Within ``depth_down`` levels every node in a layer is expanded. At and
    beyond ``depth_down`` only nodes whose key is in ``spine_keys`` are
    expanded — that ensures the focus ticket and its direct ancestors are
    always reachable in the result, even when the focus sits below
    ``depth_down`` levels from the root.

    Returns ``parent_key -> [child_ticket, ...]``, preserving the JQL
    response order (Jira's natural rank). Keys not present in the map have
    no fetched children — either they are leaves in Jira, or they were not
    expanded by policy.
    """
    children_map: dict[str, list[Ticket]] = {}
    layer: list[Ticket] = [root]
    level = 0

    while layer:
        if level < depth_down:
            keys_to_expand = [t.key for t in layer]
        else:
            keys_to_expand = [t.key for t in layer if t.key in spine_keys]

        if not keys_to_expand:
            break

        layer_children = await client.get_children_of(keys_to_expand)
        children_map.update(layer_children)

        next_layer = [child for kids in layer_children.values() for child in kids]
        if not next_layer:
            break
        layer = next_layer
        level += 1

    return children_map


def _build_subtree(
    ticket: Ticket,
    *,
    children_map: dict[str, list[Ticket]],
    focus_key: str,
) -> TreeNode:
    """Recursively construct :class:`TreeNode` instances from the children map."""
    raw_children = children_map.get(ticket.key, [])
    children = [
        _build_subtree(child, children_map=children_map, focus_key=focus_key)
        for child in raw_children
    ]
    return TreeNode(
        ticket=ticket,
        children=children,
        is_focus=(ticket.key == focus_key),
    )

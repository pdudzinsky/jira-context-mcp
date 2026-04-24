"""Entry point for ``python -m jira_context_mcp`` and the installed ``uvx`` script."""

from __future__ import annotations

import logging

from .server import mcp


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="[%(levelname)s %(name)s] %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()

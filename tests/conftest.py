"""Shared fixtures for the test suite."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from jira_context_mcp.config import get_settings
from jira_context_mcp.models import (
    Checklist,
    ChecklistItem,
    ChecklistSection,
    Ticket,
    TreeNode,
)


@pytest.fixture
def jira_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set placeholder JIRA_* env vars and clear the settings cache."""
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "dummy-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace asyncio.sleep with a no-op so retry tests run instantly."""

    async def _noop(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _noop)


@pytest.fixture
def base_url() -> str:
    return "https://example.atlassian.net"


def make_ticket(
    key: str,
    *,
    summary: str | None = None,
    status: str = "Open",
    issue_type: str = "Story",
    assignee: str | None = None,
    description_md: str | None = None,
    parent_key: str | None = None,
) -> Ticket:
    return Ticket(
        key=key,
        summary=summary or f"summary of {key}",
        status=status,
        issue_type=issue_type,
        assignee=assignee,
        description_md=description_md,
        parent_key=parent_key,
        url=f"https://example.atlassian.net/browse/{key}",
    )


@pytest.fixture
def sample_ticket() -> Ticket:
    return make_ticket("FOO-1")


def make_issue_payload(key: str, parent: str | None = None) -> dict[str, Any]:
    """Construct a minimal Jira issue JSON shape mirroring real responses."""
    return {
        "key": key,
        "fields": {
            "summary": f"summary of {key}",
            "status": {"name": "Open"},
            "issuetype": {"name": "Story"},
            "assignee": None,
            "description": None,
            "parent": {"key": parent} if parent else None,
        },
    }


@pytest.fixture
def issue_payload():
    """Factory fixture for building Jira issue JSON payloads in tests."""
    return make_issue_payload


@pytest.fixture
def utc_datetime():
    """Factory for tz-aware datetimes used by Comment fixtures."""

    def _make(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
        return datetime(year, month, day, hour, minute, tzinfo=UTC)

    return _make


@pytest.fixture
def make_tree_node():
    """Factory for recursive TreeNode instances with sane defaults."""

    def _make(
        ticket: Ticket,
        *,
        children: list[TreeNode] | None = None,
        is_focus: bool = False,
    ) -> TreeNode:
        return TreeNode(
            ticket=ticket,
            children=children or [],
            is_focus=is_focus,
        )

    return _make


@pytest.fixture
def simple_checklist() -> Checklist:
    """A two-section, three-item checklist used by several renderer tests."""
    return Checklist(
        sections=[
            ChecklistSection(
                title="1. First section",
                items=[
                    ChecklistItem(name="alpha", status="done"),
                    ChecklistItem(name="beta", status="open"),
                ],
            ),
            ChecklistSection(
                title="2. Second section",
                items=[ChecklistItem(name="gamma", status="in_progress")],
            ),
        ]
    )

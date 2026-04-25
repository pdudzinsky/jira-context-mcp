"""End-to-end tests for the FastMCP server tools (HTTP mocked via respx)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

CHECKLIST_PATH = "/rest/api/3/issue/{key}/properties/com.railsware.SmartChecklist.checklist"


def _ticket_payload(key: str, parent: str | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "summary": f"summary {key}",
            "status": {"name": "Open"},
            "issuetype": {"name": "Story"},
            "assignee": None,
            "description": None,
            "parent": {"key": parent} if parent else None,
        },
    }


# ===================================================================
# Tool registration & schemas
# ===================================================================


@pytest.mark.usefixtures("jira_env")
async def test_three_tools_registered() -> None:
    from jira_context_mcp.server import mcp

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"get_issue_tree", "get_ticket_content", "get_smart_checklist"}


@pytest.mark.usefixtures("jira_env")
async def test_get_issue_tree_schema() -> None:
    from jira_context_mcp.server import mcp

    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_issue_tree")
    schema = tool.to_mcp_tool().inputSchema
    props = schema["properties"]
    assert "issue_key" in props
    assert "depth_up" in props
    assert "depth_down" in props
    assert schema.get("required") == ["issue_key"]


@pytest.mark.usefixtures("jira_env")
async def test_get_ticket_content_schema() -> None:
    from jira_context_mcp.server import mcp

    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_ticket_content")
    schema = tool.to_mcp_tool().inputSchema
    props = schema["properties"]
    assert "issue_key" in props
    assert "include_comments" in props
    assert schema.get("required") == ["issue_key"]


@pytest.mark.usefixtures("jira_env")
async def test_get_smart_checklist_schema() -> None:
    from jira_context_mcp.server import mcp

    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_smart_checklist")
    schema = tool.to_mcp_tool().inputSchema
    assert "issue_key" in schema["properties"]
    assert schema.get("required") == ["issue_key"]


# ===================================================================
# get_issue_tree
# ===================================================================


@pytest.mark.usefixtures("jira_env", "no_sleep")
class TestGetIssueTree:
    async def test_happy_path_renders_tree_with_overview(self, base_url: str) -> None:
        from jira_context_mcp.server import get_issue_tree

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=_ticket_payload("FOO-1"))
            )
            router.post("/rest/api/3/search/jql").mock(
                return_value=httpx.Response(200, json={"issues": [], "isLast": True})
            )
            out = await get_issue_tree(issue_key="FOO-1")
        assert "# Issue tree: FOO-1" in out
        assert "## Overview" in out
        assert "## Tree" in out
        assert "🎯 FOO-1" in out

    async def test_404_returns_not_found_message(self, base_url: str) -> None:
        from jira_context_mcp.server import get_issue_tree

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/MISSING-9").mock(
                return_value=httpx.Response(404, json={})
            )
            out = await get_issue_tree(issue_key="MISSING-9")
        assert "not found" in out
        assert "MISSING-9" in out

    async def test_401_returns_auth_error(self, base_url: str) -> None:
        from jira_context_mcp.server import get_issue_tree

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(return_value=httpx.Response(401, json={}))
            out = await get_issue_tree(issue_key="FOO-1")
        assert out.startswith("Error: Jira authentication failed")

    async def test_depth_up_zero_returns_invalid_depth(self, base_url: str) -> None:
        from jira_context_mcp.server import get_issue_tree

        out = await get_issue_tree(issue_key="FOO-1", depth_up=0)
        assert out.startswith("Error: invalid depth parameter")

    async def test_cycle_returns_cycle_error(self, base_url: str) -> None:
        from jira_context_mcp.server import get_issue_tree

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/A-1").mock(
                return_value=httpx.Response(200, json=_ticket_payload("A-1", parent="B-1"))
            )
            router.get("/rest/api/3/issue/B-1").mock(
                return_value=httpx.Response(200, json=_ticket_payload("B-1", parent="A-1"))
            )
            out = await get_issue_tree(issue_key="A-1")
        assert out.startswith("Error: hierarchy cycle detected")


# ===================================================================
# get_ticket_content
# ===================================================================


@pytest.mark.usefixtures("jira_env", "no_sleep")
class TestGetTicketContent:
    async def test_happy_path_renders_full_content(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_content

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=_ticket_payload("FOO-1"))
            )
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(404, json={})
            )
            out = await get_ticket_content(issue_key="FOO-1")
        assert out.startswith("# FOO-1 ·")
        assert "## Description" in out

    async def test_includes_checklist_when_present(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_content

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=_ticket_payload("FOO-1"))
            )
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(200, json={"value": "## Section\n- alpha\n- beta"})
            )
            out = await get_ticket_content(issue_key="FOO-1")
        assert "## Smart Checklist (2 items)" in out
        assert "### Section" in out
        assert "- [ ] alpha" in out

    async def test_include_comments_fetches_and_renders(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_content

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=_ticket_payload("FOO-1"))
            )
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(404, json={})
            )
            router.get("/rest/api/3/issue/FOO-1/comment").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "comments": [
                            {
                                "author": {"displayName": "Bob"},
                                "created": "2026-04-22T14:05:00+00:00",
                                "body": None,
                            }
                        ],
                        "total": 1,
                    },
                )
            )
            out = await get_ticket_content(issue_key="FOO-1", include_comments=True)
        assert "## Comments" in out
        assert "Bob" in out

    async def test_404_returns_not_found(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_content

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/MISSING-9").mock(
                return_value=httpx.Response(404, json={})
            )
            # get_checklist runs in parallel with get_ticket; mock its endpoint too
            router.get(CHECKLIST_PATH.format(key="MISSING-9")).mock(
                return_value=httpx.Response(404, json={})
            )
            out = await get_ticket_content(issue_key="MISSING-9")
        assert "not found" in out
        assert "MISSING-9" in out

    async def test_401_returns_auth_error(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_content

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(return_value=httpx.Response(401, json={}))
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(401, json={})
            )
            out = await get_ticket_content(issue_key="FOO-1")
        assert out.startswith("Error: Jira authentication failed")


# ===================================================================
# get_smart_checklist
# ===================================================================


@pytest.mark.usefixtures("jira_env", "no_sleep")
class TestGetSmartChecklist:
    async def test_with_items_returns_header_and_sections(self, base_url: str) -> None:
        from jira_context_mcp.server import get_smart_checklist

        body = "## 1. First\n- a\n- b\n## 2. Second\n- c"
        with respx.mock(base_url=base_url) as router:
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(200, json={"value": body})
            )
            out = await get_smart_checklist(issue_key="FOO-1")
        assert out.startswith("# Smart Checklist: FOO-1 (3 items)")
        assert "## 1. First" in out

    async def test_empty_returns_message(self, base_url: str) -> None:
        from jira_context_mcp.server import get_smart_checklist

        with respx.mock(base_url=base_url) as router:
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(200, json={"value": ""})
            )
            out = await get_smart_checklist(issue_key="FOO-1")
        assert out.startswith("Smart Checklist on FOO-1: empty")

    async def test_404_returns_not_present(self, base_url: str) -> None:
        from jira_context_mcp.server import get_smart_checklist

        with respx.mock(base_url=base_url) as router:
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(404, json={})
            )
            out = await get_smart_checklist(issue_key="FOO-1")
        assert out.startswith("Smart Checklist on FOO-1: not present")

    async def test_401_returns_auth_error(self, base_url: str) -> None:
        from jira_context_mcp.server import get_smart_checklist

        with respx.mock(base_url=base_url) as router:
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(401, json={})
            )
            out = await get_smart_checklist(issue_key="FOO-1")
        assert out.startswith("Error: Jira authentication failed")


# ===================================================================
# Config validation
# ===================================================================


async def test_get_issue_tree_missing_env_returns_missing_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    from jira_context_mcp.config import get_settings

    get_settings.cache_clear()
    from jira_context_mcp.server import get_issue_tree

    out = await get_issue_tree(issue_key="FOO-1")
    assert out.startswith("Error: missing required environment variable(s):")
    get_settings.cache_clear()


async def test_get_ticket_content_invalid_url_returns_invalid_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JIRA_BASE_URL", "not-a-url")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "dummy")
    from jira_context_mcp.config import get_settings

    get_settings.cache_clear()
    from jira_context_mcp.server import get_ticket_content

    out = await get_ticket_content(issue_key="FOO-1")
    assert out.startswith("Error: invalid Jira configuration")
    get_settings.cache_clear()

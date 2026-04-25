"""End-to-end tests for the FastMCP server tools (HTTP mocked via respx)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

CHECKLIST_PATH = (
    "/rest/api/3/issue/{key}/properties/com.railsware.SmartChecklist.checklist"
)


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
# Tool registration
# ===================================================================


@pytest.mark.usefixtures("jira_env")
async def test_both_tools_registered() -> None:
    from jira_context_mcp.server import mcp

    tools = await mcp.list_tools()
    names = [t.name for t in tools]
    assert "get_ticket_context" in names
    assert "get_smart_checklist" in names


@pytest.mark.usefixtures("jira_env")
async def test_get_ticket_context_schema_has_expected_params() -> None:
    from jira_context_mcp.server import mcp

    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_ticket_context")
    schema = tool.to_mcp_tool().inputSchema
    props = schema["properties"]
    assert "issue_key" in props
    assert "include_comments" in props
    assert "max_depth" in props
    assert schema.get("required") == ["issue_key"]


@pytest.mark.usefixtures("jira_env")
async def test_get_smart_checklist_schema_minimal() -> None:
    from jira_context_mcp.server import mcp

    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_smart_checklist")
    schema = tool.to_mcp_tool().inputSchema
    assert "issue_key" in schema["properties"]
    assert schema.get("required") == ["issue_key"]


# ===================================================================
# get_ticket_context error paths
# ===================================================================


@pytest.mark.usefixtures("jira_env", "no_sleep")
class TestGetTicketContextErrors:
    async def test_401_returns_auth_error_message(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_context

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(401, json={})
            )
            out = await get_ticket_context(issue_key="FOO-1")
        assert out.startswith("Error: Jira authentication failed")

    async def test_404_on_entry_returns_message_with_key(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_context

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/MISSING-9").mock(
                return_value=httpx.Response(404, json={})
            )
            out = await get_ticket_context(issue_key="MISSING-9")
        assert "ticket(s) not found" in out
        assert "MISSING-9" in out

    async def test_429_exhausted_returns_rate_limit_error(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_context

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(429, headers={"Retry-After": "0"})
            )
            out = await get_ticket_context(issue_key="FOO-1", max_depth=1)
        assert out.startswith("Error: Jira rate limit exceeded")

    async def test_max_depth_zero_returns_invalid_max_depth(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_context

        out = await get_ticket_context(issue_key="FOO-1", max_depth=0)
        assert out.startswith("Error: invalid max_depth parameter")

    async def test_cycle_returns_hierarchy_cycle_error(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_context

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/A-1").mock(
                return_value=httpx.Response(200, json=_ticket_payload("A-1", parent="B-1"))
            )
            router.get("/rest/api/3/issue/B-1").mock(
                return_value=httpx.Response(200, json=_ticket_payload("B-1", parent="A-1"))
            )
            out = await get_ticket_context(issue_key="A-1")
        assert out.startswith("Error: hierarchy cycle detected")


# ===================================================================
# Config validation errors (no jira_env fixture)
# ===================================================================


async def test_missing_env_returns_listing_each_missing_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # chdir into a clean dir so the repo's .env (if present) isn't picked up
    monkeypatch.chdir(tmp_path)
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    from jira_context_mcp.config import get_settings
    get_settings.cache_clear()
    from jira_context_mcp.server import get_ticket_context

    out = await get_ticket_context(issue_key="FOO-1")
    assert out.startswith("Error: missing required environment variable(s):")
    assert "JIRA_BASE_URL" in out
    assert "JIRA_EMAIL" in out
    assert "JIRA_API_TOKEN" in out
    get_settings.cache_clear()


async def test_invalid_url_returns_invalid_jira_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JIRA_BASE_URL", "not-a-url")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "dummy")
    from jira_context_mcp.config import get_settings
    get_settings.cache_clear()
    from jira_context_mcp.server import get_ticket_context

    out = await get_ticket_context(issue_key="FOO-1")
    assert out.startswith("Error: invalid Jira configuration")
    get_settings.cache_clear()


# ===================================================================
# get_ticket_context happy path
# ===================================================================


@pytest.mark.usefixtures("jira_env", "no_sleep")
async def test_happy_path_returns_full_render(base_url: str) -> None:
    from jira_context_mcp.server import get_ticket_context

    with respx.mock(base_url=base_url) as router:
        router.get("/rest/api/3/issue/FOO-1").mock(
            return_value=httpx.Response(200, json=_ticket_payload("FOO-1"))
        )
        router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
            return_value=httpx.Response(404, json={})
        )
        out = await get_ticket_context(issue_key="FOO-1")
    assert "# Ticket context: FOO-1" in out
    assert "⬅️ ENTRY" in out
    assert "## Issue tree" in out


# ===================================================================
# get_smart_checklist tool — output shapes + errors
# ===================================================================


@pytest.mark.usefixtures("jira_env", "no_sleep")
class TestGetSmartChecklist:
    async def test_with_items_returns_header_with_count_and_sections(
        self, base_url: str
    ) -> None:
        from jira_context_mcp.server import get_smart_checklist

        body = "## 1. First\n- a\n- b\n## 2. Second\n- c"
        with respx.mock(base_url=base_url) as router:
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(200, json={"value": body})
            )
            out = await get_smart_checklist(issue_key="FOO-1")
        assert out.startswith("# Smart Checklist: FOO-1 (3 items)")
        assert "## 1. First" in out
        assert "## 2. Second" in out
        assert "- [ ] a" in out
        assert "- [ ] c" in out

    async def test_empty_value_returns_empty_message(self, base_url: str) -> None:
        from jira_context_mcp.server import get_smart_checklist

        with respx.mock(base_url=base_url) as router:
            router.get(CHECKLIST_PATH.format(key="FOO-2")).mock(
                return_value=httpx.Response(200, json={"value": ""})
            )
            out = await get_smart_checklist(issue_key="FOO-2")
        assert out.startswith("Smart Checklist on FOO-2: empty")

    async def test_404_returns_not_present_message(self, base_url: str) -> None:
        from jira_context_mcp.server import get_smart_checklist

        with respx.mock(base_url=base_url) as router:
            router.get(CHECKLIST_PATH.format(key="FOO-3")).mock(
                return_value=httpx.Response(404, json={})
            )
            out = await get_smart_checklist(issue_key="FOO-3")
        assert out.startswith("Smart Checklist on FOO-3: not present")

    async def test_auth_failure_returns_error(self, base_url: str) -> None:
        from jira_context_mcp.server import get_smart_checklist

        with respx.mock(base_url=base_url) as router:
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(401, json={})
            )
            out = await get_smart_checklist(issue_key="FOO-1")
        assert out.startswith("Error: Jira authentication failed")

    async def test_rate_limit_exhausted_returns_error(
        self, base_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ensure max_retries is small so we don't loop forever
        monkeypatch.setenv("MAX_RETRIES", "1")
        from jira_context_mcp.config import get_settings
        get_settings.cache_clear()
        from jira_context_mcp.server import get_smart_checklist

        with respx.mock(base_url=base_url) as router:
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(429, headers={"Retry-After": "0"})
            )
            out = await get_smart_checklist(issue_key="FOO-1")
        assert out.startswith("Error: Jira rate limit exceeded")
        get_settings.cache_clear()

    async def test_5xx_exhausted_returns_generic_request_failed(
        self, base_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAX_RETRIES", "1")
        from jira_context_mcp.config import get_settings
        get_settings.cache_clear()
        from jira_context_mcp.server import get_smart_checklist

        with respx.mock(base_url=base_url) as router:
            router.get(CHECKLIST_PATH.format(key="FOO-1")).mock(
                return_value=httpx.Response(503, text="busy")
            )
            out = await get_smart_checklist(issue_key="FOO-1")
        assert out.startswith("Error: Jira request failed")
        get_settings.cache_clear()

    async def test_missing_env_returns_missing_vars_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.chdir(tmp_path)
        for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        from jira_context_mcp.config import get_settings
        get_settings.cache_clear()
        from jira_context_mcp.server import get_smart_checklist

        out = await get_smart_checklist(issue_key="FOO-1")
        assert out.startswith("Error: missing required environment variable(s):")
        get_settings.cache_clear()

"""Integration-style tests for the Jira async client (HTTP mocked via respx)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from jira_context_mcp.jira import (
    JiraAuthError,
    JiraClient,
    JiraError,
    JiraNotFoundError,
    JiraRateLimitError,
)


CHECKLIST_PROPERTY_PATH = (
    "/rest/api/3/issue/{key}/properties/com.railsware.SmartChecklist.checklist"
)


@pytest.fixture
def client_factory(base_url: str):
    """Build a JiraClient with sane defaults; tests pick max_retries per case."""

    def _make(*, max_retries: int = 0, timeout: float = 5.0) -> JiraClient:
        return JiraClient(
            base_url=base_url,
            email="test@example.com",
            api_token="dummy",
            timeout=timeout,
            max_retries=max_retries,
        )

    return _make


def _ticket_payload(key: str, *, parent: str | None = None, **fields: Any) -> dict[str, Any]:
    """Construct a minimal Jira issue JSON shape with optional field overrides."""
    f: dict[str, Any] = {
        "summary": fields.get("summary", f"summary {key}"),
        "status": {"name": fields.get("status", "Open")},
        "issuetype": {"name": fields.get("issuetype", "Story")},
        "assignee": fields.get("assignee", None),
        "description": fields.get("description", None),
        "parent": {"key": parent} if parent else None,
    }
    return {"key": key, "fields": f}


# ===================================================================
# _request retry / status-code dispatch
# ===================================================================


@pytest.mark.usefixtures("no_sleep")
class TestRequestRetry:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_status_raises_jira_auth_error(
        self, client_factory, base_url: str, status: int
    ) -> None:
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.get("/rest/api/3/issue/FOO-1").mock(
                    return_value=httpx.Response(status, json={})
                )
                with pytest.raises(JiraAuthError):
                    await client.get_ticket("FOO-1")

    async def test_404_without_allow_404_raises_not_found(
        self, client_factory, base_url: str
    ) -> None:
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.get("/rest/api/3/issue/FOO-1").mock(
                    return_value=httpx.Response(404, json={})
                )
                with pytest.raises(JiraNotFoundError):
                    await client.get_ticket("FOO-1")

    async def test_404_with_allow_404_returns_response(
        self, client_factory, base_url: str
    ) -> None:
        """get_checklist sets allow_404=True under the hood; verify the path."""
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.get(CHECKLIST_PROPERTY_PATH.format(key="FOO-1")).mock(
                    return_value=httpx.Response(404, json={})
                )
                result = await client.get_checklist("FOO-1")
                assert result is None

    async def test_429_exhausted_raises_rate_limit_error(
        self, client_factory, base_url: str
    ) -> None:
        async with client_factory(max_retries=1) as client:
            with respx.mock(base_url=base_url) as router:
                router.get("/rest/api/3/issue/FOO-1").mock(
                    return_value=httpx.Response(429, headers={"Retry-After": "0"})
                )
                with pytest.raises(JiraRateLimitError):
                    await client.get_ticket("FOO-1")

    async def test_429_then_200_succeeds(
        self, client_factory, base_url: str
    ) -> None:
        async with client_factory(max_retries=2) as client:
            with respx.mock(base_url=base_url) as router:
                route = router.get("/rest/api/3/issue/FOO-1")
                route.side_effect = [
                    httpx.Response(429, headers={"Retry-After": "0"}),
                    httpx.Response(200, json=_ticket_payload("FOO-1")),
                ]
                ticket = await client.get_ticket("FOO-1")
                assert ticket.key == "FOO-1"

    async def test_retry_after_seconds_respected(
        self, client_factory, base_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When Retry-After is set, the client should sleep at least that long."""
        sleep_args: list[float] = []

        async def _capture(seconds: float) -> None:
            sleep_args.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _capture)
        async with client_factory(max_retries=1) as client:
            with respx.mock(base_url=base_url) as router:
                route = router.get("/rest/api/3/issue/FOO-1")
                route.side_effect = [
                    httpx.Response(429, headers={"Retry-After": "5"}),
                    httpx.Response(200, json=_ticket_payload("FOO-1")),
                ]
                await client.get_ticket("FOO-1")
        assert sleep_args, "asyncio.sleep was never called"
        assert max(sleep_args) >= 5.0

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_5xx_exhausted_raises_jira_error(
        self, client_factory, base_url: str, status: int
    ) -> None:
        async with client_factory(max_retries=1) as client:
            with respx.mock(base_url=base_url) as router:
                router.get("/rest/api/3/issue/FOO-1").mock(
                    return_value=httpx.Response(status, text="oops")
                )
                with pytest.raises(JiraError):
                    await client.get_ticket("FOO-1")

    async def test_5xx_then_200_succeeds(
        self, client_factory, base_url: str
    ) -> None:
        async with client_factory(max_retries=2) as client:
            with respx.mock(base_url=base_url) as router:
                route = router.get("/rest/api/3/issue/FOO-1")
                route.side_effect = [
                    httpx.Response(503, text="busy"),
                    httpx.Response(200, json=_ticket_payload("FOO-1")),
                ]
                ticket = await client.get_ticket("FOO-1")
                assert ticket.key == "FOO-1"

    async def test_timeout_exception_retried(
        self, client_factory, base_url: str
    ) -> None:
        async with client_factory(max_retries=2) as client:
            with respx.mock(base_url=base_url) as router:
                route = router.get("/rest/api/3/issue/FOO-1")
                route.side_effect = [
                    httpx.TimeoutException("slow"),
                    httpx.Response(200, json=_ticket_payload("FOO-1")),
                ]
                ticket = await client.get_ticket("FOO-1")
                assert ticket.key == "FOO-1"

    async def test_network_error_retried(
        self, client_factory, base_url: str
    ) -> None:
        async with client_factory(max_retries=2) as client:
            with respx.mock(base_url=base_url) as router:
                route = router.get("/rest/api/3/issue/FOO-1")
                route.side_effect = [
                    httpx.NetworkError("dns"),
                    httpx.Response(200, json=_ticket_payload("FOO-1")),
                ]
                ticket = await client.get_ticket("FOO-1")
                assert ticket.key == "FOO-1"


# ===================================================================
# Public API: get_ticket / get_checklist / get_comments / get_children_of
# ===================================================================


@pytest.mark.usefixtures("no_sleep")
class TestPublicApi:
    async def test_get_ticket_maps_full_payload(
        self, client_factory, base_url: str
    ) -> None:
        payload = {
            "key": "FOO-1",
            "fields": {
                "summary": "Foo",
                "status": {"name": "In Progress"},
                "issuetype": {"name": "Story"},
                "assignee": {"displayName": "Alice"},
                "description": None,
                "parent": {"key": "ROOT-1"},
            },
        }
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.get("/rest/api/3/issue/FOO-1").mock(
                    return_value=httpx.Response(200, json=payload)
                )
                t = await client.get_ticket("FOO-1")
        assert t.key == "FOO-1"
        assert t.summary == "Foo"
        assert t.status == "In Progress"
        assert t.issue_type == "Story"
        assert t.assignee == "Alice"
        assert t.parent_key == "ROOT-1"
        assert t.url == f"{base_url}/browse/FOO-1"

    async def test_get_ticket_404_attaches_key_to_exception(
        self, client_factory, base_url: str
    ) -> None:
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.get("/rest/api/3/issue/MISSING-9").mock(
                    return_value=httpx.Response(404, json={})
                )
                with pytest.raises(JiraNotFoundError) as exc:
                    await client.get_ticket("MISSING-9")
        assert exc.value.key == "MISSING-9"

    async def test_get_checklist_with_bullet_markdown_returns_sectioned(
        self, client_factory, base_url: str
    ) -> None:
        body = "## 1. Section\n- alpha\n- beta\n## 2. Other\n- gamma"
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.get(CHECKLIST_PROPERTY_PATH.format(key="FOO-1")).mock(
                    return_value=httpx.Response(200, json={"value": body})
                )
                cl = await client.get_checklist("FOO-1")
        assert cl is not None
        assert [s.title for s in cl.sections] == ["1. Section", "2. Other"]
        assert [it.name for it in cl.items] == ["alpha", "beta", "gamma"]

    async def test_get_checklist_with_legacy_task_list_preserves_statuses(
        self, client_factory, base_url: str
    ) -> None:
        body = "[x] done one\n[ ] open one\n[-] in progress"
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.get(CHECKLIST_PROPERTY_PATH.format(key="FOO-1")).mock(
                    return_value=httpx.Response(200, json={"value": body})
                )
                cl = await client.get_checklist("FOO-1")
        assert cl is not None
        assert [(it.name, it.status) for it in cl.items] == [
            ("done one", "done"),
            ("open one", "open"),
            ("in progress", "in_progress"),
        ]

    async def test_get_checklist_empty_value_returns_no_sections(
        self, client_factory, base_url: str
    ) -> None:
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.get(CHECKLIST_PROPERTY_PATH.format(key="FOO-1")).mock(
                    return_value=httpx.Response(200, json={"value": ""})
                )
                cl = await client.get_checklist("FOO-1")
        assert cl is not None
        assert cl.sections == []

    async def test_get_comments_happy_path(
        self, client_factory, base_url: str
    ) -> None:
        payload = {
            "comments": [
                {
                    "author": {"displayName": "Bob"},
                    "created": "2026-04-20T10:00:00.000+0200",
                    "body": None,
                }
            ],
            "total": 1,
        }
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.get("/rest/api/3/issue/FOO-1/comment").mock(
                    return_value=httpx.Response(200, json=payload)
                )
                comments = await client.get_comments("FOO-1")
        assert len(comments) == 1
        assert comments[0].author == "Bob"

    async def test_get_comments_skips_malformed_created(
        self, client_factory, base_url: str
    ) -> None:
        payload = {
            "comments": [
                {"author": {"displayName": "Bob"}, "created": "not-a-date", "body": None},
                {
                    "author": {"displayName": "Eve"},
                    "created": "2026-04-22T14:05:00+00:00",
                    "body": None,
                },
            ],
            "total": 2,
        }
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.get("/rest/api/3/issue/FOO-1/comment").mock(
                    return_value=httpx.Response(200, json=payload)
                )
                comments = await client.get_comments("FOO-1")
        assert [c.author for c in comments] == ["Eve"]

    async def test_get_comments_warns_when_total_exceeds_returned(
        self,
        client_factory,
        base_url: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        payload = {
            "comments": [
                {
                    "author": {"displayName": "A"},
                    "created": "2026-01-01T00:00:00+00:00",
                    "body": None,
                }
            ],
            "total": 250,
        }
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.get("/rest/api/3/issue/FOO-1/comment").mock(
                    return_value=httpx.Response(200, json=payload)
                )
                with caplog.at_level(logging.WARNING, logger="jira_context_mcp.jira"):
                    await client.get_comments("FOO-1")
        assert any("pagination not implemented" in r.message for r in caplog.records)

    async def test_get_children_of_empty_returns_empty_dict(
        self, client_factory
    ) -> None:
        async with client_factory() as client:
            assert await client.get_children_of([]) == {}

    async def test_get_children_of_paginates_via_next_page_token(
        self, client_factory, base_url: str
    ) -> None:
        page1 = {
            "issues": [_ticket_payload("CH-1", parent="ROOT-1")],
            "nextPageToken": "tok-2",
            "isLast": False,
        }
        page2 = {
            "issues": [_ticket_payload("CH-2", parent="ROOT-1")],
            "isLast": True,
        }
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                route = router.post("/rest/api/3/search/jql")
                route.side_effect = [
                    httpx.Response(200, json=page1),
                    httpx.Response(200, json=page2),
                ]
                kids = await client.get_children_of(["ROOT-1"])
        assert [t.key for t in kids["ROOT-1"]] == ["CH-1", "CH-2"]

    async def test_get_children_of_preserves_jql_response_order(
        self, client_factory, base_url: str
    ) -> None:
        issues = [
            _ticket_payload("Z-LAST", parent="ROOT-1"),
            _ticket_payload("A-FIRST", parent="ROOT-1"),
            _ticket_payload("M-MIDDLE", parent="ROOT-1"),
        ]
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                router.post("/rest/api/3/search/jql").mock(
                    return_value=httpx.Response(
                        200, json={"issues": issues, "isLast": True}
                    )
                )
                kids = await client.get_children_of(["ROOT-1"])
        assert [t.key for t in kids["ROOT-1"]] == ["Z-LAST", "A-FIRST", "M-MIDDLE"]


# ===================================================================
# Headers + auth
# ===================================================================


@pytest.mark.usefixtures("no_sleep")
class TestHeaders:
    async def test_user_agent_and_accept_headers_set(
        self, client_factory, base_url: str
    ) -> None:
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                route = router.get("/rest/api/3/issue/FOO-1").mock(
                    return_value=httpx.Response(200, json=_ticket_payload("FOO-1"))
                )
                await client.get_ticket("FOO-1")
        assert route.called
        request = route.calls.last.request
        assert request.headers.get("user-agent", "").startswith("jira-context-mcp/")
        assert request.headers.get("accept") == "application/json"

    async def test_basic_auth_header_present(
        self, client_factory, base_url: str
    ) -> None:
        async with client_factory() as client:
            with respx.mock(base_url=base_url) as router:
                route = router.get("/rest/api/3/issue/FOO-1").mock(
                    return_value=httpx.Response(200, json=_ticket_payload("FOO-1"))
                )
                await client.get_ticket("FOO-1")
        request = route.calls.last.request
        assert request.headers.get("authorization", "").startswith("Basic ")

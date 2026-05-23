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


def _attachment_payload(
    att_id: str,
    *,
    filename: str = "design.png",
    mime_type: str = "image/png",
    size: int = 4096,
    base_url: str = "https://example.atlassian.net",
) -> dict[str, Any]:
    return {
        "id": att_id,
        "filename": filename,
        "mimeType": mime_type,
        "size": size,
        "created": "2026-05-12T10:00:00+00:00",
        "author": {"displayName": "Jane"},
        "content": f"{base_url}/rest/api/3/attachment/content/{att_id}",
    }


def _ticket_payload_with_attachments(key: str, attachments: list[dict[str, Any]]) -> dict[str, Any]:
    payload = _ticket_payload(key)
    payload["fields"]["attachment"] = attachments
    return payload


# ===================================================================
# Tool registration & schemas
# ===================================================================


@pytest.mark.usefixtures("jira_env")
async def test_all_tools_registered() -> None:
    from jira_context_mcp.server import mcp

    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "get_issue_tree",
        "get_ticket_content",
        "get_smart_checklist",
        "get_ticket_attachment",
    }


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


@pytest.mark.usefixtures("jira_env")
async def test_get_ticket_attachment_schema() -> None:
    from jira_context_mcp.server import mcp

    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_ticket_attachment")
    schema = tool.to_mcp_tool().inputSchema
    props = schema["properties"]
    assert "issue_key" in props
    assert "attachment_id" in props
    assert set(schema.get("required") or []) == {"issue_key", "attachment_id"}


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
# get_ticket_attachment
# ===================================================================


@pytest.mark.usefixtures("jira_env", "no_sleep")
class TestGetTicketAttachment:
    async def test_image_returns_native_image_payload(self, base_url: str) -> None:
        from fastmcp.utilities.types import Image

        from jira_context_mcp.server import get_ticket_attachment

        payload = _ticket_payload_with_attachments(
            "FOO-1",
            [_attachment_payload("12345", filename="design.png", mime_type="image/png")],
        )
        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            router.get("/rest/api/3/attachment/content/12345").mock(
                return_value=httpx.Response(
                    200, content=b"PNGBYTES", headers={"Content-Type": "image/png"}
                )
            )
            result = await get_ticket_attachment(issue_key="FOO-1", attachment_id="12345")
        assert isinstance(result, Image)
        assert result.data == b"PNGBYTES"
        # FastMCP stores the format in a private slot; round-trip through the
        # MCP content converter to verify the mime survives end-to-end.
        assert result.to_image_content().mimeType == "image/png"

    async def test_pdf_returns_native_file_payload(self, base_url: str) -> None:
        from fastmcp.utilities.types import File

        from jira_context_mcp.server import get_ticket_attachment

        payload = _ticket_payload_with_attachments(
            "FOO-1",
            [_attachment_payload("777", filename="spec.pdf", mime_type="application/pdf")],
        )
        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            router.get("/rest/api/3/attachment/content/777").mock(
                return_value=httpx.Response(
                    200,
                    content=b"%PDF-1.4...",
                    headers={"Content-Type": "application/pdf"},
                )
            )
            result = await get_ticket_attachment(issue_key="FOO-1", attachment_id="777")
        assert isinstance(result, File)
        assert result.data == b"%PDF-1.4..."
        # Round-trip through the MCP content converter to verify the mime
        # survives end-to-end; FastMCP's File stores format/name privately.
        embedded = result.to_resource_content()
        assert embedded.resource.mimeType == "application/pdf"

    async def test_text_returns_markdown_string(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_attachment

        payload = _ticket_payload_with_attachments(
            "FOO-1",
            [_attachment_payload("42", filename="notes.txt", mime_type="text/plain")],
        )
        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            router.get("/rest/api/3/attachment/content/42").mock(
                return_value=httpx.Response(
                    200,
                    content=b"hello world\nline two",
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                )
            )
            out = await get_ticket_attachment(issue_key="FOO-1", attachment_id="42")
        assert isinstance(out, str)
        assert out.startswith("# Attachment: notes.txt (text/plain)")
        assert "hello world\nline two" in out

    async def test_unsupported_mime_returns_error_with_url(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_attachment

        payload = _ticket_payload_with_attachments(
            "FOO-1",
            [_attachment_payload("9", filename="archive.zip", mime_type="application/zip")],
        )
        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            out = await get_ticket_attachment(issue_key="FOO-1", attachment_id="9")
        assert isinstance(out, str)
        assert out.startswith("Error: unsupported mime type 'application/zip'")
        assert "/rest/api/3/attachment/content/9" in out

    async def test_attachment_not_found_lists_available_ids(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_attachment

        payload = _ticket_payload_with_attachments(
            "FOO-1",
            [
                _attachment_payload("AAA"),
                _attachment_payload("BBB"),
            ],
        )
        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            out = await get_ticket_attachment(issue_key="FOO-1", attachment_id="ZZZ")
        assert isinstance(out, str)
        assert "not found on FOO-1" in out
        assert "AAA" in out
        assert "BBB" in out

    async def test_attachment_too_large_by_metadata_short_circuits(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_attachment

        # 100 MB > default 20 MB cap; tool should refuse without downloading.
        huge = 100 * 1024 * 1024
        payload = _ticket_payload_with_attachments(
            "FOO-1",
            [
                _attachment_payload(
                    "BIG", filename="huge.pdf", mime_type="application/pdf", size=huge
                )
            ],
        )
        # ``assert_all_called=False`` because the short-circuit path should
        # never hit the download route — that's exactly what this test asserts.
        with respx.mock(base_url=base_url, assert_all_called=False) as router:
            ticket_route = router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            download_route = router.get("/rest/api/3/attachment/content/BIG").mock(
                return_value=httpx.Response(200, content=b"X")
            )
            out = await get_ticket_attachment(issue_key="FOO-1", attachment_id="BIG")
        assert isinstance(out, str)
        assert out.startswith("Error: attachment too large")
        assert "20 MB" in out
        assert ticket_route.called
        assert not download_route.called

    async def test_ticket_not_found_returns_error(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_attachment

        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/MISSING-9").mock(return_value=httpx.Response(404))
            out = await get_ticket_attachment(issue_key="MISSING-9", attachment_id="1")
        assert isinstance(out, str)
        assert "not found" in out
        assert "MISSING-9" in out

    async def test_mid_download_too_large_routes_through_shared_handler(
        self, base_url: str
    ) -> None:
        """When ``attachment.size`` is 0 / missing the metadata short-circuit
        is bypassed; the streaming cap must kick in and the tool must surface
        the resulting ``JiraAttachmentTooLargeError`` as an Error string."""
        from jira_context_mcp.server import get_ticket_attachment

        payload = _ticket_payload_with_attachments(
            "FOO-1",
            # size=0 means metadata check thinks "ok, small"; the actual
            # download then crosses the cap mid-stream.
            [_attachment_payload("X", filename="x.png", mime_type="image/png", size=0)],
        )
        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            router.get("/rest/api/3/attachment/content/X").mock(
                # 30 MB blob, default cap is 20 MB.
                return_value=httpx.Response(
                    200,
                    content=b"X" * (30 * 1024 * 1024),
                    headers={"Content-Type": "image/png"},
                )
            )
            out = await get_ticket_attachment(issue_key="FOO-1", attachment_id="X")
        assert isinstance(out, str)
        assert out.startswith("Error: attachment exceeds size cap")

    async def test_text_with_latin1_bytes_decodes_with_replacement(self, base_url: str) -> None:
        """text/* fallback decode must not crash on non-utf-8 input."""
        from jira_context_mcp.server import get_ticket_attachment

        payload = _ticket_payload_with_attachments(
            "FOO-1",
            [_attachment_payload("42", filename="log.txt", mime_type="text/plain")],
        )
        latin1_bytes = "café".encode("latin-1")  # b'caf\xe9' — invalid UTF-8
        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            router.get("/rest/api/3/attachment/content/42").mock(
                return_value=httpx.Response(
                    200, content=latin1_bytes, headers={"Content-Type": "text/plain"}
                )
            )
            out = await get_ticket_attachment(issue_key="FOO-1", attachment_id="42")
        assert isinstance(out, str)
        # The replacement char is U+FFFD; the file body should still appear.
        assert "log.txt" in out
        assert "caf" in out

    async def test_svg_dispatches_with_canonical_mime(self, base_url: str) -> None:
        """SVG must round-trip through Image with the IANA-canonical mime."""
        from fastmcp.utilities.types import Image

        from jira_context_mcp.server import get_ticket_attachment

        payload = _ticket_payload_with_attachments(
            "FOO-1",
            [_attachment_payload("S", filename="icon.svg", mime_type="image/svg+xml")],
        )
        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            router.get("/rest/api/3/attachment/content/S").mock(
                return_value=httpx.Response(
                    200,
                    content=b"<svg xmlns='http://www.w3.org/2000/svg'/>",
                    headers={"Content-Type": "image/svg+xml"},
                )
            )
            result = await get_ticket_attachment(issue_key="FOO-1", attachment_id="S")
        assert isinstance(result, Image)
        assert result.to_image_content().mimeType == "image/svg+xml"

    async def test_401_during_download_returns_auth_error(self, base_url: str) -> None:
        from jira_context_mcp.server import get_ticket_attachment

        payload = _ticket_payload_with_attachments(
            "FOO-1",
            [_attachment_payload("12345")],
        )
        with respx.mock(base_url=base_url) as router:
            router.get("/rest/api/3/issue/FOO-1").mock(
                return_value=httpx.Response(200, json=payload)
            )
            router.get("/rest/api/3/attachment/content/12345").mock(
                return_value=httpx.Response(401)
            )
            out = await get_ticket_attachment(issue_key="FOO-1", attachment_id="12345")
        assert isinstance(out, str)
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

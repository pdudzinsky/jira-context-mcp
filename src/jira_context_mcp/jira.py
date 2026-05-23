"""Async read-only Jira Cloud client with retry and rate-limit handling.

Wraps the Jira REST API v3 with exponential-backoff + full-jitter retries,
honors ``Retry-After`` on 429 responses, and maps raw JSON into the domain
models from :mod:`.models`. Smart Checklist data is read from the plugin
issue property and parsed from its markdown form.

The client is meant to be used as an async context manager so the underlying
``httpx.AsyncClient`` is created and torn down deterministically per call
site (typically once per MCP tool invocation).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime
from types import TracebackType
from typing import Any, Final

import httpx

from .adf import adf_to_markdown, collect_media_filenames, normalise_attachment_filename
from .config import Settings
from .models import (
    Attachment,
    Checklist,
    ChecklistItem,
    ChecklistSection,
    ChecklistStatus,
    Comment,
    Ticket,
)

logger = logging.getLogger(__name__)


class JiraError(Exception):
    """Base class for Jira client errors surfaced to callers."""


class JiraAuthError(JiraError):
    """Credentials were rejected by Jira (HTTP 401 or 403)."""


class JiraNotFoundError(JiraError):
    """A requested Jira issue does not exist (HTTP 404 on an issue endpoint).

    When the caller knows the key that triggered the 404 it is attached as
    ``self.key`` so the MCP layer can surface it in the user-facing error.
    """

    def __init__(self, message: str, *, key: str | None = None) -> None:
        super().__init__(message)
        self.key: str | None = key


class JiraRateLimitError(JiraError):
    """Rate limit could not be cleared within the configured retry budget."""


class JiraAttachmentTooLargeError(JiraError):
    """Attachment exceeds the configured download size cap.

    ``size`` and ``max_bytes`` (when set) let callers format an actionable
    user-facing message — the MCP server surfaces them in the tool response.
    """

    def __init__(
        self,
        message: str,
        *,
        size: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.size = size
        self.max_bytes = max_bytes


_USER_AGENT: Final = "jira-context-mcp/0.2.0 (+https://github.com/pdudzinsky/jira-context-mcp)"
_DEFAULT_HEADERS: Final[dict[str, str]] = {
    "Accept": "application/json",
    "User-Agent": _USER_AGENT,
}
_CHECKLIST_PROPERTY: Final = "com.railsware.SmartChecklist.checklist"
_TICKET_FIELDS: Final = [
    "summary",
    "status",
    "issuetype",
    "assignee",
    "description",
    "parent",
    "attachment",
]
_JQL_FIELDS: Final = ["summary", "status", "issuetype", "assignee", "parent"]

_BACKOFF_BASE_SECONDS: Final = 1.0
_BACKOFF_MAX_SECONDS: Final = 30.0

# Legacy task-list form: optional bullet, then [<marker>] <name>
# (status carried inline via the marker character).
_CHECKLIST_TASK_RE: Final = re.compile(r"^\s*[-*]?\s*\[(.)\]\s+(.+?)\s*$")
# Modern Smart Checklist v3 form: plain bullet item with no inline status —
# statuses live in a sibling property (``SmartChecklist`` aggregate /
# ``ItemStatusSearchMeta`` per-item hashes), so every parsed item defaults
# to ``"open"`` here. Section headers ("# ...") are skipped silently.
_CHECKLIST_BULLET_RE: Final = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_HEADER_RE: Final = re.compile(r"^\s*#")

_STATUS_MAP: Final[dict[str, ChecklistStatus]] = {
    " ": "open",
    "x": "done",
    "X": "done",
    "-": "in_progress",
    "~": "skipped",
}


def parse_checklist_markdown(raw: str) -> Checklist:
    """Parse a Smart Checklist markdown blob into a structured :class:`Checklist`.

    Handles two formats:

    1. **Modern (Smart Checklist v3+)** — plain bullet items ``- text``,
       grouped under ``## section`` headers. Items default to status
       ``"open"`` because per-item status lives in sibling Jira properties
       (``SmartChecklist`` aggregate flags, ``ItemStatusSearchMeta`` hashes).
    2. **Legacy task-list** — ``[ ] text`` / ``[x] text`` / ``[-] text`` /
       ``[~] text`` with the status carried inline. Recognised for
       backward compatibility with older instances and ad-hoc fixtures.

    Section grouping is preserved: each ``#``/``##`` header opens a new
    :class:`ChecklistSection`. Items appearing before the first header are
    grouped under a section with ``title=None``. Sections that contain no
    items are kept in the structure (so the renderer can decide whether to
    show or skip them); blank lines between items are ignored.

    Lines with an unrecognised legacy marker are kept with status coerced
    to ``"open"`` and a warning emitted.
    """
    sections: list[ChecklistSection] = []
    current_title: str | None = None
    current_items: list[ChecklistItem] = []
    seen_any_section = False

    def flush() -> None:
        # Don't emit a leading title=None section if it has no items —
        # that's the common case where the first line of the markdown is
        # a header, and there is no orphan content to record.
        if not seen_any_section and not current_items:
            return
        sections.append(ChecklistSection(title=current_title, items=list(current_items)))

    for line in raw.splitlines():
        if not line.strip():
            continue

        if _HEADER_RE.match(line):
            flush()
            current_title = line.lstrip("#").strip() or None
            current_items = []
            seen_any_section = True
            continue

        # Try the legacy task-list form first — it's the more specific shape
        # ("- [ ] x" must not be parsed as a bare bullet of "[ ] x").
        task = _CHECKLIST_TASK_RE.match(line)
        if task:
            marker = task.group(1)
            name = task.group(2).strip()
            if not name:
                continue
            status = _STATUS_MAP.get(marker)
            if status is None:
                logger.warning(
                    "unknown Smart Checklist status marker [%s] in line: %r",
                    marker,
                    line,
                )
                status = "open"
            current_items.append(ChecklistItem(name=name, status=status))
            continue

        bullet = _CHECKLIST_BULLET_RE.match(line)
        if bullet:
            name = bullet.group(1).strip()
            if not name:
                continue
            current_items.append(ChecklistItem(name=name, status="open"))
            continue

    flush()
    return Checklist(sections=sections)


class JiraClient:
    """Async read-only client for Jira Cloud REST API v3.

    Must be used as an async context manager::

        async with JiraClient.from_settings(settings) as client:
            ticket = await client.get_ticket("PROJ-1234")

    Auth failures, missing issues, and unresolved rate limits surface as
    :class:`JiraError` subclasses. Transient network errors and 5xx responses
    are retried with exponential backoff and full jitter up to
    ``settings.max_retries`` additional attempts.
    """

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        timeout: float,
        max_retries: int,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._api_token = api_token
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> JiraClient:
        return cls(
            base_url=settings.jira_base_url,
            email=settings.jira_email,
            api_token=settings.jira_api_token.get_secret_value(),
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
        )

    async def __aenter__(self) -> JiraClient:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=httpx.BasicAuth(self._email, self._api_token),
            headers=_DEFAULT_HEADERS,
            timeout=self._timeout,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_ticket(self, key: str) -> Ticket:
        """Fetch a single issue and map it to :class:`Ticket`."""
        try:
            response = await self._request(
                "GET",
                f"/rest/api/3/issue/{key}",
                params={"fields": ",".join(_TICKET_FIELDS)},
            )
        except JiraNotFoundError as e:
            raise JiraNotFoundError(str(e), key=key) from e
        return self._to_ticket(response.json())

    async def get_checklist(self, key: str) -> Checklist | None:
        """Fetch the Smart Checklist for ``key``, or ``None`` if absent.

        ``None`` means the plugin is not installed or the issue has no
        checklist property; an empty but present checklist surfaces as a
        :class:`Checklist` with no sections.
        """
        response = await self._request(
            "GET",
            f"/rest/api/3/issue/{key}/properties/{_CHECKLIST_PROPERTY}",
            allow_404=True,
        )
        if response.status_code == 404:
            return None
        value = response.json().get("value")
        if not isinstance(value, str):
            return Checklist(sections=[])
        return parse_checklist_markdown(value)

    async def get_comments(self, key: str) -> list[Comment]:
        """Fetch the first page of comments for ``key`` (up to 100).

        Pagination is intentionally not implemented; issues with more than 100
        comments surface a WARN log and truncated list (see CHANGELOG).
        """
        response = await self._request(
            "GET",
            f"/rest/api/3/issue/{key}/comment",
            params={"maxResults": 100},
        )
        payload = response.json()
        raw_comments = payload.get("comments") or []
        total = payload.get("total")
        if isinstance(total, int) and total > len(raw_comments):
            logger.warning(
                "Comment pagination not implemented; returning first %d of %d "
                "for %s. See https://github.com/pdudzinsky/jira-context-mcp/issues",
                len(raw_comments),
                total,
                key,
            )
        comments: list[Comment] = []
        for raw in raw_comments:
            parsed = _parse_comment(raw)
            if parsed is not None:
                comments.append(parsed)
        return comments

    async def get_children_of(self, parent_keys: list[str]) -> dict[str, list[Ticket]]:
        """Return children grouped by parent key via a single JQL search.

        Uses the new ``/rest/api/3/search/jql`` endpoint with cursor-based
        pagination. Result preserves the order Jira returns (roughly by rank).
        Keys with no children still appear in the result with an empty list
        so callers can index unconditionally.
        """
        unique = sorted({k for k in parent_keys if k})
        result: dict[str, list[Ticket]] = {k: [] for k in unique}
        if not unique:
            return result

        jql_keys = ", ".join(f'"{k}"' for k in unique)
        jql = f"parent in ({jql_keys})"
        next_token: str | None = None

        while True:
            body: dict[str, Any] = {"jql": jql, "fields": _JQL_FIELDS}
            if next_token:
                body["nextPageToken"] = next_token
            response = await self._request("POST", "/rest/api/3/search/jql", json=body)
            data = response.json()
            for issue in data.get("issues") or []:
                ticket = self._to_ticket(issue)
                if ticket.parent_key in result:
                    result[ticket.parent_key].append(ticket)
            next_token = data.get("nextPageToken")
            if data.get("isLast") or not next_token:
                break
        return result

    async def _request(
        self,
        method: str,
        url: str,
        *,
        allow_404: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("JiraClient used outside of its async context manager")

        for attempt in range(self._max_retries + 1):
            is_last = attempt == self._max_retries
            try:
                response = await self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if is_last:
                    raise JiraError(f"network error after {attempt + 1} attempt(s): {e}") from e
                await asyncio.sleep(_compute_backoff(attempt))
                continue

            code = response.status_code
            if code in (401, 403):
                raise JiraAuthError(
                    f"{code} {response.reason_phrase} — check JIRA_EMAIL and JIRA_API_TOKEN"
                )
            if code == 404 and not allow_404:
                raise JiraNotFoundError(f"{method} {url} returned 404")
            if code == 429:
                if is_last:
                    raise JiraRateLimitError(f"rate-limited after {attempt + 1} attempt(s)")
                server_hint = _parse_retry_after(response)
                sleep = max(server_hint, _compute_backoff(attempt))
                logger.warning(
                    "Jira 429 on %s %s, sleeping %.1fs (attempt %d/%d)",
                    method,
                    url,
                    sleep,
                    attempt + 1,
                    self._max_retries + 1,
                )
                await asyncio.sleep(sleep)
                continue
            if 500 <= code < 600:
                if is_last:
                    raise JiraError(
                        f"server error {code} after {attempt + 1} attempt(s): {response.text[:200]}"
                    )
                await asyncio.sleep(_compute_backoff(attempt))
                continue

            return response

        raise JiraError("retry loop exited without returning — unreachable")

    def _to_ticket(self, payload: dict[str, Any]) -> Ticket:
        fields = payload.get("fields") or {}
        key = payload["key"]
        parent = fields.get("parent") or {}
        status_obj = fields.get("status") or {}
        issuetype_obj = fields.get("issuetype") or {}
        assignee_obj = fields.get("assignee") or {}

        # Pre-pass: collect every media filename referenced from the
        # description body so we can mark each attachment as embedded
        # (mentioned in the prose) vs orphan (uploaded but unreferenced).
        description_adf = fields.get("description")
        embedded_filenames = collect_media_filenames(description_adf)
        attachments = _parse_attachments(fields.get("attachment"), embedded_filenames)

        return Ticket(
            key=key,
            summary=fields.get("summary") or "",
            status=status_obj.get("name") or "Unknown",
            issue_type=issuetype_obj.get("name") or "Unknown",
            assignee=assignee_obj.get("displayName"),
            # heading_offset=3 keeps user-authored headings ("# Story",
            # "## Goal") inside the renderer's level-3 ### Description
            # section instead of breaking out above the document title.
            # ``attachments`` is also passed so embedded media nodes in the
            # description can be resolved to their filename via attrs.alt.
            description_md=adf_to_markdown(
                description_adf,
                heading_offset=3,
                attachments=attachments,
            ),
            parent_key=parent.get("key"),
            url=f"{self._base_url}/browse/{key}",
            attachments=attachments,
        )

    async def download_attachment(
        self,
        content_url: str,
        *,
        max_bytes: int,
    ) -> tuple[bytes, str]:
        """Stream an attachment download, aborting early if it exceeds ``max_bytes``.

        Returns ``(data, mime_type)``. ``content_url`` is the absolute URL Jira
        returns in the ``attachment.content`` field; it's served by the same
        host that needs BasicAuth, so we reuse the client's auth and redirects.

        Raises:
            JiraAuthError: 401/403 from Jira.
            JiraNotFoundError: 404 — the attachment was deleted between the
                list-fetch and the download.
            JiraAttachmentTooLargeError: ``Content-Length`` header (when
                present) exceeds ``max_bytes``, or accumulated bytes exceed
                it mid-stream.
            JiraError: any other transport / server failure.
        """
        if self._client is None:
            raise RuntimeError("JiraClient used outside of its async context manager")

        try:
            async with self._client.stream("GET", content_url, follow_redirects=True) as response:
                code = response.status_code
                if code in (401, 403):
                    raise JiraAuthError(
                        f"{code} {response.reason_phrase} — check JIRA_EMAIL and JIRA_API_TOKEN"
                    )
                if code == 404:
                    raise JiraNotFoundError(f"GET {content_url} returned 404")
                if code >= 400:
                    raise JiraError(f"attachment download failed: HTTP {code}")

                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        # Some proxies and edge servers send malformed values
                        # (chunked-then-rewritten responses, etc.). Log so a
                        # misbehaving intermediary is debuggable, then fall
                        # through to the streaming accumulator which enforces
                        # the cap independently of the header.
                        logger.warning(
                            "Ignoring invalid Content-Length %r on attachment download",
                            content_length,
                        )
                        declared = 0
                    if declared > max_bytes:
                        raise JiraAttachmentTooLargeError(
                            f"attachment is {declared} bytes, exceeds cap {max_bytes}",
                            size=declared,
                            max_bytes=max_bytes,
                        )

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise JiraAttachmentTooLargeError(
                            f"attachment exceeds cap {max_bytes} bytes mid-download",
                            size=total,
                            max_bytes=max_bytes,
                        )
                    chunks.append(chunk)

                mime_type = (
                    response.headers.get("content-type", "application/octet-stream")
                    .split(";", 1)[0]
                    .strip()
                )
                return b"".join(chunks), mime_type
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise JiraError(f"network error during attachment download: {e}") from e


def _compute_backoff(attempt: int) -> float:
    cap = min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_MAX_SECONDS)
    return random.uniform(0.0, cap)


def _parse_retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        # Jira Cloud always returns integer seconds. If support for HTTP-date
        # form is ever needed, parse via email.utils.parsedate_to_datetime.
        return 0.0


def _parse_attachments(
    raw: Any,
    embedded_filenames: set[str] | None = None,
) -> tuple[Attachment, ...]:
    """Map the ``fields.attachment`` array onto :class:`Attachment` instances.

    Returns ``()`` when ``raw`` is missing or empty. Skipped items:

    - **Missing ``id`` or ``content``** — WARN log emitted (the upstream
      payload is genuinely broken; we want it visible).
    - **Malformed ``created``** (non-ISO string) — WARN log emitted.
    - **Naive ``created``** (no tzinfo) — silently skipped; Jira always
      sends a tz so this would only fire on synthetic / mangled payloads.
    - **Missing ``created``** — silently skipped, same reasoning.

    Partial data must not break the tool response — every error path
    drops the offending item and keeps going.

    ``embedded_filenames`` (when provided) is the set of media filenames
    referenced from inside the description body. Each attachment is
    marked ``embedded=True`` when its filename — or its UUID-suffix
    normalised form — appears in that set; otherwise ``embedded=False``.
    """
    if not isinstance(raw, list):
        return ()
    embedded_set = embedded_filenames or set()
    out: list[Attachment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        attachment_id = item.get("id")
        content_url = item.get("content")
        if not attachment_id or not content_url:
            logger.warning("Skipping attachment with missing id or content URL")
            continue
        created_raw = item.get("created")
        if not created_raw:
            continue
        try:
            created = datetime.fromisoformat(created_raw)
        except ValueError:
            logger.warning("Skipping attachment with malformed created: %r", created_raw)
            continue
        if created.tzinfo is None:
            continue
        author = (item.get("author") or {}).get("displayName") or "Unknown"
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        filename = item.get("filename") or ""
        # ADF's attrs.alt keeps the original filename even when Jira has
        # appended ``(uuid)`` to the stored attachment.filename for
        # disambiguation — normalise before comparing.
        embedded = bool(embedded_set) and (
            filename in embedded_set or normalise_attachment_filename(filename) in embedded_set
        )
        out.append(
            Attachment(
                id=str(attachment_id),
                filename=filename,
                mime_type=item.get("mimeType") or "application/octet-stream",
                size=size,
                created=created,
                author=author,
                content_url=str(content_url),
                embedded=embedded,
            )
        )
    return tuple(out)


def _parse_comment(raw: dict[str, Any]) -> Comment | None:
    created_raw = raw.get("created")
    if not created_raw:
        return None
    try:
        created = datetime.fromisoformat(created_raw)
    except ValueError:
        return None
    if created.tzinfo is None:
        return None
    author = (raw.get("author") or {}).get("displayName") or "Unknown"
    # Same heading_offset rationale as for descriptions: comment bodies live
    # inside the ### Comments section (and a > blockquote on top), so any
    # user-authored heading is shifted to keep document hierarchy sound.
    body_md = adf_to_markdown(raw.get("body"), heading_offset=3) or ""
    return Comment(author=author, created=created, body_md=body_md)

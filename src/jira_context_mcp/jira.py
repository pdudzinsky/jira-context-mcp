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

from .adf import adf_to_markdown
from .config import Settings
from .models import Checklist, ChecklistItem, ChecklistStatus, Comment, Ticket

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


_USER_AGENT: Final = (
    "jira-context-mcp/0.1.0 (+https://github.com/pdudzinsky/jira-context-mcp)"
)
_DEFAULT_HEADERS: Final[dict[str, str]] = {
    "Accept": "application/json",
    "User-Agent": _USER_AGENT,
}
_CHECKLIST_PROPERTY: Final = "com.railsware.SmartChecklist.checklist"
_TICKET_FIELDS: Final = ["summary", "status", "issuetype", "assignee", "description", "parent"]
_JQL_FIELDS: Final = ["summary", "status", "issuetype", "assignee", "parent"]

_BACKOFF_BASE_SECONDS: Final = 1.0
_BACKOFF_MAX_SECONDS: Final = 30.0

_CHECKLIST_ITEM_RE: Final = re.compile(r"^\s*\[(.)\]\s+(.+?)\s*$")
_STATUS_MAP: Final[dict[str, ChecklistStatus]] = {
    " ": "open",
    "x": "done",
    "X": "done",
    "-": "in_progress",
    "~": "skipped",
}


def parse_checklist_markdown(raw: str) -> list[ChecklistItem]:
    """Parse a Smart Checklist markdown blob into a flat item list.

    Section headers, blank lines, and any text outside the ``[<marker>] <name>``
    shape are silently skipped. Lines with an unrecognized marker are kept
    with ``status`` coerced to ``"open"`` and a warning written to stderr so
    upstream changes to the plugin's status set are noticed quickly.
    """
    items: list[ChecklistItem] = []
    for line in raw.splitlines():
        match = _CHECKLIST_ITEM_RE.match(line)
        if not match:
            continue
        marker = match.group(1)
        name = match.group(2).strip()
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
        items.append(ChecklistItem(name=name, status=status))
    return items


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
        checklist property; an empty but present checklist surfaces as
        ``Checklist(items=[])``.
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
            return Checklist(items=[])
        return Checklist(items=parse_checklist_markdown(value))

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

    async def get_children_of(
        self, parent_keys: list[str]
    ) -> dict[str, list[Ticket]]:
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
                    raise JiraError(
                        f"network error after {attempt + 1} attempt(s): {e}"
                    ) from e
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
                    raise JiraRateLimitError(
                        f"rate-limited after {attempt + 1} attempt(s)"
                    )
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
                        f"server error {code} after {attempt + 1} attempt(s): "
                        f"{response.text[:200]}"
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

        return Ticket(
            key=key,
            summary=fields.get("summary") or "",
            status=status_obj.get("name") or "Unknown",
            issue_type=issuetype_obj.get("name") or "Unknown",
            assignee=assignee_obj.get("displayName"),
            description_md=adf_to_markdown(fields.get("description")),
            parent_key=parent.get("key"),
            url=f"{self._base_url}/browse/{key}",
        )


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
    body_md = adf_to_markdown(raw.get("body")) or ""
    return Comment(author=author, created=created, body_md=body_md)

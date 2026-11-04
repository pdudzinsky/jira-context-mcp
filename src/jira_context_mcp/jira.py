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
import json
import logging
import random
import re
from collections.abc import Iterator
from contextlib import contextmanager
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
    ChecklistText,
    Comment,
    Ticket,
)

logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO, including the attachment redirect target,
# whose query carries a media ``token``. Keep that out of the host's logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


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
# Failures that can pass by themselves. Repeating a call is safe, as every one only reads.
_RETRIED_TRANSPORT_ERRORS: Final = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)

# Smart Checklist grammar. Status lives inline, in the marker that starts the line:
# https://railsware.atlassian.net/wiki/spaces/CHK/pages/90636343/Formatting+Guide+Markdown
#
# Markers count only at the very start of a line. The plugin parses line by line and
# its own counters (``totalChecklistItems``, the "Smart Checklist Progress" field) leave
# indented lines out, while including items inside a code fence.
_ITEM_STATUS: Final[dict[str, ChecklistStatus]] = {
    "-": "open",
    "+": "done",
    "~": "in_progress",
    "x": "skipped",
}
# ``!`` right after the marker makes the item mandatory. ``-`` and ``+`` may be followed
# directly by the text (plugin release note v1.0.2, which also names ``#``). ``~`` and
# ``x`` may not: "~~struck~~", "~>>LT-1" and "xylophone" are not items.
_ITEM_RE: Final = re.compile(r"(?P<marker>[-+]|[~x](?=!?\s))(?P<mandatory>!?)\s*(?P<name>.+)")
_EMPTY_ITEM_RE: Final = re.compile(r"[-+~x]!?|\s*\[[ xX~-]\]")
# ``- [FAILED] text``. The name is at most 50 characters, the plugin's limit, and may sit
# next to ``[key="value"]`` attributes, which stay in the item name. What follows the
# bracket rules out a markdown link, ``- [text](url)``.
_CUSTOM_STATUS_RE: Final = re.compile(
    r"(?P<attributes>(?:\[\w+=[^\]]*\]\s*)*)"
    r"\[\s*(?P<status>[^\[\]=\s][^\[\]=]{0,49}?)\s*\]"
    r"(?:\s+|(?=\[))"
    r"(?P<rest>.+)"
)
_HEADER_RE: Final = re.compile(r"(#{1,6})(?!#)\s*(.*)")
# ``> text`` or ``>text`` below an item. A further ``>`` is a nested quote inside the detail.
_DETAIL_RE: Final = re.compile(r">\s?(.*)")
_SEPARATOR_RE: Final = re.compile(r"-{3,}")
# Wrapper around items imported from a linked template, the items themselves are regular:
# https://railsware.atlassian.net/wiki/spaces/CHK/pages/4000579601/Linked+Templates
_LINKED_TEMPLATE_RE: Final = re.compile(r"(?:<<~|~>>)LT-\d+")

# ``[x] text`` is not Smart Checklist syntax: the plugin doesn't document it, nor write it,
# and Jira doesn't count such a line as an item. It is kept for compatibility. Its markers
# are the ones this server prints, so they don't mean what they do at the start of a plugin
# line: compare ``[x]`` (done) with ``x`` (skipped), and ``[~]`` (skipped) with ``~``.
_TASK_LIST_RE: Final = re.compile(r"\s*\[([ xX~-])\]\s+(.+)")
_TASK_LIST_STATUS: Final[dict[str, ChecklistStatus]] = {
    " ": "open",
    "x": "done",
    "X": "done",
    "-": "in_progress",
    "~": "skipped",
}


def parse_checklist_markdown(raw: str) -> Checklist:
    """Parse a Smart Checklist markdown blob into a structured :class:`Checklist`.

    Nothing that may carry content is dropped. Each line becomes one of:

    - a header (``#`` to ``######``), opening a :class:`ChecklistSection`;
    - an item (see :data:`_ITEM_STATUS`, or :data:`_TASK_LIST_RE` for the bare ``[x] text``
      form), optionally mandatory and with a custom status;
    - a detail of the item right above it (``>``);
    - :class:`ChecklistText`, for every line that is none of these. Jira shows such lines
      but doesn't count them as items: bold sub-headings, notes, numbered or indented text.

    Only lines with no content of their own disappear: blank lines, ``---`` separators,
    linked-template wrappers and markers with no text. Content before the first header
    goes to a section with ``title=None``. Sections with no entries are kept, because a
    header can be a parent of the next one or a note in its own right.
    """
    sections: list[ChecklistSection] = []
    current_title: str | None = None
    current_level = 1
    current_entries: list[ChecklistItem | ChecklistText] = []
    seen_any_section = False

    def flush() -> None:
        # Don't emit a leading title=None section if it has no entries —
        # that's the common case where the first line of the markdown is
        # a header, and there is no orphan content to record.
        if not seen_any_section and not current_entries:
            return
        sections.append(
            ChecklistSection(
                title=current_title,
                level=current_level,
                entries=list(current_entries),
            )
        )

    for raw_line in raw.splitlines():
        line = raw_line.rstrip()
        if (
            not line
            or _SEPARATOR_RE.fullmatch(line)
            or _LINKED_TEMPLATE_RE.fullmatch(line)
            or _EMPTY_ITEM_RE.fullmatch(line)
        ):
            continue

        header = _HEADER_RE.fullmatch(line)
        if header:
            flush()
            current_title = header.group(2).strip() or None
            current_level = len(header.group(1))
            current_entries = []
            seen_any_section = True
            continue

        detail = _DETAIL_RE.fullmatch(line)
        last = current_entries[-1] if current_entries else None
        if detail and isinstance(last, ChecklistItem):
            details = [*last.details, detail.group(1)]
            current_entries[-1] = last.model_copy(update={"details": details})
            continue

        current_entries.append(_parse_item(line) or ChecklistText(text=line))

    flush()
    return Checklist(sections=sections)


def _parse_item(line: str) -> ChecklistItem | None:
    """Return the item ``line`` holds, or ``None`` if it isn't one."""
    item = _ITEM_RE.fullmatch(line)
    if item:
        name = item.group("name")
        custom_status = None
        custom = _CUSTOM_STATUS_RE.fullmatch(name)
        if custom:
            custom_status = custom.group("status")
            name = f"{custom.group('attributes').rstrip()} {custom.group('rest')}".strip()
        return ChecklistItem(
            name=name,
            status=_ITEM_STATUS[item.group("marker")],
            custom_status=custom_status,
            mandatory=bool(item.group("mandatory")),
        )

    task = _TASK_LIST_RE.fullmatch(line)
    if task:
        marker, name = task.groups()
        return ChecklistItem(name=name, status=_TASK_LIST_STATUS[marker])

    return None


class JiraClient:
    """Async read-only client for Jira Cloud REST API v3.

    Must be used as an async context manager::

        async with JiraClient.from_settings(settings) as client:
            ticket = await client.get_ticket("PROJ-1234")

    Auth failures, missing issues, and unresolved rate limits surface as
    :class:`JiraError` subclasses; any other status outside 2xx, a 2xx body that
    isn't the expected JSON and a transport failure as :class:`JiraError` itself.
    Transient transport errors and 5xx responses are retried with exponential
    backoff and full jitter up to ``settings.max_retries`` additional attempts.
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
        with _reading_body(response):
            return self._to_ticket(_utf8_safe(response.json()))

    async def get_checklist(self, key: str) -> Checklist | None:
        """Fetch the Smart Checklist for ``key``, or ``None`` if Jira answers 404.

        Jira answers 404 alike for an issue with no checklist property and for an
        issue it doesn't show, so ``None`` says only that the property wasn't found.
        An empty but present checklist surfaces as a :class:`Checklist` with no
        sections. The plugin documents the value as raw markdown, so any other value
        is kept as text rather than passing for an empty checklist. A response that
        doesn't hold a property value raises :class:`JiraError`, for the same reason.
        """
        response = await self._request(
            "GET",
            f"/rest/api/3/issue/{key}/properties/{_CHECKLIST_PROPERTY}",
            allow_404=True,
        )
        if response.status_code == 404:
            return None
        with _reading_body(response):
            value = _utf8_safe(response.json())["value"]
        if value is None:
            return Checklist(sections=[])
        if isinstance(value, str):
            return parse_checklist_markdown(value)
        logger.warning(
            "Smart Checklist property of %s is %s, not markdown text",
            key,
            type(value).__name__,
        )
        text = ChecklistText(text=json.dumps(value, ensure_ascii=False))
        return Checklist(sections=[ChecklistSection(title=None, entries=[text])])

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
        with _reading_body(response):
            payload = _utf8_safe(response.json())
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
            with _reading_body(response):
                data = _utf8_safe(response.json())
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
        """Send a request and return a 2xx response, or a 404 when ``allow_404`` is set.

        Every other status raises a :class:`JiraError`, after retries for 429 and 5xx.
        A redirect is one of them: unlike ``download_attachment``, this doesn't follow it.
        """
        if self._client is None:
            raise RuntimeError("JiraClient used outside of its async context manager")

        for attempt in range(self._max_retries + 1):
            is_last = attempt == self._max_retries
            try:
                response = await self._client.request(method, url, **kwargs)
            except _RETRIED_TRANSPORT_ERRORS as e:
                if is_last:
                    raise JiraError(
                        f"{method} {url} failed after {attempt + 1} attempt(s): "
                        f"{_describe_transport_error(e)}"
                    ) from e
                await asyncio.sleep(_compute_backoff(attempt))
                continue
            except httpx.HTTPError as e:
                raise JiraError(f"{method} {url} failed: {_describe_transport_error(e)}") from e
            except (httpx.InvalidURL, UnicodeError) as e:
                # What httpx refused is in ``url``: a line break, a lone surrogate, sheer length.
                raise JiraError(f"{method} {url[:200]!r} is not a valid request") from e

            code = response.status_code
            if response.is_success or (code == 404 and allow_404):
                return response

            outcome = _describe_response(response)
            if code in (401, 403):
                raise JiraAuthError(outcome)
            if code == 404:
                raise JiraNotFoundError(outcome)
            if code == 429:
                if is_last:
                    raise JiraRateLimitError(f"{outcome} after {attempt + 1} attempt(s)")
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
                    raise JiraError(f"{outcome} after {attempt + 1} attempt(s)")
                await asyncio.sleep(_compute_backoff(attempt))
                continue
            raise JiraError(outcome)

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
            JiraError: any other status outside 2xx once redirects are followed,
                or a transport failure.
        """
        if self._client is None:
            raise RuntimeError("JiraClient used outside of its async context manager")

        try:
            async with self._client.stream("GET", content_url, follow_redirects=True) as response:
                outcome = _describe_response(response)
                code = response.status_code
                if code in (401, 403):
                    raise JiraAuthError(outcome)
                if code == 404:
                    raise JiraNotFoundError(outcome)
                if not response.is_success:
                    raise JiraError(outcome)

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
        except httpx.HTTPError as e:
            raise JiraError(f"attachment download failed: {_describe_transport_error(e)}") from e
        except (httpx.InvalidURL, UnicodeError) as e:
            raise JiraError("attachment download failed: invalid URL") from e


def _describe_transport_error(error: httpx.HTTPError) -> str:
    """Name a transport failure, e.g. ``ConnectError: All connection attempts failed``.

    Timeouts and network errors come from the socket or TLS layer, so their text is
    safe to quote and says what went wrong. Any other error is named by type alone: its
    text can quote what the server sent, like the header line h11 rejected.
    """
    name = type(error).__name__
    if isinstance(error, (httpx.TimeoutException, httpx.NetworkError)) and str(error):
        return f"{name}: {error}"
    return name


def _describe_response(response: httpx.Response) -> str:
    """Name a response's request and status, e.g. ``GET /path returned 400 Bad Request``.

    Built from the method, the URL path and the status code alone, so that no body, reason
    phrase or query string can carry a secret into an error message. After a redirect it
    names the request this client made, not the target the server chose. The path stays
    percent-encoded, which keeps the message on one line.
    """
    request = response.history[0].request if response.history else response.request
    code = response.status_code
    status = f"{code} {httpx.codes.get_reason_phrase(code)}".rstrip()
    path = request.url.raw_path.partition(b"?")[0].decode("ascii")
    return f"{request.method} {path} returned {status}"


@contextmanager
def _reading_body(response: httpx.Response) -> Iterator[None]:
    """Report a 2xx body that isn't the JSON the block expects as a :class:`JiraError`.

    The block only maps a body already received onto the models, so an error raised
    there means the wrong body: malformed JSON or undecodable bytes (``ValueError``),
    JSON of another shape, or values the models reject (pydantic's ``ValidationError``
    is a ``ValueError`` too). A tool response holds only the message, which leaves the
    log as the one place to tell such a body from a bug in the mapping itself.
    """
    try:
        yield
    except (AttributeError, LookupError, RecursionError, TypeError, ValueError) as e:
        message = f"{_describe_response(response)}, but the body is not the expected JSON"
        logger.warning("%s", message, exc_info=True)
        raise JiraError(message) from e


def _utf8_safe(value: Any) -> Any:
    """Return ``value`` with every string in it encodable as UTF-8.

    JSON can carry a lone surrogate (half of a UTF-16 pair, such as an unpaired
    ``\\ud83d`` escape). Python's parser keeps it, but UTF-8 can't encode it, and
    the MCP SDK serializes each reply in the task that writes stdout, so one such
    character in a tool result ends the whole server.

    Everything read from a Jira response body goes through here, rather than tool
    results on their way out, because filenames, URLs and keys are used before any
    result exists. Each surrogate becomes ``?``; a complete pair is one character
    by this point, so it stays as it is.
    """
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [_utf8_safe(item) for item in value]
    if isinstance(value, dict):
        return {_utf8_safe(key): _utf8_safe(item) for key, item in value.items()}
    return value


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

    These kinds of partial data must not break the tool response: each such
    item is dropped and the rest kept. A value of the wrong JSON type, such as
    a numeric ``created`` or an ``author`` that isn't an object, fails the whole
    response through :func:`_reading_body`.

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

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **ADF `table` support** — tables render as GitHub-flavored pipe tables (closes the gap noted in 0.2.0). A first row of all `tableHeader` cells becomes the header; otherwise a blank header is synthesized and no data row is promoted. `tableHeader` cells elsewhere (row-scoped / first-column headers) render **bold**. Cell content is flattened onto one line (`<br>` for line breaks, `\|`-escaped pipes); embedded media still resolves to `[attachment: filename (id=…)]`. `colspan` / `rowspan` merges are preserved positionally via empty filler cells; oversized spans clamp (`colspan` to 20, `rowspan` to the table's content length) instead of collapsing to 1, which would slide every later cell out of its column. Ragged rows size the header row, not every row — GFM fills short body rows in itself, and padding them all would make the emitted cell count `rows x widest row`. Presentation attrs (`layout`, `width`, `isNumberColumnEnabled`, `colwidth`, `background`, `localId`) are ignored — rationale in `_render_table`.

## [0.2.0] - 2026-05-23

First tagged release. Consolidates the initial development cycle — scaffolding, the composable 3-tool architecture, and the v3 Smart Checklist parser — with the new attachment-handling tool and its supporting metadata.

### Added
- **`get_ticket_attachment(issue_key, attachment_id)`** — fetches an attachment as a native MCP payload (image / PDF / text); other mimes return an explanatory error with the original `content_url`. Size cap via `JIRA_ATTACHMENT_MAX_MB` (default 20 MB), enforced before download and via streaming guard mid-flight. See README for the full mime dispatch table.
- **`## Attachments` section in `get_ticket_content`** — lists every file on the ticket (id, filename, mime, size, date, author) with an `embedded` / `orphan` marker indicating whether the file is referenced from the description prose. The id is the action key for `get_ticket_attachment`.
- **Embedded media nodes inside ADF descriptions** are resolved against the attachment list and rendered as `[attachment: filename (id=...)]` instead of bare `[image]`. Resolution matches by `attrs.alt` (Jira filename) first, then `attrs.id`; UUID-disambiguated filenames are normalised before lookup. Orphan media (no match) falls back to `[image]`.
- **Composable 3-tool architecture**: `get_issue_tree`, `get_ticket_content`, `get_smart_checklist`. Each answers one question (hierarchy / single-ticket content / Smart Checklist only). See README for parameters, defaults, and example output.
- **ADF → markdown converter** covers paragraphs, headings (with `heading_offset` to nest under the surrounding hierarchy), lists, code, blockquotes, marks, hard breaks, mentions, emoji, inline cards (URL extraction), media (resolved to `[attachment: ...]` when possible), and rules. `panel` and `table` still render as `[unsupported: <type>]`.
- **Smart Checklist parser** supports both formats: legacy task-list (`[ ]`/`[x]`/`[-]`/`[~]`) and modern v3+ bullet-list (`- text` items under `## section` headers). v3+ items default to `status='open'` — see README Known limitations.
- **Smart Checklist headers carry an item count** — `(23 items)` per-node and standalone. Switches to `(N/M done)` when legacy task-list markers show progress.
- **New DTOs**: `Attachment`, `Comment`, `Ticket`, `Checklist`, `ChecklistSection`, `ChecklistItem`, `TreeNode` — frozen pydantic models. `Ticket.attachments` defaults to an empty tuple for source-compatibility.
- **New exception** `JiraAttachmentTooLargeError` (subclass of `JiraError`) carrying `size` and `max_bytes` for actionable error messages.
- **`JIRA_ATTACHMENT_MAX_MB` setting** (default `20`, validated `gt=0` — misconfigured `0` / negative values fail loudly at startup).
- **Initial project scaffolding** — FastMCP server, async httpx client with retry/backoff, ADF parser, hierarchy walker, pydantic-settings config, and a structured `JiraError` exception hierarchy. See README Project layout for file-level breakdown.

### Changed
- **Smart Checklist rendering preserves section grouping** (`## 1. Section`, …) instead of a flat list. Section headers nest under the surrounding hierarchy (`##` standalone, `####` inside `get_ticket_content`). Empty sections are skipped.
- **`Checklist` model** is now `Checklist(sections: list[ChecklistSection])`; the flat `Checklist.items` accessor is preserved as a property for backward compatibility.
- **Per-node Smart Checklist section** in `get_ticket_content` is omitted entirely for tickets without a checklist, instead of rendering `_(no checklist)_` placeholders.
- **Internal:** `server.py` extracts `_format_jira_errors` — single dispatch helper replacing per-tool `except*` chains. Tool bodies ~50 → ~25 lines.
- **Internal:** `get_ticket_attachment` lifts `download_attachment` out of per-mime branches; unsupported mimes short-circuit before the network call.
- **Internal:** `adf_to_markdown`'s `attachments` parameter typed `Sequence[Attachment] | None` (was `Iterable`) — lookup tables are built in two passes, a generator would be exhausted.

### Removed
- **`get_ticket_context`** removed entirely, replaced by the composable 3-tool architecture above. No consumers to migrate (project was unreleased).

### Fixed
- **Smart Checklist parser** now handles the modern v3+ bullet-list format. Previously only the legacy task-list (`[ ] text`) was recognised, so v3 tickets reported as "empty checklist" even when items existed.
- **ADF heading levels** are now shifted via `heading_offset` when the walker is invoked from the ticket-content renderer (`heading_offset=3` for descriptions/comments). Previously user-authored `# Story` inside descriptions leaked above the document title or collided with the `## PROJ-XXXX` header.

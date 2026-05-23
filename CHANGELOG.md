# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-23

### Added
- **Attachments support — `get_ticket_attachment` (4th MCP tool) plus attachment metadata in `get_ticket_content`.** When an LLM needs the actual contents of a design mockup, PDF spec, or log file uploaded to a ticket (rather than just the description text), it can now reach them through the same composable contract as the other tools.
  - `get_ticket_content` now renders a `## Attachments` section listing every file on the ticket with its id, filename, mime type, size, creation date, author, and an `embedded` / `orphan` marker. The id is the action key — the LLM passes it to the next tool to fetch bytes. The marker tells the LLM whether the file is referenced from inside the description prose (`embedded` — part of the narrative, high priority) or merely attached without mention (`orphan` — typically post-review revisions or supplementary material).
  - Embedded media nodes inside the description (ADF `mediaSingle` / `mediaGroup` / `media` / `mediaInline`) are now resolved against the attachment list and rendered as `[attachment: filename (id=...)]`. Resolution is primarily by **filename** (ADF carries the Atlassian Media Services UUID in `attrs.id` — which is *not* the Jira attachment id — but Jira fills `attrs.alt` with the original filename). When `alt` is missing, the resolver falls back to direct `attrs.id`-vs-Jira-id matching. The emitted placeholder always carries the Jira numeric id — the actionable key for `get_ticket_attachment`. Filenames that Jira has disambiguated with a UUID v4 suffix (e.g. `"foo (c56f4a5c-...).png"` for duplicates) are normalised back to their original form (`"foo.png"`) before lookup, so the match still recovers. Orphan media (no matchable filename or id) falls back to the original `[image]` placeholder.
  - `get_ticket_attachment(issue_key, attachment_id)` returns the file as a native MCP payload: `image/*` → `ImageContent`, `application/pdf` → embedded resource (`BlobResourceContents`), `text/*` → markdown string with a small header. Anything else returns an explanatory error pointing at the original `content_url` for manual download.
  - Size cap is configurable via `JIRA_ATTACHMENT_MAX_MB` env var (default `20`). Files exceeding the cap are refused before any bytes are pulled when `attachment.size` is known; otherwise an early `Content-Length` check and a streaming guard abort the download mid-flight, so the server never buffers an oversized payload.
- New `Attachment` frozen DTO (`models.Attachment`); `Ticket.attachments` defaults to an empty tuple so all existing call sites stay source-compatible.
- New `JiraAttachmentTooLargeError` exception (subclass of `JiraError`) carrying `size` and `max_bytes` so the MCP layer can format actionable user-facing messages.
- `JIRA_ATTACHMENT_MAX_MB` setting on `Settings` (default `20`, validated `gt=0` so configuration mistakes — `0`, negative — fail loudly at startup rather than silently breaking every download).

### Changed (internal)
- Server.py was refactored to extract `_format_jira_errors` — a single dispatch helper that replaces the four near-identical per-tool `except*` chains. Tool bodies are now ~25 lines each (down from ~50) and error wording stays in lock-step across tools.
- `get_ticket_attachment` dispatch lifts the `download_attachment` call out of the three per-mime branches; the unsupported-mime path now short-circuits *before* hitting the network instead of after.
- ADF media node resolution: `adf_to_markdown`'s `attachments` parameter is now typed `Sequence[Attachment] | None` (not `Iterable`) because the lookup tables are built in two passes — a single-shot generator would be exhausted after the first.

### Changed (breaking)
- **Replaced the monolithic `get_ticket_context` with a 3-tool composable architecture.** The server now exposes `get_issue_tree`, `get_ticket_content`, and `get_smart_checklist` — each answering a single, focused question. `get_ticket_context` is removed entirely (no consumers to migrate; the project was unreleased).
  - `get_issue_tree(issue_key, depth_up=10, depth_down=2)` — walks UP from the focus to the topmost reachable ancestor, then BFS DOWN expanding every node up to `depth_down` levels (clamped to 3) plus the spine to the focus regardless of depth. Lite per-ticket info (key, type, summary, status); the focus ticket carries a 🎯 + ⬅️ FOCUS marker. Output also carries an Overview aggregate with counts by type and status.
  - `get_ticket_content(issue_key, include_comments=False)` — single ticket detail: description, Smart Checklist (when items exist), optional comments. Description and checklist fetched in parallel under one TaskGroup.
  - `get_smart_checklist(issue_key)` — unchanged, still the leanest option when the LLM needs only ACCs.

### Added
- Initial project scaffolding.
- `get_smart_checklist(issue_key)` MCP tool.

### Fixed
- Smart Checklist parser now handles the modern Smart Checklist v3+ bullet-list format (`- text` items grouped under `## section` headers). Previously only the legacy task-list format (`[ ] text`) was recognised, so tickets using the v3 format reported as "empty checklist" even when items existed. Items in the bullet form default to `status="open"` because per-item status lives in sibling Jira properties (`SmartChecklist`, `ItemStatusSearchMeta`); legacy `[ ]/[x]/[-]/[~]` markers continue to be honored when present.
- ADF heading levels are now shifted via a `heading_offset` parameter when the walker is invoked from the ticket-context renderer (`heading_offset=3` for descriptions and comment bodies). Previously user-authored headings inside ticket descriptions ("# Story", "## Goal") leaked above the document title or collided with the per-node `## PROJ-XXXX` header, breaking the markdown hierarchy. With the offset, "# Story" becomes "#### Story" and nests correctly under `### Description`.

### Added
- ADF nodes for media (`mediaSingle`, `mediaGroup`, `media`, `mediaInline`) now render as `[image]` placeholders instead of `[unsupported: ...]` — clearer signal to the reader that an attachment was here. `inlineCard` now surfaces its URL (LLM- and human-friendly), and `rule` renders as `---`.
- Smart Checklist headers now show an item count: `### Smart Checklist (23 items)` per-node, `# Smart Checklist: KEY (23 items)` standalone. When some items are marked done (legacy task-list format), the header switches to `(N/M done)` to show progress at a glance.

### Changed
- Smart Checklist rendering now preserves section grouping (e.g. `## 1. Comment Section`, `## 2. Add comments`). Previously items were flattened into a single list and section headers were discarded. The new layout improves both LLM comprehension (related items are grouped semantically) and human readability when contexts are reviewed manually. Section headers nest under the surrounding document hierarchy: `##` in the standalone `get_smart_checklist` output, `####` in the per-node Smart Checklist section of `get_ticket_context`. Empty sections (header with no items) are skipped to avoid visual noise.
- Model: `Checklist` is now `Checklist(sections: list[ChecklistSection])`; the flat `Checklist.items` accessor is preserved as a property for backward compatibility.
- Per-node Smart Checklist section in `get_ticket_context` is now omitted entirely for tickets without a checklist (plugin not installed or zero items), instead of rendering placeholders like `_(no checklist)_` / `_(empty checklist)_`. The placeholders were visual noise — every ticket got the section regardless of whether it carried any acceptance criteria. Tickets that genuinely have ACCs still render the section as before.

### Known limitations
- Comment fetching is capped at 100 per ticket (first page only). A WARN is logged on stderr when this limit is hit.

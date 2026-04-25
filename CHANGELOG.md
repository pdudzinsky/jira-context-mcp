# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

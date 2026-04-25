# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial project scaffolding.
- `get_smart_checklist(issue_key)` MCP tool — fetches only the Smart Checklist for a single ticket, without the hierarchy walk. Useful as a token-efficient companion to `get_ticket_context` when only the ACCs are needed.

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

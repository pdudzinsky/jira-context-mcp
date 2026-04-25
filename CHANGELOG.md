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

### Changed
- Smart Checklist rendering now preserves section grouping (e.g. `## 1. Comment Section`, `## 2. Add comments`). Previously items were flattened into a single list and section headers were discarded. The new layout improves both LLM comprehension (related items are grouped semantically) and human readability when contexts are reviewed manually. Section headers nest under the surrounding document hierarchy: `##` in the standalone `get_smart_checklist` output, `####` in the per-node Smart Checklist section of `get_ticket_context`. Empty sections (header with no items) are skipped to avoid visual noise.
- Model: `Checklist` is now `Checklist(sections: list[ChecklistSection])`; the flat `Checklist.items` accessor is preserved as a property for backward compatibility.
- Per-node Smart Checklist section in `get_ticket_context` is now omitted entirely for tickets without a checklist (plugin not installed or zero items), instead of rendering placeholders like `_(no checklist)_` / `_(empty checklist)_`. The placeholders were visual noise — every ticket got the section regardless of whether it carried any acceptance criteria. Tickets that genuinely have ACCs still render the section as before.

### Known limitations
- Comment fetching is capped at 100 per ticket (first page only). A WARN is logged on stderr when this limit is hit.

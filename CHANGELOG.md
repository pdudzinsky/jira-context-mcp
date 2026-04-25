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

### Known limitations
- Comment fetching is capped at 100 per ticket (first page only). A WARN is logged on stderr when this limit is hit.

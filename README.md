<p align="center">
  <img src=".github/assets/banner.png" alt="jira-context-mcp" width="100%">
</p>

# jira-context-mcp

[![CI](https://github.com/pdudzinsky/jira-context-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/pdudzinsky/jira-context-mcp/actions/workflows/ci.yml)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

Pull rich Jira ticket context into your LLM during development. Four composable MCP tools — one for the surrounding hierarchy, one for a single ticket's full content, one for just the Smart Checklist (ACCs), one for fetching an attachment as a native MCP image / PDF / text payload — render Jira data as structured markdown. Read-only by design, built for developers who read tickets, not manage them.

## The four tools at a glance

| Tool | Question it answers | Output |
|---|---|---|
| `get_issue_tree` | _What's around this ticket?_ | Hierarchy with focus marker — root → leaves, lite info per ticket, status overview |
| `get_ticket_content` | _What's in this specific ticket?_ | Full description + Smart Checklist + attachment list + optional comments — single ticket only |
| `get_smart_checklist` | _Just the ACCs._ | Just the Smart Checklist — token-efficient when nothing else is needed |
| `get_ticket_attachment` | _Give me that file._ | Native MCP image / PDF / text payload for a single attachment, by id |

A typical workflow has the LLM call `get_issue_tree` first to discover structure, then drill into specific tickets with `get_ticket_content`. When the description references an attached design / spec / log, the model can pull just that file via `get_ticket_attachment` instead of guessing. Each tool does one thing; they compose.

## What you get

### `get_issue_tree` example

Given any ticket — leaf, mid, or root — the tool walks **upward** to the topmost ancestor and then **downward** from there, building a tree centered on your ticket. Sample (simplified) output:

````markdown
# Issue tree: PROJ-1234

## Overview

Total: 27 tickets · By type: 1 Epic, 5 Story, 21 Subtask
By status: 24 Done, 2 In QA, 1 Rejected

## Tree

```
PROJ-100 · [Epic] Refactor billing module · In Progress
├── PROJ-239 · [Story] Payment retry logic · Done
│   ├── PROJ-1230 · [Subtask] [BE] Retry policy · Done
│   └── PROJ-1231 · [Subtask] [BE] Idempotency keys · Done
├── PROJ-240 · [Story] Extract invoice generation · In Progress
│   ├── PROJ-1233 · [Subtask] [BE] Extract CSV export · Done
│   ├── 🎯 PROJ-1234 · [Subtask] [BE] Add PDF template · In Progress ⬅️ FOCUS
│   └── PROJ-1235 · [Subtask] [BE] Add XML export · To Do
└── PROJ-260 · [Story] Email notifications · To Do
```
````

Notes:
- **Root** is at the top, no marker — its position alone distinguishes it.
- **Path nodes** (the spine from root to focus) get expanded regardless of `depth_down`. Other nodes expand only up to `depth_down` levels — protects against runaway trees on huge epics.
- **Focus** marker (`🎯` + `⬅️ FOCUS`) lands on the ticket you asked about, wherever it sits in the hierarchy.
- **JQL response order** is preserved (no alphabetical re-sort) — matches what you see in Jira UI.
- **Lite per-ticket info** (key, type, summary, status) keeps the output scannable. Use `get_ticket_content` for full descriptions and ACCs.

### `get_ticket_content` example

Full content of a single ticket — description, Smart Checklist (when present), attachment list (when any), optional comments. No hierarchy walk, no peers.

````markdown
# PROJ-240 · [Story] Extract invoice generation
**Status:** In Progress · **Assignee:** Piotr D. · **URL:** https://your-org.atlassian.net/browse/PROJ-240

## Description
Pull invoice logic out of BillingService into a new InvoiceService.
The endpoint contract is in the attached PDF — see [attachment: invoice-api.pdf (id=12348)].

## Smart Checklist (1/3 done)
### 1. Service alignment
- [x] Service boundary alignment reviewed
- [-] Migration plan drafted
- [ ] Rollout communication to support

## Attachments
- [12347] mockup-invoice-pdf.png · image/png · 412 KB · 2026-05-12 by Piotr D. · embedded
- [12348] invoice-api.pdf · application/pdf · 2.4 MB · 2026-05-12 by Piotr D. · embedded
- [12349] revised-mockup-v2.png · image/png · 480 KB · 2026-05-16 by Anna K. · orphan

## Comments  (only when include_comments=True)
**2026-04-22 14:05, Piotr D.:**
> Started profiling. 80% in CSV writer.
````

Notes:
- **Attachments section** is omitted entirely when the ticket has none — no `_(no attachments)_` noise.
- **Embedded media** inside the description (e.g. a pasted screenshot) renders as `[attachment: filename (id=...)]` when the file is also in the attachment list. The id is the same key the model needs for `get_ticket_attachment`.
- **`embedded` vs `orphan`** marker per attachment: `embedded` means the file is referenced from inside the description body (someone pasted it into the prose); `orphan` means the file is attached to the ticket but never mentioned. Orphan attachments are typically post-review revisions or supplementary material the author chose not to inline — the LLM treats them as lower-priority context unless something in the prose redirects to them.

### `get_smart_checklist` example

````markdown
# Smart Checklist: PROJ-240 (1/3 done)

## 1. Service alignment

- [x] Service boundary alignment reviewed
- [-] Migration plan drafted
- [ ] Rollout communication to support
````

### `get_ticket_attachment` example

Unlike the other three tools, this one returns a **native MCP content payload** rather than markdown. The model decides which attachment to pull based on the `## Attachments` list it saw via `get_ticket_content`, then calls this tool with the corresponding id. Dispatch is by mime type:

| Mime | Returned as | What the LLM sees |
|---|---|---|
| `image/*` | `fastmcp.Image` (MCP `ImageContent`) | The image, natively (Claude reads PNG/JPG/GIF/WebP/SVG/BMP) |
| `application/pdf` | `fastmcp.File` (MCP `EmbeddedResource`) | The PDF, natively (Claude reads pages of text + figures) |
| `text/*` | `str` (MCP `TextContent`) | File contents under a `# Attachment: filename (mime)` header — readable in any client |
| Anything else | `Error: unsupported mime …` string | Pointer to `content_url` for manual download |

Size cap: `JIRA_ATTACHMENT_MAX_MB` env var (default `20`). Files larger than the cap return an `Error: attachment too large (X MB > limit Y MB). Download manually from <url>` string — the tool refuses to pull bytes that would blow the model's context window.

## Why this and not Atlassian's official MCP?

Atlassian ships an official [Rovo MCP Server](https://www.atlassian.com/platform/remote-mcp-server) covering Jira, Confluence, Compass, and Bitbucket. It supports read **and** write actions, uses OAuth 2.1, and is administered at the organisation level. For broad team-wide automation it's the right default — start there if your use case overlaps.

`jira-context-mcp` exists for a narrower slot: **reading tickets while writing code**, with the LLM as the primary consumer. Concrete differences against [Rovo's supported tool list](https://support.atlassian.com/atlassian-rovo-mcp-server/docs/supported-tools/) (verified 2026-05 — Atlassian's API moves, re-check if reading much later):

- **Smart Checklist (Railsware) plugin support.** The plugin stores acceptance criteria in an issue property (`com.railsware.SmartChecklist.checklist`) that Rovo's tools don't surface. On Atlassian Cloud teams that use it as the canonical ACC location (with descriptions like "See ACCs"), `get_smart_checklist` is often the single most useful tool — Rovo has no equivalent.
- **Attachment fetching as a native MCP payload.** `get_ticket_attachment` returns native MCP image / PDF / text payloads, so the LLM reads designs, PDF specs, and logs directly in-context. Rovo's tool list has no attachment download.
- **Multi-level issue hierarchy walk in one call.** `get_issue_tree` traverses parents and children across multiple levels, builds a tree centred on the focus, and renders a status aggregate. Rovo's `getJiraIssue` exposes one ticket at a time including its `parent` field — building the same tree requires multiple calls plus client-side rendering.
- **Read-only by design.** No `create`, `edit`, or `transition` tools — the API surface can't mutate Jira state, so the LLM can't accidentally close a ticket or edit a description.
- **Local stdio + API token install.** No OAuth flow, no organisation admin involvement, no managed service. Three env vars, one config block, restart the client. Fits a "personal dev assistant" workflow rather than team-wide automation.
- **Markdown output, not raw fields.** Tickets render as headings + lists + structured attachment metadata (`[id, filename, mime, size, embedded/orphan]`) — smaller token footprint, and the model can quote and reason over them without parsing nested fields.

Rule of thumb: **pick Rovo** for write actions or multi-product team workflows; **pick this one** for read-heavy dev workflows, Smart Checklist teams, and attachment-aware ticket context.

## Prerequisites

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (`brew install uv` on macOS, [other platforms](https://docs.astral.sh/uv/getting-started/installation/))
- An Atlassian Cloud instance (Jira Server / Data Center are not supported)
- An [Atlassian API token](https://id.atlassian.com/manage-profile/security/api-tokens)

## Install

```bash
git clone https://github.com/pdudzinsky/jira-context-mcp.git
cd jira-context-mcp
uv sync
```

`uv sync` creates `.venv/` and installs everything from the committed `uv.lock`. No activation needed — the launchers below use `uv run` which handles it.

## Configure your MCP client

The server needs three required environment variables — `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` — plus a handful of optional ones. Provide them either inline in the MCP client config (recommended) or via a `.env` file in the repo (see `.env.example`).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `JIRA_BASE_URL` | yes | — | e.g. `https://your-org.atlassian.net` |
| `JIRA_EMAIL` | yes | — | The account that owns the API token |
| `JIRA_API_TOKEN` | yes | — | [Create one here](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `REQUEST_TIMEOUT` | no | `30.0` | Per-request HTTP timeout in seconds |
| `MAX_RETRIES` | no | `3` | Additional attempts on transient failures (429, 5xx, network errors) |
| `JIRA_ATTACHMENT_MAX_MB` | no | `20` | Hard cap on attachment download size. `get_ticket_attachment` refuses larger files and returns an `Error: attachment too large …` string with the original `content_url` |

### Claude Desktop (macOS)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jira-context-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/jira-context-mcp",
        "python",
        "-m",
        "jira_context_mcp"
      ],
      "env": {
        "JIRA_BASE_URL": "https://your-org.atlassian.net",
        "JIRA_EMAIL": "you@example.com",
        "JIRA_API_TOKEN": "ATATT..."
      }
    }
  }
}
```

> **Heads-up on tokens:** Atlassian API tokens are ~192 characters of `base64`-ish goo. If you paste one and your line wraps in the JSON editor, whitespace can sneak into the middle of the string — Jira will then silently return 404 on private projects. Paste carefully, or strip the value through `tr -d '[:space:]'` before saving.

Quit Claude Desktop fully (`Cmd+Q`, not just close the window) and reopen. The four tools should appear in the available-tools list.

### Other MCP clients

Any client that supports stdio-based MCP servers works the same way — point `command` at `uv run ... python -m jira_context_mcp` and provide the three env vars. Cursor's `.cursor/mcp.json`, Zed's `settings.json`, and `fastmcp dev` all use the same shape.

### Local `.env` alternative

```bash
cp .env.example .env
$EDITOR .env
```

`.env` is git-ignored. It's loaded when the process is launched with `--directory` pointing at the repo root.

## Usage

### Tool parameters

**`get_issue_tree`**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `issue_key` | string | required | Any ticket — leaf, mid, or root. |
| `depth_up` | int | `10` | Max levels to walk upward toward the root. Real hierarchies are 2–4 deep. |
| `depth_down` | int | `2` | Max levels to expand below the root. Hard-capped at 3 to prevent runaway expansion on epics with hundreds of descendants. The path to the focus is always shown regardless. |

**`get_ticket_content`**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `issue_key` | string | required | Single ticket. |
| `include_comments` | bool | `false` | Comments are noisy and token-heavy — opt in when needed. |

**`get_smart_checklist`**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `issue_key` | string | required | Single ticket. |

**`get_ticket_attachment`**

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `issue_key` | string | required | Ticket the attachment belongs to. |
| `attachment_id` | string | required | The numeric id from the `## Attachments` section of `get_ticket_content` (e.g. `"12345"`). |

### Workflow recipes

Prompt templates for typical developer workflows. Paste any of these into Claude (or any MCP-capable LLM) along with the relevant ticket key — they **force the model to call the right tools in the right order** instead of guessing which one fits the question. Each recipe maps a real situation to a concrete tool sequence.

#### Picking up a ticket from the sprint

You took a leaf ticket (Subtask / Task) and the description is sparse — ACCs likely live on the parent Story. This recipe walks the hierarchy upward and pulls full content for every node above the focus.

> I'm picking up **PROJ-1234** to work on. Use `jira-context-mcp` to:
>
> 1. Call `get_issue_tree(issue_key="PROJ-1234")` to see where this ticket sits in the hierarchy.
> 2. Call `get_ticket_content(issue_key="PROJ-1234")` for my ticket's full detail.
> 3. For each path node above the focus (Story, Epic), call `get_ticket_content` so I see their descriptions and ACCs. The Story usually carries the actual acceptance criteria.
> 4. Summarize: what's the parent goal, what ACCs apply to my work, and what broader context should I keep in mind?

> **Tip:** if a path node returns `Smart Checklist on KEY: not present`, skip it and check the next ancestor. ACCs sometimes hop a level.

#### Sprint planning — overview of an Epic

Use this to assess scope and progress without pulling per-ticket descriptions for every Story.

> Give me an overview of **PROJ-100** for sprint planning. Use `get_issue_tree(issue_key="PROJ-100", depth_down=2)`.
>
> Then summarize from the Overview block and tree only:
>
> - How many Stories under this Epic, statuses breakdown
> - Which Stories look stalled (In Progress / In QA without subtask completion)
> - Breakdown by BE / FE / QA based on `[BE]` / `[FE]` / `[QA]` markers in subtask titles
> - What's ready to release vs what needs follow-up
>
> Do **not** pull individual ticket descriptions unless I ask — overview only. If I ask follow-up questions about a specific Story, then call `get_ticket_content`.

#### Code review — verifying ACCs are addressed

Useful right before approving a PR linked to a Story or Subtask.

> I'm reviewing a PR linked to **PROJ-1234**. Use `jira-context-mcp` to:
>
> 1. Call `get_issue_tree(issue_key="PROJ-1234")` to find the parent Story (path node above the focus).
> 2. Call `get_smart_checklist(issue_key="<that parent Story>")` — those are the ACCs the PR should satisfy.
> 3. After I paste the diff, walk through each ACC and tell me which ones the diff likely addresses, which look untouched, and which are ambiguous.
>
> Be skeptical — if an ACC says "comment section must be hidable by the user" and the diff has no UI changes, flag it as untouched.

#### Pulling a design / spec attached to the ticket

When the ticket description is sparse and points at an attached mockup or PDF spec ("implement per the design"), this sequence puts the file directly in front of the model.

> I'm picking up **PROJ-1234** and the description says to follow the attached design. Use `jira-context-mcp` to:
>
> 1. Call `get_ticket_content(issue_key="PROJ-1234")` and read the `## Attachments` section.
> 2. Pick the most relevant file (usually a `.png` mockup or `.pdf` spec). Note its id.
> 3. Call `get_ticket_attachment(issue_key="PROJ-1234", attachment_id="<id>")` — the file will arrive as a native image / PDF you can inspect.
> 4. Describe what's in the file, then map it to the description / ACCs.
>
> Don't pull every attachment by default — most tickets have noisy extras (logs, old screenshots). Pick the one mentioned in the description, or ask me which one if it's unclear.

#### Stand-up prep — multiple tickets at a glance

Run this just before daily / weekly to get a quick status sweep across whatever you're juggling.

> Quick status check on what I'm working on. For each of **[PROJ-1234, PROJ-1235, PROJ-1236]**:
>
> - Call `get_ticket_content(issue_key=..., include_comments=False)`
> - Give me one line: ticket key, status, what the description is asking for in 10–15 words
>
> Then call `get_issue_tree(issue_key="PROJ-1234")` once and tell me if anything **else** under that Epic looks blocked or stalled — I want to know if my work has dependencies I missed.

### Errors

All errors come back as the tool response (a string starting with `Error:`) rather than exceptions:

- `Error: missing required environment variable(s): ...` — credentials not provided
- `Error: invalid Jira configuration — ...` — env vars are set but malformed (e.g. base URL isn't a valid URL)
- `Error: Jira authentication failed. ...` — wrong email/token (or whitespace polluting the token)
- `Error: ticket(s) not found in Jira: PROJ-1234` — typo, deleted, or your token lacks access
- `Error: Jira rate limit exceeded after retries. ...` — back off and retry
- `Error: invalid depth parameter. ...` — `depth_up` was `< 1`
- `Error: hierarchy cycle detected. ...` — parent link loop in Jira (shouldn't happen, but defensive)
- `Error: attachment 'X' not found on KEY. Available ids: ...` — wrong id; the message lists what's actually on the ticket
- `Error: attachment too large (X MB > limit Y MB). Download manually from <url>` — exceeds `JIRA_ATTACHMENT_MAX_MB`
- `Error: unsupported mime type 'X'. Tool supports image/*, application/pdf, text/*. ...` — e.g. `.docx` / `.zip` / video; the message includes the original URL for manual download
- `Error: attachment exceeds size cap — ...` — the file's `Content-Length` (or accumulated stream bytes) crossed the cap mid-download

### From the shell (no MCP client needed)

Each tool is an async Python function — call it directly with `uv run` for ad-hoc reads, debugging, or piping into other tools. All four snippets support standard shell redirects; the first three (markdown output) also pipe cleanly into `| glow -`.

```bash
# Tree overview
uv run python -c "
import asyncio
from jira_context_mcp.server import get_issue_tree
print(asyncio.run(get_issue_tree(issue_key='PROJ-1234')))
"

# Single ticket full content (add include_comments=True for the comment thread)
uv run python -c "
import asyncio
from jira_context_mcp.server import get_ticket_content
print(asyncio.run(get_ticket_content(issue_key='PROJ-1234')))
"

# Just the ACCs (cheapest call; doubles as a connectivity check)
uv run python -c "
import asyncio
from jira_context_mcp.server import get_smart_checklist
print(asyncio.run(get_smart_checklist(issue_key='PROJ-1234')))
"

# A single attachment (image / PDF / text). Stdio doesn't render images,
# so this is mainly useful as a debug call — pipe binary output to a file.
uv run python -c "
import asyncio
from jira_context_mcp.server import get_ticket_attachment
result = asyncio.run(get_ticket_attachment(issue_key='PROJ-1234', attachment_id='12345'))
# Result is fastmcp Image / File / str depending on the file type.
if hasattr(result, 'data'):
    import sys; sys.stdout.buffer.write(result.data)
else:
    print(result)
" > out.bin
```

## Known limitations

- **Comments:** capped at 100 per ticket. If a ticket has more, a WARN is logged to stderr and the first 100 are returned.
- **Jira Cloud only.** No Jira Server / Data Center support.
- **Smart Checklist progress:** v3+ bullet-list checklists display as `(N items)` even when some items are marked done in Jira UI. Legacy `[x]` / `[-]` / `[~]` markers display correctly as `(N/M done)`. Accurate v3+ progress is on the roadmap.
- **`depth_down` is capped at 3.** Asking for more is silently clamped. The focus ticket and its direct ancestors are always reachable in the tree, even when the focus sits below `depth_down` levels (the spine is always expanded).
- **ADF coverage:** `panel` and `table` nodes render as `[unsupported: <type>]` — add a handler in `src/jira_context_mcp/adf.py` if you need them. Full list of supported types in CHANGELOG.
- **Attachment fetch is per-call, not cached.** Every `get_ticket_attachment` call re-fetches the ticket metadata plus the file. For typical "look at one design and move on" flows that's fine; for repeated reads of the same file, expect the latency cost.
- **Attachment mime support is curated.** `image/*`, `application/pdf`, and `text/*` round-trip through native MCP content types; anything else returns an `Error: unsupported mime …` with the original `content_url`. The model can suggest a manual download, but `.docx` / `.xlsx` / `.zip` / video won't reach the conversation directly.
- **Attachment download is not retried.** Unlike the main JSON-API calls, attachment streams skip the retry loop — sporadic 5xx on the download path surface as a single `Error: Jira request failed`. Re-run the tool call if you suspect a transient failure.

## Development

```bash
git clone https://github.com/pdudzinsky/jira-context-mcp.git
cd jira-context-mcp
uv sync
cp .env.example .env  # then edit
uv run python -m jira_context_mcp  # stdio server — blocks waiting for MCP handshake
```

Run the test suite, linter, and type checker:

```bash
uv run pytest                       # 234 tests, ~1.8s
uv run ruff check src tests         # lint
uv run ruff format src tests        # format (auto-applies)
uv run ruff format --check src tests  # format check (CI-style)
uv run mypy                         # type check
```

Optional: enable pre-commit hooks so the same checks run locally on `git commit`. Install once per clone:

```bash
uv tool install pre-commit          # or pipx, brew, ...
pre-commit install                  # writes .git/hooks/pre-commit
```

Configured hooks (`.pre-commit-config.yaml`): `ruff check --fix`, `ruff format`, `mypy` (on `src/`), plus standard `pre-commit-hooks` (trailing whitespace, end-of-file, YAML/TOML validity, merge conflicts, large files).

Project layout:

```
src/jira_context_mcp/
├── __init__.py
├── __main__.py       # python -m entrypoint
├── server.py         # FastMCP server + 4 tool registrations
├── config.py         # pydantic-settings for env vars
├── models.py         # frozen pydantic DTOs (Ticket, Comment, Checklist, Attachment, TreeNode, ...)
├── jira.py           # async httpx client + retries + checklist parser + attachment download
├── tree.py           # walk-up + walk-down hierarchy builder
├── adf.py            # ADF → markdown converter (with heading_offset + media-id resolution)
└── markdown.py       # final renderers (tree, content, checklist, attachments)
```

## License

[MIT](LICENSE)

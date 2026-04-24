# jira-context-mcp

Pull rich Jira ticket context into your LLM during development. One MCP call returns the full parent hierarchy, siblings, comments, and Smart Checklist ACCs as structured markdown. Read-only by design — built for developers who read tickets, not manage them.

## What you get

Given a single ticket key, the tool walks up the parent chain to the root (Epic/Initiative), and for each ticket on the path fetches description, Smart Checklist, peer tickets (children of the parent), and optionally comments. The entry ticket is visually marked so the LLM knows where you are in the hierarchy.

Simplified sample output for `PROJ-1234`:

````markdown
# Ticket context: PROJ-1234

Path: PROJ-100 → PROJ-240 → **PROJ-1234**

_(Generated with: include_comments=False, max_depth=10)_

---

## PROJ-100 · [Epic] Refactor billing module
**Status:** In Progress · **Assignee:** Alice · **URL:** https://your-org.atlassian.net/browse/PROJ-100

### Description
Decompose the monolithic billing service into domain-aligned services.

### Smart Checklist
- [x] Service boundary alignment reviewed
- [-] Migration plan drafted
- [ ] Rollout communication to support

### Children
_(not fetched — this is the root of the traversed hierarchy)_

---

## PROJ-240 · [Story] Extract invoice generation
**Status:** In Progress · **Assignee:** Piotr D. · **URL:** https://your-org.atlassian.net/browse/PROJ-240

### Description
Pull invoice logic out of BillingService into a new InvoiceService.

### Smart Checklist
_(no checklist)_

### Children (3)
- PROJ-239 · [Story] Payment retry logic · Done
- **→ PROJ-240 · [Story] Extract invoice generation · In Progress**
- PROJ-260 · [Story] Email notifications · To Do

---

## 🎯 PROJ-1234 · [Task] Add PDF template for invoices ⬅️ ENTRY
...
````

## Prerequisites

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (`brew install uv` on macOS, [other platforms](https://docs.astral.sh/uv/getting-started/installation/))
- An Atlassian Cloud instance (Jira Server / Data Center are not supported in v0.1)
- An [Atlassian API token](https://id.atlassian.com/manage-profile/security/api-tokens)

## Install

```bash
git clone https://github.com/pdudzinsky/jira-context-mcp.git
cd jira-context-mcp
uv sync
```

`uv sync` creates `.venv/` and installs everything from the committed `uv.lock`. No activation needed — the launchers below use `uv run` which handles it.

## Configure your MCP client

The server needs three environment variables: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`. Provide them either inline in the MCP client config (recommended) or via a `.env` file in the repo (see `.env.example`).

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

Quit Claude Desktop fully (`Cmd+Q`, not just close the window) and reopen. The tool should appear in the available-tools list.

### Other MCP clients

Any client that supports stdio-based MCP servers works the same way — point `command` at `uv run ... python -m jira_context_mcp` and provide the three env vars. Cursor's `.cursor/mcp.json`, Zed's `settings.json`, and `fastmcp dev` all use the same shape.

### Local `.env` alternative

If you'd rather keep credentials in a file (handy for local dev/scripting), copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
$EDITOR .env
```

`.env` is git-ignored. It's loaded when the process is launched with `--directory` pointing at the repo root (the default for the configs above).

## Usage

Once wired up, just ask your LLM for ticket context:

- _"Show me the context of PROJ-1234"_
- _"Give me full context for PROJ-1234 including comments"_
- _"What's the parent hierarchy of PROJ-1234?"_

The LLM will call `get_ticket_context` with the arguments it inferred. You can also be explicit if the LLM guesses wrong defaults.

### Tool parameters

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `issue_key` | string | required | e.g. `"PROJ-1234"` |
| `include_comments` | bool | `false` | Comments are noisy and token-heavy — opt in when needed. |
| `max_depth` | int | `10` | Safety limit. Real hierarchies are 2–4 levels deep; rarely needs changing. |

### Errors the tool can return

All errors come back as the tool response (a string starting with `Error:`) rather than exceptions, so the LLM can reason about them:

- `Error: missing required environment variable(s): ...` — credentials not provided
- `Error: Jira authentication failed. ...` — wrong email/token
- `Error: ticket(s) not found in Jira: PROJ-1234` — typo, deleted, or your token lacks access
- `Error: Jira rate limit exceeded after retries. ...` — back off and retry
- `Error: invalid max_depth parameter. ...` — `max_depth` was < 1
- `Error: hierarchy cycle detected. ...` — parent link loop in Jira (shouldn't happen, but defensive)

### From the shell (no MCP client needed)

The tool is a plain async Python function — you can call it directly with `uv run` and get the exact same markdown that an LLM would receive. Useful for debugging, ad-hoc reading, piping into other tools, or scripting.

All snippets below assume you're in the repo root and have `.env` filled in (or that `JIRA_*` env vars are exported). Output is whatever the tool returns — either the markdown render or an `Error: ...` line.

**Fetch a single ticket (the common case):**

```bash
uv run python -c "
import asyncio
from jira_context_mcp.server import get_ticket_context
print(asyncio.run(get_ticket_context(issue_key='PROJ-1234')))
"
```

**Include comments:**

```bash
uv run python -c "
import asyncio
from jira_context_mcp.server import get_ticket_context
print(asyncio.run(get_ticket_context(issue_key='PROJ-1234', include_comments=True)))
"
```

**Narrow walk — only the ticket and its direct parent** (handy for sanity-checking parent links without pulling the whole chain):

```bash
uv run python -c "
import asyncio
from jira_context_mcp.server import get_ticket_context
print(asyncio.run(get_ticket_context(issue_key='PROJ-1234', max_depth=2)))
"
```

**Save the render to a file** for later reference or to paste into a PR description:

```bash
uv run python -c "
import asyncio
from jira_context_mcp.server import get_ticket_context
print(asyncio.run(get_ticket_context(issue_key='PROJ-1234', include_comments=True)))
" > PROJ-1234.md
```

**Pipe through a markdown pager** like [`glow`](https://github.com/charmbracelet/glow) for a rendered view in the terminal:

```bash
uv run python -c "
import asyncio
from jira_context_mcp.server import get_ticket_context
print(asyncio.run(get_ticket_context(issue_key='PROJ-1234')))
" | glow -
```

Or plain `less`:

```bash
uv run python -c "..." | less -R
```

**Ad-hoc credentials** (override `.env` for a single call, e.g. testing against a second instance):

```bash
JIRA_BASE_URL=https://other-org.atlassian.net \
JIRA_EMAIL=you@example.com \
JIRA_API_TOKEN=ATATT... \
uv run python -c "
import asyncio
from jira_context_mcp.server import get_ticket_context
print(asyncio.run(get_ticket_context(issue_key='OTHER-42')))
"
```

**Batch several tickets** in one process (reuses the event loop, so it's faster than calling the script N times):

```bash
uv run python -c "
import asyncio
from jira_context_mcp.server import get_ticket_context

async def main():
    for key in ['PROJ-1234', 'PROJ-1235', 'PROJ-2000']:
        print(await get_ticket_context(issue_key=key, include_comments=False))
        print('---\n')

asyncio.run(main())
"
```

**Quick connectivity / auth check** — if creds are wrong you'll get `Error: Jira authentication failed ...`; if OK you'll get `Error: ticket(s) not found in Jira: DEFINITELY-BOGUS-9999`:

```bash
uv run python -c "
import asyncio
from jira_context_mcp.server import get_ticket_context
print(asyncio.run(get_ticket_context(issue_key='DEFINITELY-BOGUS-9999')))
"
```

**Extract just one section** from the output — e.g. grab only the entry ticket's Smart Checklist:

```bash
uv run python -c "
import asyncio
from jira_context_mcp.server import get_ticket_context
print(asyncio.run(get_ticket_context(issue_key='PROJ-1234')))
" | awk '/^## 🎯 /,0' | sed -n '/### Smart Checklist/,/^### /{/^### [^S]/q;p}'
```

**Shell alias** for the common case — add to `~/.zshrc` / `~/.bashrc`:

```bash
jctx() {
  uv run --directory /absolute/path/to/jira-context-mcp python -c "
import asyncio, sys
from jira_context_mcp.server import get_ticket_context
print(asyncio.run(get_ticket_context(issue_key=sys.argv[1], include_comments='$2' == '--comments')))
" "$1" "$2"
}

# usage:
#   jctx PROJ-1234
#   jctx PROJ-1234 --comments
```

## Known limitations

- **Comments:** capped at 100 per ticket. If a ticket has more, a WARN is logged to stderr and the first 100 are returned.
- **Jira Cloud only.** No Jira Server / Data Center support in v0.1.
- **Read-only by design.** No `create_*`, `update_*`, `transition_*` tools — this is intentional.
- **ADF coverage:** the converter handles the common nodes (paragraph, headings, lists, code blocks, blockquote, marks, mentions, emoji). Rarer types (`mediaSingle`, `table`, `panel`, `inlineCard`, `rule`) render as `[unsupported: <type>]` so nothing is silently dropped — add a handler in `src/jira_context_mcp/adf.py` if you need one.

## Development

```bash
git clone https://github.com/pdudzinsky/jira-context-mcp.git
cd jira-context-mcp
uv sync
cp .env.example .env  # then edit
uv run python -m jira_context_mcp  # stdio server — blocks waiting for MCP handshake
```

For ad-hoc tool calls without running the full MCP server, see [From the shell](#from-the-shell-no-mcp-client-needed) above.

Project layout:

```
src/jira_context_mcp/
├── __init__.py
├── __main__.py       # uvx / python -m entrypoint
├── server.py         # FastMCP server + tool registration
├── config.py         # pydantic-settings for env vars
├── models.py         # frozen pydantic DTOs
├── jira.py           # async httpx client + retries + checklist parser
├── context.py        # two-phase hierarchy traversal
├── adf.py            # ADF → markdown converter
└── markdown.py       # final renderer
```

## License

[MIT](LICENSE)

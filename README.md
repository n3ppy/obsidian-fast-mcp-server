# obsidian-fast-mcp-server

An MCP (Model Context Protocol) server that turns an Obsidian vault into a "second brain"
Claude can read and write — from Claude Code on your computer, and from claude.ai and
the Claude iPhone/iPad/Android apps through a self-hosted server.

> **Credit:** this is a fork of [Axel687/mcp_obsiCC_IOS](https://github.com/Axel687/mcp_obsiCC_IOS)
> by axel687 (MIT). The design (Git-synced vault, OAuth for the Claude apps, conflict
> handling, multi-vault support) and most of the code are his. This fork trims it down to
> the vault itself; see [Changes from upstream](#changes-from-upstream).

## Features

- **Notes** — list, read, create, overwrite, append, insert under a specific heading,
  search (substring or regex), delete.
- **Graph of the vault** — overview, tags (frontmatter and inline, nested tags),
  backlinks, outgoing links with resolution, unresolved links, orphan notes.
- **Git sync** — the vault is a Git repo shared with Obsidian Git on your devices; the
  server pulls before working and commits + pushes after every write.
- **PDFs** — extract text from any PDF inside the vault, with optional page ranges.

Every path is resolved relative to the vault root and checked so it cannot escape it.

## Two ways to run it

| | `stdio` (default) | `http` |
| --- | --- | --- |
| Where it runs | Launched by Claude Code on your machine | On its own, e.g. Docker on a VPS |
| Who can use it | Claude Code on that machine | claude.ai, desktop & **mobile apps**, Claude Code anywhere |
| Authentication | None needed (nothing is exposed) | **Mandatory**: OAuth with a password page, plus an optional static token |

The mobile apps can only use remote servers, and connectors are added from claude.ai on
the web — they then show up automatically in the apps.

```
 iPhone / iPad / PC                        GitHub (private)              VPS
┌──────────────────┐   push / pull   ┌──────────────────┐  pull/push  ┌────────────────────┐
│ Obsidian + Git   │ ◀─────────────▶ │   vault repo     │ ◀─────────▶ │ obsidian-mcp (http)│
└──────────────────┘                 └──────────────────┘             │ behind Caddy/HTTPS │
                                                                      └─────────▲──────────┘
 Claude app (iOS) / claude.ai / Claude Code  ── MCP over HTTPS + OAuth ───────────┘
```

## Remote deployment (VPS)

Prerequisites: a VPS with Docker, a domain or subdomain whose DNS points at it, ports
80 and 443 open.

1. **Vault repo.** Create a private GitHub repo for the vault and push your vault to it
   (see "Obsidian side" below).
2. **Git token.** Create a fine-grained GitHub token restricted to that repo, permission
   *Contents: read and write*.
3. **Configure.**
   ```bash
   git clone https://github.com/n3ppy/obsidian-fast-mcp-server.git && cd obsidian-fast-mcp-server
   cp .env.example .env
   nano .env   # domain, public IP, password, vault repo URL, git token
   ```
4. **Start.**
   ```bash
   docker compose up -d --build
   docker compose logs -f obsidian-mcp   # should show "cloned ..." then "serving MCP on ..."
   ```
   Caddy fetches the HTTPS certificate automatically. `https://<domain>/healthz` answers `ok`.
5. **Connect Claude.** On claude.ai (Safari works): *Settings → Connectors → Add custom
   connector*, URL `https://<domain>/mcp`. A page from your server asks for the password;
   after that the connector is available on every device, including the mobile apps.
6. **Claude Code (optional).** Either the same OAuth flow, or with the static token:
   ```bash
   claude mcp add --transport http obsidian https://<domain>/mcp \
     --header "Authorization: Bearer <OBSIDIAN_MCP_TOKEN>"
   ```

Update later with `git pull && docker compose up -d --build`. OAuth tokens are kept in
the `state` volume, so Claude doesn't need to reconnect after a redeploy.

### How authentication works

- claude.ai discovers the server's OAuth endpoints, registers itself, and opens
  `/authorize`. The server shows a password page; on success it hands Claude a one-time
  code exchanged for an access token (1 hour) and a refresh token (30 days, rotated on
  every use).
- Only redirects to claude.ai/claude.com, to chatgpt.com (ChatGPT developer mode) and
  to loopback addresses (Claude Code) are accepted (`OBSIDIAN_MCP_ALLOWED_REDIRECTS` to change).
- After 5 wrong passwords in 15 minutes, the login is locked for 15 minutes.
- At most 50 clients stay registered. When the limit is reached, only clients without a
  live refresh token are dropped, so a flood of registrations cannot log you out.
- Tokens are stored hashed (SHA-256) in `/data/state/oauth.json`. Deleting that file
  logs every client out.
- The Git token reaches git through a credential helper reading the environment: it is
  never written into `.git/config` or the remote URL.

## Obsidian side (iPhone, iPad, PC)

1. Install the community plugin **Git** (by Vinzent) in Obsidian on each device.
2. Authenticate with your GitHub username and a token for the vault repo (on mobile the
   plugin uses its own HTTPS implementation, no SSH).
3. Suggested settings: *Pull on startup* on, *Auto pull interval* 2 min,
   *Commit-and-sync interval* 5 min.
4. First device: push the existing vault. Other devices: *Git: Clone an existing remote
   repo*.

### Keeping conflicts rare

- The server pulls before tool calls (at most every `OBSIDIAN_PULL_INTERVAL` seconds)
  and pushes after each write; a rejected push is rebased and retried.
- If a rebase hits a real conflict (same lines edited on both sides), the server aborts
  the rebase so it stays usable, keeps its commit locally and reports `sync_error`.
  Resolve on a device, push, and the next write or `sync_vault` goes through.
- Best habit: let Claude write to an inbox note or append to sections
  (`append_note`, `insert_under_heading`) rather than rewriting notes you're editing.

## Local mode (Claude Code, stdio)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

Claude Code MCP config (see `config.example.json`):

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "python",
      "args": ["-m", "obsidian_mcp"],
      "env": { "OBSIDIAN_VAULT": "/absolute/path/to/your/Vault" }
    }
  }
}
```

Add `OBSIDIAN_VAULT_REMOTE` (and `OBSIDIAN_GIT_TOKEN` if needed) to use the same Git sync
locally, e.g. from a cloud sandbox.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OBSIDIAN_VAULT` | *(required)* | Absolute path to the vault (cloned there if Git sync is on) |
| `OBSIDIAN_MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `OBSIDIAN_MCP_PUBLIC_URL` | — | HTTP mode: public base URL, e.g. `https://mcp.example.com` |
| `OBSIDIAN_MCP_PASSWORD` | — | HTTP mode: login password (12+ characters) |
| `OBSIDIAN_MCP_TOKEN` | *(unset)* | HTTP mode: optional static bearer token (32+ characters) |
| `OBSIDIAN_MCP_HOST` / `OBSIDIAN_MCP_PORT` | `0.0.0.0` / `8000` | HTTP listen address |
| `OBSIDIAN_MCP_STATE_DIR` | `~/.local/state/obsidian-mcp` | Where OAuth clients and tokens are stored |
| `OBSIDIAN_MCP_ALLOWED_REDIRECTS` | claude.ai, claude.com, chatgpt.com, loopback | Comma-separated OAuth redirect allowlist |
| `OBSIDIAN_VAULT_REMOTE` | *(unset)* | Git URL of the vault repo — enables sync |
| `OBSIDIAN_VAULT_NAME` | `principal` | Name of the main vault (used by `vault`, see below) |
| `OBSIDIAN_EXTRA_VAULTS` | *(unset)* | More vaults: `name=repo;name2=repo2` (a local path instead of a URL means no sync) |
| `OBSIDIAN_VAULTS_DIR` | `vaults` next to `OBSIDIAN_VAULT` | Where extra vaults are cloned (`/data/vaults` in Docker) |
| `OBSIDIAN_GIT_TOKEN` | *(unset)* | Token for HTTPS git auth (e.g. GitHub fine-grained PAT), shared by every vault |
| `OBSIDIAN_PULL_INTERVAL` | `30` | Min seconds between automatic pulls; `0` = every call, negative = never |
| `OBSIDIAN_AUTO_PUSH` | `true` | Commit + push after every write |
| `OBSIDIAN_GIT_NAME` / `OBSIDIAN_GIT_EMAIL` | `Obsidian MCP` / `obsidian-mcp@local` | Commit identity |

## Several vaults

Set `OBSIDIAN_EXTRA_VAULTS` to serve more vaults from the same server, each with its own
Git repo, clone and sync. Every vault tool then takes an optional `vault` argument:

- `list_vaults()` lists them (main one first, note count, whether a `CLAUDE.md` /
  `AGENTS.md` is present). The server instructions tell the AI to ask the user which
  vault to use at the start of a conversation and to pass it to every call.
- Reads without `vault` use the main vault.
- Changes (`write_note`, `append_note`, `insert_under_heading`, `delete_note`) are refused without an explicit `vault` when there is more than one,
  so nothing lands in a vault the user did not pick.
- `sync_vault()` without `vault` syncs them all.

MCP has no session the server could remember the choice in (the protocol is stateless),
so the choice lives in the conversation: the AI repeats `vault` on each call.

## Tools

All vault tools below also accept `vault` (see above).

| Tool | What it does |
| --- | --- |
| `list_vaults()` | The configured vaults, main one first. |
| `vault_overview()` | Note count, folders, top tags, most linked notes, orphans, unresolved links. |
| `list_notes(subdir?)` | List every `.md` file in the vault (or under a subdir). |
| `read_note(path)` | Return a note's full markdown content. |
| `search_notes(query, regex=false, max_results=200)` | Search note bodies. |
| `list_tags(prefix?)` | Tags with their note counts; `prefix` narrows to a tag and its nested tags. |
| `notes_with_tag(tag, include_nested=true)` | Notes carrying a tag. |
| `get_backlinks(path)` | Notes linking to this one, with the line of each link. |
| `get_links(path)` | Outgoing links, resolved to paths; lists links to missing notes. |
| `find_orphans(include_without_outgoing=false)` | Notes nothing links to. |
| `write_note(path, content, overwrite=false)` | Create (or overwrite) a note. |
| `append_note(path, content)` | Append text to a note, creating it if needed. |
| `insert_under_heading(path, heading, content, create_if_missing=true)` | Insert text at the end of a section. |
| `delete_note(path)` | Delete a note (committed, so recoverable from Git history). |
| `read_pdf(path, page_start?, page_end?, joined=true)` | Extract PDF text. |
| `sync_vault(action="both")` | Manually pull/push/both (every vault unless `vault` is given). No-op if Git sync is off. |

Links follow Obsidian's rules: `[[Note]]`, `[[Note#Heading]]`, `[[Note|alias]]`, embeds
`![[...]]` and markdown links to `.md` files; a bare name resolves to the note in the same
folder first, then the shortest path. Tags and links inside code are ignored.

## Development

```bash
pip install -e . pytest
pytest
```

`tests/test_http.py` starts a real server and walks through the OAuth flow claude.ai
uses, against a local Git remote standing in for GitHub.

## Layout

```
src/obsidian_mcp/
  server.py      entrypoint: settings, OAuth wiring, transport
  tools/         the MCP tools (notes, graph, attachments, vaults) and how they run
  notes.py       note operations on disk
  links.py       tags, wikilinks, backlinks, orphans
  pdf.py         PDF text extraction
  sync.py        Git clone / pull / commit / push, with conflict handling
  vault.py       vault selection and path safety
  auth.py        OAuth provider and the password page
  config.py      environment settings
```

## Changes from upstream

Forked from [Axel687/mcp_obsiCC_IOS](https://github.com/Axel687/mcp_obsiCC_IOS) at
`fe0622c` (2026-11-09). Since then:

- Removed the tools unrelated to the vault: X/Twitter (`read_x_*`, `search_x`),
  YouTube (`youtube_transcript`, `youtube_search_videos`), `ecosystem_status` and
  `generate_graph`, with their dependencies (twscrape, youtube-transcript-api,
  requests, matplotlib).
- Split `server.py` into a `tools/` package. The tools Claude sees are unchanged.
- Translated the login page to English.
- Replaced the VPS deploy workflow with a test-only CI on `main` and `dev`, and removed
  the shared Caddy sites setup.
- Client registration no longer evicts a client that is logged in.
- Docker Compose publishes Caddy on one address (`MCP_BIND_IP`) and sets memory limits.

## License

MIT. See [LICENSE](LICENSE). Original work © axel687.

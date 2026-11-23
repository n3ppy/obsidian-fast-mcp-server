# Dashboard UX spec

Status: MVP built, 2026-11-18. Notes marked *Built:* say where the build differs from the draft.

A small web dashboard at `<public url>/dash` showing what Claude has done in the vault,
whether sync is healthy, and what the vault looks like. Read-only for v1. Built with
FastHTML so it can grow into the web app later.

## Goals

1. **Trust:** at a glance, see what the AI read and changed, and when.
2. **Health:** spot a broken sync or a failing login before it costs you a note.
3. **Shape of the vault:** simple counts and trends, for fun and for spotting mess.

Not goals for v1: editing notes, changing settings, teams, semantic search, charts for
their own sake.

## The metrics that matter most

In priority order. If v1 only shipped the first four, it would still be worth having.

| # | Metric | Why it matters | Source |
| --- | --- | --- | --- |
| 1 | **Sync status**: last pull, last push, any `sync_error`, commits waiting to push | A stuck sync is the one failure that silently loses work across devices | `sync.py` |
| 2 | **Recent AI changes**: time, tool, note, result, link to the commit | The trust feature: "what did Claude just do to my vault?" | Activity log + git |
| 3 | **Writes by Claude vs your devices**, per day | Shows how much of the vault the AI is now writing | Git history (author) |
| 4 | **Errors**: failed tool calls by type, last 7 days | Catches broken paths, bad links, permission problems | Activity log |
| 5 | **Reads and writes per day**, split by tool | Usage trend; which tools are actually used | Activity log |
| 6 | **Most-read and most-changed notes** | What the AI relies on; candidates for cleanup or pinning | Activity log + git |
| 7 | **Vault size**: notes, folders, words, attachments, PDFs, over time | Growth trend | Vault scan (daily snapshot) |
| 8 | **Link health**: orphan notes, broken wikilinks, notes without tags | Hygiene, a nudge to tidy | `links.py` |
| 9 | **Connected clients**: claude.ai, Claude Code, last seen | Spot a client you don't recognise; revoke it later | OAuth client store |
| 10 | **Latency**: median and slowest tool calls, pull time | Spot a slow vault or a slow GitHub | Activity log |

Limits worth stating on the page itself:
- Only Claude's reads are counted. Obsidian on the laptop and phone reads files locally,
  so the server never sees those.
- Activity-log stats start on the day the log is switched on. Git-based stats cover the
  whole history.

## Pages

### Login (`/dash/login`)

- GitHub mode: one "Sign in with GitHub" button. The same allowlist
  (`OBSIDIAN_MCP_ALLOWED_USERS`) as the MCP. A non-allowed account sees "This GitHub
  account isn't allowed on this server" and nothing else.
- Password mode: the existing password form style, with the same rate limit as the MCP
  login.
- Session: signed cookie, `HttpOnly`, `Secure`, `SameSite=Lax`, 7-day expiry, separate
  from the MCP's OAuth tokens. Logging into the dashboard never grants MCP access, and the
  reverse is also true.

### Overview (`/dash`)

Top to bottom:

1. **Status strip**: one line, green or red.
   "Synced 2 min ago · pushed 14 min ago · no errors" or
   "Sync failing since 09:12: push rejected (rebase conflict in Inbox/Today.md)".
2. **Stat tiles** (today, with the change vs the 7-day average):
   AI reads · AI writes · device commits · errors.
3. **Activity charts**: last 30 days. *Built:* two charts rather than one, since reads
   outnumber commits by far: Claude's reads per day, and commits per day stacked
   Claude vs devices (from git, so they cover history from before the log).
4. **Recent activity**: the last 20 events, newest first. Each row shows the time,
   the tool, the note path, OK or error, and for writes a link to the commit on GitHub.
   Refreshes every 30 s via HTMX.
5. *Built (round 2):* the vault-at-a-glance row moved to Insights to keep the Overview short.

### Activity (`/dash/activity`)

The full event log, paged, with filters: date range, tool, reads or writes, errors only,
path contains. Each write row expands to show the commit's diff summary (files and lines
changed), fetched from git on demand, never stored.

### Wikis (`/dash/wikis`)

*Built (round 2):* the Notes page became **Insights** (`/dash/insights`): most read and
most changed with a 7 / 30 / 90-day switch, vault size and growth, link health collapsed.
`/dash/notes` redirects there. Tabs: Overview · Activity · Insights · Wikis · Health.

- The vault as a file tree, like Obsidian's file explorer: folders first, then A to Z,
  dot folders hidden, notes shown without `.md`, a file count on each folder.
- Each file shows Claude's reads (last 30 days), a "Claude" mark when Claude made the last
  change, and how long ago it changed (from git).
- A filter box narrows the vault to matching files as you type.
- Folders open with the browser's own `<details>`, so the page needs no script of its own; over
  1,500 entries, folders load their contents when opened.
- Clicking a note opens it on GitHub. An in-dashboard note view with history, diffs and
  restore is the planned next step (see Later).

### Health (`/dash/health`)

- Sync: last pull and push times and outcomes, recent `sync_error`s, commits ahead and
  behind the remote.
- Clients: registered OAuth clients, name, redirect host, created and last-seen dates.
- Server: version, uptime, auth mode, vault names, log size and retention.

## The activity log

The one piece of new plumbing, and the base for the later undo tool and access controls.

- One SQLite file in the state volume: `/data/state/activity.db`. *Built:* with Python's
  own `sqlite3` rather than fastlite, so the log has no dependency on the dashboard extra.
- One row per tool call. *Built:* recorded by a FastMCP middleware around every tool call
  (one hook, and the tool definitions are untouched); the runtime adds the commit:
  `time, vault, tool, kind (read|write|sync), path, ok, error_type, error, duration_ms,
  client_id, user, commit, sync`.
- **Never stored:** note contents, search queries, the text Claude wrote. Paths only.
- Retention: 90 days by default (`OBSIDIAN_MCP_ACTIVITY_DAYS`), pruned daily.
- Logging must never break a tool call: a failed insert is logged to stderr and ignored.
- Device commits come from git, not the log: commits whose message starts with
  `obsidian-mcp:` are Claude's, everything else is a device.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OBSIDIAN_MCP_DASHBOARD` | `false` | Serve `/dash` |
| `OBSIDIAN_MCP_ACTIVITY_DAYS` | `90` | Days of activity log to keep (the log runs even with the dashboard off, so turning it on later has history) |
| `OBSIDIAN_MCP_DASHBOARD_SECRET` | random, stored in state | Key signing the session cookie |
| `OBSIDIAN_MCP_TIMEZONE` | `UTC` | *Built:* time zone for days and times on the dashboard |

GitHub mode: *Built:* no change to the OAuth App. GitHub returns to the App's own
`<public url>/auth/callback`; the dashboard marks its sign-in `state` with `dash.`, and
the server sends those on to `/dash/callback` (MCP sign-ins pass through untouched).

FastHTML is an optional extra (`pip install .[dashboard]`) so a plain MCP install stays
small. The Docker image includes it.

## Design

- One column, readable at phone width; it will mostly be checked from a phone.
- Light and dark themes, following the system setting.
- Server-rendered HTML with HTMX for refreshes. *Built:* charts are server-rendered SVG
  with no chart library; htmx is the only script (pinned, from jsdelivr). No build step.
- Plain wording, no jargon on the page: "Claude changed 3 notes today", not
  "3 write ops".

## Security

- Every `/dash` route except login and callback requires a session; no stats are public.
- The allowlist is checked at sign-in and again on every request (as for the MCP).
- Pages are read-only in v1, so there are no state-changing forms beyond login and
  logout; logout is a POST with a CSRF token.
- `Cache-Control: no-store` on every page, since paths can be sensitive.

## Build order

1. Activity log + tests (useful even without a dashboard).
2. FastHTML mounted at `/dash`, login and logout, empty overview + tests for the
   allowlist and session.
3. Overview: status strip, tiles, recent activity.
4. Activity, Notes and Health pages.
5. Charts and daily vault snapshots.

## Later (web app direction)

- Version control: a note page in the dashboard (read-only, wikilinks, backlinks), then
  its history, diffs, and "restore this version".

- Undo a Claude change from the activity row (git revert), the same as the planned
  `revert_change` tool.
- Access controls page: read-only mode, folder scope, inbox-only writes.
- Revoke an OAuth client.
- Weekly email or Slack digest of AI activity.

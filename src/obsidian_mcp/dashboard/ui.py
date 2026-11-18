"""Shared page pieces: stylesheet, layout, formatting helpers."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fasthtml.common import (
    A, Button, Div, Form, H1, Header, Input, Main, Meta, Nav, P, Script, Section, Span, Style, Title,
)

BASE = "/dash"
HTMX_SRC = "https://cdn.jsdelivr.net/npm/htmx.org@2.0.7/dist/htmx.min.js"

TOOL_LABELS = {
    "read_note": "Read note", "read_pdf": "Read PDF", "search_notes": "Searched",
    "list_notes": "Listed notes", "vault_overview": "Vault overview", "list_tags": "Listed tags",
    "notes_with_tag": "Tag lookup", "get_backlinks": "Backlinks", "get_links": "Links",
    "find_orphans": "Orphans", "list_vaults": "Listed vaults", "write_note": "Wrote note",
    "append_note": "Appended", "insert_under_heading": "Inserted under heading",
    "delete_note": "Deleted note", "sync_vault": "Synced",
}

CSS = """
:root { color-scheme: light dark; --bg:#f6f5f2; --card:#fff; --fg:#1c1b19; --muted:#6b6862;
  --line:#e4e1da; --accent:#6f4fd8; --ok:#1e7d4f; --ok-bg:#e3f4ea; --warn:#8a5a00; --warn-bg:#fdf0d5;
  --err:#b3261e; --err-bg:#fbe5e3; --s0:#6f4fd8; --s1:#2f9e8f; --s2:#d98b2b; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --card:#1f1e1c; --fg:#ecebe7; --muted:#a19e97;
  --line:#34322e; --accent:#a48cf0; --ok:#7fd4a6; --ok-bg:#16301f; --warn:#f2c46b; --warn-bg:#3a2d10;
  --err:#f2b8b5; --err-bg:#3d1a18; --s0:#a48cf0; --s1:#5cc8b8; --s2:#f0ad5a; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system, system-ui, sans-serif; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
header { display:flex; flex-wrap:wrap; align-items:center; gap:8px 16px; padding:12px 16px;
  border-bottom:1px solid var(--line); background:var(--card); }
header h1 { font-size:16px; margin:0; margin-right:auto; }
nav { display:flex; gap:4px; flex-wrap:wrap; }
nav a { padding:4px 10px; border-radius:8px; color:var(--fg); }
nav a.on { background:var(--bg); font-weight:600; }
header form { margin:0; }
.who { color:var(--muted); font-size:13px; }
main { max-width:960px; margin:0 auto; padding:16px; }
section { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:16px; }
h2 { font-size:15px; margin:0 0 12px; }
h2 small { font-weight:400; color:var(--muted); margin-left:6px; }
.muted { color:var(--muted); } .small { font-size:13px; }
.strip { border-radius:12px; padding:12px 16px; margin-bottom:16px; font-weight:500; }
.strip.ok { background:var(--ok-bg); color:var(--ok); } .strip.warn { background:var(--warn-bg); color:var(--warn); }
.strip.error { background:var(--err-bg); color:var(--err); } .strip.local { background:var(--card); color:var(--muted); }
.tiles { display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; margin-bottom:16px; }
@media (max-width:640px) { .tiles { grid-template-columns:repeat(2, 1fr); } }
.tile { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; }
.tile .n { font-size:26px; font-weight:650; line-height:1.2; }
.tile .l { font-size:13px; color:var(--muted); }
.tile .d { font-size:12px; color:var(--muted); }
.scroll { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th { text-align:left; font-weight:600; color:var(--muted); font-size:12px; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
td { padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:last-child td { border-bottom:0; }
td.path { overflow-wrap:anywhere; } td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
td.when { white-space:nowrap; color:var(--muted); }
.badge { display:inline-block; font-size:12px; padding:1px 8px; border-radius:999px; white-space:nowrap; }
.badge.ok { background:var(--ok-bg); color:var(--ok); } .badge.err { background:var(--err-bg); color:var(--err); }
.badge.warn { background:var(--warn-bg); color:var(--warn); }
.err-text { color:var(--err); font-size:13px; }
code { font-size:12px; }
.chart { width:100%; height:auto; display:block; }
.chart .grid { stroke:var(--line); stroke-width:1; } .chart .axis { fill:var(--muted); font-size:11px; }
.chart .s0 { fill:var(--s0); } .chart .s1 { fill:var(--s1); } .chart .s2 { fill:var(--s2); }
.chart .hit { fill:transparent; } .chart g:hover rect:not(.hit) { opacity:.8; }
.legend { display:flex; gap:14px; flex-wrap:wrap; font-size:13px; color:var(--muted); margin-top:6px; }
.legend .sw { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; }
.sw.s0 { background:var(--s0); } .sw.s1 { background:var(--s1); } .sw.s2 { background:var(--s2); }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
@media (max-width:720px) { .grid2 { grid-template-columns:1fr; } }
.stats { display:grid; grid-template-columns:repeat(auto-fill, minmax(120px, 1fr)); gap:8px 16px; }
.stats div b { display:block; font-size:20px; }
.stats div span { color:var(--muted); font-size:13px; }
form.filters { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
input, select, button { font:inherit; font-size:14px; padding:6px 10px; border-radius:8px;
  border:1px solid var(--line); background:var(--card); color:inherit; }
button.primary, a.primary { background:var(--accent); color:#fff; border:0; font-weight:600; }
button.link { border:0; background:none; color:var(--accent); padding:0; cursor:pointer; font-size:13px; }
.switch { display:flex; gap:6px; font-size:14px; } .switch a { padding:3px 10px; border-radius:8px; }
.switch a.on { background:var(--bg); color:var(--fg); font-weight:600; }
.pager { display:flex; justify-content:space-between; margin-top:12px; font-size:14px; }
details summary { cursor:pointer; }
ul.plain { list-style:none; padding:0; margin:8px 0 0; } ul.plain li { padding:3px 0; overflow-wrap:anywhere; }
.stat-files { font-size:13px; margin:6px 0 0; }
.add { color:var(--ok); } .del { color:var(--err); }
@media (max-width:640px) {
  table.events thead { display:none; }
  table.events, table.events tbody { display:block; }
  table.events tr { display:grid; grid-template-columns:auto 1fr auto; gap:2px 10px; padding:10px 0;
    border-bottom:1px solid var(--line); }
  table.events tr:last-child { border-bottom:0; }
  table.events td { display:block; border:0; padding:0; }
  table.events td.res { text-align:right; }
  table.events td.path { grid-column:1 / -1; }
  table.events td.commit:empty { display:none; }
  table.events td.commit { grid-column:1 / -1; font-size:13px; }
}
.login { min-height:100vh; display:grid; place-items:center; padding:16px; }
.login section { width:100%; max-width:380px; padding:28px 24px; }
.login h1 { font-size:20px; margin:0 0 6px; }
.login input, .login .primary { width:100%; padding:12px; border-radius:10px; margin-top:12px; display:block; text-align:center; }
.login input { text-align:left; }
"""


def head(title: str) -> tuple[Any, ...]:
    return (
        Meta(charset="utf-8"),
        Meta(name="viewport", content="width=device-width, initial-scale=1"),
        Meta(name="referrer", content="no-referrer"),
        Script(src=HTMX_SRC, defer=True),
        Style(CSS),
    )


NAV = [("Overview", ""), ("Activity", "/activity"), ("Notes", "/notes"), ("Health", "/health")]


def page(title: str, active: str, user: str | None, csrf: str | None, *content: Any) -> tuple[Any, ...]:
    links = [A(label, href=f"{BASE}{path}/" if not path else f"{BASE}{path}", cls="on" if label == active else None)
             for label, path in NAV]
    logout = Form(
        Input(type="hidden", name="csrf", value=csrf or ""),
        Button("Sign out", cls="link"),
        method="post", action=f"{BASE}/logout",
    )
    return (
        Title(f"{title} · Vault dashboard"),
        Header(H1("Vault dashboard"), Nav(*links), Span(user or "", cls="who"), logout),
        Main(*content),
    )


def login_page(*content: Any, status_title: str = "Sign in") -> tuple[Any, ...]:
    return (
        Title(f"{status_title} · Vault dashboard"),
        Div(Section(*content), cls="login"),
    )


class Fmt:
    """Formatting in the configured time zone."""

    def __init__(self, tz: ZoneInfo) -> None:
        self.tz = tz

    def when(self, ts: float | None) -> str:
        if not ts:
            return "never"
        return datetime.fromtimestamp(ts, self.tz).strftime("%d %b %Y, %H:%M")

    def short(self, ts: float | None) -> str:
        if not ts:
            return "never"
        dt = datetime.fromtimestamp(ts, self.tz)
        today = datetime.now(self.tz).date()
        return dt.strftime("%H:%M") if dt.date() == today else dt.strftime("%d %b %H:%M")

    @staticmethod
    def ago(ts: float | None) -> str:
        if not ts:
            return "never"
        delta = max(0, time.time() - ts)
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{int(delta // 60)} min ago"
        if delta < 86400:
            hours = int(delta // 3600)
            return f"{hours} h ago"
        days = int(delta // 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"


def tool_label(tool: str) -> str:
    return TOOL_LABELS.get(tool, tool)


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def bytes_label(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def empty(text: str) -> Any:
    return P(text, cls="muted small")

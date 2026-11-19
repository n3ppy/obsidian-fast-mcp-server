"""Shared page pieces: stylesheet, layout, theme, formatting helpers.

The look: warm paper, near-black ink, hairlines and one violet accent. Light is the
default; the header's sun/moon link switches to dark and back, and the choice is kept in a
site-wide ``theme`` cookie (no JavaScript needed).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fasthtml.common import (
    A, Body, Button, Div, Form, H1, Head, Header, Html, Input, Main, Meta, Nav, NotStr, P, Script, Section, Span,
    Style, Title, to_xml,
)
from starlette.requests import cookie_parser
from starlette.responses import HTMLResponse

BASE = "/dash"
HTMX_SRC = "https://cdn.jsdelivr.net/npm/htmx.org@2.0.7/dist/htmx.min.js"
THEME_COOKIE = "theme"
THEMES = ("light", "dark")

TOOL_LABELS = {
    "read_note": "Read note", "read_pdf": "Read PDF", "search_notes": "Searched",
    "list_notes": "Listed notes", "vault_overview": "Vault overview", "list_tags": "Listed tags",
    "notes_with_tag": "Tag lookup", "get_backlinks": "Backlinks", "get_links": "Links",
    "find_orphans": "Orphans", "list_vaults": "Listed vaults", "write_note": "Wrote note",
    "append_note": "Appended", "insert_under_heading": "Inserted under heading",
    "delete_note": "Deleted note", "sync_vault": "Synced",
}

# Colour tokens. Chart series (s0, s1, s2) are validated for colour-blind separation and
# 3:1 contrast against the card, in each mode (the dark ones are their own steps).
_LIGHT = (
    "--bg:#fafaf7; --card:#ffffff; --soft:#f3f3ee; --line:#e8e8e0; "
    "--fg:#0e0e10; --fg-soft:#2a2a30; --muted:#6b6b76; --faint:#a3a3ab; "
    "--accent:#5b3df5; --accent-ink:#ffffff; --accent-soft:#eeebff; "
    "--ok:#127a5a; --ok-bg:#dcf4ea; --warn:#8a5d00; --warn-bg:#fff1d1; --err:#b3263b; --err-bg:#fde5e9; "
    "--s0:#5b3df5; --s1:#eb6834; --s2:#1baf7a;"
)
_DARK = (
    "color-scheme:dark; "
    "--bg:#0e0e10; --card:#16161a; --soft:#1f1f25; --line:#2b2b32; "
    "--fg:#f3f3ee; --fg-soft:#d6d6cf; --muted:#a1a1ab; --faint:#6f6f7a; "
    "--accent:#9d89ff; --accent-ink:#0e0e10; --accent-soft:#25213f; "
    "--ok:#5fd9ae; --ok-bg:#133226; --warn:#f6c766; --warn-bg:#382d10; --err:#ff8d9c; --err-bg:#3c1a21; "
    "--s0:#8b74f8; --s1:#d95926; --s2:#199e70;"
)
_PICKED_DARK = ':root[data-theme="dark"]'  # light is the default; dark only when the viewer picks it


def _dark(scope: str) -> str:
    return (f"{scope} {{ {_DARK} }}\n{scope} .theme-toggle .to-light {{ display:inline-flex; }} "
            f"{scope} .theme-toggle .to-dark {{ display:none; }}\n")


THEME_CSS = f":root {{ color-scheme:light; {_LIGHT} }}\n{_dark(_PICKED_DARK)}"

CSS = THEME_CSS + """
:root { --r:16px; --sans:ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto,
  sans-serif; --mono:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 var(--sans); -webkit-font-smoothing:antialiased; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
header { position:sticky; top:0; z-index:5; display:flex; flex-wrap:wrap; align-items:center; gap:8px 20px;
  min-height:56px; padding:8px max(20px, calc((100% - 64rem) / 2 + 20px)); background:var(--bg);
  border-bottom:1px solid var(--line); }
header h1, header .brand { margin:0; font-size:16px; font-weight:600; letter-spacing:-.025em; }
header .brand a { display:flex; align-items:center; gap:8px; color:var(--fg); text-decoration:none; }
header .brand svg { width:22px; height:22px; flex:none; }
nav { display:flex; flex-wrap:wrap; gap:2px; }
nav a { padding:5px 10px; border-radius:8px; color:var(--muted); font-size:14px; }
nav a:hover { color:var(--fg); text-decoration:none; }
nav a.on { color:var(--fg); background:var(--soft); font-weight:600; }
header .right { margin-left:auto; display:flex; align-items:center; gap:12px; }
header form { margin:0; }
.who { color:var(--muted); font-size:13px; }
.theme-toggle a { display:inline-flex; padding:5px; border-radius:8px; color:var(--muted); }
.theme-toggle a:hover { color:var(--fg); background:var(--soft); }
.theme-toggle .to-light { display:none; }
.ic-theme { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round;
  stroke-linejoin:round; }
main { max-width:64rem; margin:0 auto; padding:24px 20px 48px; }
section { min-width:0; margin-bottom:16px; padding:20px; background:var(--card); border:1px solid var(--line);
  border-radius:var(--r); }
h2 { margin:0 0 14px; font-size:11px; font-weight:600; letter-spacing:.18em; text-transform:uppercase; color:var(--muted); }
h2 small { margin-left:8px; font-size:12px; font-weight:400; letter-spacing:0; text-transform:none; }
.muted { color:var(--muted); } .small { font-size:13px; }
.strip { border-radius:12px; padding:12px 16px; margin-bottom:16px; font-weight:500; }
.strip.ok { background:var(--ok-bg); color:var(--ok); } .strip.warn { background:var(--warn-bg); color:var(--warn); }
.strip.error { background:var(--err-bg); color:var(--err); }
.strip.local { background:var(--card); color:var(--muted); border:1px solid var(--line); }
.tiles { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; margin-bottom:16px; }
@media (max-width:640px) { .tiles { grid-template-columns:repeat(2, minmax(0, 1fr)); } }
.tile { display:flex; flex-direction:column; min-width:0; padding:16px 18px; background:var(--card);
  border:1px solid var(--line); border-radius:var(--r); }
.tile .l { order:-1; margin-bottom:4px; font-size:11px; font-weight:600; letter-spacing:.14em; text-transform:uppercase;
  color:var(--muted); }
.tile .n { font-size:28px; font-weight:600; letter-spacing:-.025em; line-height:1.1; font-variant-numeric:tabular-nums; }
.tile .d { margin-top:2px; font-size:12px; color:var(--muted); }
.scroll { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th { padding:8px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; font-size:11px;
  font-weight:600; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); }
td { padding:9px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:last-child td { border-bottom:0; }
td.path { font-family:var(--mono); font-size:13px; overflow-wrap:anywhere; }
td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
td.when { white-space:nowrap; color:var(--muted); }
.badge { display:inline-block; padding:1px 9px; border-radius:999px; font-size:12px; font-weight:600; white-space:nowrap; }
.badge.ok { background:var(--ok-bg); color:var(--ok); } .badge.err { background:var(--err-bg); color:var(--err); }
.badge.warn { background:var(--warn-bg); color:var(--warn); }
.err-text { color:var(--err); font-size:13px; }
code { font-family:var(--mono); font-size:12px; }
/* bar charts (charts.py): tinted bars, today's in full colour, 2px gaps, rounded tops */
.chart .plot { position:relative; display:flex; align-items:flex-end; gap:3px; height:150px; padding-top:20px;
  border-bottom:1px solid var(--line); }
.chart .plot::before { content:""; position:absolute; left:0; right:0; top:20px; border-top:1px dashed var(--line); }
.chart .ymax { position:absolute; top:0; left:0; font-size:11px; line-height:1; color:var(--muted);
  font-variant-numeric:tabular-nums; }
.chart .col { position:relative; flex:1; min-width:0; height:100%; display:flex; flex-direction:column;
  justify-content:flex-end; }
.chart .stack { display:flex; flex-direction:column-reverse; gap:2px; flex-shrink:0; overflow:hidden;
  border-radius:4px 4px 0 0; }
.chart .stack i { display:block; flex-basis:0; min-height:2px; }
.chart .s0, .sw.s0 { background:var(--s0); } .chart .s1, .sw.s1 { background:var(--s1); }
.chart .s2, .sw.s2 { background:var(--s2); }
.chart.emph .col:not(.last) .s0 { background:color-mix(in srgb, var(--s0) 70%, var(--card)); }
.chart .col:hover .stack { opacity:.8; }
.chart .val { align-self:center; margin-bottom:4px; font-size:12px; font-weight:600; line-height:1; color:var(--fg);
  font-variant-numeric:tabular-nums; }
.chart .axis { display:flex; justify-content:space-between; margin-top:6px; font-size:11px; color:var(--muted); }
@media (max-width:640px) { .chart .plot { gap:2px; } }
.legend { display:flex; flex-wrap:wrap; gap:14px; margin-top:10px; font-size:13px; color:var(--muted); }
.legend .sw { display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:6px; }
.grid2 { display:grid; grid-template-columns:minmax(0, 1fr) minmax(0, 1fr); gap:16px; }
@media (max-width:720px) { .grid2 { grid-template-columns:minmax(0, 1fr); } }
.stats { display:grid; grid-template-columns:repeat(auto-fill, minmax(120px, 1fr)); gap:12px 16px; }
.stats div b { display:block; font-size:22px; font-weight:600; letter-spacing:-.02em; font-variant-numeric:tabular-nums; }
.stats div span { color:var(--muted); font-size:13px; }
form.filters { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
input, select, button { font:inherit; font-size:14px; padding:7px 11px; border-radius:10px; border:1px solid var(--line);
  background:var(--card); color:inherit; }
button.primary, a.primary { background:var(--fg); color:var(--bg); border:0; font-weight:600; }
button.link { border:0; background:none; color:var(--muted); padding:0; cursor:pointer; font-size:13px; }
button.link:hover { color:var(--fg); }
.switch { display:flex; gap:4px; font-size:14px; } .switch a { padding:4px 10px; border-radius:8px; color:var(--muted); }
.switch a.on { background:var(--soft); color:var(--fg); font-weight:600; }
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
.bar { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:8px 16px; margin:0 0 14px; }
.bar h2 { margin:0; }
details.health { margin-top:16px; } details.health > p, details.health > details { margin:8px 0 0 12px; }
input.filter { flex:1; min-width:180px; max-width:320px; }
.tree, ul.results { font-size:14px; }
.tree ul, ul.results { list-style:none; margin:0; padding:0; }
.tree ul ul { margin-left:12px; padding-left:8px; border-left:1px solid var(--line); }
.tree summary { list-style:none; display:flex; align-items:center; gap:6px; padding:4px 6px; border-radius:8px;
  cursor:pointer; user-select:none; }
.tree summary::-webkit-details-marker { display:none; }
.tree summary::before { content:""; flex:none; width:6px; height:6px; margin:0 3px 0 1px;
  border-right:1.5px solid var(--muted); border-bottom:1.5px solid var(--muted); transform:rotate(-45deg);
  transition:transform .12s; }
.tree details[open] > summary::before { transform:rotate(45deg); }
.tree summary:hover, li.f:hover { background:var(--soft); }
li.f { display:flex; align-items:center; gap:8px; padding:4px 6px 4px 19px; border-radius:8px; min-width:0; }
ul.results li.f { padding-left:6px; }
li.vault-label { padding:10px 6px 2px; }
.file { display:flex; align-items:center; gap:6px; min-width:0; flex:1; color:var(--fg); }
a.file:hover { text-decoration:none; } a.file:hover .nm { text-decoration:underline; }
.nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.file .dir { color:var(--muted); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.count { margin-left:auto; font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; }
.meta { display:flex; align-items:center; gap:8px; margin-left:auto; flex:none; font-size:12px; color:var(--muted);
  white-space:nowrap; }
.badge.ai { background:var(--soft); color:var(--accent); border:1px solid var(--line); font-size:11px; padding:0 6px; }
.ic { width:16px; height:16px; flex:none; fill:none; stroke:currentColor; stroke-width:1.7; stroke-linecap:round;
  stroke-linejoin:round; color:var(--muted); }
.ic.folder, .ic.vault { color:var(--accent); } .ic.pdf { color:var(--err); } .ic.image { color:var(--s1); }
@media (max-width:640px) {
  .meta .reads { display:none; } .file .dir { display:none; } header .who { display:none; }
  /* phones: logo and sign-out on one row, the tabs scroll underneath, and the header scrolls away */
  header { position:static; }
  header nav { order:3; flex-basis:100%; flex-wrap:nowrap; overflow-x:auto; margin:0 -4px; }
  nav a { white-space:nowrap; }
}
.login { min-height:100vh; display:grid; place-items:center; padding:16px; }
.login section { width:100%; max-width:380px; padding:28px 24px; }
.login h1 { margin:0 0 6px; font-size:22px; font-weight:600; letter-spacing:-.025em; }
.login input, .login .primary { display:block; width:100%; margin-top:12px; padding:12px; border-radius:12px;
  text-align:center; }
.login input { text-align:left; }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition:none !important; } }
"""

# Feather icons (MIT), drawn in the current text colour.
_MOON = ('<svg class="ic-theme" viewBox="0 0 24 24" aria-hidden="true">'
         '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>')
_SUN = ('<svg class="ic-theme" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.5"/>'
        '<path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41'
        'M17.66 6.34l1.41-1.41"/></svg>')

_theme: ContextVar[str | None] = ContextVar("dash_theme", default=None)
_here: ContextVar[str] = ContextVar("dash_here", default=f"{BASE}/")


class ThemeMiddleware:
    """ASGI: for each request, remember the viewer's theme cookie and the page's own path
    (the theme links send people back to where they were)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = next((v for k, v in scope.get("headers") or [] if k == b"cookie"), b"")
        theme = cookie_parser(raw.decode("latin-1")).get(THEME_COOKIE)
        root, path = scope.get("root_path", ""), scope.get("path", "")
        here = path if path.startswith(root) else root + path
        query = scope.get("query_string", b"").decode("latin-1")
        tokens = (_theme.set(theme if theme in THEMES else None), _here.set(here + (f"?{query}" if query else "")))
        try:
            await self.app(scope, receive, send)
        finally:
            _theme.reset(tokens[0])
            _here.reset(tokens[1])


def theme_attrs() -> dict[str, str]:
    """``data-theme`` for the <html> tag, when the viewer picked one."""
    theme = _theme.get()
    return {"data_theme": theme} if theme else {}


def safe_next(value: str | None, default: str) -> str:
    """Only paths on this site: never another host, a scheme or a header break."""
    if not value or not value.startswith("/") or value.startswith("//") or any(c in value for c in "\\\r\n"):
        return default
    return value


def theme_toggle(action: str = f"{BASE}/theme") -> Any:
    """Two links, one per theme. The stylesheet shows the one that switches away from what
    is on screen, so it works without JavaScript."""
    here = quote(_here.get(), safe="/")
    return Span(
        A(NotStr(_MOON), href=f"{action}?mode=dark&next={here}", cls="to-dark", title="Dark mode",
          aria_label="Switch to dark mode"),
        A(NotStr(_SUN), href=f"{action}?mode=light&next={here}", cls="to-light", title="Light mode",
          aria_label="Switch to light mode"),
        cls="theme-toggle",
    )


def head(title: str) -> tuple[Any, ...]:
    return (
        Meta(charset="utf-8"),
        Meta(name="viewport", content="width=device-width, initial-scale=1"),
        Meta(name="referrer", content="no-referrer"),
        Meta(name="theme-color", content="#0e0e10" if _theme.get() == "dark" else "#fafaf7"),
        Script(src=HTMX_SRC, defer=True),
        Style(CSS),
    )


def document(title: str, body: Any, status: int = 200) -> HTMLResponse:
    """A whole page, so the <html> tag can carry the viewer's theme."""
    doc = Html(Head(Title(title), *head(title)), body, lang="en", **theme_attrs())
    return HTMLResponse(to_xml(doc), status_code=status)  # Html() brings its own doctype


NAV = [("Overview", ""), ("Activity", "/activity"), ("Insights", "/insights"), ("Wikis", "/wikis"), ("Health", "/health")]


def page(
    title: str, active: str, user: str | None, csrf: str | None, *content: Any,
    brand: str = "Vault dashboard", extra_nav: tuple[tuple[str, str], ...] = (),
    logout_action: str | None = None, masthead: Any = None,
) -> HTMLResponse:
    links = [A(label, href=f"{BASE}{path}/" if not path else f"{BASE}{path}", cls="on" if label == active else None)
             for label, path in NAV]
    links += [A(label, href=href) for label, href in extra_nav]
    logout = Form(
        Input(type="hidden", name="csrf", value=csrf or ""),
        Button("Sign out", cls="link"),
        method="post", action=logout_action or f"{BASE}/logout",
    )
    top = Div(masthead, cls="brand") if masthead is not None else H1(brand)
    right = Div(theme_toggle(), Span(user or "", cls="who"), logout, cls="right")
    return document(f"{title} · {brand}", Body(Header(top, Nav(*links), right), Main(*content)))


def login_page(*content: Any, status_title: str = "Sign in", brand: str = "Vault dashboard") -> tuple[Any, ...]:
    return (
        Title(f"{status_title} · {brand}"),
        Div(Section(*content), cls="login"),
    )


class Fmt:
    """Formatting in the configured time zone (or the zone of the current request,
    when given a function)."""

    def __init__(self, tz: ZoneInfo | Callable[[], ZoneInfo]) -> None:
        self._tz = tz

    @property
    def tz(self) -> ZoneInfo:
        return self._tz() if callable(self._tz) else self._tz

    def when(self, ts: float | None) -> str:
        if not ts:
            return "never"
        return datetime.fromtimestamp(ts, self.tz).strftime("%d %b %Y, %H:%M")

    def short(self, ts: float | None) -> str:
        if not ts:
            return "never"
        tz = self.tz
        dt = datetime.fromtimestamp(ts, tz)
        today = datetime.now(tz).date()
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

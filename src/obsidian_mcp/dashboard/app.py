"""The dashboard web app (FastHTML), mounted at ``/dash`` inside the MCP server.

Sign-in is separate from the MCP's OAuth: a dashboard session never grants MCP access,
and an MCP token never opens the dashboard.

- GitHub mode: "Sign in with GitHub" through the same OAuth App as the MCP, limited to
  ``OBSIDIAN_MCP_ALLOWED_USERS``. GitHub returns to the App's ``/auth/callback``;
  the ``dash.`` state prefix lets ``GitHubCallbackRouter`` forward it to
  ``/dash/callback``. The GitHub token is used once to read the login, then dropped.
- Password mode: the server password, sharing the MCP login's lockout.

The session is a signed cookie limited to ``/dash`` (7 days). The allowlist is checked
again on every request, so removing a name locks that person out at once.

- External mode (``external=ExternalAuth(...)``): an embedding app signs people in and
  chooses their workspace; the dashboard has no login of its own (see ``external.py``).
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx2 as httpx
from fasthtml.common import (
    A, Article, B, Beforeware, Body, Button, Code, Details, Div, FastHTML, Form, H1, H2, Head, Html, Input,
    Label, Li, Link, NotStr, Option, P, Script, Section, Select, Small, Span, Summary, Table, Tbody, Td, Th, Thead,
    Tr, Ul, to_xml,
)
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .. import __version__, gitstats, links, workspace
from ..activity import ActivityLog, ScopedLog
from ..config import Settings
from ..render import render_note
from ..vault import VaultError, configured_vaults, use_vault
from . import assets, charts, stats, viewer
from .external import ExternalAuth
from .ui import (
    BASE, FOCUS_COOKIE, THEME_COOKIE, THEMES, Fmt, ThemeMiddleware, base, bytes_label, empty, focused, head,
    login_page, page, plural, safe_next, theme_attrs, tool_label,
)

GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_USER = "https://api.github.com/user"
CALLBACK_PATH = "/auth/callback"  # the OAuth App's own callback, shared with MCP login
STATE_PREFIX = "dash."  # marks a dashboard sign-in so the server routes it to /dash
SESSION_SECONDS = 7 * 24 * 3600
COOKIE = "obsidian_dash"
PAGE_SIZE = 50
PUBLIC_PATHS = ("/login", "/login/github", "/callback")

# Scripts: htmx from its CDN, and the note viewer's own (assets.py: note.js, KaTeX, Pele),
# never inline ones. Fonts: KaTeX's, from this site. Images: this site and data: URIs only,
# so a note can't load another site's image (no tracking pixels).
CSP = (
    "default-src 'none'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)
STATIC_CACHE = b"public, max-age=31536000, immutable"  # static/<digest>/...: a new digest for any change

ClientNames = Callable[[str], Awaitable[str | None]]


async def github_login(code: str, redirect_uri: str, client_id: str, client_secret: str) -> str | None:
    """Exchange an OAuth code for the GitHub login it belongs to (the token is discarded)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            GITHUB_TOKEN,
            data={"client_id": client_id, "client_secret": client_secret, "code": code,
                  "redirect_uri": redirect_uri},
            headers={"Accept": "application/json"},
        )
        token = resp.json().get("access_token") if resp.status_code == 200 else None
        if not token:
            return None
        user = await client.get(
            GITHUB_USER, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        )
        login = user.json().get("login") if user.status_code == 200 else None
        return login if isinstance(login, str) else None


class _SecurityHeaders:
    """ASGI middleware: no caching (except the static files, whose addresses change with
    their content), no framing, strict CSP on every dashboard response."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        root, path = scope.get("root_path", ""), scope.get("path", "")
        static = (path[len(root):] if root and path.startswith(root) else path).startswith("/static/")

        async def wrapped(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"cache-control", STATIC_CACHE if static and message.get("status") == 200 else b"no-store"),
                    (b"x-frame-options", b"DENY"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", CSP.encode()),
                ]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


def _rel(path: str) -> str:
    """The path inside the dashboard ("/dash/activity" -> "/activity")."""
    root = base()
    return path[len(root):] if path.startswith(root) else path


def _zone(name: str, fallback: ZoneInfo) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown name: keep the server's zone
        return fallback


def build_app(
    settings: Settings,
    log: ActivityLog,
    *,
    password_check: Any = None,
    client_names: ClientNames | None = None,
    secret_key: str,
    external: ExternalAuth | None = None,
) -> FastHTML:
    """The FastHTML app. `password_check` is the password provider (password mode);
    `external` hands sign-in to an embedding app (see ``external.py``)."""
    server_tz = ZoneInfo(settings.timezone)

    def zone() -> ZoneInfo:
        """The viewer's time zone: their workspace's, else the server's."""
        ws = workspace.current()
        return _zone(ws.timezone, server_tz) if ws is not None and ws.timezone else server_tz

    clock = stats.Clock(zone)
    fmt = Fmt(zone)
    log = ScopedLog(log)  # the viewer's own log when the embedding app chose one
    github = settings.auth == "github" and external is None
    brand = external.brand if external else "Vault dashboard"
    login_target = external.login_url if external else f"{BASE}/login"

    # -- session and access ----------------------------------------------------------

    def current_user(session) -> str | None:
        if external is not None:
            return external.user()
        user, at = session.get("user"), session.get("at", 0)
        if not isinstance(user, str) or time.time() - float(at) > SESSION_SECONDS:
            return None
        if github and user.lower() not in settings.allowed_users:
            return None
        return user

    def before(req: Request, session) -> Response | None:
        if _rel(req.url.path) in PUBLIC_PATHS:
            return None
        if current_user(session) is None:
            if external is None:
                session.clear()
            if req.headers.get("hx-request"):
                return Response(status_code=401, headers={"HX-Redirect": login_target})
            return RedirectResponse(login_target, status_code=303)
        return None

    def start_session(session, user: str) -> None:
        session.clear()
        session.update(user=user, at=time.time(), csrf=secrets.token_urlsafe(24))

    app = FastHTML(
        default_hdrs=False,
        hdrs=head(brand),
        canonical=False,
        surreal=False,
        htmx=False,
        secret_key=secret_key,
        session_cookie=COOKIE,
        max_age=SESSION_SECONDS,
        sess_path=BASE,
        same_site="lax",
        sess_https_only=(settings.public_url or "").startswith("https://"),
        before=Beforeware(before),
        title=brand,
    )
    app.add_middleware(_SecurityHeaders)
    app.add_middleware(ThemeMiddleware)

    def shell(title: str, active: str, session, *content: Any) -> Any:
        choices = tuple((name, where(vault)) for name, vault in _vaults().items())
        if external is not None:
            return page(title, active, external.user(), external.csrf(), *content, brand=brand,
                        extra_nav=external.nav, logout_action=external.logout_url, masthead=external.masthead,
                        menu=external.menu(), vaults=choices, vault_links=external.vault_links(),
                        vault_words=external.vault_words)
        return page(title, active, current_user(session), session.get("csrf"), *content, vaults=choices)

    # -- which vaults the pages are about (the header's switcher) ----------------------

    def scope() -> str | None:
        """The one vault the viewer picked, or None: all of theirs."""
        return focused(list(_vaults()))

    def in_scope() -> dict[str, Any]:
        vaults = _vaults()
        name = focused(list(vaults))
        return {name: vaults[name]} if name else vaults

    @app.get("/focus")
    def set_focus(req: Request, vault: str = "", next: str = "") -> Response:  # noqa: A002 - the query's name
        """Pick the vault the pages are about (a cookie, limited to the dashboard's path),
        or none, for all of them; then back to the page the viewer was on."""
        response = RedirectResponse(safe_next(next, f"{base()}/"), status_code=303)
        if vault in _vaults():
            response.set_cookie(FOCUS_COOKIE, vault, max_age=365 * 24 * 3600, path=base(), samesite="lax",
                                httponly=True, secure=req.url.scheme == "https")
        else:
            response.delete_cookie(FOCUS_COOKIE, path=base())
        return response

    # -- sign in ---------------------------------------------------------------------

    def login_form(error: str | None = None, status: int = 200) -> Any:
        intro = P("See what Claude has done in your vault.", cls="muted")
        if github:
            body = [H1("Vault dashboard"), intro]
            if error:
                body.append(P(error, cls="err-text"))
            body.append(A("Sign in with GitHub", href=f"{base()}/login/github", cls="primary"))
        else:
            body = [
                H1("Vault dashboard"), intro,
                P(error, cls="err-text") if error else "",
                Form(
                    Input(type="password", name="password", placeholder="Password",
                          autocomplete="current-password", autofocus=True, required=True),
                    Button("Sign in", cls="primary"),
                    method="post", action=f"{base()}/login",
                ),
            ]
        return _html(login_page(*body), status)

    def _html(content: tuple[Any, ...], status: int = 200) -> HTMLResponse:
        """A full page with a chosen status code (FastHTML renders 200 by default)."""
        titles = [c for c in content if getattr(c, "tag", None) == "title"]
        body = [c for c in content if getattr(c, "tag", None) != "title"]
        doc = Html(Head(*titles, *head("")), Body(*body), lang="en", **theme_attrs())
        return HTMLResponse(to_xml(doc), status_code=status)  # Html() brings its own doctype

    @app.get("/theme")
    def set_theme(req: Request, mode: str = "", next: str = "") -> Response:  # noqa: A002 - the query's name
        """Remember the viewer's light/dark choice in a site-wide cookie; any other mode
        forgets it, back to light, the default."""
        response = RedirectResponse(safe_next(next, f"{base()}/"), status_code=303)
        if mode in THEMES:
            response.set_cookie(THEME_COOKIE, mode, max_age=365 * 24 * 3600, path="/", samesite="lax", httponly=True,
                                secure=req.url.scheme == "https")
        else:
            response.delete_cookie(THEME_COOKIE, path="/")
        return response

    @app.get("/login")
    def login_get(session) -> Any:
        if current_user(session):
            return RedirectResponse(f"{base()}/", status_code=303)
        if external is not None:
            return RedirectResponse(external.login_url, status_code=303)
        return login_form()

    @app.post("/login")
    async def login_post(request: Request, session) -> Any:
        if github or password_check is None or external is not None:
            return Response(status_code=404)
        form = await request.form()
        if password_check.locked_out():
            return login_form("Too many attempts. Try again in 15 minutes.", 429)
        if not password_check.check_password(str(form.get("password", ""))):
            return login_form("Incorrect password.", 401)
        start_session(session, "owner")
        return RedirectResponse(f"{base()}/", status_code=303)

    @app.get("/login/github")
    def login_github(session) -> Any:
        if not github:
            return Response(status_code=404)
        state = STATE_PREFIX + secrets.token_urlsafe(24)
        session["oauth_state"] = state
        query = urlencode({
            "client_id": settings.github_client_id, "redirect_uri": settings.public_url + CALLBACK_PATH,
            "scope": "read:user", "state": state, "allow_signup": "false",
        })
        return RedirectResponse(f"{GITHUB_AUTHORIZE}?{query}", status_code=303)

    @app.get("/callback")
    async def callback(session, code: str = "", state: str = "") -> Any:
        if not github:
            return Response(status_code=404)
        expected = session.pop("oauth_state", None)
        if not code or not expected or not secrets.compare_digest(state, expected):
            return login_form("That sign-in link has expired. Try again.", 400)
        try:
            login = await github_login(
                code, settings.public_url + CALLBACK_PATH,
                settings.github_client_id or "", settings.github_client_secret or "",
            )
        except (httpx.HTTPError, ValueError):
            login = None
        if login is None:
            return login_form("GitHub sign-in failed. Try again.", 502)
        if login.lower() not in settings.allowed_users:
            return login_form("This GitHub account isn't allowed on this server.", 403)
        start_session(session, login)
        return RedirectResponse(f"{base()}/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request, session) -> Any:
        if external is not None:
            return Response(status_code=404)  # the embedding app signs people out
        form = await request.form()
        token = session.get("csrf")
        if not token or not secrets.compare_digest(str(form.get("csrf", "")), token):
            return Response("Invalid request", status_code=403)
        session.clear()
        return RedirectResponse(f"{base()}/login", status_code=303)

    # -- pieces ----------------------------------------------------------------------

    # -- where things open ------------------------------------------------------------

    def query(**params: str) -> str:
        return urlencode(params, quote_via=quote, safe="/")  # folders stay readable: ?path=Inbox/Note.md

    def note_url(vault: str, path: str) -> str:
        return f"{base()}/wikis/note/{quote(vault, safe='')}?{query(path=path)}"

    def file_url(vault: str, path: str) -> str:
        return f"{base()}/wikis/file/{quote(vault, safe='')}?{query(path=path)}"

    def open_url(vault: str, name: str, src: str | None = None) -> str:
        return f"{base()}/wikis/open/{quote(vault, safe='')}?" + (query(name=name, src=src) if src else query(name=name))

    def tag_url(vault: str, tag: str) -> str:
        return f"{base()}/wikis/tag/{quote(vault, safe='')}?{query(tag=tag)}"

    def asset(name: str) -> str:
        return f"{base()}/static/{assets.digest()}/{name}"

    def path_link(vault: str | None, path: str | None) -> Any:
        """A note in a table: it opens in the viewer (a PDF opens the file itself)."""
        if not path:
            return Span("—", cls="muted")
        if vault and path.lower().endswith(".md"):
            return A(path, href=note_url(vault, path))
        if vault and path.lower().endswith(".pdf"):
            return A(path, href=file_url(vault, path))
        return path

    def path_cell(vault: str | None, path: str | None) -> Any:
        return Td(path_link(vault, path), cls="path")

    def commit_link(vault: str | None, sha: str | None) -> Any:
        if not sha:
            return ""
        url = gitstats.web_url(vault, sha) if vault else None
        label = Code(sha[:7])
        return A(label, href=url, target="_blank", rel="noopener") if url else label

    def result_badge(event: dict) -> Any:
        if not event["ok"]:
            return Span("error", cls="badge err", title=event.get("error") or "")
        if (event.get("sync") or "").startswith("error"):
            return Span("not pushed", cls="badge warn", title=event["sync"])
        return Span("ok", cls="badge ok")

    def event_rows(events: list[dict], expandable: bool = False) -> list[Any]:
        rows = []
        multi = scope() is None and len(_vaults()) > 1  # all of several: say which vault each was in
        for e in events:
            detail = ""
            if not e["ok"] and e.get("error"):
                detail = Div(e["error"], cls="err-text")
            elif (e.get("sync") or "").startswith("error"):
                detail = Div(e["sync"][7:], cls="err-text")
            what = [tool_label(e["tool"])]
            if multi and e.get("vault"):
                what.append(Small(f" · {e['vault']}", cls="muted"))
            extra = ""
            if expandable and e.get("commit_sha"):
                target = f"stat-{e['id']}"
                extra = Div(
                    Button("Show files", cls="link", hx_get=f"{base()}/commit/{e['vault']}/{e['commit_sha']}",
                           hx_target=f"#{target}", hx_swap="innerHTML"),
                    Div(id=target),
                )
            rows.append(Tr(
                Td(Span(fmt.short(e["ts"]), title=fmt.when(e["ts"])), cls="when"),
                Td(*what, cls="what"),
                Td(result_badge(e), cls="res"),
                Td(path_link(e.get("vault"), e.get("path")), detail, extra, cls="path"),
                Td(commit_link(e.get("vault"), e.get("commit_sha")), cls="commit"),
            ))
        return rows

    def event_table(events: list[dict], expandable: bool = False) -> Any:
        if not events:
            return empty("Nothing yet. Activity appears here as soon as Claude uses the vault.")
        return Div(Table(
            Thead(Tr(Th("When"), Th("What"), Th("Result"), Th("Note"), Th("Commit"))),
            Tbody(*event_rows(events, expandable)),
            cls="events",
        ), cls="scroll")

    def status_strip(health: list[dict]) -> Any:
        synced = [h for h in health if h["remote"]]
        if not synced:
            return Div("Sync is off: no git remote is configured.", cls="strip local")
        broken = [h for h in synced if h["state"] == "error"]
        if broken:
            h = broken[0]
            if h["push_error"]:
                text = f"Push failing since {fmt.short(h['push_error_at'])}: {h['push_error']}"
            else:
                text = f"Pull failing since {fmt.short(h['pull_error_at'])}: {h['pull_error']}"
            prefix = f"{h['vault']}: " if len(synced) > 1 else ""
            return Div(prefix + text, cls="strip error")
        waiting = sum(h["ahead"] for h in synced)
        last_pull = max((h["last_pull"] or 0) for h in synced) or None
        last_push = max((h["last_push"] or 0) for h in synced) or None
        parts = [f"Synced {fmt.ago(last_pull)}"]
        parts.append(f"pushed {fmt.ago(last_push)}" if last_push else "nothing pushed since the server started")
        if waiting:
            parts.append(f"{plural(waiting, 'commit')} waiting to be pushed")
            return Div(" · ".join(parts), cls="strip warn")
        parts.append("no errors")
        return Div(" · ".join(parts), cls="strip ok")

    def tile(t: dict) -> Any:
        avg = t["avg"]
        if t["value"] == 0 and avg == 0:
            delta = "none this week"
        else:
            delta = f"7-day avg {avg:.1f}".removesuffix(".0")
        return Div(Div(str(t["value"]), cls="n"), Div(t["label"] + " today", cls="l"), Div(delta, cls="d"), cls="tile")

    def day_label(d: Any) -> str:
        return d.strftime("%d %b")

    # -- pages -----------------------------------------------------------------------

    @app.get("/")
    @app.get("/overview")
    def overview(session) -> Any:
        focus = scope()
        daily = stats.daily_activity(log, clock, 30, vault=focus)
        health = stats.sync_health(log, vault=focus)
        week = clock.start_of(clock.days(7)[0])
        errors = stats.errors_by_type(log, week, vault=focus)
        reads_series = [("reads", "Claude reads")]
        change_series = [("claude_commits", "By Claude"), ("device_commits", "From your devices")]
        banner = external.banner() if external is not None and external.banner is not None else None

        return shell(
            "Overview", "Overview", session,
            banner if banner is not None else "",
            status_strip(health),
            Div(*[tile(t) for t in stats.tiles(daily)], cls="tiles"),
            Div(
                # One series: its title names it, so no legend; today's bar stands out.
                Section(H2("Claude's reads", Small("last 30 days")),
                        NotStr(charts.stacked_bars(daily, reads_series, label_fmt=day_label, aria="Reads per day",
                                                   emphasize_last=True, label_last=True))),
                Section(H2("Changes", Small("git commits, last 30 days")),
                        NotStr(charts.stacked_bars(daily, change_series, label_fmt=day_label,
                                                   aria="Commits per day", label_last=True)),
                        NotStr(charts.legend(change_series))),
                cls="grid2",
            ),
            Section(H2("Recent activity", Small("refreshes every 30 s")),
                    Div(recent_fragment(), hx_get=f"{base()}/fragment/recent", hx_trigger="every 30s",
                        hx_swap="innerHTML"),
                    P(A("All activity →", href=f"{base()}/activity"), cls="small")),
            Section(H2("Errors", Small("last 7 days")), errors_table(errors)),
        )

    @app.get("/insights")
    def insights(session, days: int = 30) -> Any:
        days = days if days in (7, 30, 90) else 30
        stats.ensure_snapshots(log, clock)
        return shell("Insights", "Insights", session, *top_notes(days), *vault_sections())

    def top_notes(days: int) -> list[Any]:
        since = time.time() - days * 86400
        top_read = stats.top_read(log, since, vault=scope())
        top_changed = stats.top_changed(days, vault=scope())
        switch = Div(*[A(f"{d} days", href=f"{base()}/insights?days={d}", cls="on" if d == days else None)
                       for d in (7, 30, 90)], cls="switch")
        read_table = Div(Table(
            Thead(Tr(Th("Note"), Th("Reads", cls="num"), Th("Last read"))),
            Tbody(*[Tr(path_cell(r["vault"], r["path"]), Td(str(r["n"]), cls="num"),
                       Td(fmt.short(r["last_ts"]), cls="when")) for r in top_read]),
        ), cls="scroll") if top_read else empty("No notes read by Claude in this period.")
        changed_table = Div(Table(
            Thead(Tr(Th("File"), Th("Commits", cls="num"), Th("By Claude", cls="num"), Th("From devices", cls="num"))),
            Tbody(*[Tr(path_cell(r["vault"], r["path"]), Td(str(r["n"]), cls="num"),
                       Td(str(r["claude"]), cls="num"), Td(str(r["device"]), cls="num")) for r in top_changed]),
        ), cls="scroll") if top_changed else empty("No commits in this period.")
        return [
            Div(H2("Wikis", Small("most read and changed")), switch, cls="bar"),
            Div(Section(H2("Most read by Claude"), read_table), Section(H2("Most changed"), changed_table),
                cls="grid2"),
        ]

    def vault_sections() -> list[Any]:
        sections = []
        vaults = in_scope()
        for name, vault in vaults.items():
            size = stats.vault_size(vault)
            try:
                links_ = stats.link_health(name)
            except VaultError:
                continue
            snaps = log.snapshots(name, 90)
            growth = (NotStr(charts.stacked_bars(snaps, [("notes", "Notes")], label_key="day",
                                                 label_fmt=lambda d: d[5:], aria="Notes over time"))
                      if len(snaps) > 1 else empty("Growth appears here after a second day of snapshots."))
            summary = " · ".join([
                plural(links_["orphan_count"], "orphan"), plural(links_["broken_count"], "broken link"),
                plural(links_["untagged_count"], "untagged note"),
            ])
            sections.append(Section(
                H2(f"Vault: {name}" if len(_vaults()) > 1 else "Vault"),
                Div(*[Div(Tag(v), Span(k)) for k, v in (
                    ("notes", f"{size['notes']:,}"), ("words", f"{size['words']:,}"),
                    ("folders", f"{size['folders']:,}"), ("attachments", f"{size['attachments']:,}"),
                    ("PDFs", f"{size['pdfs']:,}"), ("on disk", bytes_label(size["bytes"])),
                )], cls="stats"),
                H2("Notes over time", style="margin-top:16px"), growth,
                Details(
                    Summary(B("Link health"), Span(f" · {summary}", cls="muted"), cls="small"),
                    _list_details(f"{plural(links_['orphan_count'], 'orphan')}: notes nothing links to",
                                  [Li(path_link(name, p)) for p in links_["orphans"]], links_["orphan_count"]),
                    _list_details(f"{plural(links_['broken_count'], 'broken link')}: links to notes that don't exist",
                                  [Li(path_link(name, b["note"]), Span(" → ", cls="muted"), b["target"])
                                   for b in links_["broken"]], links_["broken_count"]),
                    _list_details(plural(links_["untagged_count"], "untagged note"),
                                  [Li(path_link(name, p)) for p in links_["untagged"]], links_["untagged_count"]),
                    cls="health",
                ),
            ))
        return sections

    def recent_fragment() -> Any:
        return event_table(log.events(limit=20, vault=scope()))

    @app.get("/fragment/recent")
    def recent(session) -> Any:
        return recent_fragment()

    def errors_table(errors: list[dict]) -> Any:
        if not errors:
            return empty("No errors. ")
        return Div(Table(
            Thead(Tr(Th("Type"), Th("Count", cls="num"), Th("Last"), Th("Last message"))),
            Tbody(*[Tr(Td(e["type"]), Td(str(e["count"]), cls="num"),
                       Td(f"{fmt.short(e['last_ts'])} · {tool_label(e['last_tool'])}", cls="when"),
                       Td(e["last_error"] or "", cls="path small")) for e in errors]),
        ), cls="scroll")

    @app.get("/activity")
    def activity(session, days: int = 7, kind: str = "", tool: str = "", errors: str = "",
                 q: str = "", page_no: int = 1) -> Any:
        days = days if days in (1, 7, 30, 90) else 7
        kind = kind if kind in ("read", "write", "sync") else ""
        since = time.time() - days * 86400
        page_no = max(page_no, 1)
        focus = scope()
        filters = dict(since=since, kind=kind or None, tool=tool or None, errors_only=bool(errors),
                       path_contains=q or None, vault=focus)
        total = log.count(**filters)
        events = log.events(**filters, limit=PAGE_SIZE, offset=(page_no - 1) * PAGE_SIZE)
        tools = sorted({r["tool"] for r in (log.query("SELECT DISTINCT tool FROM events WHERE vault = ?", (focus,))
                                            if focus else log.query("SELECT DISTINCT tool FROM events"))})

        def opt(value: str, label: str, current: str) -> Any:
            return Option(label, value=value, selected=(value == current))

        form = Form(
            Select(*[opt(str(d), label, str(days)) for d, label in
                     ((1, "Last 24 h"), (7, "Last 7 days"), (30, "Last 30 days"), (90, "Last 90 days"))],
                   name="days", aria_label="Period"),
            Select(opt("", "Reads and changes", kind), opt("read", "Reads", kind), opt("write", "Changes", kind),
                   opt("sync", "Syncs", kind), name="kind", aria_label="Kind"),
            Select(opt("", "All tools", tool), *[opt(t, tool_label(t), tool) for t in tools],
                   name="tool", aria_label="Tool"),
            Input(type="search", name="q", value=q, placeholder="Path contains…", aria_label="Path contains"),
            Label(Input(type="checkbox", name="errors", value="1", checked=bool(errors)), " Errors only",
                  cls="small"),
            Button("Filter"),
            method="get", action=f"{base()}/activity", cls="filters",
        )
        params = {k: v for k, v in dict(days=days, kind=kind, tool=tool, errors=errors, q=q).items() if v}
        pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        prev_link = A("← Newer", href=f"{base()}/activity?{urlencode({**params, 'page_no': page_no - 1})}") \
            if page_no > 1 else Span("")
        next_link = A("Older →", href=f"{base()}/activity?{urlencode({**params, 'page_no': page_no + 1})}") \
            if page_no < pages else Span("")
        return shell(
            "Activity", "Activity", session,
            Section(H2("Activity", Small(plural(total, "event"))), form),
            Section(event_table(events, expandable=True),
                    Div(prev_link, Span(f"Page {page_no} of {pages}", cls="muted"), next_link, cls="pager")),
        )

    @app.get("/commit/{vault}/{sha}")
    def commit(session, vault: str, sha: str) -> Any:
        if vault not in _vaults():
            return Response(status_code=404)
        info = gitstats.commit_stat(vault, sha)
        if info is None:
            return P("Commit not found (it may have been rewritten).", cls="muted small")
        items = []
        for f in info["files"]:
            change = Span("binary", cls="muted") if f["added"] is None else \
                Span(Span(f"+{f['added']}", cls="add"), " ", Span(f"−{f['removed']}", cls="del"))
            items.append(Li(change, " ", f["path"]))
        return Ul(*items, cls="plain stat-files") if items else P("No file changes.", cls="muted small")

    @app.get("/notes")
    def notes_moved(session) -> Any:
        return RedirectResponse(f"{base()}/insights", status_code=301)

    # -- wikis: the vault as a file tree ---------------------------------------------

    def wiki_context(name: str) -> dict[str, Any]:
        return {"vault": name, "changes": _safe(lambda: gitstats.last_changes(name), {}),
                "reads": stats.read_counts(log, time.time() - 30 * 86400),
                "github": gitstats.web_url(name) is not None}

    def file_item(ctx: dict[str, Any], f: dict[str, Any], show_dir: bool = False) -> Any:
        """A file in the tree. A note opens in the viewer; an image or another file opens
        the file itself. With a GitHub remote, a GitHub button shows at the far right on
        hover (always, on touch screens)."""
        vault = ctx["vault"]
        name = f["name"][:-3] if f["kind"] == "note" else f["name"]
        label = [icon(f["kind"]), Span(name, cls="nm")]
        if show_dir and "/" in f["path"]:
            label.append(Span(f["path"].rsplit("/", 1)[0], cls="dir"))
        if f["kind"] == "note":
            link = A(*label, href=note_url(vault, f["path"]), cls="file", title=f["path"])
        else:
            link = A(*label, href=file_url(vault, f["path"]), cls="file", title=f["path"], target="_blank",
                     rel="noopener")
        github = gitstats.file_url(vault, f["path"]) if ctx["github"] else None
        gh = A(NotStr(GITHUB_MARK), href=github, cls="gh", target="_blank", rel="noopener", title="Open on GitHub",
               aria_label=f"Open {name} on GitHub") if github else ""
        meta = []
        reads = ctx["reads"].get((vault, f["path"]))
        if reads:
            meta.append(Span(plural(reads, "read"), cls="reads", title="Claude's reads, last 30 days"))
        change = ctx["changes"].get(f["path"])
        if change:
            if change[1]:
                meta.append(Span("Claude", cls="badge ai", title="Claude made the last change"))
            meta.append(Span(fmt.ago(change[0]).replace(" ago", ""), title=f"Last changed {fmt.when(change[0])}"))
        return Li(link, Span(*meta, cls="meta"), gh, cls="f")

    def dir_items(ctx: dict[str, Any], node: dict[str, Any], lazy: bool, depth: int = 0) -> list[Any]:
        items = []
        for d in node["dirs"]:
            summary = Summary(icon("folder"), Span(d["name"], cls="nm"), Span(str(d["count"]), cls="count"))
            if lazy and depth >= 0:
                query = urlencode({"path": d["path"]})
                items.append(Li(Details(
                    summary, Ul(Li("Loading…", cls="muted small f")),
                    hx_get=f"{base()}/wikis/dir/{ctx['vault']}?{query}", hx_trigger="toggle once",
                    hx_target="find ul", hx_swap="innerHTML",
                )))
            else:
                items.append(Li(Details(summary, Ul(*dir_items(ctx, d, lazy, depth + 1)))))
        items.extend(file_item(ctx, f) for f in node["files"])
        if not items:
            items.append(Li("Empty folder", cls="muted small f"))
        return items

    def vault_tree(name: str, vault: Any) -> Any:
        root = stats.tree(vault)
        ctx = wiki_context(name)
        lazy = root["size"] > LAZY_TREE_ENTRIES
        body = Ul(*dir_items(ctx, root, lazy))
        if len(in_scope()) == 1:
            return body
        return Ul(Li(Details(
            Summary(icon("vault"), Span(name, cls="nm"), Span(str(root["count"]), cls="count")), body, open=True,
        )))

    @app.get("/wikis")
    def wikis(session) -> Any:
        vaults = in_scope()
        trees = [vault_tree(name, vault) for name, vault in vaults.items()]
        total = sum(stats.tree(v)["count"] for v in vaults.values())
        search = Input(
            type="search", name="q", placeholder="Filter files…", aria_label="Filter files", autocomplete="off",
            hx_get=f"{base()}/wikis/search", hx_trigger="input changed delay:250ms, search",
            hx_target="#tree", hx_swap="innerHTML", cls="filter",
        )
        on_github = any(gitstats.web_url(name) for name in vaults)
        legend = P("Click a note to read it" + (", or the GitHub mark beside it to open it on GitHub. " if on_github
                                                else ". "),
                   Span("Claude", cls="badge ai"), " marks notes Claude changed last; read counts cover the last 30 days.",
                   cls="muted small")
        return shell(
            "Wikis", "Wikis", session,
            NotStr(ICONS),
            Section(
                Div(H2("Wikis", Small(plural(total, "file"))), search, cls="bar"),
                Div(*trees, cls="tree", id="tree") if trees else empty("No vault is configured."),
                legend,
            ),
        )

    @app.get("/wikis/dir/{vault}")
    def wiki_dir(session, vault: str, path: str = "") -> Any:
        found = _vaults().get(vault)
        node = stats.find_dir(stats.tree(found), path) if found else None
        if node is None:
            return Response(status_code=404)
        return tuple(dir_items(wiki_context(vault), node, lazy=True))

    @app.get("/wikis/search")
    def wiki_search(session, q: str = "") -> Any:
        vaults = in_scope()
        if not q.strip():
            return tuple(vault_tree(name, vault) for name, vault in vaults.items())
        results, total = [], 0
        for name, vault in vaults.items():
            found, n = stats.search_tree(stats.tree(vault), q)
            total += n
            ctx = wiki_context(name)
            if len(vaults) > 1 and found:
                results.append(Li(B(name), cls="small vault-label"))
            results.extend(file_item(ctx, f, show_dir=True) for f in found)
        if not total:
            return empty(f"No files match \u201c{q}\u201d.")
        shown = sum(1 for r in results if "f" in (r.attrs.get("class") or "").split())
        more = P(f"Showing {shown} of {total}.", cls="muted small") if total > shown else ""
        return Ul(*results, cls="results"), more

    # -- the note viewer: a note, rendered (render/), read-only ------------------------------

    def wiki_page(title: str, session, *content: Any, status: int = 200) -> Any:
        response = shell(title, "Wikis", session, Link(rel="stylesheet", href=asset("note.css")), *content)
        response.status_code = status
        return response

    def gone(session, heading: str, text: str, status: int = 404) -> Any:
        return wiki_page(heading, session, Section(
            Div(Div(A("Wikis", href=f"{base()}/wikis"), cls="crumbs"), cls="note-top"),
            Div(H1(heading, cls="note-title"), P(text, cls="muted"), cls="note-head"),
            cls="note-page"), status=status)

    def crumbs(vault: str, path: str) -> Any:
        parts: list[Any] = [A("Wikis", href=f"{base()}/wikis")]
        if len(_vaults()) > 1:
            parts += [Span("/", cls="sep"), Span(vault)]
        for folder in path.split("/")[:-1]:
            parts += [Span("/", cls="sep"), Span(folder)]
        return Div(*parts, cls="crumbs")

    def vault_context(vault: str, index: Any) -> Any:
        found = _vaults()[vault]
        return viewer.context(
            index, viewer.Files(stats.tree(found)), own_prefix=f"{base()}/",
            note_url=lambda p: note_url(vault, p), file_url=lambda p: file_url(vault, p),
            open_url=lambda n: open_url(vault, n), tag_url=lambda t: tag_url(vault, t),
        )

    @app.get("/wikis/note/{vault}")
    def wiki_note(session, vault: str, path: str = "") -> Any:
        if vault not in _vaults():
            return gone(session, "Note not found", "There's no such vault here.")
        with use_vault(vault):
            located = viewer.locate(path) if path.lower().endswith(".md") else None
            if located is None:
                return gone(session, "Note not found",
                            "It may have been moved or deleted. Notes change on every device, so try Wikis.")
            rel, target = located
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return gone(session, "Note not found", "This note can't be read right now.")
            index = links.VaultIndex()
            ctx = vault_context(vault, index)
            note = render_note(text, rel, ctx, strict_breaks=viewer.strict_line_breaks())
            linked = viewer.backlinks(index, rel)
        github = gitstats.file_url(vault, rel)
        change = _safe(lambda: gitstats.last_changes(vault), {}).get(rel)
        meta: list[Any] = []
        if change:
            meta.append(Span(f"Changed {fmt.ago(change[0])}", title=fmt.when(change[0])))
            if change[1]:
                meta.append(Span("by Claude", cls="by-claude"))
        reads = stats.read_counts(log, time.time() - 30 * 86400).get((vault, rel))
        if reads:
            meta.append(Span(f"{plural(reads, 'read')} by Claude in 30 days"))
        actions = A(NotStr(GITHUB_MARK), "GitHub", href=github, cls="btn-gh", target="_blank", rel="noopener",
                    title="Open on GitHub") if github else ""
        page = [
            Section(
                Div(crumbs(vault, rel), Div(actions, cls="note-actions"), cls="note-top"),
                Div(H1(note.title, cls="note-title"), Div(*meta, cls="note-meta") if meta else "", cls="note-head"),
                NotStr(note.properties) if note.properties else "",
                P("This note is over 1 MB, so it's shown as plain text.", cls="muted small big-note")
                if note.plain else "",
                # note.js reads these: where its files are, and how a diagram opens a note by name.
                Article(NotStr(note.html), cls="note", data_note="1", data_assets=f"{base()}/static/{assets.digest()}/",
                        data_open=f"{base()}/wikis/open/{quote(vault, safe='')}?{query(src=rel)}&name="),
                cls="note-page",
            ),
        ]
        if linked:
            items = [Li(A(p.rsplit("/", 1)[-1][:-3], href=note_url(vault, p)),
                        Span(p.rsplit("/", 1)[0], cls="dir") if "/" in p else "") for p in linked]
            page.append(Section(Div(H2("Linked from", Small(plural(len(linked), "note"))), Ul(*items),
                                    cls="linked")))
        page.append(Script(src=asset("note.js"), defer=True))
        return wiki_page(note.title, session, *page)

    @app.get("/wikis/file/{vault}")
    def wiki_file(session, vault: str, path: str = "") -> Any:
        """A file from the vault: an image is shown, anything else downloads."""
        if vault not in _vaults():
            return Response("Not found", status_code=404)
        with use_vault(vault):
            located = viewer.locate(path)
        if located is None:
            return Response("Not found", status_code=404)
        rel, target = located
        kind = viewer.INLINE_TYPES.get(rel.rsplit(".", 1)[-1].lower() if "." in rel else "")
        if kind:
            return FileResponse(target, media_type=kind, headers={
                "content-disposition": "inline",
                "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            })
        return FileResponse(target, media_type="application/octet-stream", filename=target.name)

    @app.get("/wikis/open/{vault}")
    def wiki_open(session, vault: str, name: str = "", src: str = "") -> Any:
        """A link by name (from a diagram, or to a note that doesn't exist yet)."""
        if vault not in _vaults() or not name.strip():
            return gone(session, "Note not found", "There's no such note here.")
        with use_vault(vault):
            index = links.VaultIndex()
            found = index.resolve(name, from_note=src or None)
        if found:
            return RedirectResponse(note_url(vault, found), status_code=303)
        return gone(session, f"No note called “{name}” yet",
                    "Something links to it, but it doesn't exist. In Obsidian, clicking the link creates it.")

    @app.get("/wikis/tag/{vault}")
    def wiki_tag(session, vault: str, tag: str = "") -> Any:
        """The notes with a tag (nested tags included, as in Obsidian)."""
        tag = tag.strip().lstrip("#")
        if vault not in _vaults() or not tag:
            return gone(session, "Tag not found", "There's no such tag here.")
        with use_vault(vault):
            found = sorted(links.notes_with_tag(tag), key=str.casefold)
        items = [Li(A(p.rsplit("/", 1)[-1][:-3], href=note_url(vault, p)),
                    Span(p.rsplit("/", 1)[0], cls="dir") if "/" in p else "") for p in found]
        return wiki_page(f"#{tag}", session, Section(
            Div(crumbs(vault, ""), cls="note-top"),
            Div(H1(f"#{tag}", cls="note-title"), Div(Span(plural(len(found), "note")), cls="note-meta"),
                cls="note-head"),
            Div(Ul(*items), cls="linked tagged") if items else P("No notes have this tag.", cls="muted"),
            cls="note-page"))

    @app.get("/static/{digest}/{name:path}")
    def static_file(digest: str, name: str) -> Any:
        """The viewer's script and stylesheet, KaTeX and Pele (assets.py). Any digest works:
        a page from before a deploy still gets today's files."""
        if name == assets.GENERATED:
            return Response(assets.note_css(), media_type=assets.content_type(name))
        path = assets.files().get(name)
        if path is None:
            return Response("Not found", status_code=404)
        return FileResponse(path, media_type=assets.content_type(name))

    def _list_details(summary: str, items: list[Any], total: int) -> Any:
        if not total:
            return P(summary, cls="small")
        more = P(f"Showing the first {len(items)}.", cls="muted small") if total > len(items) else ""
        return Details(Summary(summary, cls="small"), Ul(*items, cls="plain small"), more)

    @app.get("/health")
    async def health(session) -> Any:
        focus = scope()
        sync_rows = stats.sync_health(log, vault=focus)
        week = clock.start_of(clock.days(7)[0])
        client_rows = stats.clients(log, vault=focus)
        names: dict[str, str | None] = {}
        if client_names is not None:
            for row in client_rows:
                cid = row["client_id"]
                if cid not in names:
                    try:
                        names[cid] = await client_names(cid)
                    except Exception:  # noqa: BLE001 - a lookup failure only hides the name
                        names[cid] = None

        def sync_state(h: dict) -> Any:
            label = {"ok": ("ok", "In sync"), "warn": ("warn", f"{plural(h['ahead'], 'commit')} not pushed"),
                     "error": ("err", "Failing"), "local": ("warn", "No remote")}[h["state"]]
            return Span(label[1], cls=f"badge {label[0]}")

        sync_table = Div(Table(
            Thead(Tr(Th("Vault"), Th("State"), Th("Last pull"), Th("Last push"), Th("Not pushed", cls="num"), Th("Not pulled", cls="num"))),
            Tbody(*[Tr(Td(h["vault"]), Td(sync_state(h)), Td(fmt.short(h["last_pull"]), cls="when"),
                       Td(fmt.short(h["last_push"]), cls="when"), Td(str(h["ahead"]), cls="num"),
                       Td(str(h["behind"]), cls="num")) for h in sync_rows]),
        ), cls="scroll")
        problems = []
        for h in sync_rows:
            for label, key in (("Pull", "pull_error"), ("Push", "push_error")):
                if h[key]:
                    problems.append(P(Span(f"{h['vault']} · {label}: ", cls="muted"), h[key], cls="err-text"))
        recent_errors = [e for h in sync_rows for e in h["recent_errors"]]
        recent_sync = Div(Table(
            Thead(Tr(Th("When"), Th("Tool"), Th("Note"), Th("Error"))),
            Tbody(*[Tr(Td(fmt.short(e["ts"]), cls="when"), Td(tool_label(e["tool"])),
                       Td(e["path"] or "—", cls="path"), Td((e["sync"] or "")[7:], cls="path small"))
                    for e in sorted(recent_errors, key=lambda e: -e["ts"])[:10]]),
        ), cls="scroll") if recent_errors else empty("No sync errors recorded.")

        clients_table = Div(Table(
            Thead(Tr(Th("Client"), Th("Signed in as"), Th("Calls", cls="num"), Th("First seen"), Th("Last seen"))),
            Tbody(*[Tr(Td(names.get(r["client_id"]) or _client_label(r["client_id"]),
                          Div(Code(r["client_id"][:24]), cls="muted")),
                       Td(r["user"] or "—"), Td(str(r["n"]), cls="num"),
                       Td(fmt.short(r["first_ts"]), cls="when"), Td(fmt.short(r["last_ts"]), cls="when"))
                    for r in client_rows]),
        ), cls="scroll") if client_rows else empty("No clients have called the server yet.")

        lat = stats.latency(log, week, vault=focus)
        latency_table = Div(Table(
            Thead(Tr(Th("Tool"), Th("Calls", cls="num"), Th("Median", cls="num"), Th("95th pct", cls="num"), Th("Slowest", cls="num"))),
            Tbody(*[Tr(Td(tool_label(r["tool"])), Td(str(r["count"]), cls="num"),
                       Td(f"{r['median']} ms", cls="num"), Td(f"{r['p95']} ms", cls="num"),
                       Td(f"{r['max']} ms", cls="num")) for r in lat]),
        ), cls="scroll") if lat else empty("No calls in the last 7 days.")

        rows = log.query("SELECT COUNT(*) AS n, MIN(ts) AS first FROM events")[0]
        retention = "forever" if settings.activity_days == 0 else plural(settings.activity_days, "day")
        sign_in = f"{brand} account" if external is not None else "GitHub" if github else "Password"
        info = [
            ("Version", __version__), ("Up since", f"{fmt.when(stats.STARTED_AT)} ({fmt.ago(stats.STARTED_AT)})"),
            ("Sign-in", sign_in),
            ("Vaults", ", ".join(stats.vault_names()) or "none"),
            ("Activity log", f"{rows['n']:,} events since {fmt.when(rows['first'])}, {bytes_label(log.size_bytes())}, "
                             f"kept {retention}"),
            ("Time zone", zone().key), ("Pull interval", f"{settings.pull_interval:g} s"),
        ]
        return shell(
            "Health", "Health", session,
            Section(H2("Sync"), sync_table, *problems),
            Section(H2("Recent sync errors"), recent_sync),
            Section(H2("Clients", Small("seen in the activity log")), clients_table),
            Section(H2("Speed", Small("last 7 days")), latency_table),
            Section(H2("Server"), Table(Tbody(*[Tr(Th(k), Td(v)) for k, v in info]))),
        )

    return app


LAZY_TREE_ENTRIES = 1500  # above this, folders load their contents when opened

_ICON_PATHS = {
    "folder": '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2h9A1.5 1.5 0 0 1 21 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z"/>',
    "vault": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 4v16"/>',
    "note": '<path d="M14 3H7a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V7z"/><path d="M14 3v4h4M9 12h6M9 16h4"/>',
    "pdf": '<path d="M14 3H7a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V7z"/><path d="M14 3v4h4M9 15h6"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m21 16-5-5-8 9"/>',
    "file": '<path d="M14 3H7a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V7z"/><path d="M14 3v4h4"/>',
}
# GitHub's mark (Octicons, MIT), in the current text colour.
GITHUB_MARK = (
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17'
    '.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01'
    ' 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15'
    '-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44'
    ' 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2'
    ' 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>'
)
ICONS = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true">'
         + "".join(f'<symbol id="i-{k}" viewBox="0 0 24 24">{v}</symbol>' for k, v in _ICON_PATHS.items())
         + "</svg>")


def icon(kind: str) -> Any:
    return NotStr(f'<svg class="ic {kind}" aria-hidden="true"><use href="#i-{kind}"/></svg>')


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except VaultError:
        return default


def Tag(text: str) -> Any:  # noqa: N802 - FT-style component
    return B(text)


def _client_label(client_id: str) -> str:
    if client_id == "static-token":
        return "Static token (Claude Code)"
    return "MCP client"


def _vaults() -> dict[str, Any]:
    try:
        return configured_vaults()
    except VaultError:
        return {}


def where(vault: Any) -> str:
    """Where a vault lives, for the switcher: the embedding app's label, else its remote."""
    if vault.label:
        return vault.label
    if not vault.remote:
        return "Local folder"
    found = re.match(r"^(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?$", vault.remote)
    return f"GitHub · {found.group(1)}" if found else "Git"

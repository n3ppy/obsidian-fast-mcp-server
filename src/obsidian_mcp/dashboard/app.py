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

import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx2 as httpx
from fasthtml.common import (
    A, B, Beforeware, Body, Button, Code, Details, Div, FastHTML, Form, H1, H2, Head, Html, Input,
    Label, Li, NotStr, Option, P, Section, Select, Small, Span, Summary, Table, Tbody, Td, Th, Thead,
    Tr, Ul, to_xml,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .. import __version__, gitstats, workspace
from ..activity import ActivityLog, ScopedLog
from ..config import Settings
from ..vault import VaultError, configured_vaults
from . import charts, stats
from .external import ExternalAuth
from .ui import BASE, Fmt, bytes_label, empty, head, login_page, page, plural, tool_label

GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_USER = "https://api.github.com/user"
CALLBACK_PATH = "/auth/callback"  # the OAuth App's own callback, shared with MCP login
STATE_PREFIX = "dash."  # marks a dashboard sign-in so the server routes it to /dash
SESSION_SECONDS = 7 * 24 * 3600
COOKIE = "obsidian_dash"
PAGE_SIZE = 50
PUBLIC_PATHS = ("/login", "/login/github", "/callback")

CSP = (
    "default-src 'none'; script-src https://cdn.jsdelivr.net; style-src 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)

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
    """ASGI middleware: no caching, no framing, strict CSP on every dashboard response."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"cache-control", b"no-store"),
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
    return path[len(BASE):] if path.startswith(BASE) else path


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

    def shell(title: str, active: str, session, *content: Any) -> tuple[Any, ...]:
        if external is not None:
            return page(title, active, external.user(), external.csrf(), *content, brand=brand,
                        extra_nav=external.nav, logout_action=external.logout_url)
        return page(title, active, current_user(session), session.get("csrf"), *content)

    # -- sign in ---------------------------------------------------------------------

    def login_form(error: str | None = None, status: int = 200) -> Any:
        intro = P("See what Claude has done in your vault.", cls="muted")
        if github:
            body = [H1("Vault dashboard"), intro]
            if error:
                body.append(P(error, cls="err-text"))
            body.append(A("Sign in with GitHub", href=f"{BASE}/login/github", cls="primary"))
        else:
            body = [
                H1("Vault dashboard"), intro,
                P(error, cls="err-text") if error else "",
                Form(
                    Input(type="password", name="password", placeholder="Password",
                          autocomplete="current-password", autofocus=True, required=True),
                    Button("Sign in", cls="primary"),
                    method="post", action=f"{BASE}/login",
                ),
            ]
        return _html(login_page(*body), status)

    def _html(content: tuple[Any, ...], status: int = 200) -> HTMLResponse:
        """A full page with a chosen status code (FastHTML renders 200 by default)."""
        titles = [c for c in content if getattr(c, "tag", None) == "title"]
        body = [c for c in content if getattr(c, "tag", None) != "title"]
        doc = Html(Head(*titles, *head("")), Body(*body), lang="en")
        return HTMLResponse("<!doctype html>\n" + to_xml(doc), status_code=status)

    @app.get("/login")
    def login_get(session) -> Any:
        if current_user(session):
            return RedirectResponse(f"{BASE}/", status_code=303)
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
        return RedirectResponse(f"{BASE}/", status_code=303)

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
        return RedirectResponse(f"{BASE}/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request, session) -> Any:
        if external is not None:
            return Response(status_code=404)  # the embedding app signs people out
        form = await request.form()
        token = session.get("csrf")
        if not token or not secrets.compare_digest(str(form.get("csrf", "")), token):
            return Response("Invalid request", status_code=403)
        session.clear()
        return RedirectResponse(f"{BASE}/login", status_code=303)

    # -- pieces ----------------------------------------------------------------------

    def path_link(vault: str | None, path: str | None) -> Any:
        if not path:
            return Span("—", cls="muted")
        url = gitstats.file_url(vault, path) if vault and path.endswith((".md", ".pdf")) else None
        return A(path, href=url, target="_blank", rel="noopener") if url else path

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
        multi = len(stats.vault_names()) > 1
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
                    Button("Show files", cls="link", hx_get=f"{BASE}/commit/{e['vault']}/{e['commit_sha']}",
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
    def overview(session) -> Any:
        daily = stats.daily_activity(log, clock, 30)
        health = stats.sync_health(log)
        week = clock.start_of(clock.days(7)[0])
        errors = stats.errors_by_type(log, week)
        reads_series = [("reads", "Claude reads")]
        change_series = [("claude_commits", "By Claude"), ("device_commits", "From your devices")]
        banner = external.banner() if external is not None and external.banner is not None else None

        return shell(
            "Overview", "Overview", session,
            banner if banner is not None else "",
            status_strip(health),
            Div(*[tile(t) for t in stats.tiles(daily)], cls="tiles"),
            Div(
                Section(H2("Claude's reads", Small("last 30 days")),
                        NotStr(charts.stacked_bars(daily, reads_series, label_fmt=day_label, aria="Reads per day")),
                        NotStr(charts.legend(reads_series))),
                Section(H2("Changes", Small("git commits, last 30 days")),
                        NotStr(charts.stacked_bars(daily, change_series, label_fmt=day_label,
                                                   aria="Commits per day")),
                        NotStr(charts.legend(change_series))),
                cls="grid2",
            ),
            Section(H2("Recent activity", Small("refreshes every 30 s")),
                    Div(recent_fragment(), hx_get=f"{BASE}/fragment/recent", hx_trigger="every 30s",
                        hx_swap="innerHTML"),
                    P(A("All activity →", href=f"{BASE}/activity"), cls="small")),
            Section(H2("Errors", Small("last 7 days")), errors_table(errors)),
        )

    @app.get("/insights")
    def insights(session, days: int = 30) -> Any:
        days = days if days in (7, 30, 90) else 30
        stats.ensure_snapshots(log, clock)
        return shell("Insights", "Insights", session, *top_notes(days), *vault_sections())

    def top_notes(days: int) -> list[Any]:
        since = time.time() - days * 86400
        top_read = stats.top_read(log, since)
        top_changed = stats.top_changed(days)
        switch = Div(*[A(f"{d} days", href=f"{BASE}/insights?days={d}", cls="on" if d == days else None)
                       for d in (7, 30, 90)], cls="switch")
        read_table = Div(Table(
            Thead(Tr(Th("Note"), Th("Reads"), Th("Last read"))),
            Tbody(*[Tr(path_cell(r["vault"], r["path"]), Td(str(r["n"]), cls="num"),
                       Td(fmt.short(r["last_ts"]), cls="when")) for r in top_read]),
        ), cls="scroll") if top_read else empty("No notes read by Claude in this period.")
        changed_table = Div(Table(
            Thead(Tr(Th("File"), Th("Commits"), Th("By Claude"), Th("From devices"))),
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
        vaults = _vaults()
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
                H2(f"Vault: {name}" if len(vaults) > 1 else "Vault"),
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
        return event_table(log.events(limit=20))

    @app.get("/fragment/recent")
    def recent(session) -> Any:
        return recent_fragment()

    def errors_table(errors: list[dict]) -> Any:
        if not errors:
            return empty("No errors. ")
        return Div(Table(
            Thead(Tr(Th("Type"), Th("Count"), Th("Last"), Th("Last message"))),
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
        filters = dict(since=since, kind=kind or None, tool=tool or None, errors_only=bool(errors),
                       path_contains=q or None)
        total = log.count(**filters)
        events = log.events(**filters, limit=PAGE_SIZE, offset=(page_no - 1) * PAGE_SIZE)
        tools = sorted({r["tool"] for r in log.query("SELECT DISTINCT tool FROM events")})

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
            method="get", action=f"{BASE}/activity", cls="filters",
        )
        params = {k: v for k, v in dict(days=days, kind=kind, tool=tool, errors=errors, q=q).items() if v}
        pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        prev_link = A("← Newer", href=f"{BASE}/activity?{urlencode({**params, 'page_no': page_no - 1})}") \
            if page_no > 1 else Span("")
        next_link = A("Older →", href=f"{BASE}/activity?{urlencode({**params, 'page_no': page_no + 1})}") \
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
        return RedirectResponse(f"{BASE}/insights", status_code=301)

    # -- wikis: the vault as a file tree ---------------------------------------------

    def wiki_context(name: str) -> dict[str, Any]:
        return {"vault": name, "changes": _safe(lambda: gitstats.last_changes(name), {}),
                "reads": stats.read_counts(log, time.time() - 30 * 86400)}

    def file_item(ctx: dict[str, Any], f: dict[str, Any], show_dir: bool = False) -> Any:
        vault = ctx["vault"]
        name = f["name"][:-3] if f["kind"] == "note" else f["name"]
        label = [icon(f["kind"]), Span(name, cls="nm")]
        if show_dir and "/" in f["path"]:
            label.append(Span(f["path"].rsplit("/", 1)[0], cls="dir"))
        url = gitstats.file_url(vault, f["path"])
        link = A(*label, href=url, target="_blank", rel="noopener", cls="file", title=f["path"]) if url \
            else Span(*label, cls="file", title=f["path"])
        meta = []
        reads = ctx["reads"].get((vault, f["path"]))
        if reads:
            meta.append(Span(plural(reads, "read"), cls="reads", title="Claude's reads, last 30 days"))
        change = ctx["changes"].get(f["path"])
        if change:
            if change[1]:
                meta.append(Span("Claude", cls="badge ai", title="Claude made the last change"))
            meta.append(Span(fmt.ago(change[0]).replace(" ago", ""), title=f"Last changed {fmt.when(change[0])}"))
        return Li(link, Span(*meta, cls="meta"), cls="f")

    def dir_items(ctx: dict[str, Any], node: dict[str, Any], lazy: bool, depth: int = 0) -> list[Any]:
        items = []
        for d in node["dirs"]:
            summary = Summary(icon("folder"), Span(d["name"], cls="nm"), Span(str(d["count"]), cls="count"))
            if lazy and depth >= 0:
                query = urlencode({"path": d["path"]})
                items.append(Li(Details(
                    summary, Ul(Li("Loading…", cls="muted small f")),
                    hx_get=f"{BASE}/wikis/dir/{ctx['vault']}?{query}", hx_trigger="toggle once",
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
        if len(_vaults()) == 1:
            return body
        return Ul(Li(Details(
            Summary(icon("vault"), Span(name, cls="nm"), Span(str(root["count"]), cls="count")), body, open=True,
        )))

    @app.get("/wikis")
    def wikis(session) -> Any:
        vaults = _vaults()
        trees = [vault_tree(name, vault) for name, vault in vaults.items()]
        total = sum(stats.tree(v)["count"] for v in vaults.values())
        search = Input(
            type="search", name="q", placeholder="Filter files…", aria_label="Filter files", autocomplete="off",
            hx_get=f"{BASE}/wikis/search", hx_trigger="input changed delay:250ms, search",
            hx_target="#tree", hx_swap="innerHTML", cls="filter",
        )
        legend = P("Click a note to open it on GitHub. ", Span("Claude", cls="badge ai"),
                   " marks notes Claude changed last; read counts cover the last 30 days.",
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
        vaults = _vaults()
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

    def _list_details(summary: str, items: list[Any], total: int) -> Any:
        if not total:
            return P(summary, cls="small")
        more = P(f"Showing the first {len(items)}.", cls="muted small") if total > len(items) else ""
        return Details(Summary(summary, cls="small"), Ul(*items, cls="plain small"), more)

    @app.get("/health")
    async def health(session) -> Any:
        sync_rows = stats.sync_health(log)
        week = clock.start_of(clock.days(7)[0])
        client_rows = stats.clients(log)
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
            Thead(Tr(Th("Vault"), Th("State"), Th("Last pull"), Th("Last push"), Th("Not pushed"), Th("Not pulled"))),
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
            Thead(Tr(Th("Client"), Th("Signed in as"), Th("Calls"), Th("First seen"), Th("Last seen"))),
            Tbody(*[Tr(Td(names.get(r["client_id"]) or _client_label(r["client_id"]),
                          Div(Code(r["client_id"][:24]), cls="muted")),
                       Td(r["user"] or "—"), Td(str(r["n"]), cls="num"),
                       Td(fmt.short(r["first_ts"]), cls="when"), Td(fmt.short(r["last_ts"]), cls="when"))
                    for r in client_rows]),
        ), cls="scroll") if client_rows else empty("No clients have called the server yet.")

        lat = stats.latency(log, week)
        latency_table = Div(Table(
            Thead(Tr(Th("Tool"), Th("Calls"), Th("Median"), Th("95th pct"), Th("Slowest"))),
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

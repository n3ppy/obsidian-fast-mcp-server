"""The activity log and the /dash dashboard.

The server runs in-process (ASGI transport), with a real git remote, so the log, the
commit links and the sync status are checked against real git operations."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fasthtml", reason="the dashboard extra is not installed")

import httpx2  # noqa: E402
from fastmcp import Client  # noqa: E402

from obsidian_mcp.activity import ActivityLog, Event  # noqa: E402
from obsidian_mcp.config import ConfigError, load_settings  # noqa: E402
from obsidian_mcp.server import build_server  # noqa: E402
from test_http import git  # noqa: E402

PASSWORD = "correct horse battery staple"
STATIC_TOKEN = "s" * 40
BASE_URL = "https://vault.test"


# -- fixtures --------------------------------------------------------------------------


@pytest.fixture
def vault_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    phone = tmp_path / "phone"
    git("clone", str(remote), str(phone), cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=phone)
    git("config", "user.name", "phone", cwd=phone)
    (phone / "Inbox").mkdir()
    (phone / "Inbox" / "Welcome.md").write_text("# Hi\n[[Other]] [[Missing]] #tag\n", encoding="utf-8")
    (phone / "Other.md").write_text("no tags\n", encoding="utf-8")
    git("add", "-A", cwd=phone)
    git("commit", "-m", "vault backup", cwd=phone)
    git("push", "-u", "origin", "main", cwd=phone)

    env = {
        "OBSIDIAN_MCP_TRANSPORT": "http",
        "OBSIDIAN_MCP_PUBLIC_URL": BASE_URL,
        "OBSIDIAN_MCP_PASSWORD": PASSWORD,
        "OBSIDIAN_MCP_TOKEN": STATIC_TOKEN,
        "OBSIDIAN_MCP_STATE_DIR": str(tmp_path / "state"),
        "OBSIDIAN_VAULT": str(tmp_path / "server-vault"),
        "OBSIDIAN_VAULT_REMOTE": str(remote),
        "OBSIDIAN_PULL_INTERVAL": "0",
        "OBSIDIAN_MCP_DASHBOARD": "true",
        "OBSIDIAN_MCP_TIMEZONE": "Australia/Perth",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ("OBSIDIAN_MCP_AUTH", "OBSIDIAN_EXTRA_VAULTS", "OBSIDIAN_VAULT_NAME"):
        monkeypatch.delenv(key, raising=False)
    from obsidian_mcp import sync

    sync._status.clear()  # noqa: SLF001 - module state from earlier tests
    sync.bootstrap_all()
    return {"tmp": tmp_path, "remote": remote, "phone": phone, "state": tmp_path / "state",
            "server_vault": tmp_path / "server-vault", "monkeypatch": monkeypatch}


def make_server():
    return build_server(load_settings())


def log_of(env: dict[str, Any]) -> ActivityLog:
    return ActivityLog(env["state"] / "activity.db")


async def call_tools(server: Any, calls: list[tuple[str, dict]]) -> None:
    async with Client(server) as client:
        for name, args in calls:
            await client.call_tool(name, args, raise_on_error=False)


class Web:
    """A browser against the server's HTTP app, in-process."""

    def __init__(self, server: Any) -> None:
        from obsidian_mcp.config import load_settings
        from obsidian_mcp.server import http_middleware

        self.app = server.http_app(path="/mcp", stateless_http=True,
                                   middleware=http_middleware(load_settings()))

    async def __aenter__(self) -> httpx2.AsyncClient:
        self._life = self.app.router.lifespan_context(self.app)
        await self._life.__aenter__()
        self.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url=BASE_URL)
        return self.client

    async def __aexit__(self, *exc: Any) -> None:
        await self.client.aclose()
        await self._life.__aexit__(*exc)


async def sign_in(web: httpx2.AsyncClient) -> None:
    resp = await web.post("/dash/login", data={"password": PASSWORD})
    assert resp.status_code == 303 and resp.headers["location"] == "/dash/"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- activity log ----------------------------------------------------------------------


def test_activity_log_filters_and_retention(tmp_path: Path) -> None:
    log = ActivityLog(tmp_path / "a.db", retention_days=30)
    now = time.time()
    log.record(Event(ts=now - 40 * 86400, tool="read_note", kind="read", ok=True, path="Old.md"))
    log.record(Event(ts=now, tool="read_note", kind="read", ok=True, path="100%_done.md"))
    log.record(Event(ts=now, tool="write_note", kind="write", ok=False, path="x.md", error="boom"))
    assert log.count() == 3
    assert log.prune() == 1
    assert [e["path"] for e in log.events(kind="read")] == ["100%_done.md"]
    assert [e["path"] for e in log.events(errors_only=True)] == ["x.md"]
    # LIKE wildcards in the filter are literal.
    assert log.count(path_contains="%_") == 1 and log.count(path_contains="0%_d") == 1
    assert log.count(path_contains="_x") == 0
    assert oct((tmp_path / "a.db").stat().st_mode & 0o777) == "0o600"


def test_every_tool_call_is_logged_without_contents(vault_env) -> None:
    server = make_server()
    secret = "my-private-search-phrase"
    run(call_tools(server, [
        ("read_note", {"path": "Inbox/Welcome"}),
        ("read_note", {"path": "Nope"}),
        ("search_notes", {"query": secret}),
        ("append_note", {"path": "Inbox/Welcome", "content": "a confidential sentence"}),
    ]))
    events = list(reversed(log_of(vault_env).events()))
    assert [(e["tool"], e["kind"], e["path"], e["ok"]) for e in events] == [
        ("read_note", "read", "Inbox/Welcome.md", 1),
        ("read_note", "read", "Nope.md", 0),
        ("search_notes", "read", None, 1),
        ("append_note", "write", "Inbox/Welcome.md", 1),
    ]
    assert events[1]["error_type"] == "VaultError"
    # The write records the commit that reached the remote, and the push outcome.
    remote_head = git("rev-parse", "main", cwd=vault_env["remote"]).strip()
    assert events[3]["commit_sha"] == remote_head and events[3]["sync"] == "pushed"
    # Neither the search query nor the text written is stored anywhere in the log.
    raw = b"".join(p.read_bytes() for p in vault_env["state"].glob("activity.db*"))
    assert secret.encode() not in raw and b"confidential" not in raw


# -- sign-in and access ------------------------------------------------------------------


def test_dashboard_requires_sign_in(vault_env) -> None:
    server = make_server()

    async def check() -> None:
        async with Web(server) as web:
            assert (await web.get("/dash")).headers["location"] == "/dash/"
            for path in ("/dash/", "/dash/activity", "/dash/insights", "/dash/wikis", "/dash/health", "/dash/fragment/recent"):
                resp = await web.get(path)
                assert resp.status_code == 303 and resp.headers["location"] == "/dash/login", path
            # htmx refreshes are told to go to the login page instead of rendering it inline.
            resp = await web.get("/dash/fragment/recent", headers={"HX-Request": "true"})
            assert resp.status_code == 401 and resp.headers["hx-redirect"] == "/dash/login"
            # The MCP's static token does not open the dashboard.
            resp = await web.get("/dash/", headers={"Authorization": f"Bearer {STATIC_TOKEN}"})
            assert resp.status_code == 303
            login = await web.get("/dash/login")
            assert login.status_code == 200 and 'type="password"' in login.text
            assert "no-store" in login.headers["cache-control"]
            assert "frame-ancestors 'none'" in login.headers["content-security-policy"]

    run(check())


def test_password_login_session_and_logout(vault_env) -> None:
    server = make_server()

    async def check() -> None:
        async with Web(server) as web:
            assert (await web.post("/dash/login", data={"password": "wrong"})).status_code == 401
            resp = await web.post("/dash/login", data={"password": PASSWORD})
            cookie = resp.headers["set-cookie"]
            assert "path=/dash" in cookie.lower() and "httponly" in cookie.lower() and "secure" in cookie.lower()
            assert "samesite=lax" in cookie.lower()
            page = await web.get("/dash/")
            assert page.status_code == 200 and "Vault dashboard" in page.text
            # A dashboard session is not an MCP token.
            mcp = await web.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                 headers={"Accept": "application/json, text/event-stream"})
            assert mcp.status_code == 401
            # Logout needs the CSRF token from the page.
            assert (await web.post("/dash/logout", data={"csrf": "forged"})).status_code == 403
            csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
            assert (await web.post("/dash/logout", data={"csrf": csrf})).status_code == 303
            assert (await web.get("/dash/")).status_code == 303

    run(check())


def test_password_lockout_is_shared_with_the_mcp_login(vault_env) -> None:
    server = make_server()

    async def check() -> None:
        async with Web(server) as web:
            for _ in range(5):
                assert (await web.post("/dash/login", data={"password": "wrong"})).status_code == 401
            resp = await web.post("/dash/login", data={"password": PASSWORD})
            assert resp.status_code == 429

    run(check())


@pytest.fixture
def github_env(vault_env) -> dict[str, Any]:
    mp = vault_env["monkeypatch"]
    mp.setenv("OBSIDIAN_MCP_AUTH", "github")
    mp.setenv("OBSIDIAN_MCP_GITHUB_CLIENT_ID", "Ov23litest")
    mp.setenv("OBSIDIAN_MCP_GITHUB_CLIENT_SECRET", "x" * 40)
    mp.setenv("OBSIDIAN_MCP_ALLOWED_USERS", "hconnorh")
    mp.delenv("OBSIDIAN_MCP_PASSWORD")
    logins = {"code-owner": "HConnorH", "code-stranger": "mallory"}

    async def fake_github_login(code: str, redirect_uri: str, client_id: str, secret: str) -> str | None:
        assert redirect_uri == BASE_URL + "/auth/callback"
        return logins.get(code)

    from obsidian_mcp.dashboard import app as dash_app

    mp.setattr(dash_app, "github_login", fake_github_login)
    return vault_env


def test_github_login_and_allowlist(github_env) -> None:
    server = make_server()

    async def start(web: httpx2.AsyncClient) -> str:
        resp = await web.get("/dash/login/github")
        assert resp.status_code == 303
        target = httpx2.URL(resp.headers["location"])
        assert str(target).startswith("https://github.com/login/oauth/authorize")
        assert target.params["redirect_uri"] == BASE_URL + "/auth/callback"
        assert target.params["state"].startswith("dash.")
        assert target.params["scope"] == "read:user"
        return target.params["state"]

    async def check() -> None:
        async with Web(server) as web:
            assert (await web.post("/dash/login", data={"password": "x"})).status_code == 404
            # GitHub returns under the OAuth App's callback URL; the server forwards it.
            state = await start(web)
            hop = await web.get(f"/auth/callback?code=code-owner&state={state}")
            assert hop.status_code == 303 and hop.headers["location"].startswith("/dash/callback?")
            # MCP sign-ins (no dash. prefix) still reach FastMCP's own callback.
            mcp_hop = await web.get("/auth/callback?code=x&state=mcp-state")
            assert not mcp_hop.headers.get("location", "").startswith("/dash/")
            # A forged or replayed state is refused.
            assert (await web.get("/dash/callback?code=code-owner&state=forged")).status_code == 400
            state = await start(web)
            assert (await web.get(f"/dash/callback?code=code-stranger&state={state}")).status_code == 403
            assert (await web.get("/dash/")).status_code == 303
            state = await start(web)
            resp = await web.get(f"/dash/callback?code=code-owner&state={state}")
            assert resp.status_code == 303 and resp.headers["location"] == "/dash/"
            page = await web.get("/dash/")
            assert page.status_code == 200 and "HConnorH" in page.text

    run(check())


def test_removing_a_user_ends_their_dashboard_session(github_env) -> None:
    from obsidian_mcp.dashboard import app as dash_app

    settings = load_settings()
    current = {"users": settings.allowed_users}

    real_build = dash_app.build_app

    def build_with_mutable_list(settings, *args, **kwargs):
        app = real_build(settings, *args, **kwargs)
        build_with_mutable_list.settings = settings
        return app

    github_env["monkeypatch"].setattr(dash_app, "build_app", build_with_mutable_list)
    server = make_server()

    async def check() -> None:
        async with Web(server) as web:
            state = (httpx2.URL((await web.get("/dash/login/github")).headers["location"])).params["state"]
            await web.get(f"/dash/callback?code=code-owner&state={state}")
            assert (await web.get("/dash/")).status_code == 200
            object.__setattr__(build_with_mutable_list.settings, "allowed_users", ("someone-else",))
            assert (await web.get("/dash/")).status_code == 303
            object.__setattr__(build_with_mutable_list.settings, "allowed_users", current["users"])

    run(check())


# -- pages -----------------------------------------------------------------------------


def test_pages_show_activity_sync_and_vault(vault_env) -> None:
    server = make_server()
    run(call_tools(server, [
        ("read_note", {"path": "Inbox/Welcome"}),
        ("read_note", {"path": "Inbox/Welcome"}),
        ("read_note", {"path": "Nope"}),
        ("append_note", {"path": "Other", "content": "more"}),
    ]))
    sha = git("rev-parse", "main", cwd=vault_env["remote"]).strip()

    async def check() -> None:
        async with Web(server) as web:
            await sign_in(web)
            overview = (await web.get("/dash/")).text
            assert 'class="strip ok"' in overview and "no errors" in overview
            assert "Claude read today" in overview and "Note not found" in overview
            assert overview.count("<svg") == 2
            assert "Most read by Claude" not in overview

            insights = (await web.get("/dash/insights")).text
            assert re.search(r"<b>2</b><span>notes</span>", insights)
            assert "Most read by Claude" in insights and "Inbox/Welcome.md" in insights
            assert "1 broken link" in insights and "Missing" in insights
            assert 'href="/dash/insights?days=7"' in insights and "most read and changed" in insights

            activity = (await web.get("/dash/activity?errors=1")).text
            assert "Nope.md" in activity and "Inbox/Welcome.md" not in activity
            activity = (await web.get("/dash/activity?kind=write")).text
            assert sha[:7] in activity and f"/dash/commit/principal/{sha}" in activity
            files = (await web.get(f"/dash/commit/principal/{sha}", headers={"HX-Request": "true"})).text
            assert "Other.md" in files and "+1" in files
            assert (await web.get("/dash/commit/nope/" + sha)).status_code == 404
            bad = await web.get("/dash/commit/principal/not-a-sha", headers={"HX-Request": "true"})
            assert "not found" in bad.text

            moved = await web.get("/dash/notes")
            assert moved.status_code == 301 and moved.headers["location"] == "/dash/insights"

            wikis = (await web.get("/dash/wikis")).text
            assert 'href="/dash/wikis"' in wikis and 'class="on"' in wikis
            # Folders first, notes shown without .md, dot folders hidden.
            assert wikis.index(">Inbox<") < wikis.index(">Other<")
            assert ".obsidian" not in wikis and ">Welcome<" in wikis
            assert "2 reads" in wikis  # Claude read Inbox/Welcome.md twice
            assert 'class="badge ai"' in wikis  # Claude made the last change to Other.md
            found = (await web.get("/dash/wikis/search?q=welc", headers={"HX-Request": "true"})).text
            assert ">Welcome<" in found and ">Other<" not in found
            none = (await web.get("/dash/wikis/search?q=zzz", headers={"HX-Request": "true"})).text
            assert "No files match" in none
            assert (await web.get("/dash/wikis/dir/principal?path=../..")).status_code == 404

            health = (await web.get("/dash/health")).text
            assert "In sync" in health and "Static token" not in health  # in-memory client: no token
            assert "Australia/Perth" in health

    run(check())


def test_unpushed_commits_turn_the_strip_amber(vault_env) -> None:
    server = make_server()
    vault = vault_env["server_vault"]
    (vault / "Local.md").write_text("only here\n", encoding="utf-8")
    git("add", "-A", cwd=vault)
    git("-c", "user.name=x", "-c", "user.email=x@x", "commit", "-m", "local only", cwd=vault)

    async def check() -> None:
        async with Web(server) as web:
            await sign_in(web)
            overview = (await web.get("/dash/")).text
            assert 'class="strip warn"' in overview and "1 commit waiting to be pushed" in overview

    run(check())


def test_charts_count_claude_and_device_commits(vault_env) -> None:
    from zoneinfo import ZoneInfo

    from obsidian_mcp.dashboard import stats

    server = make_server()
    run(call_tools(server, [("append_note", {"path": "Other", "content": "x"})]))
    daily = stats.daily_activity(log_of(vault_env), stats.Clock(ZoneInfo("Australia/Perth")), 30)
    assert len(daily) == 30
    today = daily[-1]
    assert today["claude_commits"] == 1 and today["device_commits"] == 1 and today["writes"] == 1
    tiles = {t["key"]: t for t in stats.tiles(daily)}
    assert tiles["writes"]["value"] == 1 and tiles["writes"]["avg"] == 0


# -- configuration -----------------------------------------------------------------------


def test_dashboard_configuration_is_checked(vault_env) -> None:
    mp = vault_env["monkeypatch"]
    mp.setenv("OBSIDIAN_MCP_TIMEZONE", "Mars/Olympus")
    mp.setenv("OBSIDIAN_MCP_DASHBOARD_SECRET", "short")
    with pytest.raises(ConfigError) as err:
        load_settings()
    assert "Mars/Olympus" in str(err.value) and "OBSIDIAN_MCP_DASHBOARD_SECRET" in str(err.value)
    mp.setenv("OBSIDIAN_MCP_TIMEZONE", "UTC")
    mp.delenv("OBSIDIAN_MCP_DASHBOARD_SECRET")
    mp.setenv("OBSIDIAN_MCP_ACTIVITY_DAYS", "-1")
    with pytest.raises(ConfigError, match="activity log"):
        load_settings()


def test_dashboard_off_by_default_but_log_still_runs(vault_env) -> None:
    vault_env["monkeypatch"].delenv("OBSIDIAN_MCP_DASHBOARD")
    server = make_server()
    run(call_tools(server, [("read_note", {"path": "Other"})]))
    assert log_of(vault_env).count() == 1

    async def check() -> None:
        async with Web(server) as web:
            assert (await web.get("/dash/login")).status_code == 404

    run(check())


def test_http_calls_record_the_client(vault_env) -> None:
    server = make_server()
    headers = {"Authorization": f"Bearer {STATIC_TOKEN}", "Accept": "application/json, text/event-stream"}

    async def check() -> None:
        async with Web(server) as web:
            init = await web.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "0"}}})
            assert init.status_code == 200
            call = await web.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "read_note", "arguments": {"path": "Other"}}})
            assert call.status_code == 200
            await sign_in(web)
            assert "Static token (Claude Code)" in (await web.get("/dash/health")).text

    run(check())
    (event,) = log_of(vault_env).events()
    assert event["client_id"] == "static-token" and event["path"] == "Other.md"


def test_client_id_is_read_from_the_servers_own_token() -> None:
    import base64
    import json

    from obsidian_mcp.activity import _jwt_client_id

    payload = base64.urlsafe_b64encode(json.dumps({"client_id": "abc-123"}).encode()).decode().rstrip("=")
    assert _jwt_client_id(f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig") == "abc-123"
    assert _jwt_client_id("gho_githubtoken") is None and _jwt_client_id(None) is None
    assert _jwt_client_id("a.!!!.c") is None


def test_wiki_tree_lazy_loads_big_vaults(vault_env, monkeypatch) -> None:
    from obsidian_mcp.dashboard import app as dash_app

    monkeypatch.setattr(dash_app, "LAZY_TREE_ENTRIES", 0)
    server = make_server()

    async def check() -> None:
        async with Web(server) as web:
            await sign_in(web)
            wikis = (await web.get("/dash/wikis")).text
            assert 'hx-get="/dash/wikis/dir/principal?path=Inbox"' in wikis
            assert ">Welcome<" not in wikis
            inner = (await web.get("/dash/wikis/dir/principal?path=Inbox", headers={"HX-Request": "true"})).text
            assert ">Welcome<" in inner
            assert (await web.get("/dash/wikis/dir/nope?path=Inbox")).status_code == 404

    asyncio.run(check())

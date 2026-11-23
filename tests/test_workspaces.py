"""Embedding the server for many users: workspaces, per-vault credentials and locks,
empty repositories, and the dashboard's external sign-in.

The rule under test: a request only ever sees its own workspace. Two users whose
vaults have the same name can't read or change each other's notes, and a caller the
embedding app doesn't know gets nothing, not the operator's vault."""

from __future__ import annotations

import asyncio
import subprocess
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from obsidian_mcp import sync
from obsidian_mcp.activity import ActivityLog, Event
from obsidian_mcp.config import Settings
from obsidian_mcp.server import build_server
from obsidian_mcp.vault import Vault, VaultError, configured_vaults, get_vault, use_vault, use_vaults
from obsidian_mcp.workspace import DENIED, Workspace, current, use_workspace


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_remote(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    remote = tmp_path / "remotes" / f"{name}.git"
    remote.parent.mkdir(parents=True, exist_ok=True)
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    if not files:
        return remote
    work = tmp_path / "seeds" / name
    work.parent.mkdir(parents=True, exist_ok=True)
    git("clone", str(remote), str(work), cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "phone", cwd=work)
    for rel, text in files.items():
        (work / rel).write_text(text, encoding="utf-8")
    git("add", "-A", cwd=work)
    git("commit", "-m", "vault backup", cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    return remote


def make_workspace(tmp_path: Path, who: str, remote: Path, tz: str | None = None) -> Workspace:
    """One user's workspace: a vault called "notes" (the same name for everyone)."""
    base = tmp_path / "accounts" / who
    vault = Vault("notes", base / "vault", str(remote), main=True, git_name="Claude", git_email="c@x")
    ws = Workspace(vaults={"notes": vault}, log=ActivityLog(base / "activity.db"), timezone=tz,
                   label=f"{who}@example.com", empty_message="Connect a vault first.")
    with use_workspace(ws), use_vault("notes"):
        sync.ensure_clone()
        sync.pull()
    return ws


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # The operator's own vault, in the environment: it must never show through.
    operator = tmp_path / "operator-vault"
    operator.mkdir()
    (operator / "Operator.md").write_text("operator secrets\n", encoding="utf-8")
    monkeypatch.setenv("OBSIDIAN_VAULT", str(operator))
    monkeypatch.delenv("OBSIDIAN_VAULT_REMOTE", raising=False)
    monkeypatch.delenv("OBSIDIAN_EXTRA_VAULTS", raising=False)
    monkeypatch.setenv("OBSIDIAN_PULL_INTERVAL", "0")
    remotes = {
        "alice": make_remote(tmp_path, "alice", {"Alice.md": "# Alice\nSee [[Shared]].\n"}),
        "bob": make_remote(tmp_path, "bob", {"Bob.md": "# Bob\n"}),
    }
    spaces = {
        "alice": make_workspace(tmp_path, "alice", remotes["alice"], "Australia/Perth"),
        "bob": make_workspace(tmp_path, "bob", remotes["bob"]),
    }
    caller: dict[str, str | None] = {"who": None}

    def resolve(token: Any) -> Workspace | None:
        assert token is None  # in-memory client: no HTTP, no token
        return spaces.get(caller["who"] or "")

    settings = Settings(state_dir=tmp_path / "state")
    server = build_server(settings, workspaces=resolve)
    return {"tmp": tmp_path, "remotes": remotes, "spaces": spaces, "caller": caller, "server": server,
            "server_log": tmp_path / "state" / "activity.db"}


async def calls(world: dict[str, Any], who: str | None, *steps: tuple[str, dict]) -> list[dict]:
    world["caller"]["who"] = who
    out = []
    async with Client(world["server"]) as client:
        for name, args in steps:
            result = await client.call_tool(name, args, raise_on_error=False)
            out.append(result.structured_content or {})
    return out


def test_each_caller_sees_only_their_own_workspace(world) -> None:
    listing, own, other, escape, operator = run(calls(
        world, "alice",
        ("list_vaults", {}),
        ("read_note", {"path": "Alice"}),
        ("read_note", {"path": "Bob"}),
        ("read_note", {"path": "../../bob/vault/Bob"}),
        ("read_note", {"path": "Operator"}),
    ))
    assert [v["name"] for v in listing["vaults"]] == ["notes"]
    assert listing["vaults"][0]["repository"] == str(world["remotes"]["alice"])
    assert own["ok"] and "# Alice" in own["content"]
    assert not other["ok"] and not escape["ok"] and "escapes the vault" in escape["error"]
    assert not operator["ok"]

    bob_own, bob_other = run(calls(world, "bob", ("read_note", {"path": "Bob"}), ("read_note", {"path": "Alice"})))
    assert bob_own["ok"] and not bob_other["ok"]


def test_changes_land_in_the_callers_repository_only(world) -> None:
    bob_head = git("rev-parse", "main", cwd=world["remotes"]["bob"]).strip()
    (written,) = run(calls(world, "alice", ("append_note", {"path": "Alice", "content": "from Claude"})))
    assert written["ok"] and written["sync"] == "pushed"
    log = git("log", "-1", "--format=%an %s", "main", cwd=world["remotes"]["alice"]).strip()
    assert log == "Claude obsidian-mcp: append Alice.md"
    assert git("rev-parse", "main", cwd=world["remotes"]["bob"]).strip() == bob_head


def test_activity_goes_to_each_workspaces_own_log(world) -> None:
    run(calls(world, "alice", ("read_note", {"path": "Alice"})))
    run(calls(world, "bob", ("read_note", {"path": "Bob"}), ("read_note", {"path": "Bob"})))
    alice = world["spaces"]["alice"].log.events()
    bob = world["spaces"]["bob"].log.events()
    assert [(e["path"], e["vault"]) for e in alice] == [("Alice.md", "notes")]
    assert [e["path"] for e in bob] == ["Bob.md", "Bob.md"]
    # Nothing reached the server's own log.
    assert ActivityLog(world["server_log"]).count() == 0


def test_unknown_callers_get_an_empty_workspace_not_the_environment(world) -> None:
    listing, read = run(calls(world, "mallory", ("list_vaults", {}), ("read_note", {"path": "Operator"})))
    assert listing["count"] == 0
    assert not read["ok"] and read["error"] == DENIED
    assert ActivityLog(world["server_log"]).count() == 0  # denied calls aren't logged


def test_an_empty_workspace_explains_itself(tmp_path: Path) -> None:
    ws = Workspace(empty_message="Connect a vault first.")
    with use_workspace(ws):
        assert configured_vaults() == {}
        with pytest.raises(VaultError, match="Connect a vault first"):
            get_vault()
        assert current() is ws
    assert current() is None


def test_scope_rejects_bad_names_and_does_not_leak_a_selected_vault(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path))
    with pytest.raises(VaultError):
        with use_vaults({"Bad Name": Vault("x", tmp_path, None)}):
            pass
    other = Vault("other", tmp_path / "o", None, main=True)
    with use_vault("principal"):  # selected outside the scope...
        with use_vaults({"other": other}):
            assert get_vault().name == "other"  # ...but not inside it


def test_locks_are_per_vault(tmp_path: Path) -> None:
    a = Vault("notes", tmp_path / "a", None)
    b = Vault("notes", tmp_path / "b", None)
    assert sync.vault_lock(a) is sync.vault_lock(Vault("renamed", tmp_path / "a", None))
    assert sync.vault_lock(a) is not sync.vault_lock(b)
    first = sync.vault_lock(a)
    sync.forget(a)
    assert sync.vault_lock(a) is not first


def test_vault_credentials_reach_only_network_commands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OBSIDIAN_GIT_TOKEN", "operator-token")
    asked: list[int] = []

    def credentials() -> str:
        asked.append(1)
        return "vault-token"

    seen: list[tuple[list[str], str | None]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append((cmd, kwargs["env"].get("OBSIDIAN_GIT_TOKEN")))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    own = Vault("own", tmp_path, "https://github.com/a/b.git", credentials=credentials)
    with use_vaults({"own": own}), use_vault("own"):
        sync._git("status", cwd=tmp_path)  # noqa: SLF001
        sync._git("push", cwd=tmp_path)  # noqa: SLF001
    assert seen[0] == (["git", "status"], None)  # local command: no token at all
    assert seen[1][1] == "vault-token" and "credential.helper=" in seen[1][0]
    assert asked == [1]
    seen.clear()
    sync._git("push", cwd=tmp_path)  # noqa: SLF001 - outside any workspace: the operator's token
    assert seen[0][1] == "operator-token"


def test_failing_credentials_are_a_sync_error(tmp_path: Path) -> None:
    def broken() -> str:
        raise RuntimeError("GitHub is down")

    v = Vault("v", tmp_path, "https://github.com/a/b.git", credentials=broken)
    with use_vaults({"v": v}), use_vault("v"):
        with pytest.raises(sync.SyncError, match="GitHub is down"):
            sync._git("fetch", cwd=tmp_path)  # noqa: SLF001


def test_first_write_to_an_empty_repository(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OBSIDIAN_PULL_INTERVAL", "0")
    remote = make_remote(tmp_path, "fresh", {})
    ws = Workspace(vaults={"vault": Vault("vault", tmp_path / "clone", str(remote), main=True)})
    with use_workspace(ws), use_vault("vault"):
        sync.ensure_clone()
        assert sync.pull() == sync.EMPTY_REMOTE
    server = build_server(Settings(state_dir=tmp_path / "state"), workspaces=lambda token: ws)

    async def write() -> list[dict]:
        async with Client(server) as client:
            out = []
            for content in ("first", "second"):
                result = await client.call_tool("append_note", {"path": "Hello", "content": content})
                out.append(result.structured_content or {})
            return out

    first, second = run(write())
    assert first.get("sync") == "pushed" and "sync_warning" not in first, first
    assert second.get("sync") == "pushed"
    assert git("log", "--format=%s", "main", cwd=remote).split("\n")[:2] == [
        "obsidian-mcp: append Hello.md", "obsidian-mcp: append Hello.md"]
    with use_workspace(ws), use_vault("vault"):
        assert sync.pull() != sync.EMPTY_REMOTE


# -- the dashboard, signed in by an embedding app --------------------------------------

pytest.importorskip("fasthtml", reason="the dashboard extra is not installed")

import httpx2  # noqa: E402

from obsidian_mcp.dashboard import ExternalAuth  # noqa: E402

BASE_URL = "https://hosted.test"
_viewer: ContextVar[str | None] = ContextVar("test_viewer", default=None)


class Embed:
    """What an embedding app does in front of the dashboard: decide who is looking
    (here from a test header) and run the request inside their workspace."""

    def __init__(self, app: Any, spaces: dict[str, Workspace]) -> None:
        self.app, self.spaces = app, spaces

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        who = dict(scope["headers"]).get(b"x-test-user", b"").decode()
        ws = self.spaces.get(who)
        token = _viewer.set(ws.label if ws else None)
        try:
            if ws is None:
                await self.app(scope, receive, send)
            else:
                with use_workspace(ws):
                    await self.app(scope, receive, send)
        finally:
            _viewer.reset(token)


def hosted_dashboard(world: dict[str, Any], menu: Any = None) -> Any:
    from fasthtml.common import Div

    from obsidian_mcp.github_auth import StaticTokenVerifier

    settings = Settings(transport="http", public_url=BASE_URL, state_dir=world["tmp"] / "state",
                        dashboard=True, auth="external")
    external = ExternalAuth(
        user=_viewer.get, login_url="/login", logout_url="/logout", csrf=lambda: "csrf-token",
        brand="Kintex", nav=(("Settings", "/settings"),), banner=lambda: Div("Finish setting up", id="setup"),
        menu=menu or (lambda: ()),
    )
    # Any provider will do: the embedding app brings its own (the gateway uses AuthKit).
    return build_server(settings, workspaces=lambda token: None, dashboard_auth=external,
                        auth=StaticTokenVerifier("t" * 40, []))


async def browse(world: dict[str, Any], *requests: tuple[str, str, str | None]) -> list[httpx2.Response]:
    server = hosted_dashboard(world)
    app = server.http_app(path="/mcp", stateless_http=True)
    out = []
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=Embed(app, world["spaces"]))
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as web:
            for method, path, who in requests:
                headers = {"x-test-user": who} if who else {}
                out.append(await web.request(method, path, headers=headers))
    return out


def test_hosted_dashboard_trusts_the_embedding_apps_sign_in(world) -> None:
    signed_out, page, login, logout = run(browse(
        world, ("GET", "/dash/", None), ("GET", "/dash/", "alice"), ("GET", "/dash/login", "nobody"),
        ("POST", "/dash/logout", "alice"),
    ))
    assert signed_out.status_code == 303 and signed_out.headers["location"] == "/login"
    assert page.status_code == 200
    assert "Kintex" in page.text and "Vault dashboard" not in page.text
    assert 'id="setup"' in page.text and 'href="/settings"' in page.text
    assert 'action="/logout"' in page.text and "csrf-token" in page.text
    assert "alice@example.com" in page.text
    assert login.status_code == 303 and login.headers["location"] == "/login"
    assert logout.status_code == 404  # the embedding app signs people out
    assert "set-cookie" not in page.headers  # no session of the dashboard's own


def test_an_embedding_app_can_serve_the_dashboard_at_its_own_path(world) -> None:
    """E.g. /alice/dash: the app calls the dashboard with that root_path, and the tabs, the
    live fragments, the theme switch and the account menu all follow it."""
    from fasthtml.common import Span

    async def go() -> list[httpx2.Response]:
        menu = (("Setup", "/alice/dash/setup", Span("2/3", cls="badge ok")), ("Settings", "/alice/dash/settings"))
        server = hosted_dashboard(world, menu=lambda: menu)
        app = server.http_app(path="/mcp", stateless_http=True)
        dash = Embed(server.dashboard_app, world["spaces"])

        async def at_alice(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http" and scope["path"].startswith("/alice/dash"):
                await dash(dict(scope, root_path="/alice/dash"), receive, send)
            else:
                await app(scope, receive, send)

        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=at_alice)
            async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as web:
                alice = {"x-test-user": "alice"}
                return [await web.get(p, headers=alice) for p in ("/alice/dash/overview", "/alice/dash/activity")]

    overview, activity = run(go())
    assert overview.status_code == 200 and activity.status_code == 200
    assert 'href="/alice/dash/activity"' in overview.text and 'href="/alice/dash/overview"' in activity.text
    assert 'hx-get="/alice/dash/fragment/recent"' in overview.text
    assert "/alice/dash/theme?mode=dark&amp;next=/alice/dash/overview" in overview.text
    assert 'class="account"' in overview.text and 'href="/alice/dash/settings"' in overview.text
    assert 'action="/logout"' in overview.text  # Sign out, in the account menu
    assert 'href="/alice/dash/setup">Setup<span class="badge ok">2/3</span></a>' in overview.text  # a badge
    assert '"/dash/' not in overview.text and '"/dash/' not in activity.text


def test_hosted_dashboard_shows_only_the_viewers_workspace(world) -> None:
    world["spaces"]["alice"].log.record(Event(ts=__import__("time").time(), tool="read_note", kind="read",
                                              ok=True, vault="notes", path="Alice.md"))
    world["spaces"]["bob"].log.record(Event(ts=__import__("time").time(), tool="read_note", kind="read",
                                            ok=True, vault="notes", path="Bob.md"))
    bob_sha = git("rev-parse", "main", cwd=world["remotes"]["bob"]).strip()
    activity, wikis, commit, health = run(browse(
        world, ("GET", "/dash/activity", "alice"), ("GET", "/dash/wikis", "alice"),
        ("GET", f"/dash/commit/notes/{bob_sha}", "alice"), ("GET", "/dash/health", "alice"),
    ))
    assert "Alice.md" in activity.text and "Bob.md" not in activity.text
    assert "Alice" in wikis.text and "Bob" not in wikis.text
    assert "Commit not found" in commit.text  # Bob's commit isn't in Alice's repository
    assert "Australia/Perth" in health.text and "Kintex account" in health.text

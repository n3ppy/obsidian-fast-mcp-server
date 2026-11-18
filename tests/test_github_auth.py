"""GitHub login: the allowlist is the only thing between any GitHub account and the
vault, so it is tested at each point it applies. GitHub itself is never contacted:
its token check is replaced by a fake that maps tokens to logins."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy.models import ClientCode
from mcp.server.auth.provider import AuthorizationCode, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from obsidian_mcp.config import ConfigError, Settings, load_settings
from obsidian_mcp.github_auth import AllowlistGitHubProvider, redirect_patterns
from test_http import Mcp, free_port, http

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
STATIC_TOKEN = "t" * 40


class FakeGitHub:
    """Stands in for GitHub's token check: token -> login (or not a valid token)."""

    required_scopes = ["read:user"]

    def __init__(self, logins: dict[str, str]) -> None:
        self.logins = logins

    async def verify_token(self, token: str) -> AccessToken | None:
        login = self.logins.get(token)
        if login is None:
            return None
        return AccessToken(token=token, client_id="1", scopes=["read:user"], claims={"login": login})


def github_settings(tmp_path: Path, users: tuple[str, ...] = ("hconnorh",)) -> Settings:
    return Settings(
        transport="http",
        public_url="https://vault.example.com",
        auth="github",
        github_client_id="Ov23litest",
        github_client_secret="s" * 40,
        allowed_users=users,
        state_dir=tmp_path,
    )


def provider_with_fake_github(tmp_path: Path) -> AllowlistGitHubProvider:
    import fastmcp

    fastmcp.settings.home = tmp_path / "fastmcp"
    provider = AllowlistGitHubProvider(github_settings(tmp_path))
    provider.allowlist.inner = FakeGitHub({"gh-owner": "HConnorH", "gh-stranger": "mallory"})
    provider.get_routes("/mcp")  # as at server start: sets up the token signer
    return provider


# -- configuration -----------------------------------------------------------------


@pytest.fixture
def github_env(monkeypatch):
    for key, value in {
        "OBSIDIAN_MCP_TRANSPORT": "http",
        "OBSIDIAN_MCP_PUBLIC_URL": "https://vault.example.com",
        "OBSIDIAN_MCP_AUTH": "github",
        "OBSIDIAN_MCP_GITHUB_CLIENT_ID": "Ov23litest",
        "OBSIDIAN_MCP_GITHUB_CLIENT_SECRET": "s" * 40,
        "OBSIDIAN_MCP_ALLOWED_USERS": "HConnorH, other-user",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OBSIDIAN_MCP_PASSWORD", raising=False)
    return monkeypatch


def test_github_mode_needs_no_password_and_lowercases_users(github_env) -> None:
    settings = load_settings()
    assert settings.auth == "github"
    assert settings.allowed_users == ("hconnorh", "other-user")


def test_empty_allowlist_refuses_to_start(github_env) -> None:
    github_env.setenv("OBSIDIAN_MCP_ALLOWED_USERS", " , ")
    with pytest.raises(ConfigError, match="any GitHub account could sign in"):
        load_settings()


def test_invalid_usernames_and_missing_app_are_refused(github_env) -> None:
    github_env.setenv("OBSIDIAN_MCP_ALLOWED_USERS", "ok-user,not a login,-bad")
    github_env.delenv("OBSIDIAN_MCP_GITHUB_CLIENT_SECRET")
    with pytest.raises(ConfigError) as err:
        load_settings()
    assert "not a login" in str(err.value) and "-bad" in str(err.value)
    assert "OBSIDIAN_MCP_GITHUB_CLIENT_SECRET" in str(err.value)


def test_password_mode_is_still_the_default(github_env) -> None:
    github_env.delenv("OBSIDIAN_MCP_AUTH")
    with pytest.raises(ConfigError, match="OBSIDIAN_MCP_PASSWORD is required"):
        load_settings()


def test_redirect_patterns() -> None:
    assert redirect_patterns(("https://claude.ai/api/mcp/auth_callback", "http://localhost")) == [
        "https://claude.ai/api/mcp/auth_callback",
        "http://localhost:*",
        "http://localhost:*/*",
    ]
    assert redirect_patterns(("*",)) is None


# -- the allowlist ---------------------------------------------------------------


def test_every_request_checks_the_allowlist(tmp_path: Path) -> None:
    allowlist = provider_with_fake_github(tmp_path).allowlist

    async def check() -> None:
        assert (await allowlist.verify_token("gh-owner")).claims["login"] == "HConnorH"
        assert await allowlist.verify_token("gh-stranger") is None  # valid GitHub user, not allowed
        assert await allowlist.verify_token("garbage") is None

    asyncio.run(check())


def _client_code(provider: AllowlistGitHubProvider, github_token: str) -> AuthorizationCode:
    code = f"code-{github_token}"
    now = time.time()
    asyncio.run(
        provider._code_store.put(
            key=code,
            value=ClientCode(
                code=code, client_id="claude", redirect_uri=CALLBACK, code_challenge="x" * 43,
                code_challenge_method="S256", scopes=["read:user"],
                idp_tokens={"access_token": github_token, "token_type": "bearer", "scope": "read:user"},
                expires_at=now + 300, created_at=now,
            ),
            ttl=300,
        )
    )
    return AuthorizationCode(
        code=code, scopes=["read:user"], expires_at=now + 300, client_id="claude",
        code_challenge="x" * 43, redirect_uri=AnyUrl(CALLBACK), redirect_uri_provided_explicitly=True,
    )


def test_sign_in_refuses_accounts_outside_the_allowlist(tmp_path: Path) -> None:
    provider = provider_with_fake_github(tmp_path)
    client = OAuthClientInformationFull(client_id="claude", redirect_uris=[CALLBACK])

    stranger = _client_code(provider, "gh-stranger")
    with pytest.raises(TokenError) as err:
        asyncio.run(provider.exchange_authorization_code(client, stranger))
    assert err.value.error == "invalid_grant"
    # The code is spent and no token was stored for the stranger.
    assert asyncio.run(provider._code_store.get(key=stranger.code)) is None

    owner = _client_code(provider, "gh-owner")
    tokens = asyncio.run(provider.exchange_authorization_code(client, owner))

    async def verify() -> AccessToken | None:
        return await provider.load_access_token(tokens.access_token)

    assert asyncio.run(verify()) is not None  # the owner's token works

    # Removing the owner from the list locks their existing token out too.
    provider.allowlist.allowed = frozenset({"someone-else"})
    assert asyncio.run(verify()) is None


# -- the running server ------------------------------------------------------------


def test_github_server_routes(tmp_path: Path) -> None:
    port = free_port()
    base = f"http://localhost:{port}"
    env = {
        **os.environ,
        "OBSIDIAN_MCP_TRANSPORT": "http",
        "OBSIDIAN_MCP_HOST": "127.0.0.1",
        "OBSIDIAN_MCP_PORT": str(port),
        "OBSIDIAN_MCP_PUBLIC_URL": base,
        "OBSIDIAN_MCP_AUTH": "github",
        "OBSIDIAN_MCP_GITHUB_CLIENT_ID": "Ov23litest",
        "OBSIDIAN_MCP_GITHUB_CLIENT_SECRET": "s" * 40,
        "OBSIDIAN_MCP_ALLOWED_USERS": "hconnorh",
        "OBSIDIAN_MCP_TOKEN": STATIC_TOKEN,
        "OBSIDIAN_MCP_STATE_DIR": str(tmp_path / "state"),
        "OBSIDIAN_VAULT": str(tmp_path),
    }
    env.pop("OBSIDIAN_MCP_PASSWORD", None)
    proc = subprocess.Popen([sys.executable, "-m", "obsidian_mcp"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(100):
            try:
                if http("GET", base + "/healthz")[0] == 200:
                    break
            except OSError:
                pass
            if proc.poll() is not None:
                raise RuntimeError(proc.stdout.read())
            time.sleep(0.1)

        status, _, body = http("GET", base + "/.well-known/oauth-authorization-server")
        assert status == 200 and '"registration_endpoint"' in body
        assert http("GET", base + "/login")[0] == 404  # no password page in GitHub mode
        assert Mcp(base, "not-a-token").rpc("tools/list")[0] == 401
        # A registered client is sent towards GitHub, never straight back with a code.
        status, _, body = http("POST", base + "/register", data={
            "client_name": "Claude", "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"})
        assert status == 201, body
        status, _, _ = http("POST", base + "/register", data={
            "client_name": "evil", "redirect_uris": ["https://evil.example/cb"],
            "token_endpoint_auth_method": "none"})
        assert status == 400

        static = Mcp(base, STATIC_TOKEN)
        static.initialize()
        assert static.call("list_vaults")["count"] == 1
        assert (tmp_path / "state" / "fastmcp").is_dir()  # OAuth state lives in the state volume
    finally:
        proc.terminate()
        proc.wait(10)

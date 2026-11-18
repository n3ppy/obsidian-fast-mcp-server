"""GitHub login (``OBSIDIAN_MCP_AUTH=github``): "Sign in with GitHub", for allowed accounts only.

FastMCP's ``GitHubProvider`` does the OAuth work: claude.ai registers with this server,
the user is sent to GitHub to sign in (with whatever GitHub requires: password, 2FA,
passkey), and this server then issues its own tokens to Claude. The GitHub OAuth App
needs the callback URL ``<public url>/auth/callback``.

On its own, GitHubProvider lets *any* GitHub account in. This module adds the
allowlist (``OBSIDIAN_MCP_ALLOWED_USERS``), checked twice:

- **At sign-in**, before any token is issued: an account outside the list gets an
  OAuth error and nothing is stored for it.
- **On every request**, against GitHub's answer for the token: removing a name from
  the list takes effect at once (within the verification cache, 5 minutes), and so
  does revoking the app's access on GitHub.

GitHub logins are matched case-insensitively. A login can be renamed and later
claimed by someone else, so update the list if you rename your account.
"""

from __future__ import annotations

import hmac
from typing import Any

import fastmcp
from fastmcp.server.auth import MultiAuth, TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider
from mcp.server.auth.provider import AuthorizationCode, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import Settings

# Only the account's identity is needed: no repository or email access.
GITHUB_SCOPES = ["read:user"]
# How long a successful GitHub check is reused before GitHub is asked again.
VERIFY_CACHE_SECONDS = 300
STATIC_CLIENT_ID = "static-token"


def _login(token: AccessToken | None) -> str | None:
    login = (token.claims or {}).get("login") if token else None
    return login.lower() if isinstance(login, str) else None


class AllowlistVerifier(TokenVerifier):
    """Wraps GitHub's token check and only accepts the allowed accounts."""

    def __init__(self, inner: TokenVerifier, allowed: tuple[str, ...]) -> None:
        super().__init__(required_scopes=inner.required_scopes)
        self.inner = inner
        self.allowed = frozenset(allowed)

    def allows(self, token: AccessToken | None) -> bool:
        return token is not None and _login(token) in self.allowed

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await self.inner.verify_token(token)
        return result if self.allows(result) else None


class AllowlistGitHubProvider(GitHubProvider):
    def __init__(self, settings: Settings) -> None:
        assert settings.public_url and settings.github_client_id and settings.github_client_secret
        if not settings.allowed_users:
            raise ValueError("GitHub login needs at least one allowed user")
        super().__init__(
            client_id=settings.github_client_id,
            client_secret=settings.github_client_secret,
            base_url=settings.public_url,
            required_scopes=GITHUB_SCOPES,
            cache_ttl_seconds=VERIFY_CACHE_SECONDS,
            allowed_client_redirect_uris=redirect_patterns(settings.allowed_redirects),
            jwt_signing_key=settings.jwt_key,
        )
        # FastMCP keeps its token check in `_token_validator`; fail loudly if a
        # FastMCP upgrade moves it, rather than silently dropping the allowlist.
        inner = getattr(self, "_token_validator", None)
        if not isinstance(inner, TokenVerifier):
            raise RuntimeError("FastMCP changed GitHubProvider internals: allowlist cannot be installed")
        self.allowlist = AllowlistVerifier(inner, settings.allowed_users)
        self._token_validator = self.allowlist

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Refuse accounts outside the allowlist before any token is issued or stored."""
        code_model = await self._code_store.get(key=authorization_code.code)
        if code_model is not None:
            github_token = (code_model.idp_tokens or {}).get("access_token")
            identity = await self.allowlist.inner.verify_token(github_token) if github_token else None
            if not self.allowlist.allows(identity):
                await self._code_store.delete(key=authorization_code.code)
                raise TokenError(
                    "invalid_grant",
                    "This GitHub account is not allowed on this server.",
                )
        return await super().exchange_authorization_code(client, authorization_code)


class StaticTokenVerifier(TokenVerifier):
    """The optional fixed bearer token (``OBSIDIAN_MCP_TOKEN``) for Claude Code."""

    def __init__(self, token: str, scopes: list[str]) -> None:
        super().__init__()
        self._token = token
        self._scopes = scopes  # the scopes every request must carry

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._token.encode()):
            return AccessToken(
                token=token, client_id=STATIC_CLIENT_ID, scopes=list(self._scopes), subject="owner"
            )
        return None


def redirect_patterns(allowed: tuple[str, ...]) -> list[str] | None:
    """Translate OBSIDIAN_MCP_ALLOWED_REDIRECTS into FastMCP's pattern syntax.

    An entry without a path (``http://localhost``) means any port and path on that
    host, as in the password login; ``*`` allows everything.
    """
    if "*" in allowed:
        return None
    patterns: list[str] = []
    for entry in allowed:
        scheme, _, rest = entry.partition("://")
        if "/" in rest:
            patterns.append(entry)
        else:
            host = rest if ":" in rest else f"{rest}:*"
            patterns += [f"{scheme}://{host}", f"{scheme}://{host}/*"]
    return patterns


def build_github_auth(settings: Settings) -> Any:
    """The `auth=` value for FastMCP in GitHub mode."""
    # FastMCP stores registered clients and (encrypted) GitHub tokens under its home
    # directory; keep them in the state volume so a redeploy doesn't log Claude out.
    fastmcp.settings.home = settings.state_dir / "fastmcp"
    provider = AllowlistGitHubProvider(settings)
    if settings.static_token:
        static = StaticTokenVerifier(settings.static_token, provider.required_scopes)
        return MultiAuth(server=provider, verifiers=[static])
    return provider

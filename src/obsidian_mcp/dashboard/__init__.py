"""Optional web dashboard at ``/dash`` (``OBSIDIAN_MCP_DASHBOARD=true``).

Needs the ``dashboard`` extra (FastHTML). See ``app.py`` for sign-in and pages and
``stats.py`` for where each number comes from.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from urllib.parse import parse_qs

from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Mount

from ..activity import ActivityLog
from ..config import Settings
from .external import ExternalAuth
from .ui import BASE

__all__ = ["BASE", "ExternalAuth", "http_middleware", "install", "session_secret"]


def session_secret(settings: Settings) -> str:
    """OBSIDIAN_MCP_DASHBOARD_SECRET, or a random key kept in the state directory so
    sessions survive a restart."""
    if settings.dashboard_secret:
        return settings.dashboard_secret
    path = Path(settings.state_dir) / "dashboard_secret"
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return value


class GitHubCallbackRouter:
    """Send dashboard sign-ins on to ``/dash/callback``.

    GitHub only returns to the OAuth App's callback URL (``/auth/callback``), which the
    MCP login owns. The dashboard marks its ``state`` with ``dash.``; such requests are
    redirected before FastMCP's handler sees them, and everything else passes through.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        from .app import CALLBACK_PATH, STATE_PREFIX

        if scope["type"] == "http" and scope["path"] == CALLBACK_PATH and scope["method"] == "GET":
            query = scope.get("query_string", b"").decode("latin-1")
            states = parse_qs(query).get("state", [])
            if states and states[0].startswith(STATE_PREFIX):
                response = RedirectResponse(f"{BASE}/callback?{query}", status_code=303)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def http_middleware(settings: Settings) -> list[Middleware]:
    """Middleware the HTTP app needs for the dashboard (pass to ``server.run``)."""
    if settings.dashboard and settings.auth == "github":
        return [Middleware(GitHubCallbackRouter)]
    return []


def install(
    server: Any, settings: Settings, log: ActivityLog, *, password_check: Any = None,
    client_names: Any = None, external: ExternalAuth | None = None,
) -> None:
    """Mount the dashboard on the FastMCP server's HTTP app (routes added this way are
    public to the MCP auth layer; the dashboard does its own sign-in, or trusts
    `external`)."""
    from .app import build_app

    app = build_app(
        settings, log, password_check=password_check, client_names=client_names,
        secret_key=session_secret(settings), external=external,
    )
    routes = getattr(server, "_additional_http_routes", None)
    if not isinstance(routes, list):
        raise RuntimeError("FastMCP changed its HTTP routes API: the dashboard cannot be mounted")

    @server.custom_route(BASE, methods=["GET"])
    async def dash_root(request: Request) -> Response:
        return RedirectResponse(f"{BASE}/", status_code=307)

    routes.append(Mount(BASE, app=app))

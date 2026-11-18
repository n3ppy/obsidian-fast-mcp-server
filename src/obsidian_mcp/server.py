"""MCP server entrypoint, built on FastMCP.

Runs over stdio (Claude Code on the same machine) or Streamable HTTP (a server reachable
by claude.ai, the mobile apps and Claude Code), depending on ``OBSIDIAN_MCP_TRANSPORT``.
See ``config.py`` for every setting and ``tools/`` for what the server can do.
"""

from __future__ import annotations

import sys
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

from . import sync
from .config import ConfigError, Settings, load_settings
from .tools import TOOLS

INSTRUCTIONS = """\
This server is the user's Obsidian vault, their "second brain". Paths are relative to
the vault root; the `.md` suffix is optional. Good habits:
- Start with `vault_overview` to see folders, main tags and the most-linked notes.
- Follow the graph with `get_backlinks` / `get_links` and `notes_with_tag` rather than
  only full-text search.
- Read a note before rewriting it, and prefer `append_note` or `insert_under_heading`
  over `write_note(overwrite=true)`: the user may be editing the same note on another
  device. Use wikilinks ([[Note]]) when referring to other notes.
- The server can hold several vaults. If `list_vaults` reports more than one and the
  user has not said which to use, call it and ask them once, at the start of the
  conversation, then pass that name as `vault` to every call. Reads default to the
  main vault; changes are refused without an explicit `vault`. Each vault may have its
  own rules file (CLAUDE.md / AGENTS.md at its root): read it before working there.
"""


def _log(message: str) -> None:
    print(f"[obsidian-mcp] {message}", file=sys.stderr, flush=True)


def _auth(settings: Settings) -> tuple[Any, Any]:
    """(auth provider for FastMCP, password provider or None). None, None for stdio."""
    if not settings.is_http:
        return None, None
    if settings.auth == "github":
        from .github_auth import build_github_auth

        return build_github_auth(settings), None
    from .auth import VaultOAuthProvider

    provider = VaultOAuthProvider(settings)
    return provider, provider


def build_server(settings: Settings) -> FastMCP:
    auth, password_provider = _auth(settings)
    server = FastMCP("obsidian-mcp", instructions=INSTRUCTIONS, auth=auth)
    for fn, annotations in TOOLS:
        server.tool(fn, annotations=annotations)

    if password_provider is not None:
        from .auth import install_login_routes

        install_login_routes(server, password_provider)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return Response("ok", media_type="text/plain")

    return server


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        _log(str(exc))
        sys.exit(2)

    # Clone (if needed) and pull before serving any requests.
    for line in sync.bootstrap_all():
        _log(line)
    server = build_server(settings)

    if not settings.is_http:
        server.run(show_banner=False)
        return

    _log(
        f"serving MCP on {settings.mcp_url} (listening on {settings.host}:{settings.port}, "
        f"{settings.auth} login)"
    )
    server.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        path="/mcp",
        stateless_http=True,
        show_banner=False,
        # TLS is terminated by the reverse proxy, which also fixes the Host header;
        # access control is the OAuth layer, not the Host check.
        host_origin_protection=False,
    )


if __name__ == "__main__":
    main()

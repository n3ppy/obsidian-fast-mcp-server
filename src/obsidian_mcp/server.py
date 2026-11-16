"""MCP server entrypoint.

Runs over stdio (Claude Code on the same machine) or Streamable HTTP (a server reachable
by claude.ai, the mobile apps and Claude Code), depending on ``OBSIDIAN_MCP_TRANSPORT``.
See ``config.py`` for every setting and ``tools/`` for what the server can do.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

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


def _auth_kwargs(settings: Settings) -> tuple[dict[str, Any], Any]:
    """OAuth wiring for HTTP mode: (MCPServer kwargs, provider). Empty for stdio."""
    if not settings.is_http:
        return {}, None
    from .auth import VaultOAuthProvider

    provider = VaultOAuthProvider(settings)
    kwargs = {
        "auth_server_provider": provider,
        "auth": AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=settings.mcp_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            # Single-user server: every token we issue is for this server anyway.
            validate_token_resource=False,
        ),
    }
    return kwargs, provider


def build_server(settings: Settings) -> MCPServer:
    kwargs, provider = _auth_kwargs(settings)
    server = MCPServer("obsidian-mcp", instructions=INSTRUCTIONS, **kwargs)
    for fn, annotations in TOOLS:
        server.add_tool(fn, annotations=annotations)

    if provider is not None:
        from .auth import install_login_routes

        install_login_routes(server, provider)
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
        server.run()
        return

    _log(f"serving MCP on {settings.mcp_url} (listening on {settings.host}:{settings.port})")
    server.run(
        "streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        stateless_http=True,
        # TLS is terminated by the reverse proxy, which also fixes the Host header;
        # access control is the OAuth layer, not the Host check.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


if __name__ == "__main__":
    main()

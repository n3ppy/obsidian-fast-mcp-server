"""MCP server entrypoint.

Runs over stdio (Claude Code on the same machine) or Streamable HTTP (a VPS reachable
by claude.ai, the mobile apps and Claude Code), depending on
``OBSIDIAN_MCP_TRANSPORT``. See ``config.py`` for every setting.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import links, notes, pdf, sync
from .config import ConfigError, Settings, load_settings
from .vault import Vault, VaultError, configured_vaults, use_vault

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

VAULT_DOC = (
    "\n\n`vault`: name of the vault to use (see `list_vaults`). Optional for reading "
    "(main vault by default); required for changes when several vaults are configured."
)


def _log(message: str) -> None:
    print(f"[obsidian-mcp] {message}", file=sys.stderr, flush=True)


def _err(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(exc), "type": exc.__class__.__name__}


def _refresh(payload: dict[str, Any]) -> None:
    """Pull changes from other devices if the local copy may be stale.

    A failed pull must not block reading: the tool still answers from the local copy
    and reports the problem.
    """
    try:
        sync.maybe_pull()
    except VaultError as exc:
        payload["sync_warning"] = f"could not pull latest changes, answering from local copy: {exc}"


def _tag(vault: Vault) -> dict[str, Any]:
    """Name the vault in the answer, but only when there is a choice of vaults."""
    return {"vault": vault.name} if len(configured_vaults()) > 1 else {}


def _read(fn: Callable[[], dict[str, Any]], vault: str | None = None) -> dict[str, Any]:
    try:
        with use_vault(vault) as v:
            payload: dict[str, Any] = _tag(v)
            _refresh(payload)
            try:
                result = fn()
            except VaultError as exc:
                return {**_err(exc), **payload}
            return {"ok": True, **result, **payload}
    except VaultError as exc:  # unknown vault, bad configuration
        return _err(exc)


def _write(
    vault: str | None, action: str, fn: Callable[[], tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    """Run a change on one vault: pick the vault (explicitly when there are several),
    then hold the git lock for pull → write → commit → push, so a background pull
    can't rebase under a half-written file. `fn` returns (path, payload)."""
    try:
        with use_vault(vault, explicit=True) as v, sync.lock:
            extra: dict[str, Any] = _tag(v)
            _refresh(extra)
            try:
                path, payload = fn()
            except VaultError as exc:
                return {**_err(exc), **extra}
            return _with_sync(action, path, {"ok": True, **payload, **extra})
    except VaultError as exc:
        return _err(exc)


def _with_sync(action: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        status = sync.auto_commit(action, path)
    except VaultError as exc:
        payload["sync_error"] = str(exc)
    else:
        if status not in ("auto-push off", "sync disabled", "nothing to commit"):
            payload["sync"] = status
    return payload


# -- notes ---------------------------------------------------------------------------


def list_notes(subdir: str | None = None, vault: str | None = None) -> dict[str, Any]:
    """List all markdown notes in the vault (optionally scoped to a subdirectory)."""
    return _read(lambda: {"notes": notes.list_notes(subdir)}, vault)


def read_note(path: str, vault: str | None = None) -> dict[str, Any]:
    """Read a note's full markdown content. `path` is vault-relative; the `.md` suffix is optional."""
    return _read(lambda: {"path": path, "content": notes.read_note(path)}, vault)


def write_note(
    path: str, content: str, overwrite: bool = False, vault: str | None = None
) -> dict[str, Any]:
    """Create a note (or overwrite if `overwrite=true`). Creates parent directories as needed."""

    def run() -> tuple[str, dict[str, Any]]:
        written = notes.write_note(path, content, overwrite=overwrite)
        return written, {"path": written}

    return _write(vault, "write", run)


def append_note(path: str, content: str, vault: str | None = None) -> dict[str, Any]:
    """Append text to the end of a note (creates the note if it doesn't exist)."""

    def run() -> tuple[str, dict[str, Any]]:
        written = notes.append_note(path, content)
        return written, {"path": written}

    return _write(vault, "append", run)


def insert_under_heading(
    path: str,
    heading: str,
    content: str,
    create_if_missing: bool = True,
    vault: str | None = None,
) -> dict[str, Any]:
    """Insert text at the end of the section whose heading matches `heading` exactly.

    If the heading isn't present and `create_if_missing` is true, a new `## heading`
    section is appended with the content underneath.
    """

    def run() -> tuple[str, dict[str, Any]]:
        written = notes.insert_under_heading(
            path, heading, content, create_if_missing=create_if_missing
        )
        return written, {"path": written}

    return _write(vault, "insert", run)


def search_notes(
    query: str, regex: bool = False, max_results: int = 200, vault: str | None = None
) -> dict[str, Any]:
    """Search note contents. Default is case-insensitive substring; pass `regex=true` for regex."""

    def run() -> dict[str, Any]:
        hits = notes.search_notes(query, regex=regex, max_results=max_results)
        return {"count": len(hits), "hits": [asdict(h) for h in hits]}

    return _read(run, vault)


def delete_note(path: str, vault: str | None = None) -> dict[str, Any]:
    """Delete a note from the vault. The deletion is committed, so it can be undone from Git history."""

    def run() -> tuple[str, dict[str, Any]]:
        deleted = notes.delete_note(path)
        return deleted, {"deleted": deleted}

    return _write(vault, "delete", run)


def _sync_one(action: str) -> dict[str, Any]:
    if not sync.is_enabled():
        return {"ok": True, "enabled": False, "note": "no Git remote for this vault"}
    result: dict[str, Any] = {"ok": True, "enabled": True}
    try:
        if action in {"pull", "both"}:
            result["pull"] = sync.pull()
        if action in {"push", "both"}:
            result["push"] = sync.commit_and_push("obsidian-mcp: manual sync")
    except VaultError as exc:
        return _err(exc)
    return result


def sync_vault(action: str = "both", vault: str | None = None) -> dict[str, Any]:
    """Manually sync the vault with its Git remote.

    `action` can be "pull", "push", or "both" (default). No-op for a vault without a
    Git remote. Reads already pull automatically every few seconds. Without `vault`,
    every configured vault is synced.
    """
    action = action.lower()
    if action not in {"pull", "push", "both"}:
        return _err(VaultError(f"unknown action {action!r} (expected pull|push|both)"))
    try:
        names = [vault] if vault is not None else list(configured_vaults())
        if len(names) <= 1:
            with use_vault(vault):
                return _sync_one(action)
        results: dict[str, Any] = {}
        for name in names:
            with use_vault(name):
                results[name] = _sync_one(action)
    except VaultError as exc:
        return _err(exc)
    return {"ok": all(r.get("ok") for r in results.values()), "vaults": results}


def list_vaults() -> dict[str, Any]:
    """The vaults this server can use, main one first. When there are several and the
    user has not said which one to use, ask them (once per conversation), then pass its
    name as `vault` to the other tools."""
    try:
        vaults = configured_vaults()
    except VaultError as exc:
        return _err(exc)
    out: list[dict[str, Any]] = []
    for name, v in vaults.items():
        entry: dict[str, Any] = {"name": name, "main": v.main, "git_sync": v.remote is not None}
        if v.remote:
            entry["repository"] = v.remote
        try:
            with use_vault(name):
                entry["note_count"] = len(notes.list_notes(None))
                entry["has_rules_file"] = any(
                    (v.path / f).is_file() for f in ("CLAUDE.md", "AGENTS.md")
                )
        except VaultError as exc:
            entry["error"] = str(exc)
        out.append(entry)
    return {
        "ok": True,
        "count": len(out),
        "default": out[0]["name"] if out else None,
        "choice_needed": len(out) > 1,
        "vaults": out,
    }


# -- graph of the vault: tags, links ---------------------------------------------------


def vault_overview(vault: str | None = None) -> dict[str, Any]:
    """Map of the vault: note count, folders, most used tags, most linked notes, orphans
    and unresolved links. A good first call to get oriented."""
    return _read(links.overview, vault)


def list_tags(prefix: str | None = None, vault: str | None = None) -> dict[str, Any]:
    """List tags with how many notes use each (frontmatter and inline #tags).

    With `prefix="philo"`, only `#philo` and nested tags like `#philo/nietzsche`.
    """
    return _read(lambda: {"tags": links.list_tags(prefix)}, vault)


def notes_with_tag(tag: str, include_nested: bool = True, vault: str | None = None) -> dict[str, Any]:
    """Notes carrying a tag. `#` is optional; nested tags (`tag/sub`) are included by default."""

    def run() -> dict[str, Any]:
        found = links.notes_with_tag(tag, include_nested=include_nested)
        return {"tag": tag.lstrip("#"), "count": len(found), "notes": found}

    return _read(run, vault)


def get_backlinks(path: str, vault: str | None = None) -> dict[str, Any]:
    """Notes that link to this one (wikilinks, embeds and markdown links), with the line
    containing each link. Accepts a vault path or a bare note name."""
    return _read(lambda: links.backlinks(path), vault)


def get_links(path: str, vault: str | None = None) -> dict[str, Any]:
    """Links going out of a note, each resolved to a vault path when the target exists;
    `unresolved` lists links to notes that don't exist yet."""
    return _read(lambda: links.outgoing_links(path), vault)


def find_orphans(include_without_outgoing: bool = False, vault: str | None = None) -> dict[str, Any]:
    """Notes no other note links to. With `include_without_outgoing=true`, only notes
    that are completely isolated (no links in either direction)."""

    def run() -> dict[str, Any]:
        found = links.find_orphans(include_without_outgoing)
        return {"count": len(found), "notes": found}

    return _read(run, vault)


# -- attachments -----------------------------------------------------------------------


def read_pdf(
    path: str,
    page_start: int | None = None,
    page_end: int | None = None,
    joined: bool = True,
    vault: str | None = None,
) -> dict[str, Any]:
    """Extract text from a PDF stored in the vault. Pages are 1-indexed, inclusive.

    Set `joined=false` to receive per-page strings instead of a single concatenated text.
    """

    def run() -> dict[str, Any]:
        extract = pdf.read_pdf(path, page_start=page_start, page_end=page_end)
        out: dict[str, Any] = {"path": extract.path, "page_count": extract.page_count}
        if joined:
            out["text"] = extract.joined()
        else:
            out["pages"] = extract.pages
        return out

    return _read(run, vault)


# -- assembly --------------------------------------------------------------------------

# Every tool working on a vault documents its `vault` parameter the same way.
for _fn in (
    list_notes, read_note, write_note, append_note, insert_under_heading, search_notes,
    delete_note, vault_overview, list_tags, notes_with_tag, get_backlinks,
    get_links, find_orphans, read_pdf,
):
    _fn.__doc__ = (_fn.__doc__ or "").rstrip() + VAULT_DOC

_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_ADD = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
_CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

_TOOLS: list[tuple[Callable[..., Any], ToolAnnotations]] = [
    (list_vaults, _READ),
    (vault_overview, _READ),
    (list_notes, _READ),
    (read_note, _READ),
    (search_notes, _READ),
    (list_tags, _READ),
    (notes_with_tag, _READ),
    (get_backlinks, _READ),
    (get_links, _READ),
    (find_orphans, _READ),
    (read_pdf, _READ),
    (append_note, _ADD),
    (insert_under_heading, _ADD),
    (write_note, _CHANGE),
    (delete_note, _CHANGE),
    (sync_vault, _ADD),
]


def build_server(settings: Settings) -> MCPServer:
    kwargs: dict[str, Any] = {}
    provider = None
    if settings.is_http:
        from .auth import VaultOAuthProvider

        provider = VaultOAuthProvider(settings)
        kwargs["auth_server_provider"] = provider
        kwargs["auth"] = AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=settings.mcp_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            # Single-user server: every token we issue is for this server anyway.
            validate_token_resource=False,
        )

    server = MCPServer("obsidian-mcp", instructions=INSTRUCTIONS, **kwargs)
    for fn, annotations in _TOOLS:
        server.add_tool(fn, annotations=annotations)

    if provider is not None:
        from .auth import install_login_routes

        install_login_routes(server, provider)
    return server


def _bootstrap_sync() -> None:
    """Clone (if needed) and pull before serving any requests."""
    for line in sync.bootstrap_all():
        _log(line)


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        _log(str(exc))
        sys.exit(2)

    _bootstrap_sync()
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

"""Vault-level tools: which vaults exist, and manual Git sync."""

from __future__ import annotations

from typing import Any

from .. import notes, sync
from ..vault import VaultError, configured_vaults, use_vault
from ._runtime import error

_RULES_FILES = ("CLAUDE.md", "AGENTS.md")


def list_vaults() -> dict[str, Any]:
    """The vaults this server can use, main one first. When there are several and the
    user has not said which one to use, ask them (once per conversation), then pass its
    name as `vault` to the other tools."""
    try:
        vaults = configured_vaults()
    except VaultError as exc:
        return error(exc)
    out: list[dict[str, Any]] = []
    for name, v in vaults.items():
        entry: dict[str, Any] = {"name": name, "main": v.main, "git_sync": v.remote is not None}
        if v.remote:
            entry["repository"] = v.remote
        try:
            with use_vault(name):
                entry["note_count"] = len(notes.list_notes(None))
                entry["has_rules_file"] = any((v.path / f).is_file() for f in _RULES_FILES)
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
        return error(exc)
    return result


def sync_vault(action: str = "both", vault: str | None = None) -> dict[str, Any]:
    """Manually sync the vault with its Git remote.

    `action` can be "pull", "push", or "both" (default). No-op for a vault without a
    Git remote. Reads already pull automatically every few seconds. Without `vault`,
    every configured vault is synced.
    """
    action = action.lower()
    if action not in {"pull", "push", "both"}:
        return error(VaultError(f"unknown action {action!r} (expected pull|push|both)"))
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
        return error(exc)
    return {"ok": all(r.get("ok") for r in results.values()), "vaults": results}

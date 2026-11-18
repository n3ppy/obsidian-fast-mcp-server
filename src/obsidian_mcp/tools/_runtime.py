"""How every tool runs: pick the vault, pull if stale, call the core function, and for
changes, commit + push under the git lock. Tool modules only describe *what* to do."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.types import ToolAnnotations

from .. import activity, sync
from ..vault import Vault, VaultError, configured_vaults, use_vault

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
ADD = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

VAULT_DOC = (
    "\n\n`vault`: name of the vault to use (see `list_vaults`). Optional for reading "
    "(main vault by default); required for changes when several vaults are configured."
)

# Sync outcomes that are not worth reporting back to the model.
_QUIET_SYNC = ("auto-push off", "sync disabled", "nothing to commit")


def error(exc: Exception) -> dict[str, Any]:
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


def run_read(fn: Callable[[], dict[str, Any]], vault: str | None = None) -> dict[str, Any]:
    """Run a read on one vault (the main vault unless `vault` is given)."""
    try:
        with use_vault(vault) as v:
            payload: dict[str, Any] = _tag(v)
            _refresh(payload)
            try:
                result = fn()
            except VaultError as exc:
                return {**error(exc), **payload}
            return {"ok": True, **result, **payload}
    except VaultError as exc:  # unknown vault, bad configuration
        return error(exc)


def run_write(
    vault: str | None, action: str, fn: Callable[[], tuple[str, dict[str, Any]]]
) -> dict[str, Any]:
    """Run a change on one vault: pick the vault (explicitly when there are several),
    then hold the vault's git lock for pull → write → commit → push, so a background
    pull can't rebase under a half-written file. `fn` returns (path, payload)."""
    try:
        with use_vault(vault, explicit=True) as v, sync.vault_lock(v):
            extra: dict[str, Any] = _tag(v)
            _refresh(extra)
            try:
                path, payload = fn()
            except VaultError as exc:
                return {**error(exc), **extra}
            return _with_sync(action, path, {"ok": True, **payload, **extra})
    except VaultError as exc:
        return error(exc)


def _with_sync(action: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        status = sync.auto_commit(action, path)
    except VaultError as exc:
        payload["sync_error"] = str(exc)
        activity.note_commit(None, f"error: {exc}")
    else:
        if status not in _QUIET_SYNC:
            payload["sync"] = status
        if status.startswith("pushed"):
            activity.note_commit(sync.head(), status)
    return payload

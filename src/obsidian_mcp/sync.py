"""Git-backed sync for the vault.

Enabled for each vault that has a Git remote (`OBSIDIAN_VAULT_REMOTE` for the main
vault, URLs in `OBSIDIAN_EXTRA_VAULTS` for the others, see ``vault.py``). Every
function here acts on the current vault (:func:`vault.use_vault`). On MCP startup the vault is cloned
(or pulled). Before tools run, :func:`maybe_pull` fetches changes pushed from your
other devices (at most every `OBSIDIAN_PULL_INTERVAL` seconds), so a long-running
server never works on a stale copy. After each write the server calls
:func:`commit_and_push` to mirror the change back to the remote. Push conflicts trigger
an automatic pull-rebase and retry.

All git operations on one vault are serialised by that vault's lock: tools run in
worker threads and two concurrent `git` processes on one repository would trip over
`index.lock`. Different vaults don't wait for each other.

Authentication: set `OBSIDIAN_GIT_TOKEN` (e.g. a fine-grained GitHub token limited to
the vault repos; one token covers every vault). An embedding app can instead give each
vault its own `credentials` function (see ``vault.Vault``). Either way the token is
handed to git through a credential helper that reads the environment, so it never ends
up in the remote URL or in `.git/config`.

A remote with no commits yet (a freshly created repository) is fine: pulls report it as
empty and the first write pushes the branch and sets its upstream.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .vault import Vault, VaultError, configured_vaults, get_vault, use_vault, vault_root


class SyncError(VaultError):
    pass


# One re-entrant lock per vault (commit_and_push() calls pull logic while holding it).
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_last_pull: dict[str, float] = {}  # vault key -> monotonic time of last pull
# vault key -> last pull/push outcomes (wall-clock times), shown on the dashboard.
_status: dict[str, dict[str, object]] = {}

_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get || exit 0; echo username=x-access-token; '
    'echo "password=$OBSIDIAN_GIT_TOKEN"; }; f'
)
# Git commands that talk to the remote, and so may need the token.
_NETWORK_COMMANDS = {"clone", "pull", "push", "fetch", "ls-remote"}
EMPTY_REMOTE = "remote is empty"


def vault_lock(vault: Vault | None = None) -> threading.RLock:
    """The lock serialising git work on `vault` (the current vault by default)."""
    key = (vault or get_vault()).key
    with _locks_guard:
        found = _locks.get(key)
        if found is None:
            found = _locks[key] = threading.RLock()
        return found


def forget(vault: Vault) -> None:
    """Drop the lock and sync state kept for a vault (when it is removed)."""
    with _locks_guard:
        _locks.pop(vault.key, None)
    _last_pull.pop(vault.key, None)
    _status.pop(vault.key, None)


def _current_vault() -> Vault | None:
    try:
        return get_vault()
    except VaultError:
        return None


def remote_url() -> str | None:
    try:
        return get_vault().remote
    except VaultError:
        return None


def is_enabled() -> bool:
    return remote_url() is not None


def _last(key: str) -> float:
    return _last_pull.get(key, 0.0)


def auto_push_enabled() -> bool:
    return os.environ.get("OBSIDIAN_AUTO_PUSH", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _note(event: str, error: str | None = None) -> None:
    """Remember the outcome of a pull or push for the current vault."""
    vault = _current_vault()
    if vault is None:
        return
    entry = _status.setdefault(vault.key, {})
    if error is None:
        entry[f"last_{event}"] = time.time()
        entry.pop(f"{event}_error", None)
    else:
        entry[f"{event}_error"] = error
        entry[f"{event}_error_at"] = time.time()


def status_snapshot() -> dict[str, dict[str, object]]:
    """Last pull and push times and errors, per vault key (copies)."""
    return {key: dict(entry) for key, entry in _status.items()}


def _token(vault: Vault | None, args: tuple[str, ...]) -> str | None:
    """The git password for this command: the vault's own credentials (asked only for
    commands that reach the remote), else the environment's OBSIDIAN_GIT_TOKEN."""
    if vault is None or vault.credentials is None:
        return os.environ.get("OBSIDIAN_GIT_TOKEN") or None
    if not args or args[0] not in _NETWORK_COMMANDS:
        return None
    try:
        return vault.credentials()
    except Exception as exc:  # noqa: BLE001 - reported as a sync problem
        raise SyncError(f"could not get git credentials for vault {vault.name!r}: {exc}") from exc


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    # Only the token chosen below reaches git: a vault with its own credentials never
    # sees the operator's token.
    env.pop("OBSIDIAN_GIT_TOKEN", None)
    token = _token(_current_vault(), args)
    prefix: list[str] = []
    if token:
        env["OBSIDIAN_GIT_TOKEN"] = token
        # The empty value resets any helper inherited from the global config.
        prefix = ["-c", "credential.helper=", "-c", f"credential.helper={_CREDENTIAL_HELPER}"]
    result = subprocess.run(
        ["git", *prefix, *args], cwd=str(cwd), capture_output=True, text=True, check=False, env=env
    )
    if check and result.returncode != 0:
        raise SyncError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def _configure_identity(repo: Path) -> None:
    vault = _current_vault()
    name = (vault.git_name if vault else None) or os.environ.get("OBSIDIAN_GIT_NAME", "Obsidian MCP")
    email = (vault.git_email if vault else None) or os.environ.get("OBSIDIAN_GIT_EMAIL", "obsidian-mcp@local")
    _git("config", "user.name", name, cwd=repo, check=False)
    _git("config", "user.email", email, cwd=repo, check=False)


def _remote_is_empty(repo: Path) -> bool:
    """True when the remote has no branches yet (a freshly created repository)."""
    result = _git("ls-remote", "--heads", "origin", cwd=repo, check=False)
    return result.returncode == 0 and not result.stdout.strip()


def _has_upstream(repo: Path) -> bool:
    return _git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=repo, check=False).returncode == 0


def _push(repo: Path) -> subprocess.CompletedProcess[str]:
    """Push the current branch; the first push to an empty remote also sets its upstream."""
    if _has_upstream(repo):
        return _git("push", cwd=repo, check=False)
    return _git("push", "-u", "origin", "HEAD", cwd=repo, check=False)


def ensure_clone() -> str:
    """Clone the remote vault on first use, or ensure the existing clone is wired.

    Returns a short status string. Safe to call on every server start.
    """
    remote = remote_url()
    if not remote:
        return "sync disabled"

    root = get_vault().path

    if (root / ".git").is_dir():
        _configure_identity(root)
        return f"existing clone at {root}"

    if root.exists() and any(root.iterdir()):
        raise SyncError(
            f"{root} exists and is not a git repo; refusing to clone into a non-empty directory"
        )

    root.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", remote, str(root), cwd=root.parent)
    _configure_identity(root)
    return f"cloned {remote} -> {root}"


def _abort_rebase_if_needed(repo: Path) -> None:
    """Leave the working tree usable if a rebase stopped on a conflict.

    Without this, a single conflicting edit would leave the vault mid-rebase and
    every later write and pull would fail until someone fixed it by hand.
    """
    git_dir = repo / ".git"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        _git("rebase", "--abort", cwd=repo, check=False)


def _pull_locked(root: Path) -> str:
    result = _git("pull", "--rebase", "--autostash", cwd=root, check=False)
    message = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        _abort_rebase_if_needed(root)
        if not _has_upstream(root) and _remote_is_empty(root):
            # Nothing to pull from a repository with no commits; the first write pushes.
            _last_pull[get_vault().key] = time.monotonic()
            _note("pull")
            return EMPTY_REMOTE
        _note("pull", message)
        raise SyncError(f"git pull failed: {message}")
    _last_pull[get_vault().key] = time.monotonic()
    _note("pull")
    return message or "up to date"


def pull() -> str:
    """Fetch and rebase from the remote. No-op if sync is disabled."""
    if not is_enabled():
        return "sync disabled"
    root = vault_root()
    with vault_lock():
        return _pull_locked(root)


def pull_interval() -> float:
    try:
        return float(os.environ.get("OBSIDIAN_PULL_INTERVAL", "30"))
    except ValueError:
        return 30.0


def maybe_pull() -> str | None:
    """Pull if the last successful pull is older than `OBSIDIAN_PULL_INTERVAL` seconds.

    Returns ``None`` when nothing was attempted. A negative interval disables it.
    """
    interval = pull_interval()
    if not is_enabled() or interval < 0:
        return None
    key = get_vault().key
    if time.monotonic() - _last(key) < interval:
        return None
    root = vault_root()
    with vault_lock():
        # Another thread may have pulled while we waited for the lock.
        if time.monotonic() - _last(key) < interval:
            return None
        return _pull_locked(root)


def _has_changes(repo: Path) -> bool:
    result = _git("status", "--porcelain", cwd=repo)
    return bool(result.stdout.strip())


def commit_and_push(message: str) -> str:
    """Stage everything, commit (if there are changes), then push. Idempotent on clean tree."""
    if not is_enabled():
        return "sync disabled"
    root = vault_root()

    with vault_lock():
        _git("add", "-A", cwd=root)
        if not _has_changes(root):
            return "nothing to commit"

        _git("commit", "-m", message, cwd=root)

        push = _push(root)
        if push.returncode == 0:
            _note("push")
            return "pushed"

        pull_result = _git("pull", "--rebase", "--autostash", cwd=root, check=False)
        if pull_result.returncode != 0:
            _abort_rebase_if_needed(root)
            detail = (pull_result.stderr or pull_result.stdout).strip()
            _note("push", f"push rejected and automatic rebase failed: {detail}")
            raise SyncError(
                "push rejected and automatic rebase failed (your change is committed locally "
                "and will be pushed once the conflict is resolved on another device): "
                f"{detail}"
            )
        push = _push(root)
        if push.returncode != 0:
            detail = (push.stderr or push.stdout).strip()
            _note("push", f"push failed after rebase: {detail}")
            raise SyncError(f"git push failed after rebase: {detail}")
        _last_pull[get_vault().key] = time.monotonic()
        _note("pull")
        _note("push")
        return "pushed (after rebase)"


def head() -> str | None:
    """The current commit of the current vault, or None outside a git repository."""
    try:
        result = _git("rev-parse", "HEAD", cwd=vault_root(), check=False)
    except (VaultError, OSError):
        return None
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def ahead_behind() -> tuple[int, int] | None:
    """(commits not yet pushed, commits not yet pulled) against the last fetch of the
    upstream branch. No network access. None when there is no upstream."""
    if not is_enabled():
        return None
    try:
        result = _git(
            "rev-list", "--left-right", "--count", "HEAD...@{upstream}", cwd=vault_root(), check=False
        )
    except (VaultError, OSError):
        return None
    parts = result.stdout.split()
    if result.returncode != 0 or len(parts) != 2:
        return None
    return int(parts[0]), int(parts[1])


def auto_commit(action: str, path: str) -> str:
    """Convenience wrapper used by the server after successful writes."""
    if not is_enabled() or not auto_push_enabled():
        return "auto-push off"
    return commit_and_push(f"obsidian-mcp: {action} {path}")


def bootstrap_all() -> list[str]:
    """Clone (if needed) and pull every vault with a remote. One failure does not stop
    the others; each line of the result says what happened to one vault."""
    lines: list[str] = []
    try:
        vaults = configured_vaults()
    except VaultError as exc:
        return [f"vault configuration error: {exc}"]
    for name, vault in vaults.items():
        if not vault.remote:
            continue
        try:
            with use_vault(name):
                lines.append(f"{name}: {ensure_clone()}")
                lines.append(f"{name}: pull: {pull()}")
        except VaultError as exc:
            lines.append(f"{name}: sync bootstrap failed: {exc}")
    return lines

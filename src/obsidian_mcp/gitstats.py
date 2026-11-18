"""Read-only views of a vault's git history, for the dashboard.

Commits made by this server start with ``obsidian-mcp:`` (see ``sync.auto_commit``);
everything else came from a device (Obsidian Git on the laptop or phone) or by hand.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import sync
from .vault import VaultError, get_vault, use_vault

CLAUDE_PREFIX = "obsidian-mcp:"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_CACHE_SECONDS = 60
_cache: dict[tuple[str, str, int], tuple[float, list["Commit"]]] = {}
_cache_lock = threading.Lock()


@dataclass
class Commit:
    sha: str
    ts: float
    author: str
    subject: str
    files: list[str] = field(default_factory=list)

    @property
    def by_claude(self) -> bool:
        return self.subject.startswith(CLAUDE_PREFIX)


def commits(vault: str, days: int, limit: int = 5000) -> list[Commit]:
    """Commits of the last `days` days, newest first (cached for a minute per HEAD)."""
    with use_vault(vault) as v:
        head = sync.head()
        if head is None:
            return []
        key = (v.key, head, days)
        with _cache_lock:
            hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
            return hit[1]
        result = sync._git(  # noqa: SLF001 - same package
            "log", f"--since={days} days ago", f"--max-count={limit}", "--no-merges",
            "--format=%x1e%H%x1f%ct%x1f%an%x1f%s", "--name-only",
            cwd=v.path, check=False,
        )
    found: list[Commit] = []
    if result.returncode == 0:
        for chunk in result.stdout.split("\x1e"):
            lines = chunk.strip("\n").split("\n")
            if not lines or not lines[0]:
                continue
            parts = lines[0].split("\x1f")
            if len(parts) != 4:
                continue
            sha, ts, author, subject = parts
            files = [line for line in lines[1:] if line.strip()]
            found.append(Commit(sha=sha, ts=float(ts), author=author, subject=subject, files=files))
    with _cache_lock:
        for stale in [k for k in _cache if k[0] == key[0] and k != key]:
            del _cache[stale]
        _cache[key] = (time.monotonic(), found)
    return found


def commit_stat(vault: str, sha: str) -> dict[str, Any] | None:
    """Files and line counts changed by one commit (no contents)."""
    if not _SHA_RE.match(sha):
        return None
    with use_vault(vault) as v:
        result = sync._git(  # noqa: SLF001
            "show", "--numstat", "--format=%H%x1f%ct%x1f%an%x1f%s", sha, cwd=v.path, check=False
        )
    if result.returncode != 0:
        return None
    lines = result.stdout.strip("\n").split("\n")
    head = lines[0].split("\x1f")
    if len(head) != 4:
        return None
    files = []
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) == 3:
            added, removed, path = cols
            files.append({
                "path": path,
                "added": int(added) if added.isdigit() else None,  # "-" for binary files
                "removed": int(removed) if removed.isdigit() else None,
            })
    return {"sha": head[0], "ts": float(head[1]), "author": head[2], "subject": head[3], "files": files}


def web_url(vault: str, sha: str | None = None) -> str | None:
    """GitHub link for the vault repository, or one commit in it. None for other hosts."""
    try:
        remote = get_vault(vault).remote
    except VaultError:
        return None
    if not remote:
        return None
    match = re.match(r"^(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?$", remote)
    if not match:
        return None
    base = f"https://github.com/{match.group(1)}"
    return f"{base}/commit/{sha}" if sha else base


def file_url(vault: str, path: str) -> str | None:
    base = web_url(vault)
    if base is None:
        return None
    from urllib.parse import quote

    return f"{base}/blob/HEAD/{quote(path)}"


_last_cache: dict[str, tuple[str, dict[str, tuple[float, bool]]]] = {}


def last_changes(vault: str, limit: int = 20000) -> dict[str, tuple[float, bool]]:
    """For each path in the history: when it last changed and whether Claude made that
    change. Cached per HEAD, so it reruns only after a new commit."""
    with use_vault(vault) as v:
        head = sync.head()
        if head is None:
            return {}
        with _cache_lock:
            hit = _last_cache.get(v.key)
        if hit and hit[0] == head:
            return hit[1]
        result = sync._git(  # noqa: SLF001 - same package
            "log", f"--max-count={limit}", "--no-merges", "--format=%x1e%ct%x1f%s", "--name-only",
            cwd=v.path, check=False,
        )
    found: dict[str, tuple[float, bool]] = {}
    if result.returncode == 0:
        for chunk in result.stdout.split("\x1e"):  # newest first: keep the first sighting
            lines = chunk.strip("\n").split("\n")
            parts = lines[0].split("\x1f") if lines and lines[0] else []
            if len(parts) != 2:
                continue
            ts, subject = float(parts[0]), parts[1]
            for path in lines[1:]:
                if path.strip() and path not in found:
                    found[path] = (ts, subject.startswith(CLAUDE_PREFIX))
    with _cache_lock:
        _last_cache[v.key] = (head, found)
    return found


def forget(key: str) -> None:
    """Drop the cached history of one vault (by its key)."""
    with _cache_lock:
        for stale in [k for k in _cache if k[0] == key]:
            del _cache[stale]
        _last_cache.pop(key, None)

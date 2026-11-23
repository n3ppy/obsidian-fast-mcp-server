"""The numbers behind the dashboard, computed from the activity log, git and the vault.

Plain Python with no web code, so it can be tested on its own. Times are Unix
timestamps; days are calendar days in the configured time zone.
"""

from __future__ import annotations

import os
import statistics
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from .. import gitstats, links, sync
from ..activity import ActivityLog
from ..vault import Vault, VaultError, configured_vaults, use_vault

# Tools whose `path` is a note being read (not a folder being listed).
NOTE_READ_TOOLS = ("read_note", "read_pdf", "get_backlinks", "get_links")
_VAULT_CACHE_SECONDS = 60
STARTED_AT = time.time()


@dataclass
class Clock:
    # A zone, or a function giving the zone of the current request (embedding apps).
    tz: ZoneInfo | Callable[[], ZoneInfo]

    @property
    def zone(self) -> ZoneInfo:
        return self.tz() if callable(self.tz) else self.tz

    def day(self, ts: float) -> date:
        return datetime.fromtimestamp(ts, self.zone).date()

    def today(self) -> date:
        return self.day(time.time())

    def start_of(self, day: date) -> float:
        return datetime(day.year, day.month, day.day, tzinfo=self.zone).timestamp()

    def days(self, n: int) -> list[date]:
        """The last `n` days, oldest first, ending today."""
        today = self.today()
        return [today - timedelta(days=i) for i in range(n - 1, -1, -1)]


def vault_names() -> list[str]:
    try:
        return list(configured_vaults())
    except VaultError:
        return []


# -- activity --------------------------------------------------------------------------


def daily_activity(log: ActivityLog, clock: Clock, days: int = 30) -> list[dict[str, Any]]:
    """Per day: Claude's reads and errors (activity log), Claude's commits and device
    commits (git history, so it reaches back before the log existed)."""
    window = clock.days(days)
    since = clock.start_of(window[0])
    rows = {d: {"day": d, "reads": 0, "writes": 0, "errors": 0, "claude_commits": 0, "device_commits": 0}
            for d in window}
    for event in log.query("SELECT ts, kind, ok FROM events WHERE ts >= ?", (since,)):
        row = rows.get(clock.day(event["ts"]))
        if row is None:
            continue
        if event["kind"] == "read":
            row["reads"] += 1
        elif event["kind"] == "write":
            row["writes"] += 1
        if not event["ok"]:
            row["errors"] += 1
    for name in vault_names():
        try:
            history = gitstats.commits(name, days + 1)
        except VaultError:
            continue
        for commit in history:
            row = rows.get(clock.day(commit.ts))
            if row is not None:
                row["claude_commits" if commit.by_claude else "device_commits"] += 1
    return [rows[d] for d in window]


def tiles(daily: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Today's numbers, each with the average of the 7 days before."""
    today, before = daily[-1], daily[-8:-1]

    def tile(label: str, key: str) -> dict[str, Any]:
        avg = sum(d[key] for d in before) / len(before) if before else 0
        return {"label": label, "key": key, "value": today[key], "avg": avg}

    return [
        tile("Claude read", "reads"),
        tile("Claude changed", "writes"),
        tile("Device commits", "device_commits"),
        tile("Errors", "errors"),
    ]


def errors_by_type(log: ActivityLog, since: float) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for event in log.query(
        "SELECT ts, tool, error_type, error, sync, ok FROM events "
        "WHERE ts >= ? AND (ok = 0 OR sync LIKE 'error%') ORDER BY ts DESC",
        (since,),
    ):
        kind = event["error_type"] if not event["ok"] else "SyncError"
        group = groups.setdefault(kind or "Error", {
            "type": kind or "Error", "count": 0, "last_ts": event["ts"], "last_tool": event["tool"],
            "last_error": event["error"] if not event["ok"] else (event["sync"] or "")[7:],
        })
        group["count"] += 1
    return sorted(groups.values(), key=lambda g: (-g["count"], -g["last_ts"]))


def latency(log: ActivityLog, since: float) -> list[dict[str, Any]]:
    by_tool: dict[str, list[int]] = defaultdict(list)
    for event in log.query(
        "SELECT tool, duration_ms FROM events WHERE ts >= ? AND duration_ms IS NOT NULL", (since,)
    ):
        by_tool[event["tool"]].append(event["duration_ms"])
    rows = []
    for tool, values in by_tool.items():
        values.sort()
        p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
        rows.append({"tool": tool, "count": len(values), "median": int(statistics.median(values)),
                     "p95": p95, "max": values[-1]})
    return sorted(rows, key=lambda r: -r["count"])


def tool_counts(log: ActivityLog, since: float) -> list[dict[str, Any]]:
    return log.query(
        "SELECT tool, kind, COUNT(*) AS n, SUM(ok = 0) AS errors FROM events WHERE ts >= ? "
        "GROUP BY tool, kind ORDER BY n DESC",
        (since,),
    )


# -- notes -----------------------------------------------------------------------------


def top_read(log: ActivityLog, since: float, limit: int = 10) -> list[dict[str, Any]]:
    marks = ",".join("?" * len(NOTE_READ_TOOLS))
    return log.query(
        f"SELECT vault, path, COUNT(*) AS n, MAX(ts) AS last_ts FROM events "
        f"WHERE kind = 'read' AND ok = 1 AND path IS NOT NULL AND ts >= ? AND tool IN ({marks}) "
        f"GROUP BY vault, path ORDER BY n DESC, last_ts DESC LIMIT ?",
        (since, *NOTE_READ_TOOLS, limit),
    )


def top_changed(days: int, limit: int = 10) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    by_claude: Counter[tuple[str, str]] = Counter()
    for name in vault_names():
        try:
            history = gitstats.commits(name, days)
        except VaultError:
            continue
        for commit in history:
            for path in commit.files:
                if path.startswith(".obsidian/") or path == ".gitignore":
                    continue
                counts[(name, path)] += 1
                if commit.by_claude:
                    by_claude[(name, path)] += 1
    return [
        {"vault": v, "path": p, "n": n, "claude": by_claude[(v, p)], "device": n - by_claude[(v, p)]}
        for (v, p), n in counts.most_common(limit)
    ]


_size_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_word_cache: dict[str, tuple[float, int]] = {}
_cache_lock = threading.Lock()


def forget(root: Any) -> None:
    """Drop the sizes, word counts and file tree cached for the vault at `root`."""
    key = str(root)
    with _cache_lock:
        _size_cache.pop(key, None)
        _tree_cache.pop(key, None)
        for path in [p for p in _word_cache if p.startswith(key + os.sep)]:
            del _word_cache[path]


def vault_size(vault: Vault) -> dict[str, Any]:
    """Notes, folders, words, attachments and bytes (cached for a minute)."""
    with _cache_lock:
        hit = _size_cache.get(str(vault.path))
    if hit and time.monotonic() - hit[0] < _VAULT_CACHE_SECONDS:
        return hit[1]
    notes = folders = attachments = pdfs = words = size = 0
    root = vault.path
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if current != str(root):
            folders += 1
        for name in files:
            if name.startswith("."):
                continue
            full = os.path.join(current, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            size += stat.st_size
            if name.endswith(".md"):
                notes += 1
                words += _words(full, stat.st_mtime)
            else:
                attachments += 1
                if name.lower().endswith(".pdf"):
                    pdfs += 1
    result = {"notes": notes, "folders": folders, "words": words, "attachments": attachments,
              "pdfs": pdfs, "bytes": size}
    with _cache_lock:
        _size_cache[str(vault.path)] = (time.monotonic(), result)
    return result


def _words(path: str, mtime: float) -> int:
    with _cache_lock:
        hit = _word_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            count = len(fh.read().split())
    except OSError:
        return 0
    with _cache_lock:
        _word_cache[path] = (mtime, count)
    return count


def link_health(vault: str, limit: int = 200) -> dict[str, Any]:
    """Orphans (nothing links to them), broken wikilinks and untagged notes."""
    with use_vault(vault):
        index = links.VaultIndex()
    linked: set[str] = set()
    broken: list[dict[str, str]] = []
    for note in index.notes.values():
        for link in note.links:
            if link.resolved and link.resolved != note.path:
                linked.add(link.resolved)
            elif link.resolved is None:
                target = link.target
                if not PurePosixPath(target).suffix or target.lower().endswith(".md"):
                    broken.append({"note": note.path, "target": target})
    orphans = sorted(p for p in index.notes if p not in linked)
    untagged = sorted(p for p, n in index.notes.items() if not n.tags)
    return {
        "notes": len(index.notes),
        "orphan_count": len(orphans), "orphans": orphans[:limit],
        "broken_count": len(broken), "broken": broken[:limit],
        "untagged_count": len(untagged), "untagged": untagged[:limit],
    }


def ensure_snapshots(log: ActivityLog, clock: Clock) -> None:
    """Record today's size of each vault once (refreshed when the page is viewed)."""
    day = clock.today().isoformat()
    try:
        vaults = configured_vaults()
    except VaultError:
        return
    for name, vault in vaults.items():
        size = vault_size(vault)
        log.snapshot(day, name, size["notes"], size["words"], size["attachments"])


# -- wikis (file tree) -----------------------------------------------------------------

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif", ".heic")
_tree_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def file_kind(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".md"):
        return "note"
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith(IMAGE_SUFFIXES):
        return "image"
    return "file"


def tree(vault: Vault) -> dict[str, Any]:
    """The vault as nested folders, sorted like Obsidian (folders first, then A to Z,
    ignoring case). Dot folders and files (.obsidian, .git) are left out. Each folder
    carries ``count`` (files inside it, at any depth) and ``size`` (entries in total).
    Cached for a minute."""
    key = str(vault.path)
    with _cache_lock:
        hit = _tree_cache.get(key)
    if hit and time.monotonic() - hit[0] < _VAULT_CACHE_SECONDS:
        return hit[1]

    def walk(folder: str, rel: str) -> dict[str, Any]:
        dirs, files = [], []
        try:
            entries = list(os.scandir(folder))
        except OSError:
            entries = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            path = f"{rel}/{entry.name}" if rel else entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(walk(entry.path, path))
                elif entry.is_file(follow_symlinks=False):
                    files.append({"name": entry.name, "path": path, "kind": file_kind(entry.name)})
            except OSError:
                continue
        dirs.sort(key=lambda d: d["name"].casefold())
        files.sort(key=lambda f: f["name"].casefold())
        return {
            "name": PurePosixPath(rel).name if rel else vault.name, "path": rel, "dirs": dirs, "files": files,
            "count": len(files) + sum(d["count"] for d in dirs),
            "size": len(files) + sum(1 + d["size"] for d in dirs),
        }

    result = walk(str(vault.path), "")
    with _cache_lock:
        _tree_cache[key] = (time.monotonic(), result)
    return result


def find_dir(root: dict[str, Any], path: str) -> dict[str, Any] | None:
    """A folder of the tree by its vault-relative path (looked up, never opened from
    the path, so a crafted path can't escape the vault)."""
    node = root
    for part in [p for p in path.split("/") if p]:
        node = next((d for d in node["dirs"] if d["name"] == part), None)
        if node is None:
            return None
    return node


def search_tree(root: dict[str, Any], query: str, limit: int = 200) -> tuple[list[dict[str, Any]], int]:
    """Files whose path contains every word of `query` (ignoring case)."""
    words = query.casefold().split()
    found: list[dict[str, Any]] = []
    total = 0
    stack = [root]
    while stack:
        node = stack.pop()
        for f in node["files"]:
            if all(w in f["path"].casefold() for w in words):
                total += 1
                if len(found) < limit:
                    found.append(f)
        stack.extend(reversed(node["dirs"]))
    found.sort(key=lambda f: f["path"].casefold())
    return found, total


def read_counts(log: ActivityLog, since: float) -> dict[tuple[str, str], int]:
    marks = ",".join("?" * len(NOTE_READ_TOOLS))
    rows = log.query(
        f"SELECT vault, path, COUNT(*) AS n FROM events WHERE kind = 'read' AND ok = 1 "
        f"AND path IS NOT NULL AND ts >= ? AND tool IN ({marks}) GROUP BY vault, path",
        (since, *NOTE_READ_TOOLS),
    )
    return {(r["vault"], r["path"]): r["n"] for r in rows}


# -- health ----------------------------------------------------------------------------


def sync_health(log: ActivityLog) -> list[dict[str, Any]]:
    """Per vault: last pull and push, current errors, commits waiting to be pushed."""
    status = sync.status_snapshot()
    rows = []
    try:
        vaults = configured_vaults()
    except VaultError:
        return []
    for name, vault in vaults.items():
        entry = status.get(vault.key, {})
        counts = None
        if vault.remote:
            try:
                with use_vault(name):
                    counts = sync.ahead_behind()
            except VaultError:
                counts = None
        last_pull, last_push = entry.get("last_pull"), entry.get("last_push")
        pull_error = entry.get("pull_error") if entry.get("pull_error_at", 0) > (last_pull or 0) else None
        push_error = entry.get("push_error") if entry.get("push_error_at", 0) > (last_push or 0) else None
        ahead = counts[0] if counts else 0
        if not vault.remote:
            state = "local"
        elif pull_error or push_error:
            state = "error"
        elif ahead:
            state = "warn"
        else:
            state = "ok"
        recent_errors = log.query(
            "SELECT ts, tool, path, sync FROM events WHERE vault = ? AND sync LIKE 'error%' "
            "ORDER BY ts DESC LIMIT 5",
            (name,),
        )
        rows.append({
            "vault": name, "remote": bool(vault.remote), "state": state,
            "last_pull": last_pull, "last_push": last_push,
            "pull_error": pull_error, "pull_error_at": entry.get("pull_error_at"),
            "push_error": push_error, "push_error_at": entry.get("push_error_at"),
            "ahead": ahead, "behind": counts[1] if counts else 0,
            "recent_errors": recent_errors,
        })
    return rows


def clients(log: ActivityLog) -> list[dict[str, Any]]:
    return log.query(
        "SELECT client_id, user, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
        "FROM events WHERE client_id IS NOT NULL GROUP BY client_id, user ORDER BY last_ts DESC"
    )

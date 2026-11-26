"""Activity log: one row per MCP tool call, in a small SQLite file in the state directory.

This is what the dashboard reads ("what did Claude do in my vault?"), and the base for
a later undo tool. It records *which* tool touched *which* path, never what a note
says: no note contents, no search queries, no text Claude wrote.

Every tool call passes through :class:`ActivityMiddleware`, so all 16 tools are
covered by one hook. Writes that end in a git commit also record the commit, through
:func:`note_commit` (called from the tool runtime while the git lock is held).

Logging must never break a tool call: any failure here is printed and ignored.

An embedding app with several users gives each request its own log with
:func:`use_log`; the middleware and the dashboard then read and write that one.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

DEFAULT_RETENTION_DAYS = 90
_ERROR_LIMIT = 300  # characters of an error message kept

# Set by the middleware for the duration of one tool call; the tool runtime adds the
# commit to it. A mutable dict, so a tool running in a worker thread can fill it in.
_current: ContextVar[dict[str, Any] | None] = ContextVar("obsidian_activity", default=None)
# The log of the current request, when an embedding app chose one (see use_log).
_scope: ContextVar["ActivityLog | None"] = ContextVar("obsidian_activity_log", default=None)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    vault TEXT,
    tool TEXT NOT NULL,
    kind TEXT NOT NULL,          -- read | write | sync
    path TEXT,
    ok INTEGER NOT NULL,
    error_type TEXT,
    error TEXT,
    duration_ms INTEGER,
    client_id TEXT,
    user TEXT,
    commit_sha TEXT,
    sync TEXT                    -- push outcome for writes, or the sync error
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS snapshots (
    day TEXT NOT NULL,
    vault TEXT NOT NULL,
    notes INTEGER NOT NULL,
    words INTEGER NOT NULL,
    attachments INTEGER NOT NULL,
    PRIMARY KEY (day, vault)
);
"""

COLUMNS = (
    "id", "ts", "vault", "tool", "kind", "path", "ok", "error_type", "error",
    "duration_ms", "client_id", "user", "commit_sha", "sync",
)


def _warn(message: str) -> None:
    print(f"[obsidian-mcp] activity log: {message}", file=sys.stderr, flush=True)


@dataclass
class Event:
    ts: float
    tool: str
    kind: str
    ok: bool
    vault: str | None = None
    path: str | None = None
    error_type: str | None = None
    error: str | None = None
    duration_ms: int | None = None
    client_id: str | None = None
    user: str | None = None
    commit_sha: str | None = None
    sync: str | None = None


class ActivityLog:
    """Thread-safe access to the activity database."""

    def __init__(self, path: Path, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.path = path
        self.retention_days = retention_days
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self._last_prune = 0.0
        self.prune()

    # -- writing ---------------------------------------------------------------------

    def record(self, event: Event) -> None:
        values = (
            event.ts, event.vault, event.tool, event.kind, event.path, int(event.ok),
            event.error_type, (event.error or "")[:_ERROR_LIMIT] or None, event.duration_ms,
            event.client_id, event.user, event.commit_sha, event.sync,
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO events (ts, vault, tool, kind, path, ok, error_type, error, "
                "duration_ms, client_id, user, commit_sha, sync) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        if time.time() - self._last_prune > 24 * 3600:
            self.prune()

    def prune(self) -> int:
        """Drop events older than the retention period. Returns how many were removed."""
        self._last_prune = time.time()
        if self.retention_days <= 0:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        with self._lock:
            cur = self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        return cur.rowcount

    def snapshot(self, day: str, vault: str, notes: int, words: int, attachments: int) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO snapshots (day, vault, notes, words, attachments) "
                "VALUES (?, ?, ?, ?, ?)",
                (day, vault, notes, words, attachments),
            )

    # -- reading ---------------------------------------------------------------------

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, params).fetchall()]

    def events(
        self,
        *,
        since: float | None = None,
        kind: str | None = None,
        tool: str | None = None,
        errors_only: bool = False,
        path_contains: str | None = None,
        vault: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = self._filters(since, kind, tool, errors_only, path_contains, vault)
        return self.query(
            f"SELECT * FROM events {where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )

    def count(self, **filters: Any) -> int:
        where, params = self._filters(
            filters.get("since"), filters.get("kind"), filters.get("tool"),
            filters.get("errors_only", False), filters.get("path_contains"), filters.get("vault"),
        )
        return self.query(f"SELECT COUNT(*) AS n FROM events {where}", params)[0]["n"]

    @staticmethod
    def _filters(
        since: float | None, kind: str | None, tool: str | None, errors_only: bool,
        path_contains: str | None, vault: str | None,
    ) -> tuple[str, tuple[Any, ...]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if tool:
            clauses.append("tool = ?")
            params.append(tool)
        if errors_only:
            clauses.append("(ok = 0 OR sync LIKE 'error%')")
        if path_contains:
            clauses.append("path LIKE ? ESCAPE '\\'")
            escaped = path_contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        if vault:
            clauses.append("vault = ?")
            params.append(vault)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", tuple(params)

    def rename_vault(self, old: str, new: str) -> None:
        """Keep a renamed vault's history under its new name."""
        with self._lock:
            self._db.execute("UPDATE events SET vault = ? WHERE vault = ?", (new, old))
            self._db.execute("UPDATE OR REPLACE snapshots SET vault = ? WHERE vault = ?", (new, old))

    def forget_vault(self, name: str) -> None:
        """Drop the history of a vault that was removed, so a later one of the same name
        starts clean."""
        with self._lock:
            self._db.execute("DELETE FROM events WHERE vault = ?", (name,))
            self._db.execute("DELETE FROM snapshots WHERE vault = ?", (name,))

    def snapshots(self, vault: str, limit: int = 120) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM snapshots WHERE vault = ? ORDER BY day DESC LIMIT ?", (vault, limit)
        )
        return list(reversed(rows))

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += Path(str(self.path) + suffix).stat().st_size
            except OSError:
                pass
        return total

    def close(self) -> None:
        with self._lock:
            self._db.close()


# -- one log per request (embedding apps) -----------------------------------------------

_scoped_none: ContextVar[bool] = ContextVar("obsidian_activity_log_off", default=False)


@contextmanager
def use_log(log: ActivityLog | None) -> Iterator[None]:
    """Record and read activity in `log` for the enclosed code. None turns logging off
    there (rather than falling back to the server's own log)."""
    token = _scope.set(log)
    off = _scoped_none.set(log is None)
    try:
        yield
    finally:
        _scoped_none.reset(off)
        _scope.reset(token)


def current_log(default: ActivityLog | None) -> ActivityLog | None:
    """The log chosen with :func:`use_log`, else `default`."""
    if _scoped_none.get():
        return None
    return _scope.get() or default


class ScopedLog:
    """Stands in for an :class:`ActivityLog`, forwarding every call to the log of the
    current request (or to `default` outside any :func:`use_log`)."""

    def __init__(self, default: ActivityLog) -> None:
        self._default = default

    def _target(self) -> ActivityLog:
        log = current_log(self._default)
        if log is None:
            raise RuntimeError("no activity log for this request")
        return log

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)


# -- recording tool calls --------------------------------------------------------------


def note_commit(sha: str | None, sync_status: str | None = None) -> None:
    """Called by the tool runtime after a write: the commit it made and how the push went."""
    holder = _current.get()
    if holder is not None:
        if sha:
            holder["commit_sha"] = sha
        if sync_status:
            holder["sync"] = sync_status


def _structured(result: Any) -> dict[str, Any]:
    content = getattr(result, "structured_content", None)
    if isinstance(content, dict) and set(content) == {"result"} and isinstance(content["result"], dict):
        content = content["result"]
    return content if isinstance(content, dict) else {}


def _jwt_client_id(raw: str | None) -> str | None:
    """The MCP client a FastMCP-issued token was given to. The token has already been
    verified by the auth layer; this only reads a claim for display."""
    if not raw or raw.count(".") != 2:
        return None
    try:
        payload = raw.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return None
    value = data.get("client_id") if isinstance(data, dict) else None
    return str(value) if value else None


def _caller() -> tuple[str | None, str | None]:
    """(client id, user) for the current request, when known."""
    try:
        from fastmcp.server.dependencies import get_access_token
    except ImportError:  # pragma: no cover
        return None, None
    try:
        token = get_access_token()
    except Exception:  # noqa: BLE001 - no request context (stdio)
        return None, None
    if token is None:
        return None, None
    claims = token.claims or {}
    login = claims.get("login")
    if isinstance(login, str):
        # GitHub login: the validated token is GitHub's, so the MCP client is read from
        # the server's own token in the request (already verified by the auth layer).
        return _jwt_client_id(_bearer()) or token.client_id, login.lower()
    return token.client_id, token.subject


def _bearer() -> str | None:
    try:
        from fastmcp.server.dependencies import get_http_request

        header = get_http_request().headers.get("authorization", "")
    except Exception:  # noqa: BLE001 - no HTTP request (in-memory client, stdio)
        return None
    return header[7:].strip() if header.lower().startswith("bearer ") else None


class ActivityMiddleware(Middleware):
    """FastMCP middleware recording every tool call into an :class:`ActivityLog`."""

    def __init__(self, log: ActivityLog | None, kinds: dict[str, str], main_vault: str | None = None) -> None:
        self.log = log  # the server's own log; a request's own log (use_log) wins
        self.kinds = kinds  # tool name -> read | write | sync
        self.main_vault = main_vault

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        name = getattr(context.message, "name", "?")
        args = getattr(context.message, "arguments", None) or {}
        holder: dict[str, Any] = {}
        reset = _current.set(holder)
        start = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            self._record(name, args, holder, start, None, exc)
            raise
        finally:
            _current.reset(reset)
        self._record(name, args, holder, start, result, None)
        return result

    def _record(
        self, name: str, args: dict[str, Any], holder: dict[str, Any], start: float,
        result: Any, exc: Exception | None,
    ) -> None:
        try:
            content = _structured(result) if result is not None else {}
            ok = exc is None and content.get("ok", True) is not False and not getattr(result, "is_error", False)
            path = content.get("path") if isinstance(content.get("path"), str) else None
            if path is None:
                for key in ("path", "subdir"):
                    if isinstance(args.get(key), str) and args[key]:
                        path = args[key]
                        break
            if path and name != "list_notes" and not PurePosixPath(path).suffix:
                path += ".md"  # as the tools resolve it, so one note has one path in the log
            log = current_log(self.log)
            if log is None:
                return
            vault = content.get("vault") or args.get("vault") or self._main_vault()
            sync_status = holder.get("sync")
            if content.get("sync_error"):
                sync_status = f"error: {content['sync_error']}"
            elif sync_status is None and isinstance(content.get("sync"), str):
                sync_status = content["sync"]
            error = None if ok else str(exc) if exc else content.get("error")
            error_type = None if ok else (type(exc).__name__ if exc else content.get("type"))
            client_id, user = _caller()
            log.record(
                Event(
                    ts=time.time(),
                    tool=name,
                    kind=self.kinds.get(name, "read"),
                    ok=ok,
                    vault=str(vault) if vault else None,
                    path=path,
                    error_type=error_type,
                    error=error,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    client_id=client_id,
                    user=user,
                    commit_sha=holder.get("commit_sha"),
                    sync=sync_status,
                )
            )
        except Exception as log_exc:  # noqa: BLE001 - logging must never break a tool
            _warn(f"could not record {name}: {log_exc}")

    def _main_vault(self) -> str | None:
        """The vault a call without `vault` used: the first of the request's vaults when
        an embedding app chose them, else the server's main vault."""
        from .vault import VaultError, configured_vaults, scoped

        if not scoped():
            return self.main_vault
        try:
            return next(iter(configured_vaults()), None)
        except VaultError:
            return None

"""Workspaces: what one caller may see, for apps that embed this server for many users.

A self-hosted server has one owner and reads its vaults from the environment; nothing
here is used. An embedding app (a hosted service) instead builds the server with a
``workspaces`` resolver (see ``server.build_server``). For every MCP request the
resolver turns the caller's access token into a :class:`Workspace`, and the request
runs inside it: tools see only that workspace's vaults, the activity is recorded in
its own log, and the dashboard shows its own numbers in its own time zone.

A request whose caller the resolver doesn't know gets an *empty* workspace, never the
environment's vaults.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from . import activity, gitstats, links, sync
from .activity import ActivityLog
from .vault import Vault, use_vaults

DENIED = "This account has no vault on this server."


@dataclass
class Workspace:
    vaults: dict[str, Vault] = field(default_factory=dict)
    log: ActivityLog | None = None
    timezone: str | None = None  # IANA name for the dashboard; None: the server's
    label: str | None = None  # who this is, for display (an email, a username)
    empty_message: str | None = None  # the error tools give when there's no vault


_current: ContextVar[Workspace | None] = ContextVar("obsidian_workspace", default=None)


@contextmanager
def use_workspace(workspace: Workspace) -> Iterator[Workspace]:
    """Run the enclosed code inside `workspace`."""
    with use_vaults(workspace.vaults, empty_message=workspace.empty_message), activity.use_log(workspace.log):
        token = _current.set(workspace)
        try:
            yield workspace
        finally:
            _current.reset(token)


def current() -> Workspace | None:
    """The workspace of the current request, or None (self-hosted mode)."""
    return _current.get()


Resolver = Callable[[Any], "Workspace | None | Awaitable[Workspace | None]"]


def _access_token() -> Any:
    try:
        from fastmcp.server.dependencies import get_access_token

        return get_access_token()
    except Exception:  # noqa: BLE001 - no request context (in-memory client)
        return None


class WorkspaceMiddleware(Middleware):
    """Runs each MCP request inside the caller's workspace.

    Added first (outermost), so the activity log middleware and every tool already
    see the workspace. `resolve` gets the verified access token (or None) and returns
    the workspace, or None to deny: the request then runs in an empty workspace.
    """

    def __init__(self, resolve: Resolver) -> None:
        self.resolve = resolve

    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        found = self.resolve(_access_token())
        if inspect.isawaitable(found):
            found = await found
        workspace = found if isinstance(found, Workspace) else Workspace(empty_message=DENIED)
        with use_workspace(workspace):
            return await call_next(context)


def forget(vault: Vault) -> None:
    """Drop everything cached for a vault: sync state, its lock, parsed notes, git
    history and dashboard counts. Call it when a vault is removed."""
    sync.forget(vault)
    gitstats.forget(vault.key)
    links.forget(vault.path)
    try:
        from .dashboard import stats
    except ImportError:  # the dashboard extra isn't installed
        return
    stats.forget(vault.path)

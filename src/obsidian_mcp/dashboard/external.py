"""Dashboard sign-in handled by an app that embeds the server (a hosted service).

The embedding app signs people in, picks their workspace for each request (see
``obsidian_mcp.workspace``) and tells the dashboard who is looking through the
callables below. They take no arguments: the app sets what they read (context
variables) before the dashboard sees the request. The dashboard then shows no login
of its own and trusts these answers completely.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def _none() -> None:
    return None


def _no_items() -> tuple[tuple[str, str], ...]:
    return ()


def _nothing(vault: str, path: str) -> Any:
    return None


@dataclass(frozen=True)
class ExternalAuth:
    user: Callable[[], str | None]  # label of the person signed in, or None
    login_url: str  # where signed-out visitors are sent
    logout_url: str  # POST target of the "Sign out" button
    csrf: Callable[[], str | None] = _none  # value the sign-out form must carry
    brand: str = "Vault dashboard"  # name in the header and page titles
    nav: tuple[tuple[str, str], ...] = ()  # extra header links: (label, href)
    banner: Callable[[], Any] | None = None  # shown at the top of the Overview, or None
    masthead: Any = None  # shown instead of the brand name at the top left (a logo, a link home)
    menu: Callable[[], tuple[tuple[Any, ...], ...]] = _no_items  # the account menu's pages: (label, href[, badge])
    # The vault switcher's own links, below the vaults: (label, href), e.g. "New knowledge
    # base". With any, the switcher shows even for a single vault.
    vault_links: Callable[[], tuple[tuple[str, str], ...]] = _no_items
    vault_words: tuple[str, str] = ("vault", "vaults")  # the app's word for a vault, on every page
    # The app's own tabs, before Health: (label, path inside the dashboard), e.g. ("Memory", "/memory").
    # The app serves those pages itself, with ``ui.page(..., tabs=...)``.
    tabs: Callable[[], tuple[tuple[str, str], ...]] = _no_items
    # On a note's page: buttons beside its GitHub link, and a section under it. Each gets the
    # vault's name and the note's path, and returns FT components or None.
    note_actions: Callable[[str, str], Any] = _nothing
    note_footer: Callable[[str, str], Any] = _nothing

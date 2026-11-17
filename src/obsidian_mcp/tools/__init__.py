"""Every MCP tool the server exposes, in the order clients list them.

Each tool module holds plain functions; this file decides which become tools, with
which annotations, and documents the shared `vault` parameter once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.types import ToolAnnotations

from ._runtime import ADD, CHANGE, READ, VAULT_DOC
from .attachments import read_pdf
from .graph import find_orphans, get_backlinks, get_links, list_tags, notes_with_tag, vault_overview
from .notes import (
    append_note,
    delete_note,
    insert_under_heading,
    list_notes,
    read_note,
    search_notes,
    write_note,
)
from .vaults import list_vaults, sync_vault

TOOLS: list[tuple[Callable[..., Any], ToolAnnotations]] = [
    (list_vaults, READ),
    (vault_overview, READ),
    (list_notes, READ),
    (read_note, READ),
    (search_notes, READ),
    (list_tags, READ),
    (notes_with_tag, READ),
    (get_backlinks, READ),
    (get_links, READ),
    (find_orphans, READ),
    (read_pdf, READ),
    (append_note, ADD),
    (insert_under_heading, ADD),
    (write_note, CHANGE),
    (delete_note, CHANGE),
    (sync_vault, ADD),
]

# Tools that take `vault` all document it the same way (list_vaults and sync_vault
# describe it themselves).
for _fn, _ in TOOLS:
    if _fn not in (list_vaults, sync_vault):
        _fn.__doc__ = (_fn.__doc__ or "").rstrip() + VAULT_DOC

__all__ = [fn.__name__ for fn, _ in TOOLS] + ["TOOLS"]

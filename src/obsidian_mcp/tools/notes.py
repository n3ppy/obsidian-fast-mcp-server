"""Note tools: list, read, search, and the changes (write, append, insert, delete)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .. import notes as core
from ._runtime import run_read, run_write


def list_notes(subdir: str | None = None, vault: str | None = None) -> dict[str, Any]:
    """List all markdown notes in the vault (optionally scoped to a subdirectory)."""
    return run_read(lambda: {"notes": core.list_notes(subdir)}, vault)


def read_note(path: str, vault: str | None = None) -> dict[str, Any]:
    """Read a note's full markdown content. `path` is vault-relative; the `.md` suffix is optional."""
    return run_read(lambda: {"path": path, "content": core.read_note(path)}, vault)


def search_notes(
    query: str, regex: bool = False, max_results: int = 200, vault: str | None = None
) -> dict[str, Any]:
    """Search note contents. Default is case-insensitive substring; pass `regex=true` for regex."""

    def run() -> dict[str, Any]:
        hits = core.search_notes(query, regex=regex, max_results=max_results)
        return {"count": len(hits), "hits": [asdict(h) for h in hits]}

    return run_read(run, vault)


def write_note(
    path: str, content: str, overwrite: bool = False, vault: str | None = None
) -> dict[str, Any]:
    """Create a note (or overwrite if `overwrite=true`). Creates parent directories as needed."""

    def run() -> tuple[str, dict[str, Any]]:
        written = core.write_note(path, content, overwrite=overwrite)
        return written, {"path": written}

    return run_write(vault, "write", run)


def append_note(path: str, content: str, vault: str | None = None) -> dict[str, Any]:
    """Append text to the end of a note (creates the note if it doesn't exist)."""

    def run() -> tuple[str, dict[str, Any]]:
        written = core.append_note(path, content)
        return written, {"path": written}

    return run_write(vault, "append", run)


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
        written = core.insert_under_heading(
            path, heading, content, create_if_missing=create_if_missing
        )
        return written, {"path": written}

    return run_write(vault, "insert", run)


def delete_note(path: str, vault: str | None = None) -> dict[str, Any]:
    """Delete a note from the vault. The deletion is committed, so it can be undone from Git history."""

    def run() -> tuple[str, dict[str, Any]]:
        deleted = core.delete_note(path)
        return deleted, {"deleted": deleted}

    return run_write(vault, "delete", run)

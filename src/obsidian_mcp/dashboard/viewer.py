"""The note viewer's side of the vault: finding a note or a file safely, resolving links and
attachments the way Obsidian does, and the Context the renderer needs.

Every function here works in the current vault (``use_vault``), so a request can only
reach the vaults of the workspace it runs in.

- A path is served only if it resolves inside the vault, through symlinks too, and no part
  of it starts with a dot (``.git``, ``.obsidian``).
- Links to notes resolve like the vault's backlinks (``links.VaultIndex.resolve``): by
  name, preferring the same folder, then the shortest path.
- Attachments resolve like Obsidian's: a path relative to the note, then from the vault's
  root, then by file name anywhere, preferring the note's folder, then the shortest path.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..links import VaultIndex
from ..render import Context
from ..vault import VaultError, resolve_inside_vault, vault_root

_HIDDEN = re.compile(r"(^|/)\.")
# Files a page may show inline; anything else downloads. SVG only ever appears in <img>,
# and is served with a sandbox policy in case someone opens it directly.
INLINE_TYPES = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
    "avif": "image/avif", "bmp": "image/bmp", "svg": "image/svg+xml",
}


def locate(path: str) -> tuple[str, Path] | None:
    """(vault-relative path, file) for a path inside the current vault, or None."""
    if not path or "\x00" in path or _HIDDEN.search(path.replace("\\", "/")):
        return None
    try:
        target = resolve_inside_vault(path)
        rel = target.relative_to(vault_root()).as_posix()
    except (VaultError, ValueError, OSError):
        return None
    if _HIDDEN.search(rel) or not target.is_file():
        return None
    return rel, target


def read_text(path: str) -> str | None:
    found = locate(path)
    if found is None or not found[0].lower().endswith(".md"):
        return None
    try:
        return found[1].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def strict_line_breaks() -> bool:
    """The vault's "Strict line breaks" setting, when it syncs .obsidian/app.json."""
    try:
        settings = json.loads((vault_root() / ".obsidian" / "app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, VaultError):
        return False
    return bool(isinstance(settings, dict) and settings.get("strictLineBreaks"))


class Files:
    """Every file in the vault (from the dashboard's cached tree), for resolving attachments."""

    def __init__(self, tree: dict[str, Any]) -> None:
        self.paths: set[str] = set()
        self.by_name: dict[str, list[str]] = {}
        stack = [tree]
        while stack:
            node = stack.pop()
            for f in node["files"]:
                self.paths.add(f["path"])
                self.by_name.setdefault(f["name"].lower(), []).append(f["path"])
            stack.extend(node["dirs"])
        self._lower = {p.lower(): p for p in self.paths}

    def resolve(self, name: str, from_note: str | None) -> str | None:
        name = name.strip().replace("\\", "/")
        if not name:
            return None
        folder = posixpath.dirname(from_note) if from_note else ""
        for candidate in (posixpath.normpath(posixpath.join(folder, name)), posixpath.normpath(name.lstrip("/"))):
            if not candidate.startswith("..") and candidate.lower() in self._lower:
                return self._lower[candidate.lower()]
        found = self.by_name.get(posixpath.basename(name).lower(), [])
        if not found:
            return None
        same = [p for p in found if posixpath.dirname(p) == folder]
        return same[0] if same else min(found, key=lambda p: (p.count("/"), p))


def context(index: VaultIndex, files: Files, *, own_prefix: str, note_url: Callable[[str], str],
            file_url: Callable[[str], str], open_url: Callable[[str], str], tag_url: Callable[[str], str]) -> Context:
    def resolve_note(target: str, from_note: str | None) -> str | None:
        return index.resolve(target, from_note=from_note)

    return Context(resolve_note=resolve_note, resolve_file=files.resolve, read=read_text, note_url=note_url,
                   file_url=file_url, open_url=open_url, tag_url=tag_url, own_prefix=own_prefix)


def backlinks(index: VaultIndex, path: str) -> list[str]:
    """The notes that link to `path` (embeds included), A to Z."""
    return sorted((n.path for n in index.notes.values() if n.path != path and any(link.resolved == path
                                                                                    for link in n.links)),
                  key=str.casefold)

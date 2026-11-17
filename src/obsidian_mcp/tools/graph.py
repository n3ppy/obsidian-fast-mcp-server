"""Graph tools: the vault as tags and links (overview, tags, backlinks, orphans)."""

from __future__ import annotations

from typing import Any

from .. import links
from ._runtime import run_read


def vault_overview(vault: str | None = None) -> dict[str, Any]:
    """Map of the vault: note count, folders, most used tags, most linked notes, orphans
    and unresolved links. A good first call to get oriented."""
    return run_read(links.overview, vault)


def list_tags(prefix: str | None = None, vault: str | None = None) -> dict[str, Any]:
    """List tags with how many notes use each (frontmatter and inline #tags).

    With `prefix="philo"`, only `#philo` and nested tags like `#philo/nietzsche`.
    """
    return run_read(lambda: {"tags": links.list_tags(prefix)}, vault)


def notes_with_tag(tag: str, include_nested: bool = True, vault: str | None = None) -> dict[str, Any]:
    """Notes carrying a tag. `#` is optional; nested tags (`tag/sub`) are included by default."""

    def run() -> dict[str, Any]:
        found = links.notes_with_tag(tag, include_nested=include_nested)
        return {"tag": tag.lstrip("#"), "count": len(found), "notes": found}

    return run_read(run, vault)


def get_backlinks(path: str, vault: str | None = None) -> dict[str, Any]:
    """Notes that link to this one (wikilinks, embeds and markdown links), with the line
    containing each link. Accepts a vault path or a bare note name."""
    return run_read(lambda: links.backlinks(path), vault)


def get_links(path: str, vault: str | None = None) -> dict[str, Any]:
    """Links going out of a note, each resolved to a vault path when the target exists;
    `unresolved` lists links to notes that don't exist yet."""
    return run_read(lambda: links.outgoing_links(path), vault)


def find_orphans(include_without_outgoing: bool = False, vault: str | None = None) -> dict[str, Any]:
    """Notes no other note links to. With `include_without_outgoing=true`, only notes
    that are completely isolated (no links in either direction)."""

    def run() -> dict[str, Any]:
        found = links.find_orphans(include_without_outgoing)
        return {"count": len(found), "notes": found}

    return run_read(run, vault)

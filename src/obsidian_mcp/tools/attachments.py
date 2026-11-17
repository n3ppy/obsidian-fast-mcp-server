"""Attachment tools: reading files stored in the vault that aren't notes."""

from __future__ import annotations

from typing import Any

from .. import pdf
from ._runtime import run_read


def read_pdf(
    path: str,
    page_start: int | None = None,
    page_end: int | None = None,
    joined: bool = True,
    vault: str | None = None,
) -> dict[str, Any]:
    """Extract text from a PDF stored in the vault. Pages are 1-indexed, inclusive.

    Set `joined=false` to receive per-page strings instead of a single concatenated text.
    """

    def run() -> dict[str, Any]:
        extract = pdf.read_pdf(path, page_start=page_start, page_end=page_end)
        out: dict[str, Any] = {"path": extract.path, "page_count": extract.page_count}
        if joined:
            out["text"] = extract.joined()
        else:
            out["pages"] = extract.pages
        return out

    return run_read(run, vault)

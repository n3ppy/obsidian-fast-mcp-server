"""The dashboard's own static files: the note viewer's script and stylesheet, and pinned
copies of KaTeX and Pele (``static/vendor/README.md``).

They are served at ``<dashboard>/static/<digest>/<file>`` and cached for a year: the
digest is a hash of every file, so any change gives every file a new address. Only the
files listed here are served, from the package itself.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

from ..render import code_css

STATIC = Path(__file__).parent / "static"
TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2"}
GENERATED = "note.css"  # note.css on disk, plus the code colours from Pygments


@cache
def note_css() -> bytes:
    return (STATIC / GENERATED).read_bytes() + b"\n/* Code colours (Pygments) */\n" + code_css().encode()


@cache
def files() -> dict[str, Path]:
    """Every servable file: its path under static/ -> where it is."""
    return {p.relative_to(STATIC).as_posix(): p for p in sorted(STATIC.rglob("*"))
            if p.is_file() and p.suffix in TYPES}


@cache
def digest() -> str:
    h = hashlib.sha256()
    for name, path in files().items():
        h.update(name.encode())
        h.update(note_css() if name == GENERATED else path.read_bytes())
    return h.hexdigest()[:12]


def content_type(name: str) -> str | None:
    return TYPES.get(Path(name).suffix)

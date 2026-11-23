"""Code colouring with Pygments, on the server.

Pygments knows about 600 languages and is the same kind of highlighter as Obsidian's
Prism. Blocks over 200 KB are shown without colours. The colours come from two Pygments
styles, one per theme, scoped to ``.code`` (light) and ``[data-theme=dark] .code``.
"""

from __future__ import annotations

import html
from functools import cache

from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

MAX_COLOURED = 200_000  # characters; bigger blocks are shown plain
LIGHT_STYLE = "default"
DARK_STYLE = "github-dark"
# Community plugins whose blocks are queries or scripts: shown as written, never run.
PLUGIN_BLOCKS = {"dataview", "dataviewjs", "tasks", "query", "button", "chart", "excalidraw", "kanban", "tracker",
                 "contracts", "meta-bind", "templater"}
_FORMATTER = HtmlFormatter(nowrap=True)
# Names people write that Pygments knows by another.
_ALIASES = {"sh": "bash", "shell": "bash", "zsh": "bash", "console": "console", "yml": "yaml", "py": "python",
            "js": "javascript", "ts": "typescript", "md": "markdown", "rs": "rust", "kt": "kotlin", "jsonc": "json",
            "dockerfile": "docker", "ps1": "powershell", "tex": "latex"}


@cache
def _lexer(lang: str):  # noqa: ANN202 - a Pygments lexer
    try:
        return get_lexer_by_name(_ALIASES.get(lang, lang), stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def highlight(source: str, lang: str) -> tuple[str, str]:
    """(HTML for inside <code>, the language label to show). Unknown languages and very
    big blocks come back escaped, without colours."""
    lang = (lang or "").strip().lower()
    lexer = _lexer(lang) if lang and len(source) <= MAX_COLOURED else None
    if lexer is None:
        return html.escape(source), lang
    return pygments_highlight(source, lexer, _FORMATTER), lexer.name


def _rules(style: str, scope: str) -> str:
    """The token colours of a Pygments style, without its background (ours is the card's)."""
    defs = HtmlFormatter(style=style).get_style_defs(scope)
    keep = []
    for line in defs.splitlines():
        line = line.strip()
        # Skip the block's own background, line numbers and the red boxes Pygments draws
        # around what it can't parse (half a snippet is normal in notes): token colours only.
        if not line.startswith(scope + " .") or any(k in line for k in (" .hll ", "linenos", " .err ")):
            continue
        keep.append(line.replace("background-color: ", "--bg: "))
    return "\n".join(keep)


@cache
def css() -> str:
    """The code colours for both themes."""
    return (_rules(LIGHT_STYLE, ".code") + "\n"
            + _rules(DARK_STYLE, ':root[data-theme="dark"] .code') + "\n")

"""Notes as web pages: Obsidian Flavored Markdown to safe HTML, on the server.

The dashboard's note viewer uses it (``/dash/wikis/note/...``). In order:

1. ``syntax``: markdown-it-py, CommonMark exactly, plus Obsidian's syntax as small
   plugins (links, embeds, callouts, highlights, comments, tags, block ids, tasks).
2. ``code``: Pygments colours code blocks.
3. ``note``: properties as a header, embedded notes and sections, size limits.
4. ``clean``: nh3 keeps only safe HTML.

Math and diagrams are left as marked-up text (``.math``, ``.diagram``) for the browser:
KaTeX and Pele (Obsidian's own diagram renderer since 1.15), served by the dashboard
itself and loaded only on pages that need them (``dashboard/static/note.js``).

Why these: markdown-it-py follows CommonMark exactly, takes plugins in Python and knows
each block's source lines; the Rust parsers are faster but offer no plugins from Python,
and a typical note renders in a few milliseconds anyway. Everything here is pure Python
or ships binary wheels, so nothing needs Node.js.
"""

from .code import css as code_css
from .note import MAX_EMBED_DEPTH, MAX_NOTE_BYTES, Context, Rendered, render_note

__all__ = ["MAX_EMBED_DEPTH", "MAX_NOTE_BYTES", "Context", "Rendered", "code_css", "render_note"]

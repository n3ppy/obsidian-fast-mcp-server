"""Obsidian Flavored Markdown on markdown-it-py: the parser and its plugins.

The base is CommonMark (markdown-it-py), with GitHub's tables and strikethrough,
footnotes (inline ``^[...]`` ones too), front matter, Pandoc-style math (``$5 and $10``
stays text), heading anchors and bare-URL links. On top of it, Obsidian's own syntax, as
small rules that run where markdown-it's own do, so ``%%`` or ``==`` inside code is left
alone:

- ``[[links]]``, with ``#Heading``, ``#^block`` and ``|alias``, resolved the way the
  vault's backlinks are (``Context.resolve_note``); missing notes are dimmed;
- ``![[embeds]]``: images (``|300`` or ``|300x200``), and notes, sections and blocks shown
  inline (see note.py);
- ``> [!type]± Title`` callouts, case-insensitive, with Obsidian's aliases, nesting and
  folding (``-`` closed, ``+`` open, as ``<details>``: no script needed);
- ``==highlights==``, ``%%comments%%`` (never shown; an unclosed block comment hides the
  rest of the note, as in Obsidian's reading view), ``#tags`` (a tag needs a character
  that isn't a digit), ``^block-ids``;
- tasks with any character in ``[ ]`` (kept as ``data-task``, so custom states can be
  styled);
- Living Memory's citations, ``^[folder/Note.md:4-5]``;
- ``mermaid`` blocks (drawn by Pele in the browser), plugin blocks such as ``dataview``
  (shown, never run), math blocks, and code coloured by Pygments (code.py);
- markdown links and images to files in the vault, which become the viewer's own links.

Every block that came from the note's own text carries ``data-line``, its first line in
the file (1-based), so a citation can point at lines. The HTML this produces is always
cleaned by clean.py before it leaves the server.
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.gfm_autolink import gfm_autolink_plugin

from . import code

IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "avif", "svg", "bmp"}
CALLOUT_RE = re.compile(r"^\[!([^\]\s]+)\]([+-]?)[ \t]*(.*)$")
# Obsidian's callout aliases, and the types it draws; anything else looks like a note.
CALLOUT_ALIASES = {
    "summary": "abstract", "tldr": "abstract", "hint": "tip", "important": "tip", "check": "success",
    "done": "success", "help": "question", "faq": "question", "caution": "warning", "attention": "warning",
    "fail": "failure", "missing": "failure", "error": "danger", "cite": "quote",
}
CALLOUT_TYPES = {"note", "abstract", "info", "todo", "tip", "success", "question", "warning", "failure", "danger",
                 "bug", "example", "quote"}
WIKI_RE = re.compile(r"(!?)\[\[([^\[\]\n|#]*)(#[^\[\]\n|]*)?(?:\|([^\[\]\n]*))?\]\]")
CITE_RE = re.compile(r"\^\[([^\]\n]+?\.md):(\d+)(?:-(\d+))?\]")
TAG_RE = re.compile(r"#([\w\-/]+)")
TAG_BEFORE = set(" \t\n([{,;")
BLOCK_ID_RE = re.compile(r"[ \t]\^([A-Za-z0-9-]+)$")
LONE_BLOCK_ID_RE = re.compile(r"^\^([A-Za-z0-9-]+)$")
TASK_RE = re.compile(r"^\[(.)\] ")
SIZE_RE = re.compile(r"^(\d{1,4})(?:x(\d{1,4}))?$")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def slug(text: str) -> str:
    """A heading's id: what ``[[Note#Heading]]`` points at."""
    return "h-" + re.sub(r"\s+", "-", re.sub(r"[^\w\- ]", "", text.strip().lower()))


def anchor(sub: str) -> str:
    """The fragment for ``#Heading`` or ``#^block`` (without the ``#``)."""
    sub = sub.lstrip("#")
    return sub if sub.startswith("^") else slug(sub)


def _ext(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


# -- inline rules ---------------------------------------------------------------------


def _wikilink(state: Any, silent: bool) -> bool:
    if not (state.src.startswith("[[", state.pos) or state.src.startswith("![[", state.pos)):
        return False
    m = WIKI_RE.match(state.src, state.pos)
    if not m or m.end() > state.posMax:
        return False
    if not silent:
        tok = state.push("wikilink", "", 0)
        alias = m.group(4)
        tok.meta = {
            "embed": m.group(1) == "!",
            # In a table, the pipe before an alias is written \| ; the backslash isn't the target's.
            "target": m.group(2).strip().rstrip("\\").strip(),
            "sub": (m.group(3) or "").strip(),
            "alias": alias.strip() if alias is not None else None,
        }
    state.pos = m.end()
    return True


def _citation(state: Any, silent: bool) -> bool:
    if not state.src.startswith("^[", state.pos):
        return False
    m = CITE_RE.match(state.src, state.pos)
    if not m or m.end() > state.posMax:
        return False
    if not silent:
        tok = state.push("citation", "", 0)
        start = int(m.group(2))
        tok.meta = {"path": m.group(1), "start": start, "end": max(start, int(m.group(3) or start))}
    state.pos = m.end()
    return True


def _mark(state: Any, silent: bool) -> bool:
    if not state.src.startswith("==", state.pos):
        return False
    start = state.pos + 2
    end = state.src.find("==", start, state.posMax)
    if end <= start or state.src[start].isspace() or state.src[end - 1].isspace():
        return False
    if not silent:
        old_max = state.posMax
        state.push("mark_open", "mark", 1)
        state.pos, state.posMax = start, end
        state.md.inline.tokenize(state)
        state.push("mark_close", "mark", -1)
        state.posMax = old_max
    state.pos = end + 2
    return True


def _comment_inline(state: Any, silent: bool) -> bool:
    if not state.src.startswith("%%", state.pos):
        return False
    close = state.src.find("%%", state.pos + 2, state.posMax)
    if close < 0:
        return False
    state.pos = close + 2  # nothing is emitted: comments never reach the page
    return True


def _tag(state: Any, silent: bool) -> bool:
    pos = state.pos
    if state.src[pos] != "#" or (pos > 0 and state.src[pos - 1] not in TAG_BEFORE):
        return False
    m = TAG_RE.match(state.src, pos)
    if not m or m.end() > state.posMax or not re.search(r"[^\d/\-]", m.group(1)):
        return False
    if not silent:
        tok = state.push("tag", "", 0)
        tok.content = m.group(1).rstrip("/")
    state.pos = m.end()
    return True


# -- block rules --------------------------------------------------------------------------


def _comment_block(state: Any, start: int, end: int, silent: bool) -> bool:
    pos = state.bMarks[start] + state.tShift[start]
    if state.sCount[start] - state.blkIndent >= 4 or not state.src.startswith("%%", pos):
        return False
    close = state.src.find("%%", pos + 2)
    if 0 <= close < state.eMarks[start] and state.src[close + 2:state.eMarks[start]].strip():
        return False  # "%% x %% more text" on one line: an inline comment
    if silent:
        return True
    line = start
    if close < 0:
        line = end  # unclosed: hides the rest of the note, as Obsidian does
    else:
        while line < end and state.eMarks[line] < close:
            line += 1
        line += 1
    state.line = line
    tok = state.push("obsidian_comment", "", 0)
    tok.map = [start, line]
    return True


# -- core rules (after block parsing) --------------------------------------------------------


def _callouts(state: Any) -> None:
    """Blockquotes that start with ``[!type]`` become callouts."""
    toks, stack, i = state.tokens, [], 0
    while i < len(toks):
        t = toks[i]
        if t.type == "blockquote_open":
            m = None
            if i + 2 < len(toks) and toks[i + 1].type == "paragraph_open" and toks[i + 2].type == "inline":
                first, _, rest = toks[i + 2].content.partition("\n")
                m = CALLOUT_RE.match(first)
            if not m:
                stack.append(None)
                i += 1
                continue
            kind, fold, title = m.group(1).lower(), m.group(2), m.group(3).strip()
            base = CALLOUT_ALIASES.get(kind, kind)
            tag = "details" if fold else "div"
            t.tag = tag
            t.attrs = {**t.attrs, "class": "callout", "data-callout": base if base in CALLOUT_TYPES else "note"}
            if fold == "+":
                t.attrs["open"] = ""
            head = "summary" if fold else "div"
            title_tok = Token("inline", "", 0, content=title or kind.capitalize(), children=[], map=toks[i + 2].map)
            # The title's text in one span: the title row is a flex box (icon, text, fold arrow),
            # and loose text and <strong> would otherwise each become a spaced item.
            new = [Token("callout_title_open", head, 1, attrs={"class": "callout-title"}),  # no newline before the span
                   Token("callout_text_open", "span", 1, attrs={"class": "callout-text"}), title_tok,
                   Token("callout_text_close", "span", -1),
                   Token("callout_title_close", head, -1, block=True),
                   Token("callout_body_open", "div", 1, attrs={"class": "callout-body"}, block=True)]
            if rest.strip():
                toks[i + 2].content = rest
                if toks[i + 2].map:
                    toks[i + 1].map = toks[i + 2].map = [toks[i + 2].map[0] + 1, toks[i + 2].map[1]]
            else:
                del toks[i + 1:i + 4]  # the paragraph held only the [!type] line
            toks[i + 1:i + 1] = new
            stack.append(tag)
            i += 1 + len(new)
            continue
        if t.type == "blockquote_close":
            tag = stack.pop() if stack else None
            if tag:
                t.tag = tag
                toks.insert(i, Token("callout_body_close", "div", -1, block=True))
                i += 1
        i += 1


def _lines(state: Any) -> None:
    """``data-line`` on every block from the note's own text (1-based, as in an editor)."""
    if not state.env.get("lines"):
        return
    for t in state.tokens:
        if t.map and t.block and (t.nesting == 1 or t.type in ("fence", "code_block", "hr", "html_block")):
            t.attrs["data-line"] = str(t.map[0] + 1)


# -- core rules (after inline parsing) --------------------------------------------------------


def _matching_open(toks: list[Token], close: int) -> int | None:
    depth = 0
    for j in range(close, -1, -1):
        depth += toks[j].nesting
        if depth == 0:
            return j
    return None


def _tidy(state: Any) -> None:
    """``^block-ids`` become ids, ``[c]`` list items become tasks, and an embed alone in its
    paragraph becomes a block (a note shown inline can't sit inside a <p>)."""
    toks = state.tokens
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.type != "inline" or not t.children:
            i += 1
            continue
        children = t.children
        # A paragraph that is only "^id" names the block before it.
        if (i > 0 and toks[i - 1].type == "paragraph_open" and len(children) == 1
                and children[0].type == "text" and LONE_BLOCK_ID_RE.match(children[0].content.strip())
                and i >= 2 and toks[i - 2].nesting == -1):
            opener = _matching_open(toks, i - 2)
            if opener is not None:
                toks[opener].attrs["id"] = "^" + children[0].content.strip()[1:]
                del toks[i - 1:i + 2]
                i -= 1
                continue
        last = children[-1]
        if last.type == "text":
            m = BLOCK_ID_RE.search(last.content)
            if m and i > 0 and toks[i - 1].nesting == 1:
                last.content = last.content[:m.start()]
                target = toks[i - 2] if i >= 2 and toks[i - 2].type == "list_item_open" and \
                    toks[i - 1].type == "paragraph_open" else toks[i - 1]
                target.attrs["id"] = "^" + m.group(1)
        first = children[0]
        if (first.type == "text" and i >= 2 and toks[i - 1].type == "paragraph_open"
                and toks[i - 2].type == "list_item_open"):
            m = TASK_RE.match(first.content)
            if m:
                status = m.group(1)
                first.content = first.content[m.end():]
                box = Token("html_inline", "", 0)
                box.content = f'<input type="checkbox" disabled{" checked" if status != " " else ""}> '
                children.insert(0, box)
                item = toks[i - 2]
                item.attrs["class"] = "task" + (" done" if status != " " else "")
                item.attrs["data-task"] = status
        # An embed of a note, alone in its paragraph: a block of its own.
        real = [c for c in children if not (c.type == "softbreak" or (c.type == "text" and not c.content.strip()))]
        if (len(real) == 1 and real[0].type == "wikilink" and real[0].meta["embed"]
                and _ext(real[0].meta["target"]) in ("", "md") and toks[i - 1].type == "paragraph_open"
                and i + 1 < len(toks) and toks[i + 1].type == "paragraph_close"):
            block = Token("note_embed", "", 0, block=True, map=toks[i - 1].map, meta=real[0].meta)
            toks[i - 1:i + 2] = [block]
            continue
        i += 1


def _local(href: str) -> tuple[str, str] | None:
    """A link or image source inside the vault: (path, fragment), or None for a URL."""
    if not href or href.startswith(("#", "/", "//")) or SCHEME_RE.match(href):
        return None
    parts = urlsplit(href)
    return unquote(parts.path), unquote(parts.fragment)


def _paths(state: Any) -> None:
    """Markdown links and images to the vault's own files become the viewer's links;
    remote images become links (the page never loads them: no tracking pixels)."""
    ctx, here = state.env["ctx"], state.env["path"]
    for t in state.tokens:
        if t.type != "inline" or not t.children:
            continue
        out: list[Token] = []
        for c in t.children:
            if c.type == "link_open":
                href = str(c.attrGet("href") or "")
                local = _local(href)
                if href.startswith("#") and len(href) > 1:
                    c.attrSet("href", "#" + anchor(unquote(href[1:])))
                elif local is not None:
                    path, frag = local
                    ext = _ext(path)
                    if ext in ("", "md"):
                        found = ctx.resolve_note(path, here)
                        c.attrSet("href", (ctx.note_url(found) + (f"#{anchor(frag)}" if frag else "")) if found
                                  else ctx.open_url(path))
                        c.attrSet("class", "internal" if found else "internal unresolved")
                    else:
                        found = ctx.resolve_file(path, here)
                        c.attrSet("href", ctx.file_url(found) if found else ctx.open_url(path))
                        c.attrSet("class", "internal file" if found else "internal unresolved")
                elif href.startswith(("http://", "https://", "mailto:")):
                    c.attrSet("class", "external")
                    c.attrSet("target", "_blank")
                out.append(c)
            elif c.type == "image":
                out.extend(_image(c, ctx, here))
            else:
                out.append(c)
        t.children = out


def _image(tok: Token, ctx: Any, here: str) -> list[Token]:
    src = str(tok.attrGet("src") or "")
    alt, _, size = tok.content.partition("|")
    m = SIZE_RE.match(size.strip()) if size else None
    if src.startswith(("http://", "https://")):
        host = urlsplit(src).netloc
        link = Token("link_open", "a", 1, attrs={"href": src, "class": "external remote-image", "target": "_blank"})
        text = Token("text", "", 0, content=f"Image: {alt.strip() or host}")
        return [link, text, Token("link_close", "a", -1)]
    local = _local(src)
    found = ctx.resolve_file(local[0], here) if local else None
    if found is None:
        miss = Token("html_inline", "", 0)
        miss.content = f'<span class="missing">{_esc(alt.strip() or src)}</span>'
        return [miss]
    tok.attrSet("src", ctx.file_url(found))
    tok.attrSet("loading", "lazy")
    if m:
        tok.attrSet("width", m.group(1))
        if m.group(2):
            tok.attrSet("height", m.group(2))
    tok.children = [Token("text", "", 0, content=alt.strip())]
    tok.content = alt.strip()
    return [tok]


# -- renderers ----------------------------------------------------------------------------


def _render_wikilink(self: Any, tokens: list[Token], idx: int, options: Any, env: dict) -> str:
    m = tokens[idx].meta
    target, sub, alias, embed = m["target"], m["sub"], m["alias"], m["embed"]
    ctx, here = env["ctx"], env["path"]
    ext = _ext(target)
    if embed and ext in IMAGE_EXT:
        found = ctx.resolve_file(target, here)
        if found is None:
            return f'<span class="missing">{_esc(target)}</span>'
        size = ""
        sm = SIZE_RE.match(alias or "")
        if sm:
            size = f' width="{sm.group(1)}"' + (f' height="{sm.group(2)}"' if sm.group(2) else "")
        return f'<img src="{_esc(ctx.file_url(found))}" alt="{_esc(target)}"{size} loading="lazy">'
    if ext not in ("", "md"):  # a PDF, a sound, any other file: a link to it
        found = ctx.resolve_file(target, here)
        label = alias if alias and not SIZE_RE.match(alias) else target.rsplit("/", 1)[-1]
        if found is None:
            return f'<a class="internal unresolved" href="{_esc(ctx.open_url(target))}">{_esc(label)}</a>'
        return f'<a class="internal file" href="{_esc(ctx.file_url(found))}">{_esc(label)}</a>'
    found = ctx.resolve_note(target, here) if target else here
    if alias:
        label = alias
    elif target:
        label = target + sub.replace("#^", " > ^").replace("#", " > ")
    else:
        label = sub.lstrip("#^") if sub.startswith("#^") else sub.lstrip("#")
    frag = f"#{anchor(sub)}" if sub else ""
    if not target:
        href, cls = frag or "#", "internal"
    elif found:
        href, cls = ctx.note_url(found) + frag, "internal"
    else:
        href, cls = ctx.open_url(target), "internal unresolved"
    if embed:
        cls += " embed-link"
    return f'<a class="{cls}" href="{_esc(href)}">{_esc(label)}</a>'


def _render_note_embed(self: Any, tokens: list[Token], idx: int, options: Any, env: dict) -> str:
    return env["embed"](tokens[idx].meta, env)


def _render_citation(self: Any, tokens: list[Token], idx: int, options: Any, env: dict) -> str:
    m = tokens[idx].meta
    ctx = env["ctx"]
    name = m["path"].rsplit("/", 1)[-1][:-3]
    lines = f"{m['start']}" if m["start"] == m["end"] else f"{m['start']}-{m['end']}"
    found = ctx.resolve_note(m["path"], env["path"])
    href = (ctx.note_url(found) if found else ctx.open_url(m["path"])) + f"#L{m['start']}-L{m['end']}"
    return (f'<sup class="cite"><a href="{_esc(href)}" title="{_esc(m["path"])}, lines {lines}">'
            f'{_esc(name)}:{lines}</a></sup>')


def _render_tag(self: Any, tokens: list[Token], idx: int, options: Any, env: dict) -> str:
    tag = tokens[idx].content
    return f'<a class="tag" href="{_esc(env["ctx"].tag_url(tag))}">#{_esc(tag)}</a>'


def _render_fence(self: Any, tokens: list[Token], idx: int, options: Any, env: dict) -> str:
    tok = tokens[idx]
    lang = tok.info.strip().split(maxsplit=1)[0].lower() if tok.info.strip() else ""
    line = tok.attrGet("data-line")
    data_line = f' data-line="{line}"' if line else ""
    flags = env["flags"]
    if lang == "mermaid":
        flags["diagrams"] = True
        return (f'<div class="diagram" data-diagram="mermaid"{data_line}>'
                f'<pre class="diagram-source"><code>{_esc(tok.content)}</code></pre></div>\n')
    if lang == "math":
        flags["math"] = True
        return f'<div class="math block"{data_line}>{_esc(tok.content)}</div>\n'
    if lang in code.PLUGIN_BLOCKS:
        return (f'<div class="plugin-block" data-plugin="{_esc(lang)}"{data_line}>'
                f'<div class="plugin-label"><span>{_esc(lang)}</span> runs in Obsidian</div>'
                f'<pre><code>{_esc(tok.content)}</code></pre></div>\n')
    body, shown = code.highlight(tok.content, lang)
    label = f'<span class="code-lang">{_esc(shown)}</span>' if shown else ""
    return (f'<div class="code-block"{data_line}>{label}<pre class="code" data-lang="{_esc(lang)}"><code>{body}'
            f'</code></pre></div>\n')


def _render_code_block(self: Any, tokens: list[Token], idx: int, options: Any, env: dict) -> str:
    tok = tokens[idx]
    line = tok.attrGet("data-line")
    data_line = f' data-line="{line}"' if line else ""
    return f'<div class="code-block"{data_line}><pre class="code"><code>{_esc(tok.content)}</code></pre></div>\n'


def _flag_math(md: MarkdownIt) -> None:
    """Note that the page has math, so the browser loads KaTeX only when it's needed."""
    for name in ("math_inline", "math_inline_double", "math_block", "math_block_label"):
        original = md.renderer.rules.get(name)
        if original is None:
            continue

        def rule(self: Any, tokens: list[Token], idx: int, options: Any, env: dict, _original: Any = original) -> str:
            env["flags"]["math"] = True
            return _original(tokens, idx, options, env)  # already bound to the renderer

        md.add_render_rule(name, rule)


def build(breaks: bool = True) -> MarkdownIt:
    """The parser. `breaks`: a single newline is a line break (Obsidian's default; off when
    the vault turns on Strict line breaks)."""
    md = MarkdownIt("commonmark", {"breaks": breaks, "html": True, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.use(front_matter_plugin).use(footnote_plugin, inline=True)
    md.use(dollarmath_plugin, allow_space=False, allow_digits=False, double_inline=True)
    md.use(anchors_plugin, min_level=1, max_level=6, slug_func=slug, permalink=False)
    md.use(gfm_autolink_plugin)
    md.block.ruler.before("paragraph", "obsidian_comment", _comment_block,
                          {"alt": ["paragraph", "reference", "blockquote", "list"]})
    md.inline.ruler.before("link", "wikilink", _wikilink)
    md.inline.ruler.before("footnote_inline", "citation", _citation)
    md.inline.ruler.before("emphasis", "mark", _mark)
    md.inline.ruler.before("emphasis", "comment", _comment_inline)
    md.inline.ruler.before("emphasis", "tag", _tag)
    md.core.ruler.after("block", "callouts", _callouts)
    md.core.ruler.after("callouts", "lines", _lines)
    md.core.ruler.after("inline", "tidy", _tidy)
    md.core.ruler.after("tidy", "paths", _paths)
    md.add_render_rule("wikilink", _render_wikilink)
    md.add_render_rule("note_embed", _render_note_embed)
    md.add_render_rule("citation", _render_citation)
    md.add_render_rule("tag", _render_tag)
    md.add_render_rule("fence", _render_fence)
    md.add_render_rule("code_block", _render_code_block)
    md.add_render_rule("obsidian_comment", lambda *a: "")
    _flag_math(md)
    return md

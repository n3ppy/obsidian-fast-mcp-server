"""A whole note, ready to read: its properties, its body, and the notes it embeds.

- **Properties** (front matter) become a small header, not YAML: tags as tags, links as
  links, lists as lists.
- **Embeds** of notes (``![[Note]]``), sections (``![[Note#Heading]]``) and blocks
  (``![[Note#^id]]``) are shown inline, at most three deep, and never in a loop.
- **Limits:** a note over 1 MB is shown as plain text, without markdown.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token

from . import syntax
from .clean import clean

MAX_NOTE_BYTES = 1_000_000
MAX_EMBED_DEPTH = 3
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?\n)?---[ \t]*(?:\n|\Z)", re.DOTALL)
_HIDDEN_PROPERTIES = {"cssclass", "cssclasses"}


@dataclass(frozen=True)
class Context:
    """How the renderer reaches the rest of the vault. The caller decides every URL, so the
    renderer works the same in the dashboard, under any path."""

    resolve_note: Callable[[str, str | None], str | None]  # (link target, from note) -> note path
    resolve_file: Callable[[str, str | None], str | None]  # (file name or path, from note) -> file path
    read: Callable[[str], str | None]  # note path -> its text, for embeds
    note_url: Callable[[str], str]  # note path -> its page (no fragment)
    file_url: Callable[[str], str]  # file path -> the file
    open_url: Callable[[str], str]  # a link to a note that doesn't exist (yet)
    tag_url: Callable[[str], str]  # tag (no #) -> the notes that have it
    own_prefix: str  # every URL above starts with this


@dataclass
class Rendered:
    html: str  # the body, sanitised
    properties: str = ""  # the properties header, sanitised ("" when there are none)
    title: str = ""  # the note's name, as Obsidian shows it
    title_dropped: bool = False  # the body started with "# <title>", left out (the page shows the title)
    tags: list[str] = field(default_factory=list)  # from the properties
    math: bool = False  # the page needs KaTeX
    diagrams: bool = False  # the page needs Pele
    plain: bool = False  # too big for markdown: shown as text


@cache
def _parser(breaks: bool) -> MarkdownIt:
    return syntax.build(breaks)


def _title(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


def render_note(text: str, path: str, ctx: Context, *, strict_breaks: bool = False) -> Rendered:
    """Render the note at `path` (vault-relative) whose content is `text`."""
    title = _title(path)
    if len(text.encode("utf-8", "replace")) > MAX_NOTE_BYTES:
        body = f'<pre class="plain">{html.escape(text)}</pre>'
        return Rendered(html=body, title=title, plain=True)
    md = _parser(not strict_breaks)
    flags: dict[str, bool] = {}
    env = _env(ctx, path, flags, depth=0, chain=frozenset({(path, "")}), md=md, lines=True)
    tokens = md.parse(text, env)
    start = 1 if tokens and tokens[0].type == "front_matter" else 0
    title_dropped = (len(tokens) >= start + 3 and tokens[start].type == "heading_open" and tokens[start].tag == "h1"
                     and tokens[start + 1].content.strip().lower() == title.lower())
    if title_dropped:  # the page shows the note's name as its title; "# <name>" would say it twice
        del tokens[start:start + 3]
    body = md.renderer.render(tokens, md.options, env)
    props, tags = _properties(text, ctx, path, md)
    return Rendered(
        html=_clean(body, ctx, path), properties=_clean(props, ctx, path) if props else "", title=title,
        title_dropped=title_dropped, tags=tags, math=flags.get("math", False),
        diagrams=flags.get("diagrams", False),
    )


def _clean(markup: str, ctx: Context, path: str) -> str:
    def file_link(name: str) -> str | None:
        found = ctx.resolve_file(name, path)
        return ctx.file_url(found) if found else None

    def note_link(name: str) -> str | None:
        found = ctx.resolve_note(name, path)
        return ctx.note_url(found) if found else None

    return clean(markup, own_prefix=ctx.own_prefix, file_link=file_link, note_link=note_link)


def _env(ctx: Context, path: str, flags: dict[str, bool], *, depth: int, chain: frozenset[tuple[str, str]], md: MarkdownIt,
         lines: bool, doc: str | None = None) -> dict[str, Any]:
    env: dict[str, Any] = {"ctx": ctx, "path": path, "flags": flags, "depth": depth, "chain": chain, "lines": lines}
    env["embed"] = lambda meta, outer: _embed(meta, outer, md)
    if doc:
        env["docId"] = doc  # footnote ids of an embedded note don't clash with the page's
    return env


# -- embeds -------------------------------------------------------------------------------


def _embed(meta: dict[str, Any], outer: dict[str, Any], md: MarkdownIt) -> str:
    ctx: Context = outer["ctx"]
    target, sub = meta["target"], meta["sub"]
    found = ctx.resolve_note(target, outer["path"]) if target else outer["path"]
    label = (target or _title(outer["path"])) + sub.replace("#^", " > ^").replace("#", " > ")
    if found is None:
        return (f'<div class="embed missing"><a class="internal unresolved" href="{_esc(ctx.open_url(target))}">'
                f'{_esc(label)}</a></div>\n')
    head = f'<div class="embed-head"><a class="internal" href="{_esc(ctx.note_url(found) + _frag(sub))}">' \
           f'{_esc(label)}</a></div>'
    # A note (or section) already being shown, further up: showing it again would never end.
    if (found, sub) in outer["chain"] or (found, "") in outer["chain"]:
        return f'<div class="embed stopped">{head}<p class="embed-note">Not shown here: it embeds itself.</p></div>\n'
    if outer["depth"] + 1 > MAX_EMBED_DEPTH:
        return f'<div class="embed stopped">{head}<p class="embed-note">Not shown: embeds go three deep.</p></div>\n'
    text = ctx.read(found)
    if text is None or len(text.encode("utf-8", "replace")) > MAX_NOTE_BYTES:
        return f'<div class="embed stopped">{head}<p class="embed-note">This note can\'t be shown inline.</p></div>\n'
    outer["embeds"] = outer.get("embeds", 0) + 1
    env = _env(ctx, found, outer["flags"], depth=outer["depth"] + 1, chain=outer["chain"] | {(found, sub)}, md=md,
               lines=False, doc=f"e{outer['depth'] + 1}-{outer['embeds']}")
    tokens = md.parse(text, env)
    if sub:
        tokens = _section(tokens, sub)
        if tokens is None:
            return (f'<div class="embed missing">{head}<p class="embed-note">'
                    f'There\'s no {"block" if sub.startswith("#^") else "heading"} '
                    f'{_esc(sub.lstrip("#"))} in it.</p></div>\n')
    elif tokens and tokens[0].type == "front_matter":
        tokens = tokens[1:]
    if not sub and len(tokens) > 2 and tokens[0].type == "heading_open" and tokens[0].tag == "h1" and \
            tokens[1].content.strip().lower() == _title(found).lower():
        tokens = tokens[3:]  # its "# Title" would repeat the embed's own header
    for t in tokens:  # the page's own ids stay unique: an embedded heading isn't a target
        if t.attrs.get("id"):
            del t.attrs["id"]
    body = md.renderer.render(tokens, md.options, env)
    return f'<div class="embed">{head}<div class="embed-body">{body}</div></div>\n'


def _section(tokens: list[Token], sub: str) -> list[Token] | None:
    """The tokens of one section (``#Heading``: up to the next heading as high or higher)
    or one block (``#^id``)."""
    want = syntax.anchor(sub)
    if want.startswith("^"):
        for i, t in enumerate(tokens):
            if t.attrs.get("id") == want and t.nesting == 1:
                depth, j = 0, i
                while j < len(tokens):
                    depth += tokens[j].nesting
                    if depth == 0:
                        break
                    j += 1
                block = tokens[i:j + 1]
                if t.type == "list_item_open":  # a list item needs its list around it
                    parent = next((tokens[k] for k in range(i - 1, -1, -1)
                                   if tokens[k].type in ("bullet_list_open", "ordered_list_open")
                                   and tokens[k].level == t.level - 1), None)
                    kind = parent.type[:-5] if parent is not None else "bullet_list"
                    tag = "ol" if kind == "ordered_list" else "ul"
                    block = [Token(f"{kind}_open", tag, 1, block=True), *block, Token(f"{kind}_close", tag, -1, block=True)]
                return block
        return None
    for i, t in enumerate(tokens):
        if t.type == "heading_open" and t.attrs.get("id") == want:
            level = int(t.tag[1])
            end = len(tokens)
            for j in range(i + 3, len(tokens)):
                if tokens[j].type == "heading_open" and int(tokens[j].tag[1]) <= level:
                    end = j
                    break
            return tokens[i:end]
    return None


def _frag(sub: str) -> str:
    return f"#{syntax.anchor(sub)}" if sub else ""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


# -- properties ------------------------------------------------------------------------------


def _properties(text: str, ctx: Context, path: str, md: MarkdownIt) -> tuple[str, list[str]]:
    """The front matter as a header: (HTML, the note's tags)."""
    m = _FRONT_MATTER.match(text)
    if not m or not (m.group(1) or "").strip():
        return "", []
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return "", []
    if not isinstance(data, dict):
        return "", []
    env = _env(ctx, path, {}, depth=0, chain=frozenset({(path, "")}), md=md, lines=False)
    rows, tags = [], []
    for key, value in data.items():
        name = str(key)
        if name.lower() in _HIDDEN_PROPERTIES or value is None or value == "" or value == []:
            continue
        values = value if isinstance(value, list) else [value]
        if name.lower() in ("tags", "tag"):
            names = []
            for v in values:
                names.extend(t for t in re.split(r"[,\s]+", str(v)) if t)
            names = [t.lstrip("#") for t in names if t.lstrip("#")]
            tags.extend(names)
            cells = [f'<a class="tag" href="{_esc(ctx.tag_url(t))}">#{_esc(t)}</a>' for t in names]
        else:
            cells = [_property_value(v, md, env) for v in values]
        rows.append(f'<div class="prop"><dt>{_esc(name)}</dt><dd>{"".join(f"<span>{c}</span>" for c in cells)}'
                    f'</dd></div>')
    if not rows:
        return "", tags
    return f'<dl class="props">{"".join(rows)}</dl>', tags


def _property_value(value: Any, md: MarkdownIt, env: dict[str, Any]) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dt.datetime):
        return _esc(value.strftime("%d %b %Y, %H:%M"))
    if isinstance(value, dt.date):
        return _esc(value.strftime("%d %b %Y"))
    if isinstance(value, (dict, list)):
        return _esc(yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip())
    text = str(value)
    if "[[" in text or "](" in text:  # links in properties are links, as in Obsidian
        return md.renderInline(text, env)
    return _esc(text)

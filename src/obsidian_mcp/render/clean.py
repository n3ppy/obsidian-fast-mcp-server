"""The last step before a note leaves the server: nh3 (the Rust sanitiser ammonia) keeps
only the tags, attributes and URL schemes listed here.

Notes can hold HTML, as in Obsidian, and a note can come from anywhere (a shared vault, an
imported repository, a page Living Memory wrote), so nothing in one may run a script on
the site. On top of nh3's own rules:

- ``style`` survives only on table cells, and only ``text-align`` (markdown's column
  alignment);
- ids survive only with our own prefixes (``h-`` headings, ``^`` blocks, ``fn``
  footnotes), so a note can't name an element ``katex`` and shadow a script's global;
- in the note's own HTML, an image from another site loses its source (the page never
  loads remote images: no tracking pixels), and a path inside the vault becomes the
  viewer's link to that file or note.

The page's Content-Security-Policy allows scripts only from the site itself, so even a
miss here couldn't run one.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

import nh3

TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "dd", "del", "details", "div", "dl", "dt", "em", "figcaption",
    "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "input", "ins", "kbd", "li", "mark", "ol", "p",
    "pre", "q", "s", "samp", "section", "small", "span", "strong", "sub", "summary", "sup", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "u", "ul", "var",
}
ATTRS = {
    "*": {"class", "id", "title", "lang", "dir"},
    "a": {"href", "target"},
    "img": {"src", "alt", "width", "height", "loading"},
    "input": {"checked", "disabled"},  # and type, only "checkbox" (below)
    "details": {"open"},
    "td": {"style", "colspan", "rowspan"},
    "th": {"style", "colspan", "rowspan"},
    "ol": {"start"},
}
ID_OK = re.compile(r"^(h-|\^|fn)[\w\-:^.]*$")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_NO_SOURCE = re.compile(r"<img(?![^>]*\ssrc=)[^>]*>")

# (a vault path, the note it's in) -> the viewer's URL for it, or None if there's no such file.
Mapper = Callable[[str], str | None]


def clean(markup: str, *, own_prefix: str, file_link: Mapper, note_link: Mapper) -> str:
    """Sanitise rendered HTML. `own_prefix` starts every URL the renderer made itself (the
    dashboard's path); other relative sources are the note's own HTML, and go through
    `file_link` (images) and `note_link` (links)."""

    def attribute(tag: str, attr: str, value: str) -> str | None:
        if attr == "id":
            return value if ID_OK.match(value) else None
        if attr == "target":
            return "_blank" if value == "_blank" else None
        if tag == "img" and attr == "src":
            if value.startswith(own_prefix):
                return value
            if value.startswith(("http:", "https:", "//")) or _SCHEME.match(value) or value.startswith("/"):
                return None  # another site's image, or a data: URI: never loaded
            return file_link(unquote(urlsplit(value).path))
        if tag == "a" and attr == "href":
            if value.startswith(own_prefix) or value.startswith("#") or _SCHEME.match(value) or \
                    value.startswith(("/", "//")):
                return value  # schemes are checked by nh3's url_schemes below
            return note_link(unquote(urlsplit(value).path))
        return value

    cleaned = nh3.clean(
        markup,
        tags=TAGS,
        attributes=ATTRS,
        generic_attribute_prefixes={"data-"},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
        attribute_filter=attribute,
        filter_style_properties={"text-align"},
        tag_attribute_values={"input": {"type": {"checkbox"}}},
        set_tag_attribute_values={"input": {"disabled": ""}},
        strip_comments=True,
    )
    return _NO_SOURCE.sub("", cleaned)  # an image whose source was refused would show as broken

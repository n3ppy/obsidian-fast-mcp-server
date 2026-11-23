"""The note renderer (render/): Obsidian Flavored Markdown to safe HTML.

Most tests render a small note and look for exact pieces of HTML. ``tests/render/fixture.md``
uses every feature at once and is compared with ``tests/render/fixture.html``: after a
deliberate change, rebuild that file with ``RENDER_GOLDEN=1 pytest tests/test_render.py``
and read the diff."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import pytest

pytest.importorskip("markdown_it", reason="the dashboard extra is not installed")

from obsidian_mcp.render import MAX_NOTE_BYTES, Context, render_note  # noqa: E402
from obsidian_mcp.render.code import MAX_COLOURED  # noqa: E402

HERE = Path(__file__).parent / "render"
NOTES = {
    "Authors.md": "# Authors\n\nIntro.\n\n## Living\n\nStill writing. [^a]\n\n[^a]: An embedded footnote.\n\n"
                  "## Dead\n\nNot any more.\n\n- Ursula ^favourite\n- Jane\n",
    "projects/library/Notes.md": "one\ntwo\n",
    "Reading list.md": "",
}
FILES = {"cover.png": "images/cover.png", "sketch.png": "attachments/sketch.png", "doc.pdf": "files/doc.pdf"}


def resolve_file(name: str, from_note: str | None = None) -> str | None:
    return FILES.get(name.rsplit("/", 1)[-1].lower())


def context(notes: dict[str, str] | None = None) -> Context:
    store = NOTES if notes is None else notes

    def resolve(target: str, from_note: str | None = None) -> str | None:
        t = target[:-3] if target.lower().endswith(".md") else target
        return next((p for p in store if p[:-3].lower() == t.lower()
                     or p[:-3].rsplit("/", 1)[-1].lower() == t.lower()), None)

    return Context(
        resolve_note=resolve, resolve_file=resolve_file, read=lambda p: store.get(p),
        note_url=lambda p: "/dash/wikis/note/v?path=" + quote(p),
        file_url=lambda p: "/dash/wikis/file/v?path=" + quote(p),
        open_url=lambda n: "/dash/wikis/open/v?name=" + quote(n),
        tag_url=lambda t: "/dash/wikis/tag/v?tag=" + quote(t), own_prefix="/dash/",
    )


def render(text: str, path: str = "Note.md", notes: dict[str, str] | None = None, **kw):  # noqa: ANN201
    return render_note(text, path, context(notes), **kw)


def html_of(text: str, **kw) -> str:
    return render(text, **kw).html


# -- the whole fixture -------------------------------------------------------------------


def test_the_fixture_renders_as_recorded() -> None:
    note = render((HERE / "fixture.md").read_text(encoding="utf-8"), "Reading list.md")
    page = f"<!-- properties -->\n{note.properties}\n<!-- body -->\n{note.html}"
    golden = HERE / "fixture.html"
    if os.environ.get("RENDER_GOLDEN"):
        golden.write_text(page, encoding="utf-8")
    assert page == golden.read_text(encoding="utf-8")
    assert note.title == "Reading list" and note.title_dropped and note.math and note.diagrams
    assert note.tags == ["reading", "lists/books"]


def test_the_fixture_is_fast() -> None:
    text = (HERE / "fixture.md").read_text(encoding="utf-8")
    render(text, "Reading list.md")  # warm up: the parser and Pygments' lexers load once
    start = time.perf_counter()
    for _ in range(10):
        render(text, "Reading list.md")
    assert (time.perf_counter() - start) / 10 < 0.1  # about 5 ms on one core; this only catches a blow-up


# -- links ------------------------------------------------------------------------------------


def test_links_resolve_like_the_vault_and_missing_ones_are_dimmed() -> None:
    out = html_of("[[Authors]] [[Authors|the authors]] [[Authors#Living]] [[Authors#^favourite]] [[#Top]] [[Nope]]")
    assert '<a class="internal" href="/dash/wikis/note/v?path=Authors.md" rel="noopener noreferrer">Authors</a>' in out
    assert ">the authors</a>" in out
    assert 'href="/dash/wikis/note/v?path=Authors.md#h-living"' in out and ">Authors &gt; Living</a>" in out
    assert 'href="/dash/wikis/note/v?path=Authors.md#^favourite"' in out and ">Authors &gt; ^favourite</a>" in out
    assert '<a class="internal" href="#h-top" rel="noopener noreferrer">Top</a>' in out
    assert '<a class="internal unresolved" href="/dash/wikis/open/v?name=Nope" rel="noopener noreferrer">Nope</a>' in out


def test_link_paths_are_encoded() -> None:
    out = html_of("[[Reading list]] [x](<Reading list.md>) [y](Reading%20list.md#Some%20Heading)")
    assert out.count('href="/dash/wikis/note/v?path=Reading%20list.md"') == 2
    assert 'href="/dash/wikis/note/v?path=Reading%20list.md#h-some-heading"' in out


def test_markdown_links_to_notes_files_anchors_and_sites() -> None:
    out = html_of("[a](Authors.md) [b](#My Heading) [c](files/doc.pdf) [d](https://example.com) [e](mailto:a@b.c)"
                  " [f](Gone.md)")
    assert '<a href="/dash/wikis/note/v?path=Authors.md" class="internal"' in out
    assert '<a href="#h-my-heading"' not in out  # "[b](#My Heading)" isn't a link in CommonMark (a space)
    assert '<a href="/dash/wikis/file/v?path=files/doc.pdf" class="internal file"' in out
    assert '<a href="https://example.com" class="external" target="_blank" rel="noopener noreferrer">d</a>' in out
    assert '<a href="mailto:a@b.c" class="external" target="_blank"' in out
    assert '<a href="/dash/wikis/open/v?name=Gone.md" class="internal unresolved"' in out
    assert '<a href="#h-my-heading"' in html_of("[b](#My%20Heading)")


def test_a_table_cell_link_with_an_escaped_pipe() -> None:
    out = html_of("| A |\n| - |\n| [[Authors\\|the authors]] |\n")
    assert 'href="/dash/wikis/note/v?path=Authors.md"' in out and ">the authors</a>" in out


# -- embeds -----------------------------------------------------------------------------------


def test_images_take_obsidian_sizes() -> None:
    out = html_of("![[cover.png|300]]\n\n![[cover.png|300x200]]\n\n![[missing.png]]\n\n![A sketch|200](sketch.png)")
    assert '<img src="/dash/wikis/file/v?path=images/cover.png" alt="cover.png" width="300" loading="lazy">' in out
    assert 'width="300" height="200"' in out
    assert '<span class="missing">missing.png</span>' in out
    assert '<img src="/dash/wikis/file/v?path=attachments/sketch.png" alt="A sketch" loading="lazy" width="200">' in out


def test_remote_images_become_links() -> None:
    out = html_of("![Tracker](https://tracker.example.com/p.png) ![](https://cdn.example.com/x.png)")
    assert "<img" not in out
    assert ('<a href="https://tracker.example.com/p.png" class="external remote-image" target="_blank" '
            'rel="noopener noreferrer">Image: Tracker</a>') in out
    assert ">Image: cdn.example.com</a>" in out


def test_a_note_section_and_block_embed() -> None:
    out = html_of("![[Authors]]\n\n![[Authors#Living]]\n\n![[Authors#^favourite]]")
    whole, section, block = out.split('<div class="embed">')[1:]
    assert '<div class="embed-head"><a class="internal" href="/dash/wikis/note/v?path=Authors.md"' in whole
    assert "<h1>" not in whole and "Intro." in whole and "Not any more." in whole  # "# Authors" isn't repeated
    assert "<h2>Living</h2>" in section and "Still writing." in section and "Not any more." not in section
    assert ">Authors &gt; ^favourite</a>" in block and "Ursula" in block and "Jane" not in block
    assert "<ul>\n<li>Ursula</li>\n</ul>" in block  # a list item, inside its list
    embedded = re.sub(r' id="fn(ref)?-e1-[\w-]+"', "", out.split('<div class="embed">', 1)[1])
    assert ' id="' not in embedded  # embedded headings and blocks aren't targets on this page
    assert 'id="fn-e1-' in out  # an embedded footnote keeps its own ids, apart from the page's


def test_missing_embeds_say_so() -> None:
    out = html_of("![[Nope]]\n\n![[Authors#Nowhere]]\n\n![[Authors#^nope]]")
    assert '<div class="embed missing"><a class="internal unresolved" href="/dash/wikis/open/v?name=Nope"' in out
    assert "There's no heading Nowhere in it." in out and "There's no block ^nope in it." in out


def test_embeds_never_loop_and_stop_three_deep() -> None:
    loop = {"A.md": "A embeds B:\n\n![[B]]", "B.md": "B embeds A:\n\n![[A]]"}
    out = render(loop["A.md"], "A.md", notes=loop).html
    assert out.count("B embeds A") == 1 and "Not shown here: it embeds itself." in out
    chain = {f"N{i}.md": f"Level {i}\n\n![[N{i + 1}]]" for i in range(6)}
    out = render(chain["N0.md"], "N0.md", notes=chain).html
    assert "Level 3" in out and "Level 4" not in out and "Not shown: embeds go three deep." in out


def test_an_embed_inside_a_sentence_is_a_link() -> None:
    out = html_of("See ![[Authors]] here.")
    assert '<a class="internal embed-link" href="/dash/wikis/note/v?path=Authors.md"' in out
    assert '<div class="embed">' not in out


def test_other_files_link_to_the_file() -> None:
    out = html_of("![[doc.pdf]] [[doc.pdf|the PDF]] ![[nope.mp3]]")
    assert '<a class="internal file" href="/dash/wikis/file/v?path=files/doc.pdf" rel="noopener noreferrer">doc.pdf</a>' in out
    assert ">the PDF</a>" in out
    assert '<a class="internal unresolved" href="/dash/wikis/open/v?name=nope.mp3"' in out


# -- Obsidian's syntax --------------------------------------------------------------------------


def test_callouts() -> None:
    out = html_of("> [!NOTE]\n> plain\n\n> [!faq]- Folded\n> body\n\n> [!tip]+ Open\n> body\n\n"
                  "> [!whatever] Custom\n> body\n\n> [!quote]\n\n> [!warning] Outer\n> > [!bug] Inner\n> > text")
    assert '<div class="callout" data-callout="note"' in out
    assert '<div class="callout-title"><span class="callout-text">Note</span></div>' in out
    assert '<details class="callout" data-callout="question" data-line' in out
    assert '<summary class="callout-title"><span class="callout-text">Folded</span></summary>' in out
    assert '<details class="callout" data-callout="tip" open=""' in out
    assert 'data-callout="note" data-line="10"' in out and ">Custom</span></div>" in out  # unknown types look like a note
    assert '<span class="callout-text">Quote</span>' in out  # a callout with a title only
    assert re.search(r'data-callout="warning".*data-callout="bug"', out, re.S)


def test_comments_are_never_shown() -> None:
    out = html_of("before %%inline%% after\n\n%%\nblock\n%%\n\n`code %%kept%%`\n\nlast")
    assert "inline" not in out and "block" not in out and "%%kept%%" in out and "last" in out
    out = html_of("shown\n\n%%\nunclosed hides\n\nthe rest")
    assert "shown" in out and "unclosed" not in out and "the rest" not in out


def test_tags_follow_obsidian() -> None:
    out = html_of("#tag (#paren) #nested/tag C# #123 #1a x#no `#code` https://x.com/#frag")
    assert '<a class="tag" href="/dash/wikis/tag/v?tag=tag" rel="noopener noreferrer">#tag</a>' in out
    assert 'tag=paren"' in out and 'tag=nested/tag"' in out and 'tag=1a"' in out
    for not_a_tag in ("tag=123", "tag=no", "tag=code", "tag=frag"):
        assert not_a_tag not in out
    assert "<h1" in html_of("# Heading, not a tag")


def test_tasks_keep_their_state() -> None:
    out = html_of("- [ ] open\n- [x] done\n- [/] doing\n- [-] dropped\n- [?] asked")
    assert '<li data-line="1" class="task" data-task=" "><input type="checkbox" disabled=""> open</li>' in out
    assert 'class="task done" data-task="x"><input type="checkbox" disabled="" checked="">' in out
    for state in ("/", "-", "?"):
        assert f'data-task="{state}"' in out


def test_highlights_strike_and_block_ids() -> None:
    out = html_of("==marked== ~~gone~~\n\npara ^p1\n\n- item ^li\n\n| a |\n| - |\n| 1 |\n\n^tbl")
    assert "<mark>marked</mark>" in out and "<s>gone</s>" in out
    assert '<p data-line="3" id="^p1">para</p>' in out
    assert '<li data-line="5" id="^li">item</li>' in out
    assert '<table data-line="7" id="^tbl">' in out and "^tbl" not in out.replace('id="^tbl"', "")


def test_line_breaks_follow_the_vault() -> None:
    assert "one<br>\ntwo" in html_of("one\ntwo")
    assert "<br>" not in html_of("one\ntwo", strict_breaks=True)


def test_blocks_know_their_lines() -> None:
    out = html_of("---\ntitle: x\n---\n\n# Head\n\ntext\n\n```py\ncode\n```\n")
    assert '<h1 data-line="5" id="h-head">Head</h1>' in out
    assert '<p data-line="7">text</p>' in out and '<div class="code-block" data-line="9">' in out


def test_the_title_isnt_said_twice() -> None:
    note = render("# Note\n\nbody")
    assert note.title_dropped and "<h1" not in note.html and "body" in note.html
    note = render("# Something else\n\nbody")
    assert not note.title_dropped and "<h1" in note.html


# -- math, diagrams, code -------------------------------------------------------------------------


def test_math_is_left_for_the_browser_and_money_stays_text() -> None:
    note = render("Euler $e^{i\\pi}=-1$, but $5 and $10 aren't math.\n\n$$\nx^2\n$$")
    assert '<span class="math inline">e^{i\\pi}=-1</span>' in note.html
    assert "$5 and $10" in note.html and note.math
    assert '<div class="math block">\nx^2\n</div>' in note.html
    assert not render("It costs $5 and $10.").math
    assert render("```math\nx\n```").math


def test_diagrams_and_plugin_blocks() -> None:
    note = render("```mermaid\nflowchart LR\n  A[<b>x</b>] --> B\n```\n\n```dataview\nLIST\n```")
    assert note.diagrams
    assert '<pre class="diagram-source"><code>flowchart LR\n  A[&lt;b&gt;x&lt;/b&gt;] --&gt; B\n</code></pre>' in note.html
    assert '<div class="plugin-label"><span>dataview</span> runs in Obsidian</div>' in note.html
    assert not render("text").diagrams


def test_code_is_coloured_and_labelled() -> None:
    out = html_of("```py\ndef f(): pass\n```\n\n```nosuchlang\n<a> & b\n```")
    assert '<span class="code-lang">Python</span>' in out and '<span class="k">def</span>' in out
    assert "&lt;a&gt; &amp; b" in out


def test_a_huge_code_block_is_not_coloured() -> None:
    out = html_of("```python\n" + "x = 1\n" * (MAX_COLOURED // 6 + 10) + "```")
    assert 'class="n"' not in out and "x = 1" in out


def test_a_huge_note_is_plain_text() -> None:
    note = render("# Big\n\n<script>x</script>" + "a" * MAX_NOTE_BYTES)
    assert note.plain and note.html.startswith('<pre class="plain"># Big') and "&lt;script&gt;" in note.html


# -- properties -----------------------------------------------------------------------------------


def test_properties_become_a_header() -> None:
    note = render("---\ntags: [one, two/three]\naliases: Other\ndate: 2026-11-02\ndone: true\n"
                  "up: \"[[Authors]]\"\ncssclasses: wide\nempty:\n---\nbody")
    p = note.properties
    assert note.tags == ["one", "two/three"]
    assert '<a class="tag" href="/dash/wikis/tag/v?tag=one" rel="noopener noreferrer">#one</a>' in p
    assert "<dt>aliases</dt><dd><span>Other</span></dd>" in p and "<span>02 Nov 2026</span>" in p
    assert "<span>Yes</span>" in p and 'href="/dash/wikis/note/v?path=Authors.md"' in p
    assert "cssclasses" not in p and "empty" not in p and "body" not in p


def test_tags_written_as_one_string() -> None:
    assert render('---\ntags: "one, #two three"\n---\n').tags == ["one", "two", "three"]
    assert render("---\ntags: one, #two\n---\n").tags == ["one"]  # unquoted, " #two" is a YAML comment


def test_bad_front_matter_is_ignored() -> None:
    note = render("---\n: : bad [\n---\ntext")
    assert note.properties == "" and "text" in note.html


# -- safety ---------------------------------------------------------------------------------------


ATTACKS = [
    '<script>alert(1)</script>',
    '<img src=x onerror="alert(1)">',
    '<img src="https://tracker.example.com/p.png">',
    '<a href="javascript:alert(1)">x</a>',
    '<a href="JaVaScRiPt:alert(1)">x</a>',
    '<a href=" javascript:alert(1)">x</a>',
    '<a href="jav&#x61;script:alert(1)">x</a>',
    '<a href="data:text/html,<script>alert(1)</script>">x</a>',
    '[x](javascript:alert(1))',
    '[x](<javascript:alert(1)>)',
    '[x](data:text/html;base64,PHNjcmlwdD4=)',
    '<svg onload="alert(1)"><circle/></svg>',
    '<iframe src="https://example.com"></iframe>',
    '<object data="x.swf"></object><embed src="x.swf">',
    '<form action="https://evil.example"><input type="text" name="q"><button>go</button></form>',
    '<style>body{display:none}</style>',
    '<meta http-equiv="refresh" content="0;url=https://evil.example">',
    '<base href="https://evil.example/">',
    '<link rel="stylesheet" href="https://evil.example/x.css">',
    '<div style="position:fixed;inset:0;background:url(javascript:alert(1))">cover</div>',
    '<a id="katex" name="pele" href="#">clobber</a>',
    '<p onmouseover="alert(1)">hover</p>',
    '[[<img src=x onerror=alert(1)>]]',
    '> [!note] <img src=x onerror=alert(1)> title\n> body',
    '```"><script>alert(1)</script>\ncode\n```',
    '<math><mi xlink:href="javascript:alert(1)">x</mi></math>',
]


DANGEROUS_TAGS = ("script", "iframe", "object", "embed", "form", "input type=\"text", "style", "meta", "base", "link",
                  "svg", "math", "button")
URL_ATTR = re.compile(r'\s(?:href|src|action|formaction|xlink:href)="([^"]*)"', re.I)


@pytest.mark.parametrize("attack", ATTACKS)
def test_nothing_in_a_note_can_run_a_script(attack: str) -> None:
    """Markdown leaves a refused link as plain text, which is harmless; what matters is that
    no tag, attribute or URL that could act survives."""
    out = html_of(f"text\n\n{attack}\n\nafter")
    lower = out.lower()
    for tag in DANGEROUS_TAGS:
        assert f"<{tag}" not in lower, (attack, out)
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", lower), (attack, out)  # no event handler on any tag
    for url in URL_ATTR.findall(out):
        assert url.startswith(("/dash/", "#", "https://", "http://", "mailto:")), (attack, url)
        assert "evil.example" not in url and "tracker.example.com" not in url, (attack, url)
    assert not re.search(r'<[^>]*\sstyle="', lower), (attack, out)  # only table cells keep a style (text-align)
    assert 'id="katex"' not in lower and 'name="pele"' not in lower
    assert "after" in out


def test_safe_html_survives() -> None:
    out = html_of('<details><summary>More</summary>Hidden **text**.</details>\n\n<kbd>Ctrl</kbd> '
                  '<a href="https://example.com" title="t">ok</a> <sup>2</sup>\n\n'
                  '| a |\n| -: |\n| 1 |')
    assert "<details><summary>More</summary>Hidden **text**.</details>" in out
    assert "<kbd>Ctrl</kbd>" in out and '<a href="https://example.com" title="t" rel="noopener noreferrer">ok</a>' in out
    assert '<td style="text-align:right">1</td>' in out


def test_a_notes_own_images_and_links_resolve_in_the_vault() -> None:
    out = html_of('<img src="attachments/sketch.png" width="50"> <img src="nowhere.png"> '
                  '<a href="Authors.md">authors</a>')
    assert '<img src="/dash/wikis/file/v?path=attachments/sketch.png" width="50">' in out
    assert "nowhere.png" not in out
    assert '<a href="/dash/wikis/note/v?path=Authors.md" rel="noopener noreferrer">authors</a>' in out

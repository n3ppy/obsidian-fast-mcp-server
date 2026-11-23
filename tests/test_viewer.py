"""The note viewer on the dashboard: /dash/wikis/note, /file, /open, /tag and /static.

The server runs in-process with a real vault and git remote (the fixtures from
test_dashboard.py); the notes and files for these tests are written straight into the
server's working copy, which is what the viewer reads."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fasthtml", reason="the dashboard extra is not installed")

from obsidian_mcp import gitstats  # noqa: E402
from obsidian_mcp.dashboard import assets  # noqa: E402
from test_dashboard import Web, call_tools, make_server, run, sign_in, vault_env  # noqa: E402, F401

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000")

GUIDE = """---
tags: [guide]
---
# Guide

Read [[Other]] and the [[Welcome|welcome note]]. Not yet: [[Someday]].

> [!tip] A tip
> With **bold**.

![[pic.png|120]]

![[Other]]

Euler: $e^{i\\pi}+1=0$.

```mermaid
flowchart LR
  A --> B
```

<img src="https://tracker.example.com/p.png"> <script>alert(1)</script>
"""


def start_tag(page: str, name: str, **attrs: str) -> str:
    """The first <name ...> tag having all these attribute values (in any order)."""
    for tag in re.findall(rf"<{name}\b[^>]*>", page):
        if all(f'{k.rstrip("_").replace("_", "-")}="{v}"' in tag for k, v in attrs.items()):
            return tag
    raise AssertionError(f"no <{name}> with {attrs}")


def write(root: Path, rel: str, content: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


@pytest.fixture
def notes(vault_env) -> dict[str, Any]:  # noqa: F811 - the fixture imported above
    root = vault_env["server_vault"]
    write(root, "Notes/Guide.md", GUIDE)
    write(root, "Notes/Linked.md", "See [[Guide]].\n")
    write(root, "Notes/Two lines.md", "one\ntwo\n")
    write(root, "Other.md", "Other's text.\n")
    write(root, "images/pic.png", PNG)
    write(root, "images/logo.svg", '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    write(root, "files/page.html", "<script>alert(1)</script>")
    write(root, ".obsidian/app.json", '{"strictLineBreaks": false}')
    os.symlink("/etc/hostname", root / "escape.md")
    return vault_env


def test_a_note_opens_rendered_in_the_dashboard(notes) -> None:
    async def check() -> None:
        async with Web(make_server()) as web:
            await sign_in(web)
            resp = await web.get("/dash/wikis/note/principal?path=Notes/Guide.md")
            page = resp.text
            assert resp.status_code == 200
            assert '<h1 class="note-title">Guide</h1>' in page and "<h1 data-line" not in page  # "# Guide" isn't repeated
            digest = assets.digest()
            start_tag(page, "article", class_="note", data_note="1", data_assets=f"/dash/static/{digest}/",
                      data_open="/dash/wikis/open/principal?src=Notes/Guide.md&amp;name=")
            assert f'<link rel="stylesheet" href="/dash/static/{digest}/note.css">' in page
            assert f'<script src="/dash/static/{digest}/note.js" defer></script>' in page
            # Links, embeds and the rest, through the vault's own resolution.
            assert '<a class="internal" href="/dash/wikis/note/principal?path=Other.md"' in page
            assert 'href="/dash/wikis/note/principal?path=Inbox/Welcome.md"' in page and "welcome note</a>" in page
            assert '<a class="internal unresolved" href="/dash/wikis/open/principal?name=Someday"' in page
            assert '<div class="callout" data-callout="tip"' in page
            assert '<img src="/dash/wikis/file/principal?path=images/pic.png" alt="pic.png" width="120"' in page
            assert '<div class="embed">' in page and "Other's text." in page
            assert '<span class="math inline">' in page and '<div class="diagram" data-diagram="mermaid"' in page
            assert '<a class="tag" href="/dash/wikis/tag/principal?tag=guide"' in page
            assert "tracker.example.com" not in page and "alert(1)" not in page
            # The page around it: where it is, what links here; no GitHub button without a GitHub remote.
            assert '<a href="/dash/wikis">Wikis</a><span class="sep">/</span><span>Notes</span>' in page
            assert "Linked from" in page and 'href="/dash/wikis/note/principal?path=Notes/Linked.md">Linked</a>' in page
            assert 'class="btn-gh"' not in page
            assert 'class="on"' in page and 'href="/dash/wikis"' in page  # the Wikis tab is the current one
            csp = resp.headers["content-security-policy"]
            assert "script-src 'self'" in csp and "font-src 'self'" in csp and "img-src 'self' data:" in csp
            assert resp.headers["cache-control"] == "no-store"

    run(check())


def test_the_vaults_line_break_setting_is_followed(notes) -> None:
    async def check() -> None:
        async with Web(make_server()) as web:
            await sign_in(web)
            assert "one<br>" in (await web.get("/dash/wikis/note/principal?path=Notes/Two lines.md")).text
            (notes["server_vault"] / ".obsidian" / "app.json").write_text('{"strictLineBreaks": true}')
            assert "one<br>" not in (await web.get("/dash/wikis/note/principal?path=Notes/Two%20lines.md")).text

    run(check())


def test_the_wikis_tree_opens_notes_here_and_github_on_hover(notes, monkeypatch) -> None:
    async def check(github: bool) -> str:
        async with Web(make_server()) as web:
            await sign_in(web)
            return (await web.get("/dash/wikis")).text

    plain = run(check(False))
    start_tag(plain, "a", href="/dash/wikis/note/principal?path=Inbox/Welcome.md", class_="file")
    assert re.search(r'<a href="/dash/wikis/file/principal\?path=images/pic.png" target="_blank"[^>]*class="file"',
                     plain)  # an image opens the file itself
    assert 'class="gh"' not in plain and "Click a note to read it." in plain

    monkeypatch.setattr(gitstats, "web_url", lambda vault, sha=None: "https://github.com/me/vault")
    with_github = run(check(True))
    item = re.search(r'<li class="f">(?:(?!</li>).)*Welcome(?:(?!</li>).)*</li>', with_github, re.S).group(0)
    assert 'href="/dash/wikis/note/principal?path=Inbox/Welcome.md"' in item  # the note opens here...
    start_tag(item, "a", href="https://github.com/me/vault/blob/HEAD/Inbox/Welcome.md", class_="gh", target="_blank",
              rel="noopener", title="Open on GitHub", aria_label="Open Welcome on GitHub")  # ...GitHub beside it
    assert item.index('class="meta"') < item.index('class="gh"')  # at the far right
    assert "or the GitHub mark beside it to open it on GitHub" in with_github

    async def note_page() -> str:
        async with Web(make_server()) as web:
            await sign_in(web)
            return (await web.get("/dash/wikis/note/principal?path=Notes/Guide.md")).text

    start_tag(run(note_page()), "a", href="https://github.com/me/vault/blob/HEAD/Notes/Guide.md", class_="btn-gh",
              target="_blank", rel="noopener", title="Open on GitHub")


def test_paths_outside_the_vault_or_hidden_are_refused(notes) -> None:
    async def check() -> None:
        async with Web(make_server()) as web:
            await sign_in(web)
            for path in ("../outside.md", "Notes/../../outside.md", ".git/config", ".obsidian/app.json",
                         "escape.md", "/etc/hostname", "Notes/Missing.md", "images/pic.png", ""):
                resp = await web.get("/dash/wikis/note/principal", params={"path": path})
                assert resp.status_code == 404 and "Note not found" in resp.text, path
            for path in ("../outside.md", ".git/config", ".git/HEAD", "escape.md", "/etc/hostname", "nope.png", ""):
                resp = await web.get("/dash/wikis/file/principal", params={"path": path})
                assert resp.status_code == 404, path
            assert (await web.get("/dash/wikis/note/nope?path=Other.md")).status_code == 404
            assert (await web.get("/dash/wikis/file/nope?path=images/pic.png")).status_code == 404

    run(check())


def test_files_are_served_safely(notes) -> None:
    async def check() -> None:
        async with Web(make_server()) as web:
            await sign_in(web)
            png = await web.get("/dash/wikis/file/principal?path=images/pic.png")
            assert png.status_code == 200 and png.content == PNG and png.headers["content-type"] == "image/png"
            assert "sandbox" in png.headers["content-security-policy"]
            svg = await web.get("/dash/wikis/file/principal?path=images/logo.svg")
            assert svg.headers["content-type"].startswith("image/svg+xml")
            assert "sandbox" in svg.headers["content-security-policy"]  # opened directly, its script can't run
            html = await web.get("/dash/wikis/file/principal?path=files/page.html")
            assert html.headers["content-type"] == "application/octet-stream"
            assert html.headers["content-disposition"].startswith("attachment")  # downloads, never shown

    run(check())


def test_links_by_name_and_tags(notes) -> None:
    async def check() -> None:
        async with Web(make_server()) as web:
            await sign_in(web)
            found = await web.get("/dash/wikis/open/principal?name=Guide&src=Notes/Linked.md")
            assert found.status_code == 303
            assert found.headers["location"] == "/dash/wikis/note/principal?path=Notes/Guide.md"
            missing = await web.get("/dash/wikis/open/principal?name=Someday")
            assert missing.status_code == 404 and "No note called “Someday” yet" in missing.text
            tag = (await web.get("/dash/wikis/tag/principal?tag=guide")).text
            assert '<h1 class="note-title">#guide</h1>' in tag and "1 note" in tag
            assert 'href="/dash/wikis/note/principal?path=Notes/Guide.md">Guide</a>' in tag
            assert "No notes have this tag." in (await web.get("/dash/wikis/tag/principal?tag=nothing")).text

    run(check())


def test_static_files_are_typed_and_cached(notes) -> None:
    async def check() -> None:
        async with Web(make_server()) as web:
            await sign_in(web)
            base = f"/dash/static/{assets.digest()}"
            js = await web.get(f"{base}/note.js")
            assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
            assert js.headers["cache-control"] == "public, max-age=31536000, immutable"
            assert "pele.mount" in js.text and "katex.render" in js.text
            css = await web.get(f"{base}/note.css")
            assert css.headers["content-type"].startswith("text/css") and "article.note" in css.text
            assert ".code .k {" in css.text and ':root[data-theme="dark"] .code .k {' in css.text  # code colours
            font = await web.get(f"{base}/vendor/katex/fonts/KaTeX_Main-Regular.woff2")
            assert font.headers["content-type"] == "font/woff2" and font.content[:4] == b"wOF2"
            pele = await web.get(f"{base}/vendor/pele/pele.global.js")
            assert pele.status_code == 200 and len(pele.content) > 100_000
            for path in ("vendor/katex/LICENSE", "vendor/README.md", "%2E%2E%2Fapp.py", "..%2F..%2Fviewer.py",
                         "nope.js"):
                resp = await web.get(f"{base}/{path}")
                assert resp.status_code == 404 and resp.headers["cache-control"] == "no-store", path
            dotted = await web.get(f"{base}/../app.py")  # the client tidies this to /dash/static/app.py: no route
            assert dotted.status_code != 200 and "def build_app" not in dotted.text

    run(check())


def test_the_viewer_needs_sign_in_and_activity_links_to_it(notes) -> None:
    server = make_server()
    run(call_tools(server, [("read_note", {"path": "Other"})]))

    async def check() -> None:
        async with Web(server) as web:
            for path in ("/dash/wikis/note/principal?path=Other.md", "/dash/wikis/file/principal?path=images/pic.png",
                         f"/dash/static/{assets.digest()}/note.js"):
                resp = await web.get(path)
                assert resp.status_code == 303 and resp.headers["location"] == "/dash/login", path
            await sign_in(web)
            activity = (await web.get("/dash/activity")).text
            assert '<a href="/dash/wikis/note/principal?path=Other.md">Other.md</a>' in activity

    run(check())

# Vendored browser files

Pinned copies, served by the dashboard itself (`/dash/static/...`), so the note viewer
loads nothing from other sites. `note.js` loads them only on a note that needs them.

| Folder | Package | Version | Licence | Files kept |
| --- | --- | --- | --- | --- |
| `katex/` | [KaTeX](https://katex.org) | 0.19.1 | MIT (`katex/LICENSE`) | `dist/katex.min.js`, `dist/katex.min.css`, `dist/contrib/mhchem.min.js`, the 20 `dist/fonts/*.woff2` |
| `pele/` | [Pele](https://pele.run), Obsidian's diagram renderer | 1.13.0 | MIT (`pele/LICENSE`) | `dist/pele.global.js` |

Both came from the npm registry on 2026-11-23, checked against the registry's integrity
hashes:

| Tarball | SHA-256 |
| --- | --- |
| `https://registry.npmjs.org/katex/-/katex-0.19.1.tgz` | `24da5a599eb2fcede2e270b99a6ff48bca816beecc95feeb84e8cccbd1bb34fd` |
| `https://registry.npmjs.org/pele/-/pele-1.13.0.tgz` | `00f879d86c5a6ece897ef70e18fd228a9952bba338a0cb1293b373900d7414bd` |

Only the woff2 fonts are kept: every browser the dashboard supports reads them, and
`katex.min.css` lists them first.

To update one: download the new tarball, check its integrity hash against
`https://registry.npmjs.org/<package>/<version>`, copy the same files over, and change
the version and hash here. KaTeX releases before 0.18.2 had security advisories; keep a
current one.

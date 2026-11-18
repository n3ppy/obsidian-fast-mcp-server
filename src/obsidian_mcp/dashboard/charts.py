"""Small server-rendered SVG charts: no chart library, no script, works with a strict CSP.

Colours come from CSS classes (``s0``, ``s1``…) defined in the page stylesheet, so the
charts follow the light/dark theme.
"""

from __future__ import annotations

import math
from html import escape
from typing import Any

W, H = 640, 180
PAD_L, PAD_R, PAD_T, PAD_B = 32, 8, 10, 22


def _nice_max(value: float) -> int:
    if value <= 4:
        return 4
    magnitude = 10 ** int(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        top = step * magnitude
        if top >= value:
            return int(math.ceil(top))
    return int(value)


def stacked_bars(
    rows: list[dict[str, Any]], series: list[tuple[str, str]], label_key: str = "day",
    label_fmt: Any = str, aria: str = "Chart",
) -> str:
    """Stacked bars, one per row. `series` is [(key, legend label)], bottom first."""
    n = max(len(rows), 1)
    totals = [sum(r[k] for k, _ in series) for r in rows]
    top = _nice_max(max(totals, default=0))
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    slot = plot_w / n
    bar = max(slot * 0.7, 1.0)
    parts = [
        f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="{escape(aria)}">'
    ]
    for frac in (0, 0.5, 1):
        y = PAD_T + plot_h * (1 - frac)
        parts.append(f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="axis" x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">{int(top * frac)}</text>')
    for i, row in enumerate(rows):
        x = PAD_L + i * slot + (slot - bar) / 2
        y = PAD_T + plot_h
        tip = escape(f"{label_fmt(row[label_key])}: " + ", ".join(f"{row[k]} {lbl.lower()}" for k, lbl in series))
        parts.append(f"<g><title>{tip}</title>")
        for idx, (key, _) in enumerate(series):
            value = row[key]
            if not value:
                continue
            h = plot_h * value / top
            y -= h
            parts.append(f'<rect class="s{idx}" x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" height="{h:.1f}" rx="1.5"/>')
        # A transparent full-height target so the tooltip works on empty days too.
        parts.append(f'<rect class="hit" x="{PAD_L + i * slot:.1f}" y="{PAD_T}" width="{slot:.1f}" height="{plot_h}"/>')
        parts.append("</g>")
    # Label the first, middle and last bars.
    for i in sorted({0, (len(rows) - 1) // 2, len(rows) - 1}) if rows else []:
        x = PAD_L + i * slot + slot / 2
        parts.append(f'<text class="axis" x="{x:.1f}" y="{H - 6}" text-anchor="middle">{escape(label_fmt(rows[i][label_key]))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def legend(series: list[tuple[str, str]]) -> str:
    items = "".join(
        f'<span class="key"><i class="sw s{idx}"></i>{escape(label)}</span>' for idx, (_, label) in enumerate(series)
    )
    return f'<div class="legend">{items}</div>'

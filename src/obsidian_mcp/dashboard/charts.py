"""Small server-rendered bar charts: HTML and CSS, no chart library, no script.

Bars are flex columns sized in percent, so text stays crisp at any width and the colours
follow the page's light/dark theme through CSS classes (``s0``, ``s1``…, defined in the
stylesheet). Stacked segments sit 2px apart, bars have rounded tops on the baseline, and
each bar's tooltip lists its values. The whole chart is one image to screen readers,
named by ``aria``.
"""

from __future__ import annotations

import math
from html import escape
from typing import Any


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
    label_fmt: Any = str, aria: str = "Chart", *, emphasize_last: bool = False, label_last: bool = False,
) -> str:
    """Stacked bars, one per row. `series` is [(key, legend label)], bottom first.

    `emphasize_last` tints every bar but the last (today's) in the first series' colour;
    `label_last` writes the last bar's total above it."""
    totals = [sum(r[k] for k, _ in series) for r in rows]
    top = _nice_max(max(totals, default=0))
    last = len(rows) - 1
    cols = []
    for i, (row, total) in enumerate(zip(rows, totals)):
        tip = f"{label_fmt(row[label_key])}: " + ", ".join(f"{row[k]} {lbl.lower()}" for k, lbl in series)
        segments = "".join(f'<i class="s{idx}" style="flex-grow:{row[key]}"></i>'
                           for idx, (key, _) in enumerate(series) if row[key])
        value = f'<span class="val">{total}</span>' if label_last and i == last else ""
        cols.append(f'<div class="{"col last" if i == last else "col"}" title="{escape(tip)}">{value}'
                    f'<div class="stack" style="height:{100 * total / top:.1f}%">{segments}</div></div>')
    marks = sorted({0, last // 2, last}) if rows else []
    axis = "".join(f"<span>{escape(label_fmt(rows[i][label_key]))}</span>" for i in marks)
    return (f'<div class="chart{" emph" if emphasize_last else ""}" role="img" aria-label="{escape(aria)}">'
            f'<div class="plot"><span class="ymax">{top}</span>{"".join(cols)}</div>'
            f'<div class="axis">{axis}</div></div>')


def legend(series: list[tuple[str, str]]) -> str:
    items = "".join(
        f'<span class="key"><i class="sw s{idx}"></i>{escape(label)}</span>' for idx, (_, label) in enumerate(series)
    )
    return f'<div class="legend">{items}</div>'

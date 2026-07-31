"""Colors, glyphs and tiny chart primitives shared by the whole TUI."""

from __future__ import annotations

from typing import List, Optional, Sequence

# --- palette -----------------------------------------------------------------

CYAN = "#22d3ee"
TEAL = "#2dd4bf"
BLUE = "#60a5fa"
INDIGO = "#818cf8"
VIOLET = "#a78bfa"
PINK = "#f472b6"
GREEN = "#4ade80"
AMBER = "#fbbf24"
RED = "#f87171"
DIM = "grey46"
FAINT = "grey30"
TEXT = "grey85"

#: left-to-right gradient used for the logo, rules and bar charts
GRADIENT: List[str] = [CYAN, "#38bdf8", BLUE, INDIGO, VIOLET, "#c084fc", "#e879f9", PINK]

#: metric -> accent color, so the same measure keeps its color everywhere
METRIC_COLOR = {
    "ttft": CYAN,
    "tps": GREEN,
    "itl": VIOLET,
    "total": BLUE,
    "tokens": AMBER,
    "prefill": PINK,
}

# --- glyphs ------------------------------------------------------------------

SPARK = "▁▂▃▄▅▆▇█"
BAR_FULL = "━"
BAR_EMPTY = "─"

LOGO = r"""
 ██████╗ ███╗   ███╗███████╗████████╗███████╗██████╗
██╔═══██╗████╗ ████║██╔════╝╚══██╔══╝██╔════╝██╔══██╗
██║   ██║██╔████╔██║█████╗     ██║   █████╗  ██████╔╝
██║   ██║██║╚██╔╝██║██╔══╝     ██║   ██╔══╝  ██╔══██╗
╚██████╔╝██║ ╚═╝ ██║███████╗   ██║   ███████╗██║  ██║
 ╚═════╝ ╚═╝     ╚═╝╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝
"""

LOGO_SMALL = "◢◤ O M E T E R ◢◤"

# 3x5 pixel font, rendered three character-rows tall with half blocks.
_PIXELS = {
    "0": ["###", "#.#", "#.#", "#.#", "###"],
    "1": ["..#", "..#", "..#", "..#", "..#"],
    "2": ["###", "..#", "###", "#..", "###"],
    "3": ["###", "..#", "###", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"],
    "7": ["###", "..#", "..#", "..#", "..#"],
    "8": ["###", "#.#", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#", "###"],
    ".": [".", ".", ".", ".", "#"],
    ",": [".", ".", ".", ".", "#"],
    "-": ["...", "...", "###", "...", "..."],
    "?": ["###", "..#", ".##", "...", ".#."],
    " ": ["..", "..", "..", "..", ".."],
}


def big_digits(text: str) -> List[str]:
    """Render `text` as three rows of half-block 'seven segment' digits."""
    glyphs = [_PIXELS.get(ch, _PIXELS["?"]) for ch in text]
    if not glyphs:
        return ["", "", ""]

    # pad every glyph to 6 pixel rows so pairs of rows map cleanly to characters
    grid = []
    for row in range(6):
        cells = []
        for g in glyphs:
            cells.append(g[row] if row < len(g) else "." * len(g[0]))
        grid.append(" ".join(cells))

    out = []
    for top, bottom in ((0, 1), (2, 3), (4, 5)):
        line = []
        for upper, lower in zip(grid[top], grid[bottom]):
            on_up, on_dn = upper == "#", lower == "#"
            if on_up and on_dn:
                line.append("█")
            elif on_up:
                line.append("▀")
            elif on_dn:
                line.append("▄")
            else:
                line.append(" ")
        out.append("".join(line))
    return out


# --- charts ------------------------------------------------------------------


def sparkline(values: Sequence[float], width: Optional[int] = None, cap: Optional[float] = None) -> str:
    """Unicode sparkline. Downsamples by bucket-averaging when too wide.

    `cap` pins the top of the scale so a single stall does not flatten
    everything else into the bottom bucket; anything above it saturates.
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    if width and len(vals) > width:
        step = len(vals) / width
        bucketed = []
        for i in range(width):
            chunk = vals[int(i * step) : max(int((i + 1) * step), int(i * step) + 1)]
            bucketed.append(sum(chunk) / len(chunk))
        vals = bucketed

    lo = min(vals)
    hi = cap if cap is not None else max(vals)
    if hi - lo < 1e-12:
        return SPARK[3] * len(vals)
    span = hi - lo
    return "".join(SPARK[min(7, max(0, int((v - lo) / span * 7.999)))] for v in vals)


def bar(value: float, maximum: float, width: int) -> str:
    """Horizontal bar with a fractional trailing block, space padded."""
    if maximum <= 0 or value <= 0:
        return " " * width
    filled = max(0.0, min(1.0, value / maximum)) * width
    whole = int(filled)
    out = "█" * whole
    remainder = filled - whole
    if whole < width:
        out += " ▏▎▍▌▋▊▉"[min(7, int(remainder * 8))]
    return out.ljust(width)[:width]


def bar_text(value: float, maximum: float, width: int, color: str, track: str = "·") -> "object":
    """Colored bar over a faint track, as a rich Text."""
    from rich.text import Text

    filled = bar(value, maximum, width).rstrip(" ")
    out = Text(filled, style=color)
    if len(filled) < width:
        out.append(track * (width - len(filled)), style=FAINT)
    return out


def gradient_text(text: str, colors: Optional[Sequence[str]] = None) -> "object":
    """Color a string left-to-right across the palette."""
    from rich.text import Text

    colors = list(colors or GRADIENT)
    out = Text()
    n = max(len(text) - 1, 1)
    for i, ch in enumerate(text):
        idx = int(i / n * (len(colors) - 1))
        out.append(ch, style=colors[idx])
    return out


def heat_color(ratio: float, invert: bool = False) -> str:
    """green -> amber -> red across 0..1 (invert flips the direction)."""
    r = max(0.0, min(1.0, ratio))
    if invert:
        r = 1.0 - r
    if r < 0.34:
        return GREEN
    if r < 0.67:
        return AMBER
    return RED

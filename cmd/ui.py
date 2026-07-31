"""Everything the user actually looks at: the live dashboard and the report."""

from __future__ import annotations

import statistics
import time
from typing import List, Optional, Sequence, Tuple

from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.measure import Measurement
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .client import Chunk
from .config import BenchConfig, mask_key
from .metrics import Report, RunResult, Stats, percentile
from .theme import (
    AMBER,
    BLUE,
    CYAN,
    DIM,
    FAINT,
    GRADIENT,
    GREEN,
    INDIGO,
    LOGO,
    PINK,
    RED,
    TEXT,
    VIOLET,
    bar_text,
    big_digits,
    gradient_text,
    heat_color,
    sparkline,
)

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# --- formatting ---------------------------------------------------------------


def fmt_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    if seconds < 1.0:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.2f} s"
    return f"{int(seconds // 60)}m {seconds % 60:04.1f}s"


def fmt_ms(seconds: Optional[float]) -> str:
    return "—" if seconds is None else f"{seconds * 1000:.1f} ms"


def fmt_tps(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.1f}"


def fmt_int(value) -> str:
    return "—" if value is None else f"{int(value):,}"


def _hero_value(seconds_or_rate: Optional[float], kind: str) -> Tuple[str, str]:
    """Return (big number string, unit) for a hero card."""
    if seconds_or_rate is None:
        return ("--", "")
    if kind == "time":
        if seconds_or_rate < 1.0:
            return (f"{seconds_or_rate * 1000:.0f}", "ms")
        return (f"{seconds_or_rate:.2f}", "s")
    if kind == "rate":
        return (f"{seconds_or_rate:.1f}", "tok/s")
    return (f"{seconds_or_rate:,.0f}", "")


# --- static chrome ------------------------------------------------------------


def print_banner(console: Console) -> None:
    if console.width >= 60:
        lines = [ln for ln in LOGO.strip("\n").splitlines()]
        art = Text()
        for i, line in enumerate(lines):
            color = GRADIENT[min(i, len(GRADIENT) - 1)]
            art.append(line + "\n", style=color)
        art.rstrip()
        console.print(Align.center(art) if console.width > 70 else art)
    else:
        console.print(gradient_text("◢◤ OMETER ◢◤"))
    tag = Text("  ollama cloud inference meter", style=f"italic {DIM}")
    console.print(Align.center(tag) if console.width > 70 else tag)
    console.print()


def config_panel(cfg: BenchConfig, api_key: str, env_files: Sequence) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=DIM, justify="right", no_wrap=True)
    grid.add_column(style=TEXT)
    for label, value in cfg.summary_rows():
        grid.add_row(label, value)
    grid.add_row("api key", f"[{FAINT}]{mask_key(api_key)}[/]")
    if env_files:
        grid.add_row("env", f"[{FAINT}]{env_files[0]}[/]")
    return Panel(
        grid,
        title=f"[{CYAN}]configuration[/]",
        title_align="left",
        border_style=FAINT,
        box=box.ROUNDED,
        padding=(0, 1),
    )


# --- live dashboard -----------------------------------------------------------


class LiveDashboard:
    """Streaming progress view; wire `hook` into Benchmark."""

    MIN_FRAME = 0.055  # seconds between repaints

    def __init__(self, console: Console, cfg: BenchConfig):
        self.console = console
        self.cfg = cfg
        self.total_runs = cfg.runs + cfg.warmup
        self.finished = 0
        self.started_at = time.perf_counter()

        self.current: Optional[RunResult] = None
        self.current_started = 0.0
        self.preview = ""
        self.completed: List[RunResult] = []
        self.errors: List[str] = []

        self._last_paint = 0.0
        self._frame = 0
        self._live: Optional[Live] = None
        self._plain = not console.is_terminal

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "LiveDashboard":
        if not self._plain:
            self._live = Live(
                self._render(),
                console=self.console,
                refresh_per_second=16,
                transient=True,
            )
            self._live.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if self._live:
            self._live.__exit__(*exc)
            self._live = None

    # -- benchmark hook ----------------------------------------------------

    def hook(self, event: str, run: RunResult, chunk: Optional[Chunk]) -> None:
        if event == "start":
            self.current = run
            self.current_started = time.perf_counter()
            self.preview = ""
            if self._plain:
                self.console.print(f"[{DIM}]› {self._label(run)} …[/]")
            self._paint(force=True)

        elif event == "chunk":
            if chunk is not None and chunk.kind == "content":
                self.preview = (self.preview + chunk.text)[-600:]
            self._paint()

        elif event == "done":
            self.finished += 1
            self.completed.append(run)
            self.current = None
            if self._plain:
                self.console.print(
                    f"  {self._label(run)}: ttft {fmt_time(run.ttft)}  "
                    f"decode {fmt_tps(run.decode_tps)} tok/s  "
                    f"total {fmt_time(run.total_wall)}"
                )
            self._paint(force=True)

        elif event == "error":
            self.finished += 1
            self.completed.append(run)
            self.errors.append(f"{self._label(run)}: {run.error}")
            self.current = None
            if self._plain:
                self.console.print(f"  [{RED}]{self._label(run)} failed: {run.error}[/]")
            self._paint(force=True)

    def _label(self, run: RunResult) -> str:
        return "warmup" if run.warmup else f"run {run.index}"

    def _paint(self, force: bool = False) -> None:
        if self._plain or self._live is None:
            return
        now = time.perf_counter()
        if not force and now - self._last_paint < self.MIN_FRAME:
            return
        self._last_paint = now
        self._frame += 1
        self._live.update(self._render())

    # -- rendering ---------------------------------------------------------

    def _render(self) -> RenderableType:
        return Group(
            self._gauges(),
            self._stream_panel(),
            self._scoreboard(),
            self._footer(),
        )

    def _gauges(self) -> Panel:
        run = self.current
        elapsed = (time.perf_counter() - self.current_started) if run else 0.0
        tokens = len(run.chunk_times) if run else 0
        ttft = run.ttft if run else None

        live_tps = None
        if run and ttft is not None and tokens > 1:
            span = (run.last_chunk_at or ttft) - ttft
            if span > 0:
                live_tps = (tokens - 1) / span

        roomy = self.console.width >= 96
        cells = [
            ("time to first token" if roomy else "first token", fmt_time(ttft), CYAN),
            ("decode speed" if roomy else "decode", f"{fmt_tps(live_tps)} tok/s", GREEN),
            ("tokens", f"{tokens} / {self.cfg.max_tokens}", AMBER),
            ("elapsed", f"{elapsed:.1f} s", BLUE),
        ]

        # four gauges side by side, folding to a 2x2 block on narrow terminals
        per_row = 4 if self.console.width >= 64 else 2
        grid = Table.grid(expand=True, padding=(0, 1))
        for _ in range(per_row):
            grid.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
        for start in range(0, len(cells), per_row):
            block = cells[start : start + per_row]
            grid.add_row(*[Text(label, style=DIM) for label, _, _ in block])
            grid.add_row(*[Text(value, style=f"bold {color}") for _, value, color in block])

        # token-budget bar + inter-token-latency trace, sized to fill the panel
        width = max(10, self.console.width - 20)
        budget = Text("budget  ", style=DIM)
        budget.append_text(bar_text(tokens, max(self.cfg.max_tokens, 1), width, VIOLET))

        itls = run.itls[-width:] if run else []
        trace = Text("latency ", style=DIM)
        if itls:
            trace.append(sparkline(itls, width=width, cap=percentile(itls, 0.95)).ljust(width), style=INDIGO)
            trace.append(f" {statistics.fmean(itls) * 1000:4.0f} ms", style=DIM)
        else:
            trace.append("·" * width, style=FAINT)

        title = "waiting"
        if run is not None:
            spin = SPINNER[self._frame % len(SPINNER)]
            title = f"{spin} {self._label(run)}"

        return Panel(
            Group(grid, Text(), budget, trace),
            title=f"[{CYAN}]{title}[/]",
            title_align="left",
            border_style=CYAN,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _stream_panel(self) -> Panel:
        width = max(20, self.console.width - 6)
        text = self.preview.replace("\n", " ⏎ ")
        text = text[-(width * 3) :]
        body = Text(text or "…", style=TEXT, overflow="fold", no_wrap=False)
        return Panel(
            body,
            title=f"[{DIM}]response[/]",
            title_align="left",
            border_style=FAINT,
            box=box.ROUNDED,
            height=5,
            padding=(0, 1),
        )

    def _scoreboard(self) -> Panel:
        rows = [r for r in self.completed][-5:]
        show_bar = self.console.width >= 56
        table = Table.grid(padding=(0, 2), expand=True)
        table.add_column(style=DIM, no_wrap=True, width=8)
        table.add_column(no_wrap=True, width=10, justify="right")
        table.add_column(no_wrap=True, width=12, justify="right")
        if show_bar:
            table.add_column(ratio=1)

        blanks = [""] * (3 if show_bar else 2)
        if not rows:
            table.add_row(f"[{FAINT}]no runs completed yet[/]", *blanks)
        else:
            speeds = [r.decode_tps for r in self.completed if r.decode_tps]
            peak = max(speeds) if speeds else 1.0
            width = max(6, self.console.width - 40)
            for r in rows:
                if not r.ok:
                    table.add_row(self._label(r), f"[{RED}]failed[/]", *blanks[:-1] if show_bar else blanks)
                    continue
                cols = [
                    self._label(r),
                    f"[{CYAN}]{fmt_time(r.ttft)}[/]",
                    f"[{GREEN}]{fmt_tps(r.decode_tps)} t/s[/]",
                ]
                if show_bar:
                    cols.append(bar_text(r.decode_tps or 0, peak, width, GREEN, track=" "))
                table.add_row(*cols)

        return Panel(
            table,
            title=f"[{DIM}]completed[/]",
            title_align="left",
            border_style=FAINT,
            box=box.ROUNDED,
            padding=(0, 1),
        )

    def _footer(self) -> Text:
        elapsed = time.perf_counter() - self.started_at
        width = max(10, self.console.width - 34)
        line = Text("  ")
        line.append_text(bar_text(self.finished, self.total_runs, width, CYAN, track="─"))
        line.append(f"  {self.finished}/{self.total_runs}", style=TEXT)
        line.append(f"  ·  {int(elapsed // 60):d}:{elapsed % 60:04.1f} elapsed", style=DIM)
        return line


# --- results ------------------------------------------------------------------


def _hero_card(title: str, value: Optional[float], kind: str, color: str, sub_parts, width: int) -> Panel:
    number, unit = _hero_value(value, kind)
    sub = ""
    for part in sub_parts:
        candidate = f"{sub}  {part}".strip() if sub else part
        if len(candidate) > width - 4:
            break
        sub = candidate
    art = big_digits(number)
    art_width = max(len(line) for line in art) if art else 0

    body = Table.grid()
    body.add_column()
    for i, line in enumerate(art):
        row = Text(line, style=f"bold {color}")
        if i == len(art) - 1 and unit:
            row.append(f" {unit}", style=DIM)
        body.add_row(row)
    body.add_row(Text(sub, style=FAINT, overflow="ellipsis", no_wrap=True))

    return Panel(
        body,
        title=f"[{color}]{title}[/]",
        title_align="left",
        border_style=FAINT,
        box=box.ROUNDED,
        padding=(0, 1),
        width=max(width, art_width + 4),
    )


def _hero_row(console: Console, report: Report) -> RenderableType:
    ttft = report.stat("ttft")
    decode = report.stat("decode_tps")
    e2e = report.stat("e2e_tps")
    total = report.stat("total_wall")

    cards = [
        ("time to first token", ttft.mean, "time", CYAN,
         [f"p95 {fmt_time(ttft.p95)}", f"min {fmt_time(ttft.minimum)}"]),
        ("decode speed", decode.mean, "rate", GREEN,
         [f"max {fmt_tps(decode.maximum)}", f"min {fmt_tps(decode.minimum)}"]),
        ("end-to-end speed", e2e.mean, "rate", AMBER,
         [f"max {fmt_tps(e2e.maximum)}", f"min {fmt_tps(e2e.minimum)}"]),
        ("total per request", total.mean, "time", BLUE,
         [f"p95 {fmt_time(total.p95)}", f"min {fmt_time(total.minimum)}"]),
    ]

    per_row = 4 if console.width >= 108 else 2 if console.width >= 48 else 1
    cell_width = max(22, (console.width - 2) // per_row - 1)

    grid = Table.grid(padding=(0, 1))
    for _ in range(per_row):
        grid.add_column()
    for start in range(0, len(cards), per_row):
        grid.add_row(*[_hero_card(*c, width=cell_width) for c in cards[start : start + per_row]])
    return grid


def _fits(console: Console, renderable, available: int) -> bool:
    """True when `renderable` can be drawn in `available` columns without clipping.

    Measured against an unbounded width, because rich clamps a measurement to
    whatever width you hand it — ask with `available` and everything "fits".
    """
    options = console.options.update(width=10_000)
    return Measurement.get(console, options, renderable).maximum <= available


def _pick(console: Console, build, variants, available: int):
    """Build the first variant that fits; fall back to the leanest one."""
    for variant in variants:
        candidate = build(*variant)
        if _fits(console, candidate, available):
            return candidate
    return build(*variants[-1])


#: aggregate column sets, richest first; each is (columns, use long labels)
_AGG_VARIANTS = [
    (["mean", "min", "p50", "p95", "max", "cv"], True),
    (["mean", "p50", "p95", "max", "cv"], True),
    (["mean", "p50", "p95", "cv"], True),
    (["mean", "p50", "p95"], True),
    (["mean", "p50", "p95", "cv"], False),
    (["mean", "p50", "p95"], False),
    (["mean", "p95"], False),
]


def _aggregate_table(console: Console, report: Report) -> Panel:
    def build(cols, wide_labels):
        table = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, header_style=f"bold {DIM}")
        table.add_column("metric", no_wrap=True, width=21 if wide_labels else 12)
        for col in cols:
            table.add_column(col, justify="right", no_wrap=True, min_width=7)

        def row(long: str, short: str, stats: Stats, formatter, color: str) -> None:
            values = {
                "mean": formatter(stats.mean),
                "min": formatter(stats.minimum),
                "p50": formatter(stats.p50),
                "p95": formatter(stats.p95),
                "max": formatter(stats.maximum),
                "cv": "—" if stats.cv is None else f"{stats.cv * 100:.1f}%",
            }
            table.add_row(Text(long if wide_labels else short, style=color), *[values[c] for c in cols])

        row("time to first token", "ttft", report.stat("ttft"), fmt_time, CYAN)
        if any(r.thinking_chars for r in report.ok_runs):
            row("time to first content", "ttf content", report.stat("ttfc"), fmt_time, CYAN)
        row("decode speed (tok/s)", "decode t/s", report.stat("decode_tps"), fmt_tps, GREEN)
        row("end-to-end (tok/s)", "e2e t/s", report.stat("e2e_tps"), fmt_tps, AMBER)
        if report.stat("prefill_tps").n:
            row("prefill speed (tok/s)", "prefill t/s", report.stat("prefill_tps"), fmt_tps, PINK)
        row("inter-token latency", "inter-token", report.itl_stats, fmt_ms, VIOLET)
        row("total request time", "total time", report.stat("total_wall"), fmt_time, BLUE)
        row("output tokens", "out tokens", report.stat("output_tokens"), fmt_int, TEXT)
        return table

    table = _pick(console, build, _AGG_VARIANTS, console.width - 4)

    return Panel(
        table,
        title=f"[{VIOLET}]aggregate[/]",
        title_align="left",
        border_style=FAINT,
        box=box.ROUNDED,
        padding=(0, 1),
    )


#: per-run column sets, richest first
_RUN_VARIANTS = [
    (["run", "ttft", "tok/s", "itl p50", "itl p95", "out", "total", "latency trace"],),
    (["run", "ttft", "tok/s", "itl p50", "itl p95", "out", "total"],),
    (["run", "ttft", "tok/s", "itl p50", "itl p95", "total"],),
    (["run", "ttft", "tok/s", "itl p50", "total"],),
    (["run", "ttft", "tok/s", "total"],),
]


def _runs_table(console: Console, report: Report) -> Panel:
    spark_width = max(14, console.width - 74)

    def build(cols):
        table = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, header_style=f"bold {DIM}")
        for col in cols:
            if col == "run":
                table.add_column("run", no_wrap=True, width=5)
            elif col == "latency trace":
                table.add_column(col, no_wrap=True, width=spark_width)
            else:
                table.add_column(col, justify="right", no_wrap=True)

        for run in report.runs:
            if run.warmup:
                continue
            if not run.ok:
                table.add_row(f"[{DIM}]{run.index}[/]", f"[{RED}]failed[/]", *([""] * (len(cols) - 2)))
                continue
            itls = run.itls
            values = {
                "run": f"[{DIM}]{run.index}[/]",
                "ttft": fmt_time(run.ttft),
                "tok/s": f"[{GREEN}]{fmt_tps(run.decode_tps)}[/]",
                "itl p50": fmt_ms(percentile(itls, 0.50)),
                "itl p95": fmt_ms(percentile(itls, 0.95)),
                "out": fmt_int(run.output_tokens),
                "total": fmt_time(run.total_wall),
                "latency trace": Text(
                    sparkline(itls, width=spark_width, cap=percentile(itls, 0.95)), style=INDIGO
                ),
            }
            table.add_row(*[values[c] for c in cols])
        return table

    table = _pick(console, build, _RUN_VARIANTS, console.width - 4)

    return Panel(
        table,
        title=f"[{CYAN}]per run[/]",
        title_align="left",
        border_style=FAINT,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _distribution_panel(console: Console, report: Report) -> Optional[Panel]:
    itls = [v for v in report.all_itls if v is not None]
    if len(itls) < 8:
        return None

    cutoff = percentile(itls, 0.99) or max(itls)
    lo = min(itls)
    hi = max(cutoff, lo + 1e-6)
    buckets = 8
    step = (hi - lo) / buckets
    counts = [0] * buckets
    outliers = 0
    for v in itls:
        if v > hi:
            outliers += 1
            continue
        idx = min(buckets - 1, int((v - lo) / step)) if step > 0 else 0
        counts[idx] += 1

    peak = max(counts) or 1
    label_width = 20
    count_width = max(4, len(str(max(max(counts), outliers))))
    width = max(12, console.width - (label_width + count_width + 10))

    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", style=DIM, no_wrap=True, width=label_width)
    grid.add_column(no_wrap=True, width=width)
    grid.add_column(justify="right", style=DIM, no_wrap=True, width=count_width)

    for i, count in enumerate(counts):
        start = (lo + i * step) * 1000
        end = (lo + (i + 1) * step) * 1000
        grid.add_row(
            f"{start:6.1f} – {end:6.1f} ms",
            bar_text(count, peak, width, heat_color(i / max(buckets - 1, 1))),
            str(count),
        )
    if outliers:
        grid.add_row(
            f"over {hi * 1000:6.1f} ms",
            bar_text(outliers, peak, width, RED),
            str(outliers),
        )

    stalls = [v for v in itls if v > (statistics.fmean(itls) * 5)]
    caption = Text(
        f"\n{len(itls)} gaps measured · {len(stalls)} stall(s) over 5× the mean",
        style=FAINT,
    )
    return Panel(
        Group(grid, caption),
        title=f"[{VIOLET}]inter-token latency distribution[/]",
        title_align="left",
        border_style=FAINT,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _breakdown_panel(console: Console, report: Report) -> Optional[Panel]:
    runs = [r for r in report.ok_runs if r.total_duration is not None]
    if not runs:
        return None

    def avg(fn):
        vals = [fn(r) for r in runs if fn(r) is not None]
        return statistics.fmean(vals) if vals else 0.0

    load = avg(lambda r: r.load_duration)
    prefill = avg(lambda r: r.prompt_eval_duration)
    decode = avg(lambda r: r.eval_duration)
    overhead = avg(lambda r: r.queue_overhead)
    total = load + prefill + decode + overhead
    if total <= 0:
        return None

    segments = [
        ("network + queue", overhead, PINK),
        ("model load", load, AMBER),
        ("prompt prefill", prefill, BLUE),
        ("token generation", decode, GREEN),
    ]

    width = max(20, console.width - 4)
    stacked = Text()
    used = 0
    for i, (_, value, color) in enumerate(segments):
        cells = width - used if i == len(segments) - 1 else int(round(value / total * width))
        cells = max(0, min(cells, width - used))
        stacked.append("█" * cells, style=color)
        used += cells

    legend = Table.grid(padding=(0, 2))
    legend.add_column(no_wrap=True)
    legend.add_column(justify="right", no_wrap=True)
    legend.add_column(justify="right", style=DIM, no_wrap=True)
    for label, value, color in segments:
        legend.add_row(
            Text("■ ", style=color) + Text(label, style=TEXT),
            fmt_time(value),
            f"{value / total * 100:4.1f}%",
        )

    return Panel(
        Group(stacked, Text(), legend),
        title=f"[{BLUE}]where the time goes[/]  [{FAINT}]mean per request[/]",
        title_align="left",
        border_style=FAINT,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _verdict_panel(cfg: BenchConfig, report: Report, width: int) -> Panel:
    label, ratio = report.stability
    decode = report.stat("decode_tps")
    ttft = report.stat("ttft")

    lines = Text()
    lines.append("model      ", style=DIM)
    lines.append(f"{cfg.model}\n", style=f"bold {TEXT}")
    lines.append("verdict    ", style=DIM)
    lines.append(
        f"{fmt_tps(decode.mean)} tok/s after {fmt_time(ttft.mean)}", style=f"bold {GREEN}"
    )
    lines.append("  ·  ", style=FAINT)
    lines.append(label, style=f"bold {heat_color(ratio)}")
    cv = decode.cv
    if cv is not None and width >= 76:
        lines.append(f" (±{cv * 100:.1f}% run to run)", style=DIM)
    lines.append("\n")

    lines.append("runs       ", style=DIM)
    lines.append(f"{len(report.ok_runs)} succeeded", style=GREEN)
    if report.failed_runs:
        lines.append(f", {len(report.failed_runs)} failed", style=RED)

    total_tokens = sum(r.output_tokens or 0 for r in report.ok_runs)
    lines.append(f"  ·  {total_tokens:,} tokens generated", style=DIM)

    return Panel(
        lines,
        border_style=GREEN if not report.failed_runs else AMBER,
        box=box.HEAVY,
        padding=(0, 2),
    )


def render_results(console: Console, cfg: BenchConfig, report: Report) -> None:
    console.print()
    console.rule(gradient_text(" R E S U L T S "), style=FAINT)
    console.print()

    if not report.ok_runs:
        console.print(
            Panel(
                Text(
                    "Every run failed — nothing to measure.\n"
                    + "\n".join(f"run {r.index}: {r.error}" for r in report.failed_runs[:5]),
                    style=RED,
                ),
                border_style=RED,
                box=box.HEAVY,
            )
        )
        return

    console.print(_hero_row(console, report))
    console.print()
    console.print(_aggregate_table(console, report))
    console.print(_runs_table(console, report))

    breakdown = _breakdown_panel(console, report)
    if breakdown:
        console.print(breakdown)

    distribution = _distribution_panel(console, report)
    if distribution:
        console.print(distribution)

    if report.failed_runs:
        errs = Text()
        for i, r in enumerate(report.failed_runs):
            if i:
                errs.append("\n")
            errs.append(f"run {r.index}: ", style=DIM)
            errs.append(str(r.error), style=RED)
        console.print(
            Panel(errs, title=f"[{RED}]failures[/]", title_align="left", border_style=RED, box=box.ROUNDED)
        )

    console.print(_verdict_panel(cfg, report, console.width))


def render_sample(console: Console, report: Report, limit: int = 400) -> None:
    sample = next((r.text for r in report.ok_runs if r.text), "")
    if not sample:
        return
    text = sample[:limit].strip()
    if len(sample) > limit:
        text += " …"
    console.print(
        Panel(
            Text(text, style=DIM),
            title=f"[{DIM}]sample output[/]",
            title_align="left",
            border_style=FAINT,
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )

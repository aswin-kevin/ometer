"""ometer — measure TTFT, throughput and latency of Ollama Cloud models.

Run it with `python3 ometer.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional, Sequence

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bench import Benchmark, BenchRunError
from client import OllamaClient, OllamaError
from config import (
    SUGGESTED_MODELS,
    BenchConfig,
    ConfigError,
    DEFAULT_PROMPT,
    load_env,
    resolve_api_key,
    resolve_host,
)
from metrics import Report
from theme import AMBER, CYAN, DIM, FAINT, GREEN, RED, TEXT
from ui import LiveDashboard, config_panel, print_banner, render_results, render_sample

__version__ = "1.0.0"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ometer",
        description="Measure TTFT, throughput and latency of Ollama Cloud models.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  ometer                                   ask for the model, then measure\n"
            "  ometer -m gpt-oss:120b-cloud -n 10       ten runs, no prompting\n"
            "  ometer -m kimi-k2:1t-cloud -t 512 --json out.json\n"
        ),
    )
    p.add_argument("-m", "--model", help="cloud model name (prompted for if omitted)")
    p.add_argument("-n", "--runs", type=int, default=5, help="measured runs (default: 5)")
    p.add_argument("-w", "--warmup", type=int, default=1, help="warmup runs, not scored (default: 1)")
    p.add_argument("-t", "--max-tokens", type=int, default=256, help="max tokens to generate (default: 256)")
    p.add_argument("-p", "--prompt", help="prompt text to send")
    p.add_argument("--prompt-file", help="read the prompt from a file")
    p.add_argument("--system", help="optional system message")
    p.add_argument("--temperature", type=float, default=0.0, help="sampling temperature (default: 0)")
    p.add_argument("--seed", type=int, default=42, help="sampling seed, use -1 for none (default: 42)")
    think = p.add_mutually_exclusive_group()
    think.add_argument("--think", dest="think", action="store_true", default=None, help="enable reasoning")
    think.add_argument("--no-think", dest="think", action="store_false", help="disable reasoning")
    p.add_argument("--host", help="API base URL (default: https://ollama.com)")
    p.add_argument("--env-file", help="path to a specific .env file")
    p.add_argument("--timeout", type=float, default=300.0, help="per-request timeout in seconds")
    p.add_argument("--cooldown", type=float, default=0.0, help="seconds to wait between runs")
    p.add_argument("--json", dest="json_out", metavar="PATH", help="write full results as JSON")
    p.add_argument("--show-sample", action="store_true", help="print a snippet of the model's output")
    p.add_argument("--no-banner", action="store_true", help="skip the logo")
    p.add_argument("--list-models", action="store_true", help="list models the key can reach, then exit")
    p.add_argument("--version", action="version", version=f"ometer {__version__}")
    return p


def error_panel(console: Console, title: str, message: str, hint: Optional[str] = None) -> None:
    body = Text(message, style=TEXT)
    if hint:
        body.append("\n\n")
        body.append(hint, style=AMBER)
    console.print(Panel(body, title=f"[{RED}]{title}[/]", title_align="left", border_style=RED, box=box.ROUNDED))


def discover_models(console: Console, client: OllamaClient) -> List[str]:
    with console.status(f"[{DIM}]looking up available models…[/]", spinner="dots"):
        try:
            return client.list_models()
        except OllamaError:
            return []


class ModelCompleter:
    """Tab-completion over the model list: prefix matches first, then substrings.

    So `gpt<Tab>` and `120b<Tab>` both reach gpt-oss:120b-cloud.
    """

    def __init__(self, options: Sequence[str]):
        self.options = list(options)
        self.matches: List[str] = []

    def __call__(self, text: str, state: int) -> Optional[str]:
        if state == 0:
            needle = text.strip().lower()
            if not needle:
                self.matches = list(self.options)
            else:
                starts = [o for o in self.options if o.lower().startswith(needle)]
                contains = [o for o in self.options if needle in o.lower() and o not in starts]
                self.matches = starts + contains
        return self.matches[state] if state < len(self.matches) else None


@contextmanager
def model_completion(options: Sequence[str]):
    """Bind Tab to complete model names, restoring readline's state afterwards."""
    try:
        import readline
    except ImportError:  # readline is optional on some builds
        yield False
        return

    previous_completer = readline.get_completer()
    previous_delims = readline.get_completer_delims()
    readline.set_completer(ModelCompleter(options))
    # model names contain ':', '-' and '/', all default delimiters — treat the
    # whole line as one word so completion sees the full fragment
    readline.set_completer_delims("")
    if "libedit" in (getattr(readline, "__doc__", "") or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    try:
        yield True
    finally:
        readline.set_completer(previous_completer)
        readline.set_completer_delims(previous_delims)


def _ansi(color: str, bold: bool = False) -> str:
    """SGR escape for a theme colour. Named colours fall back to plain grey."""
    prefix = "1;" if bold else ""
    if color.startswith("#") and len(color) == 7:
        red, green, blue = (int(color[i : i + 2], 16) for i in (1, 3, 5))
        return f"\033[{prefix}38;2;{red};{green};{blue}m"
    return f"\033[{prefix}90m"


def model_prompt(console: Console, default: Optional[str]) -> str:
    """Prompt text for input().

    Colour codes are wrapped in \\001..\\002 so readline excludes them when it
    measures the prompt width — without that the line is redrawn in the wrong
    place after a Tab.
    """
    if not console.is_terminal:
        return f"cloud model [{default}]: " if default else "cloud model: "

    def hidden(code: str) -> str:
        return f"\001{code}\002"

    reset = hidden("\033[0m")
    out = hidden(_ansi(CYAN, bold=True)) + "cloud model" + reset
    if default:
        out += " " + hidden(_ansi(FAINT)) + f"[{default}]" + reset
    return out + ": "


def choose_model(console: Console, client: OllamaClient) -> str:
    available = discover_models(console, client)
    options = available or SUGGESTED_MODELS
    heading = "models available to your key" if available else "common Ollama Cloud models"

    table = Table.grid(padding=(0, 2))
    table.add_column(style=CYAN, justify="right", no_wrap=True)
    table.add_column(style=TEXT)
    for i, name in enumerate(options[:14], start=1):
        table.add_row(str(i), name)

    console.print(
        Panel(
            table,
            title=f"[{CYAN}]{heading}[/]",
            title_align="left",
            subtitle=f"[{FAINT}]pick a number, or type a name and press Tab to complete[/]",
            subtitle_align="right",
            border_style=FAINT,
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )

    default = options[0] if options else None
    while True:
        with model_completion(options):
            answer = input(model_prompt(console, default)).strip()
        if not answer:
            if default:
                return default
            console.print(f"[{RED}]Please enter a model name.[/]")
            continue
        if answer.isdigit():
            idx = int(answer) - 1
            if 0 <= idx < len(options[:14]):
                return options[idx]
            console.print(f"[{RED}]No option {answer} in that list.[/]")
            continue
        return answer


def make_config(args: argparse.Namespace, model: str, host: str) -> BenchConfig:
    prompt = args.prompt or DEFAULT_PROMPT
    if args.prompt_file:
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
    return BenchConfig(
        model=model,
        host=host,
        runs=max(1, args.runs),
        warmup=max(0, args.warmup),
        prompt=prompt,
        system=args.system,
        max_tokens=max(1, args.max_tokens),
        temperature=args.temperature,
        seed=None if args.seed is not None and args.seed < 0 else args.seed,
        think=args.think,
        timeout=args.timeout,
        cooldown=max(0.0, args.cooldown),
    )


def write_json(path: str, cfg: BenchConfig, report: Report) -> None:
    payload = {
        "tool": "ometer",
        "version": __version__,
        "config": {
            "model": cfg.model,
            "host": cfg.host,
            "runs": cfg.runs,
            "warmup": cfg.warmup,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "seed": cfg.seed,
            "think": cfg.think,
            "prompt": cfg.prompt,
            "system": cfg.system,
        },
        "results": report.as_dict(),
    }
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()

    if not args.no_banner and not args.list_models:
        print_banner(console)

    try:
        env_files = load_env(args.env_file)
        api_key = resolve_api_key()
        host = resolve_host(args.host)
    except ConfigError as exc:
        error_panel(console, "configuration error", str(exc))
        return 1

    with OllamaClient(api_key, host, timeout=args.timeout) as client:
        if args.list_models:
            try:
                models = client.list_models()
            except OllamaError as exc:
                error_panel(console, "could not list models", str(exc), exc.hint)
                return 1
            if not models:
                console.print(f"[{DIM}]The endpoint returned no models.[/]")
            for name in models:
                console.print(f"  [{CYAN}]{name}[/]")
            return 0

        try:
            model = args.model or choose_model(console, client)
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{DIM}]cancelled[/]")
            return 130

        cfg = make_config(args, model, host)
        console.print()
        console.print(config_panel(cfg, api_key, env_files))
        console.print()

        # a tiny request first, so a bad model name fails in one second not one minute
        bench = Benchmark(client, cfg)
        try:
            with console.status(f"[{DIM}]checking {cfg.model}…[/]", spinner="dots"):
                bench.preflight()
        except BenchRunError as exc:
            error_panel(console, "preflight failed", str(exc.cause), exc.cause.hint)
            return 1
        except KeyboardInterrupt:
            console.print(f"\n[{DIM}]cancelled[/]")
            return 130
        console.print(f"[{GREEN}]✓[/] [{DIM}]{cfg.model} reachable — starting measurement[/]\n")

        report: Optional[Report] = None
        interrupted = False
        dashboard = LiveDashboard(console, cfg)
        bench.hook = dashboard.hook
        try:
            with dashboard:
                report = bench.run()
        except KeyboardInterrupt:
            interrupted = True
            report = Report(runs=[r for r in dashboard.completed])

    if report is None:
        return 1

    if interrupted:
        if not report.ok_runs:
            console.print(f"[{AMBER}]interrupted before any run completed — nothing to report[/]")
            return 130
        console.print(f"[{AMBER}]interrupted — showing the {len(report.ok_runs)} run(s) that finished[/]")

    render_results(console, cfg, report)

    if args.show_sample:
        render_sample(console, report)

    if args.json_out:
        write_json(args.json_out, cfg, report)
        console.print(f"[{DIM}]results written to[/] [{CYAN}]{args.json_out}[/]")
    elif report.ok_runs:
        console.print(f"[{FAINT}]tip: add --json results.json to keep the raw numbers[/]")

    if not report.ok_runs:
        return 2
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())

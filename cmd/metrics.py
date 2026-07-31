"""Per-run measurements and the statistics rolled up across runs."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

NS = 1e9  # Ollama reports durations in nanoseconds


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile, q in 0..1."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return vals[int(k)]
    return vals[lo] * (hi - k) + vals[hi] * (k - lo)


@dataclass
class Stats:
    """Distribution summary for one metric across runs."""

    n: int = 0
    mean: Optional[float] = None
    stdev: Optional[float] = None
    minimum: Optional[float] = None
    p50: Optional[float] = None
    p95: Optional[float] = None
    maximum: Optional[float] = None

    @property
    def cv(self) -> Optional[float]:
        """Coefficient of variation — how noisy the endpoint was."""
        if self.mean and self.stdev is not None and self.mean != 0:
            return self.stdev / self.mean
        return None

    def as_dict(self) -> Dict[str, Optional[float]]:
        return {
            "n": self.n,
            "mean": self.mean,
            "stdev": self.stdev,
            "min": self.minimum,
            "p50": self.p50,
            "p95": self.p95,
            "max": self.maximum,
            "cv": self.cv,
        }


def summarize(values: Sequence[Optional[float]]) -> Stats:
    vals = [v for v in values if v is not None]
    if not vals:
        return Stats()
    return Stats(
        n=len(vals),
        mean=statistics.fmean(vals),
        stdev=statistics.stdev(vals) if len(vals) > 1 else 0.0,
        minimum=min(vals),
        p50=percentile(vals, 0.50),
        p95=percentile(vals, 0.95),
        maximum=max(vals),
    )


@dataclass
class RunResult:
    """One request: wall-clock timings plus whatever the server reported."""

    index: int
    ok: bool = True
    error: Optional[str] = None

    # wall clock, seconds from just before the request was sent
    ttft: Optional[float] = None  # first chunk of any kind (incl. reasoning)
    ttfc: Optional[float] = None  # first *visible* content chunk
    last_chunk_at: Optional[float] = None
    total_wall: float = 0.0
    chunk_times: List[float] = field(default_factory=list)

    text: str = ""
    thinking_chars: int = 0
    warmup: bool = False

    # server-reported (nanoseconds -> seconds on ingest)
    total_duration: Optional[float] = None
    load_duration: Optional[float] = None
    prompt_eval_count: Optional[int] = None
    prompt_eval_duration: Optional[float] = None
    eval_count: Optional[int] = None
    eval_duration: Optional[float] = None

    # ---- derived -----------------------------------------------------------

    @property
    def itls(self) -> List[float]:
        """Inter-token latencies in seconds (gaps between streamed chunks)."""
        t = self.chunk_times
        return [b - a for a, b in zip(t, t[1:])] if len(t) > 1 else []

    @property
    def output_tokens(self) -> Optional[int]:
        if self.eval_count:
            return self.eval_count
        return len(self.chunk_times) or None

    @property
    def decode_tps(self) -> Optional[float]:
        """Generation speed, excluding the wait for the first token."""
        if self.eval_count and self.eval_duration and self.eval_duration > 0:
            return self.eval_count / self.eval_duration
        # wall-clock fallback for endpoints that omit the stats block
        if self.ttft is not None and self.last_chunk_at and len(self.chunk_times) > 1:
            span = self.last_chunk_at - self.ttft
            if span > 0:
                return (len(self.chunk_times) - 1) / span
        return None

    @property
    def e2e_tps(self) -> Optional[float]:
        """Tokens per second as the caller experiences it, TTFT included."""
        tokens = self.output_tokens
        if tokens and self.total_wall > 0:
            return tokens / self.total_wall
        return None

    @property
    def prefill_tps(self) -> Optional[float]:
        if self.prompt_eval_count and self.prompt_eval_duration and self.prompt_eval_duration > 0:
            return self.prompt_eval_count / self.prompt_eval_duration
        return None

    @property
    def queue_overhead(self) -> Optional[float]:
        """Wall time not accounted for by the server's own duration total."""
        if self.total_duration is None:
            return None
        return max(0.0, self.total_wall - self.total_duration)

    def ingest_done(self, raw: Dict) -> None:
        """Pull the final stats block off the terminating chunk."""

        def secs(key):
            v = raw.get(key)
            return v / NS if isinstance(v, (int, float)) else None

        self.total_duration = secs("total_duration")
        self.load_duration = secs("load_duration")
        self.prompt_eval_duration = secs("prompt_eval_duration")
        self.eval_duration = secs("eval_duration")
        self.prompt_eval_count = raw.get("prompt_eval_count")
        self.eval_count = raw.get("eval_count")

    def as_dict(self) -> Dict:
        itls = self.itls
        return {
            "run": self.index,
            "ok": self.ok,
            "error": self.error,
            "warmup": self.warmup,
            "ttft_s": self.ttft,
            "time_to_first_content_s": self.ttfc,
            "total_wall_s": self.total_wall,
            "decode_tps": self.decode_tps,
            "e2e_tps": self.e2e_tps,
            "prefill_tps": self.prefill_tps,
            "itl_mean_s": statistics.fmean(itls) if itls else None,
            "itl_p50_s": percentile(itls, 0.50),
            "itl_p95_s": percentile(itls, 0.95),
            "itl_p99_s": percentile(itls, 0.99),
            "itl_max_s": max(itls) if itls else None,
            "itl_stdev_s": statistics.stdev(itls) if len(itls) > 1 else None,
            "prompt_tokens": self.prompt_eval_count,
            "output_tokens": self.output_tokens,
            "chunks": len(self.chunk_times),
            "thinking_chars": self.thinking_chars,
            "response_chars": len(self.text),
            "server": {
                "total_duration_s": self.total_duration,
                "load_duration_s": self.load_duration,
                "prompt_eval_duration_s": self.prompt_eval_duration,
                "eval_duration_s": self.eval_duration,
            },
            "queue_overhead_s": self.queue_overhead,
        }


@dataclass
class Report:
    """All successful runs plus the aggregate view of them."""

    runs: List[RunResult]

    @property
    def ok_runs(self) -> List[RunResult]:
        return [r for r in self.runs if r.ok and not r.warmup]

    @property
    def failed_runs(self) -> List[RunResult]:
        return [r for r in self.runs if not r.ok and not r.warmup]

    @property
    def all_itls(self) -> List[float]:
        out: List[float] = []
        for r in self.ok_runs:
            out.extend(r.itls)
        return out

    def stat(self, name: str) -> Stats:
        getter = {
            "ttft": lambda r: r.ttft,
            "ttfc": lambda r: r.ttfc,
            "decode_tps": lambda r: r.decode_tps,
            "e2e_tps": lambda r: r.e2e_tps,
            "prefill_tps": lambda r: r.prefill_tps,
            "total_wall": lambda r: r.total_wall,
            "output_tokens": lambda r: r.output_tokens,
            "load": lambda r: r.load_duration,
            "overhead": lambda r: r.queue_overhead,
        }[name]
        return summarize([getter(r) for r in self.ok_runs])

    @property
    def itl_stats(self) -> Stats:
        return summarize(self.all_itls)

    @property
    def stability(self):
        """(label, color-ratio) describing how consistent decode speed was."""
        cv = self.stat("decode_tps").cv
        if cv is None:
            return ("unknown", 0.5)
        if cv < 0.05:
            return ("rock solid", 0.0)
        if cv < 0.12:
            return ("stable", 0.25)
        if cv < 0.25:
            return ("variable", 0.6)
        return ("erratic", 1.0)

    def as_dict(self) -> Dict:
        return {
            "runs": [r.as_dict() for r in self.runs],
            "aggregate": {
                name: self.stat(name).as_dict()
                for name in (
                    "ttft",
                    "ttfc",
                    "decode_tps",
                    "e2e_tps",
                    "prefill_tps",
                    "total_wall",
                    "output_tokens",
                    "load",
                    "overhead",
                )
            },
            "inter_token_latency": self.itl_stats.as_dict(),
            "stability": self.stability[0],
            "succeeded": len(self.ok_runs),
            "failed": len(self.failed_runs),
        }

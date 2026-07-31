"""Drives the requests and turns the stream into RunResults."""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from .client import Chunk, OllamaClient, OllamaError
from .config import BenchConfig
from .metrics import Report, RunResult

#: called as hook(event, run, chunk) where event is
#: "start" | "chunk" | "done" | "error"
Hook = Callable[[str, RunResult, Optional[Chunk]], None]


def _messages(cfg: BenchConfig) -> List[Dict[str, str]]:
    msgs: List[Dict[str, str]] = []
    if cfg.system:
        msgs.append({"role": "system", "content": cfg.system})
    msgs.append({"role": "user", "content": cfg.prompt})
    return msgs


class Benchmark:
    def __init__(self, client: OllamaClient, cfg: BenchConfig, hook: Optional[Hook] = None):
        self.client = client
        self.cfg = cfg
        self.hook = hook or (lambda *a: None)

    def _run_once(self, index: int, warmup: bool = False) -> RunResult:
        run = RunResult(index=index, warmup=warmup)
        parts: List[str] = []
        started = time.perf_counter()
        self.hook("start", run, None)

        try:
            for chunk in self.client.stream_chat(
                self.cfg.model,
                _messages(self.cfg),
                options=self.cfg.options(),
                think=self.cfg.think,
            ):
                if chunk.kind == "done":
                    run.ingest_done(chunk.raw)
                    break

                if run.ttft is None:
                    run.ttft = chunk.at
                run.last_chunk_at = chunk.at
                run.chunk_times.append(chunk.at)

                if chunk.kind == "thinking":
                    run.thinking_chars += len(chunk.text)
                else:
                    if run.ttfc is None:
                        run.ttfc = chunk.at
                    parts.append(chunk.text)

                self.hook("chunk", run, chunk)

            run.total_wall = time.perf_counter() - started
            run.text = "".join(parts)

            if not run.chunk_times:
                run.ok = False
                run.error = "stream produced no tokens"
                self.hook("error", run, None)
            else:
                self.hook("done", run, None)

        except OllamaError as exc:
            run.total_wall = time.perf_counter() - started
            run.ok = False
            run.error = str(exc)
            run.text = "".join(parts)
            self.hook("error", run, None)
            raise BenchRunError(run, exc) from exc

        return run

    def preflight(self) -> RunResult:
        """One tiny request to validate model + credentials before the real work."""
        saved_tokens = self.cfg.max_tokens
        self.cfg.max_tokens = min(8, saved_tokens)
        try:
            return self._run_once(0, warmup=True)
        finally:
            self.cfg.max_tokens = saved_tokens

    def run(self) -> Report:
        runs: List[RunResult] = []

        for i in range(self.cfg.warmup):
            try:
                runs.append(self._run_once(-(i + 1), warmup=True))
            except BenchRunError as exc:
                runs.append(exc.run)

        for i in range(self.cfg.runs):
            try:
                runs.append(self._run_once(i + 1))
            except BenchRunError as exc:
                runs.append(exc.run)
            if self.cfg.cooldown and i < self.cfg.runs - 1:
                time.sleep(self.cfg.cooldown)

        return Report(runs=runs)


class BenchRunError(Exception):
    """Wraps the partially-filled RunResult alongside the underlying error."""

    def __init__(self, run: RunResult, cause: OllamaError):
        super().__init__(str(cause))
        self.run = run
        self.cause = cause

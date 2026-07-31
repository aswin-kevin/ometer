"""Thin streaming client for the Ollama Cloud HTTP API."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import httpx


class OllamaError(Exception):
    """Any failure talking to the Ollama endpoint."""

    def __init__(self, message: str, status: Optional[int] = None, hint: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.hint = hint


@dataclass
class Chunk:
    """One streamed event, stamped with the offset since the request started."""

    kind: str  # "content" | "thinking" | "done"
    text: str
    at: float
    raw: Dict[str, Any]


def _hint_for(status: int, body: str) -> Optional[str]:
    if status in (401, 403):
        return "Check OLLAMA_API_KEY in your .env — it may be missing, expired or revoked."
    if status == 404:
        return (
            "That model was not found. Cloud model names usually end in '-cloud' "
            "(e.g. gpt-oss:120b-cloud). Run `ometer --list-models` to see what your key can reach."
        )
    if status == 402 or "quota" in body.lower() or "limit" in body.lower():
        return "Your account may be out of quota or rate limited."
    if status >= 500:
        return "Server-side error — retrying in a moment usually helps."
    return None


class OllamaClient:
    """Keeps one pooled connection open so TLS setup is not charged to TTFT."""

    def __init__(self, api_key: str, host: str, timeout: float = 300.0):
        self.host = host.rstrip("/")
        self._client = httpx.Client(
            base_url=self.host,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson",
                "User-Agent": "ometer/1.0",
            },
            timeout=httpx.Timeout(timeout, connect=20.0),
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- discovery ------------------------------------------------------------

    def list_models(self) -> List[str]:
        """Best effort model listing; returns [] when the endpoint has none."""
        try:
            r = self._client.get("/api/tags", timeout=20.0)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001 - discovery is optional
            raise OllamaError(f"Could not list models: {exc}") from exc
        names = [m.get("name") or m.get("model") for m in data.get("models", [])]
        return sorted(n for n in names if n)

    # -- streaming ------------------------------------------------------------

    def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, str]],
        options: Optional[Dict[str, Any]] = None,
        think: Optional[bool] = None,
    ) -> Iterator[Chunk]:
        """Yield Chunks as they arrive. `at` is seconds since just before send."""
        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if options:
            body["options"] = options
        if think is not None:
            body["think"] = think

        start = time.perf_counter()
        try:
            with self._client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code >= 400:
                    raw = response.read().decode("utf-8", errors="replace")
                    detail = raw.strip()
                    try:
                        detail = json.loads(raw).get("error", detail)
                    except Exception:  # noqa: BLE001 - body may not be json
                        pass
                    raise OllamaError(
                        f"HTTP {response.status_code}: {detail[:400]}",
                        status=response.status_code,
                        hint=_hint_for(response.status_code, raw),
                    )

                for line in response.iter_lines():
                    if not line or not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    now = time.perf_counter() - start

                    if obj.get("error"):
                        raise OllamaError(str(obj["error"]))

                    message = obj.get("message") or {}
                    thinking = message.get("thinking") or ""
                    content = message.get("content") or ""

                    if thinking:
                        yield Chunk("thinking", thinking, now, obj)
                    if content:
                        yield Chunk("content", content, now, obj)
                    if obj.get("done"):
                        yield Chunk("done", "", now, obj)
                        return
        except httpx.TimeoutException as exc:
            raise OllamaError(f"Request timed out: {exc}", hint="Raise --timeout or lower --max-tokens.") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Network error: {exc}") from exc

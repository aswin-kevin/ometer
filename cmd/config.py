"""Loading the API key / host out of .env and holding the run configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

DEFAULT_HOST = "https://ollama.com"

#: first match wins, so users can name the variable whatever they already use
KEY_VARS = (
    "OLLAMA_API_KEY",
    "OLLAMA_CLOUD_API_KEY",
    "OLLAMA_KEY",
    "OLLAMA_TOKEN",
    "OLLAMA_AUTH_TOKEN",
)
HOST_VARS = ("OLLAMA_HOST", "OLLAMA_CLOUD_HOST", "OLLAMA_BASE_URL")

DEFAULT_PROMPT = (
    "Explain how TCP congestion control works, covering slow start, congestion "
    "avoidance, fast retransmit and fast recovery. Write flowing technical prose "
    "without bullet points or headings."
)

#: shown as suggestions when the user is asked which model to measure
SUGGESTED_MODELS = [
    "gpt-oss:20b-cloud",
    "gpt-oss:120b-cloud",
    "deepseek-v3.1:671b-cloud",
    "qwen3-coder:480b-cloud",
    "kimi-k2:1t-cloud",
    "glm-4.6:cloud",
    "minimax-m2:cloud",
]


class ConfigError(Exception):
    """Raised when the environment is not usable (missing key, etc.)."""


def load_env(explicit: Optional[str] = None) -> List[Path]:
    """Load .env files. Returns the paths that were actually read."""
    loaded: List[Path] = []
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    else:
        cwd = Path.cwd()
        candidates.extend([cwd / ".env"] + [p / ".env" for p in cwd.parents])
        # the directory ometer.py lives in, so it works from any cwd
        candidates.append(Path(__file__).resolve().parent / ".env")
        candidates.append(Path.home() / ".ometer.env")

    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        load_dotenv(path, override=False)
        loaded.append(path)
        if explicit:
            break
    return loaded


def _first_env(names) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def resolve_api_key() -> str:
    key = _first_env(KEY_VARS)
    if not key:
        raise ConfigError(
            "No Ollama API key found.\n"
            "Create a .env file next to your project with:\n\n"
            "    OLLAMA_API_KEY=your_key_here\n\n"
            "Get a key at https://ollama.com/settings/keys"
        )
    return key


def resolve_host(override: Optional[str] = None) -> str:
    host = override or _first_env(HOST_VARS) or DEFAULT_HOST
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    return host.rstrip("/")


def mask_key(key: str) -> str:
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}{'*' * 8}{key[-4:]}"


@dataclass
class BenchConfig:
    """Everything that defines one measurement session."""

    model: str
    host: str = DEFAULT_HOST
    runs: int = 5
    warmup: int = 1
    prompt: str = DEFAULT_PROMPT
    system: Optional[str] = None
    max_tokens: int = 256
    temperature: float = 0.0
    seed: Optional[int] = 42
    think: Optional[bool] = None
    timeout: float = 300.0
    cooldown: float = 0.0

    def options(self) -> dict:
        opts = {"num_predict": self.max_tokens, "temperature": self.temperature}
        if self.seed is not None:
            opts["seed"] = self.seed
        return opts

    def summary_rows(self):
        rows = [
            ("model", self.model),
            ("endpoint", f"{self.host}/api/chat"),
            ("runs", f"{self.runs}  (+{self.warmup} warmup)" if self.warmup else str(self.runs)),
            ("max tokens", str(self.max_tokens)),
            ("temperature", f"{self.temperature:g}"),
            ("seed", "none" if self.seed is None else str(self.seed)),
        ]
        if self.think is not None:
            rows.append(("thinking", "on" if self.think else "off"))
        return rows

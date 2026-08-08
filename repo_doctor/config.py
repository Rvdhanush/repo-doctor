"""Configuration loading.

Every setting has a default here, so runner.py works even with no settings.yaml
on disk. The YAML file overrides defaults key by key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class SandboxConfig:
    image: str = "repo-doctor-sandbox:0.1"
    dockerfile_dir: str = "sandbox"
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 512
    workdir: str = "/work"
    repo_dir: str = "/work/repo"


@dataclass
class TimeoutConfig:
    container_start: int = 120
    clone: int = 180
    install: int = 900
    import_check: int = 120
    probe: int = 60


@dataclass
class LoggingConfig:
    results_dir: str = "results"
    inline_head_lines: int = 200
    inline_tail_lines: int = 400


@dataclass
class LimitsConfig:
    # Unused until increment 2. Present so the config shape is stable.
    attempt_cap: int = 5


@dataclass
class LLMConfig:
    """Increment 1 onward. Providers are tried in order until one answers."""

    # Each entry: {name, base_url, api_key_env, model}. All are OpenAI-compatible,
    # so only these three fields differ between them.
    providers: list = field(default_factory=lambda: [
        {"name": "groq",
         "base_url": "https://api.groq.com/openai/v1",
         "api_key_env": "GROQ_API_KEY",
         "model": "llama-3.3-70b-versatile"},
        {"name": "cerebras",
         "base_url": "https://api.cerebras.ai/v1",
         "api_key_env": "CEREBRAS_API_KEY",
         "model": "gpt-oss-120b"},
    ])
    timeout: int = 90
    max_tokens: int = 700
    # Prompt-size guards. Independent of the log's own truncation: run.json keeps
    # a generous view for humans, the prompt gets a tight one to stay well inside
    # a free tier's tokens-per-minute ceiling.
    tail_lines: int = 120
    max_chars: int = 6000


@dataclass
class Config:
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    project_root: Path = field(default_factory=Path.cwd)

    @property
    def results_root(self) -> Path:
        return self.project_root / self.logging.results_dir

    @property
    def dockerfile_path(self) -> Path:
        return self.project_root / self.sandbox.dockerfile_dir


def _apply(section: Any, values: dict | None) -> None:
    """Overlay known keys from a YAML mapping onto a dataclass instance."""
    if not values:
        return
    for key, value in values.items():
        if hasattr(section, key):
            # Coerce to the declared default's type where it is unambiguous, so
            # `cpus: 2` in YAML and `cpus: "2"` both work.
            current = getattr(section, key)
            if isinstance(current, str) and not isinstance(value, str):
                value = str(value)
            elif isinstance(current, int) and not isinstance(current, bool) and isinstance(value, str):
                value = int(value)
            setattr(section, key, value)


def load_config(path: Path | str | None, project_root: Path | None = None) -> Config:
    """Load settings.yaml, falling back to defaults for anything absent."""
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent
    cfg = Config(project_root=root)

    if path is None:
        return cfg

    p = Path(path)
    if not p.is_absolute():
        p = root / p
    if not p.exists():
        # Not an error: defaults are a complete, working configuration.
        return cfg

    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    _apply(cfg.sandbox, raw.get("sandbox"))
    _apply(cfg.timeouts, raw.get("timeouts"))
    _apply(cfg.logging, raw.get("logging"))
    _apply(cfg.limits, raw.get("limits"))
    _apply(cfg.llm, raw.get("llm"))
    return cfg


def build_llm_client(cfg: Config, env: dict[str, str] | None = None):
    """Construct an LLMClient from config plus .env / process environment."""
    import os

    from .llm import LLMClient, Provider, load_dotenv

    resolved = dict(load_dotenv(cfg.project_root / ".env"))
    resolved.update({k: v for k, v in os.environ.items() if k.endswith("_API_KEY")})
    if env:
        resolved.update(env)

    providers = [
        Provider(
            name=entry.get("name", "?"),
            base_url=entry.get("base_url", ""),
            api_key_env=entry.get("api_key_env", ""),
            model=entry.get("model", ""),
            api_key=resolved.get(entry.get("api_key_env", ""), ""),
        )
        for entry in cfg.llm.providers
    ]
    return LLMClient(providers, timeout=cfg.llm.timeout)

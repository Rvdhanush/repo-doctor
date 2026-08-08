"""Minimal OpenAI-compatible LLM client with provider fallback.

Every provider we care about (Groq, Cerebras, Gemini's compat endpoint,
OpenRouter, GitHub Models) speaks the same /chat/completions shape, so one
client covers all of them: only base_url, api_key, and model change. Providers
are tried in order and we fall through on rate limits, quota errors, and
transient failures.

Deliberately built on urllib rather than an SDK: the host-side dependency list
stays at PyYAML, which matters because the whole point of this project is that
dependency installs are fragile.

Two hard-won details are encoded here:
  * A custom User-Agent is required. Cloudflare fronts several of these APIs and
    rejects the default "Python-urllib/x.y" agent with error 1010 -- a 403 that
    looks like an auth problem but is not.
  * Token usage is captured on every call, because increment 3's telemetry is
    supposed to report cost per attempt and there is no way to recover it later.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "repo-doctor/0.1"

# Status codes worth trying the next provider for. 402 is quota exhausted,
# 429 is rate limited, 5xx is the provider having a bad day.
_FALLBACK_STATUS = {402, 408, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """No provider could complete the request."""


@dataclass
class Provider:
    name: str
    base_url: str
    api_key_env: str
    model: str
    api_key: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass
class LLMResult:
    provider: str
    model: str
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_s: float = 0.0
    attempts: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_s": round(self.latency_s, 3),
            "attempts": self.attempts,
        }


def load_dotenv(path: Path | str = ".env") -> dict[str, str]:
    """Read a .env file. Tolerant of leading whitespace, blank lines, comments.

    Deliberately lenient: a leading space before a key is easy to introduce by
    hand and silently drops the variable under a stricter parser.
    """
    p = Path(path)
    if not p.exists():
        return {}
    env: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


class LLMClient:
    """Tries each configured provider in order until one answers."""

    def __init__(self, providers: list[Provider], timeout: int = 90):
        self.providers = providers
        self.timeout = timeout

    @property
    def configured_providers(self) -> list[Provider]:
        return [p for p in self.providers if p.configured]

    def complete_json(self, system: str, user: str, max_tokens: int = 700,
                      temperature: float = 0.0) -> LLMResult:
        """Request a JSON object. Returns the raw content plus usage accounting."""
        if not self.configured_providers:
            raise LLMError(
                "No LLM provider is configured. Add a key to .env "
                "(see .env.example) — e.g. GROQ_API_KEY=..."
            )

        attempts: list[dict] = []
        started = time.monotonic()

        for provider in self.configured_providers:
            payload = {
                "model": provider.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            try:
                data = self._post(provider, payload)
            except urllib.error.HTTPError as exc:
                body = exc.read()[:300].decode("utf-8", errors="replace")
                attempts.append({"provider": provider.name, "status": exc.code,
                                 "error": body.strip()})
                if exc.code in _FALLBACK_STATUS:
                    continue  # try the next provider
                # 401/403/404 are configuration problems: the next provider has
                # its own key and may well work, so keep going either way.
                continue
            except Exception as exc:  # noqa: BLE001 - network layer
                attempts.append({"provider": provider.name, "status": None,
                                 "error": repr(exc)})
                continue

            usage = data.get("usage") or {}
            attempts.append({"provider": provider.name, "status": 200,
                             "total_tokens": usage.get("total_tokens", 0)})
            return LLMResult(
                provider=provider.name,
                model=provider.model,
                content=data["choices"][0]["message"]["content"],
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                latency_s=time.monotonic() - started,
                attempts=attempts,
            )

        detail = "; ".join(
            f"{a['provider']}: {a.get('status')} {a.get('error','')}".strip()
            for a in attempts
        )
        raise LLMError(f"Every provider failed. {detail}")

    def _post(self, provider: Provider, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{provider.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
                # Required: the default urllib agent is blocked by Cloudflare.
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a model reply.

    JSON mode usually returns clean JSON, but not every provider honours it, so
    fall back to locating the outermost braces rather than failing the run.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip ```json fences if present.
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError(f"No JSON object found in model reply: {text[:200]!r}")

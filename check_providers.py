#!/usr/bin/env python3
"""Check that each configured LLM provider actually answers. Ops utility.

Not part of any increment's pipeline -- reuses the exact same client code
path (`repo_doctor.config.build_llm_client`) that diagnose.py/fix_loop.py use,
so a pass here means diagnosis/--fix will actually reach that provider, not
just that a key is present in .env. Existing tools test the harness itself
(runner.py) or aggregate what already happened (telemetry.py); this checks
whether provider #2/#3 will be reachable BEFORE a real run needs the fallback.

Every configured provider is tested individually and independently -- unlike
a real diagnosis/fix call, which stops at the first success, this deliberately
keeps going so a working provider #1 never hides a broken provider #2.

Opt-in like --diagnose/--fix: does nothing unless you run it, and spends a
handful of tokens per configured provider to do it (one trivial prompt each).

    python check_providers.py
    python check_providers.py --json
"""

from __future__ import annotations

import argparse
import json
import sys

from repo_doctor.config import build_llm_client, load_config
from repo_doctor.llm import LLMClient, LLMError

_PROBE_SYSTEM = "Reply with a single JSON object and nothing else."
_PROBE_USER = 'Reply with exactly: {"ok": true}'


def check_provider(provider, timeout: int) -> dict:
    """Send one trivial prompt to exactly this provider. No fallback."""
    client = LLMClient([provider], timeout=timeout)
    result = {"provider": provider.name, "model": provider.model, "ok": False, "detail": ""}
    try:
        reply = client.complete_json(_PROBE_SYSTEM, _PROBE_USER, max_tokens=20)
        result["ok"] = True
        result["detail"] = f"{reply.total_tokens} tokens, {reply.latency_s:.2f}s"
    except LLMError as exc:
        # LLMError's message already carries the provider's own error body
        # (see LLMClient.complete_json) -- surface it, don't summarize it away.
        result["detail"] = str(exc).removeprefix("Every provider failed. ")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_providers.py",
        description="Send one trivial prompt to every LLM provider configured in "
                    ".env / the environment, individually, and report which actually work.",
    )
    parser.add_argument("--config", default="configs/settings.yaml",
                        help="path to settings.yaml (default: configs/settings.yaml)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    client = build_llm_client(cfg)
    configured = client.configured_providers

    if not configured:
        print("No LLM provider is configured. Add a key to .env (see .env.example).",
              file=sys.stderr)
        return 2

    results = [check_provider(p, cfg.llm.timeout) for p in configured]

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            mark = "OK  " if r["ok"] else "FAIL"
            print(f"[{mark}] {r['provider']:<10} {r['model']:<28} {r['detail']}")

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

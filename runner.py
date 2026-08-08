#!/usr/bin/env python3
"""repo-doctor runner — increment 0: the harness, with NO LLM.

Clones a GitHub repo into a Docker sandbox, attempts to install it, attempts a
basic import, and writes everything to a structured log. It does not diagnose and
it does not fix; those are increments 1 and 2.

    python runner.py https://github.com/owner/repo

Exit codes describe the TARGET repo, not the harness:
    0  the repo installed and imported
    1  the repo failed (install, import, clone, or unsupported layout)
    2  the harness itself failed (Docker unavailable, bad usage)
A failing target repo is a successful harness run; the two are never conflated.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from repo_doctor import logstore
from repo_doctor.config import Config, load_config
from repo_doctor.detect import detect_import_target, detect_install
from repo_doctor.logstore import RunLog
from repo_doctor.sandbox import (
    DockerNotAvailable,
    Sandbox,
    SandboxError,
    build_image,
    check_docker,
    image_exists,
)

# Only GitHub HTTPS URLs. An allowlist, not a denylist: it keeps the target
# surface to one known host and makes it impossible to smuggle git options or a
# local path through the URL argument.
_GITHUB_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


class UsageError(Exception):
    """Bad input from the operator (as distinct from a broken target repo)."""


def parse_github_url(url: str) -> tuple[str, str]:
    match = _GITHUB_URL.match(url.strip())
    if not match:
        raise UsageError(
            f"Not a supported GitHub URL: {url!r}\n"
            "Expected the form https://github.com/<owner>/<repo>"
        )
    owner, repo = match.group("owner"), match.group("repo")
    if owner in {".", ".."} or repo in {".", ".."}:
        raise UsageError(f"Not a supported GitHub URL: {url!r}")
    return owner, repo


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_image(cfg: Config, log: RunLog, force_build: bool) -> None:
    """Build the sandbox image if it is missing (or if --build was passed)."""
    image = cfg.sandbox.image
    if not force_build and image_exists(image):
        return

    print(f"[repo-doctor] building sandbox image {image} ...", flush=True)
    log.event("image_build_started", image=image)
    result = build_image(image, cfg.dockerfile_path)
    log.add_step("image_build", result, note=f"docker build -t {image}")
    if result.exit_code != 0:
        raise SandboxError(
            f"Failed to build the sandbox image {image}. "
            f"See results/{log.run_id}/logs/image_build.stderr.txt"
        )


def run(url: str, cfg: Config, run_id: str, force_build: bool,
        skip_import: bool) -> tuple[str, int]:
    """Execute the pipeline. Returns (status, process exit code)."""
    owner, repo = parse_github_url(url)

    log = RunLog(
        run_id=run_id,
        url=url,
        results_root=cfg.results_root,
        head_lines=cfg.logging.inline_head_lines,
        tail_lines=cfg.logging.inline_tail_lines,
    )
    log.set_target(owner=owner, repo=repo)
    log.event("run_started", url=url, owner=owner, repo=repo)
    print(f"[repo-doctor] run {run_id} -> {url}")
    print(f"[repo-doctor] logs: {log.dir}")

    sandbox: Sandbox | None = None
    try:
        check_docker()
        ensure_image(cfg, log, force_build)

        sandbox = Sandbox(cfg, run_id=run_id)
        sandbox.start()
        log.set_sandbox(sandbox.info)
        log.event("sandbox_started", container=sandbox.info.container_id)
        print(f"[repo-doctor] sandbox up: {sandbox.info.container_name} "
              f"({sandbox.info.python_version})")

        status, exit_code = _pipeline(sandbox, cfg, log, url, repo, skip_import)
        return status, exit_code

    except (DockerNotAvailable, SandboxError) as exc:
        # The harness broke, not the repo. Record it as such — never let a
        # harness failure masquerade as a verdict about the target repo.
        log.set_outcome(logstore.STATUS_HARNESS_ERROR, summary_line=str(exc))
        log.event("harness_error", error=str(exc))
        print(f"[repo-doctor] HARNESS ERROR: {exc}", file=sys.stderr)
        return logstore.STATUS_HARNESS_ERROR, 2

    except KeyboardInterrupt:
        log.set_outcome(logstore.STATUS_HARNESS_ERROR,
                        summary_line="Interrupted by the operator (Ctrl-C).")
        log.event("interrupted")
        print("\n[repo-doctor] interrupted; tearing down.", file=sys.stderr)
        return logstore.STATUS_HARNESS_ERROR, 2

    except Exception as exc:  # noqa: BLE001 - last resort, must still produce a log
        log.set_outcome(logstore.STATUS_HARNESS_ERROR,
                        summary_line=f"Unhandled harness exception: {exc!r}")
        log.event("harness_exception", traceback=traceback.format_exc())
        print(f"[repo-doctor] HARNESS EXCEPTION: {exc!r}", file=sys.stderr)
        traceback.print_exc()
        return logstore.STATUS_HARNESS_ERROR, 2

    finally:
        # Both of these must happen no matter how the run ended: no leaked
        # container, and no run without a record on disk.
        if sandbox is not None:
            sandbox.stop()
        try:
            path = log.write()
            print(f"[repo-doctor] wrote {path}")
        except Exception as exc:  # noqa: BLE001
            print(f"[repo-doctor] FAILED to write run.json: {exc!r}", file=sys.stderr)


def _pipeline(sandbox: Sandbox, cfg: Config, log: RunLog, url: str,
              repo: str, skip_import: bool) -> tuple[str, int]:
    """The fixed sequence of steps. Every step is recorded, reached or not."""
    repo_dir = cfg.sandbox.repo_dir

    # --- clone ---
    print("[repo-doctor] cloning ...", flush=True)
    clone = sandbox.exec(
        ["git", "clone", "--depth", "1", url, repo_dir],
        cwd=cfg.sandbox.workdir,
        timeout=cfg.timeouts.clone,
        # A missing repo and a private repo look identical to git: both prompt for
        # credentials. Disable the prompt so we fail immediately instead of risking
        # a wait for input that can never arrive.
        env={"GIT_TERMINAL_PROMPT": "0"},
    )
    log.add_step("clone", clone)
    if not clone.ok:
        log.add_skipped_step("detect_install", "clone failed")
        log.add_skipped_step("install", "clone failed")
        log.add_skipped_step("import_check", "clone failed")
        summary = _first_error_line(clone.stderr) or "git clone failed"
        log.set_outcome(logstore.STATUS_CLONE_FAILED, failing_step="clone",
                        exit_code=clone.exit_code, summary_line=summary)
        print(f"[repo-doctor] clone FAILED: {summary}")
        return logstore.STATUS_CLONE_FAILED, 1

    # Record exactly which commit we are judging.
    head = sandbox.exec(["git", "rev-parse", "HEAD"], cwd=repo_dir, timeout=cfg.timeouts.probe)
    branch = sandbox.exec(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                          cwd=repo_dir, timeout=cfg.timeouts.probe)
    log.set_target(resolved_commit=head.stdout.strip() or None,
                   default_branch=branch.stdout.strip() or None)
    print(f"[repo-doctor] cloned at {head.stdout.strip()[:12]}")

    # --- detect install ---
    plan = detect_install(sandbox, repo_dir)
    # Capture what the repo actually declared. This is often the single most
    # informative artifact for diagnosis — pins, extra index URLs, and git
    # dependencies all live here — and it cannot be recovered after teardown.
    if plan.chosen:
        declared = sandbox.read_file(f"{repo_dir}/{plan.chosen}", max_bytes=20_000)
        if declared:
            log.set_declared_dependencies(plan.chosen, declared)
    log.set_install_detection(plan)
    print(f"[repo-doctor] install strategy: {plan.strategy}"
          + (f" ({plan.chosen})" if plan.chosen else ""))

    if not plan.supported:
        log.add_skipped_step("install", plan.reason)
        log.add_skipped_step("import_check", "no install was attempted")
        log.set_outcome(logstore.STATUS_UNSUPPORTED, failing_step="detect_install",
                        summary_line=plan.reason)
        print(f"[repo-doctor] UNSUPPORTED: {plan.reason}")
        return logstore.STATUS_UNSUPPORTED, 1

    # --- install ---
    print(f"[repo-doctor] installing (timeout {cfg.timeouts.install}s) ...", flush=True)
    started = time.monotonic()
    install = sandbox.exec(plan.argv, cwd=repo_dir, timeout=cfg.timeouts.install)
    log.add_step("install", install, note=plan.reason)
    print(f"[repo-doctor] install exit={install.exit_code} "
          f"({install.duration_s:.1f}s, {time.monotonic() - started:.1f}s wall)")

    if not install.ok:
        log.add_skipped_step("import_check", "install failed")
        summary = _first_error_line(install.stderr) or _first_error_line(install.stdout) \
            or f"pip exited {install.exit_code}"
        if install.timed_out:
            summary = f"install exceeded the {cfg.timeouts.install}s timeout"
        log.set_outcome(logstore.STATUS_INSTALL_FAILED, failing_step="install",
                        exit_code=install.exit_code, summary_line=summary)
        print(f"[repo-doctor] install FAILED: {summary}")
        return logstore.STATUS_INSTALL_FAILED, 1

    # --- import check ---
    if skip_import:
        log.add_skipped_step("import_check", "--skip-import was passed")
        log.set_outcome(logstore.STATUS_OK, summary_line="Install succeeded; import check skipped.")
        return logstore.STATUS_OK, 0

    target = detect_import_target(
        sandbox,
        repo_dir,
        repo,
        # Only the project-install strategies produce a distribution we can
        # interrogate; a requirements.txt install installs dependencies, not this repo.
        installed_project=plan.strategy in {"pyproject", "setuptools"},
        timeout=cfg.timeouts.probe,
    )
    log.set_import_detection(target)
    print(f"[repo-doctor] import target: {target.module} "
          f"(confidence: {target.confidence}, via {target.source})")

    if target.module is None:
        log.add_skipped_step("import_check", target.reason)
        log.set_outcome(logstore.STATUS_IMPORT_FAILED, failing_step="import_check",
                        summary_line=f"Install succeeded but {target.reason}")
        print(f"[repo-doctor] import target undetermined: {target.reason}")
        return logstore.STATUS_IMPORT_FAILED, 1

    print(f"[repo-doctor] import check: import {target.module}", flush=True)
    # cwd="/" matters: run from anywhere BUT the repo directory. Inside the repo,
    # a source folder on sys.path would satisfy the import even when nothing was
    # actually installed, turning a real failure into a false pass.
    check = sandbox.exec(
        ["python", "-c", f"import {target.module}; print({target.module}.__file__)"],
        cwd="/",
        timeout=cfg.timeouts.import_check,
    )
    log.add_step("import_check", check, note=target.reason)

    if not check.ok:
        summary = _first_error_line(check.stderr) or f"import {target.module} exited {check.exit_code}"
        if target.confidence == "low":
            # Be explicit that the module name was a guess, so a downstream reader
            # does not treat a ModuleNotFoundError as a fact about the repo.
            summary += f" (module name was guessed via {target.source}; low confidence)"
        log.set_outcome(logstore.STATUS_IMPORT_FAILED, failing_step="import_check",
                        exit_code=check.exit_code, summary_line=summary)
        print(f"[repo-doctor] import FAILED: {summary}")
        return logstore.STATUS_IMPORT_FAILED, 1

    log.set_outcome(logstore.STATUS_OK,
                    summary_line=f"Installed via {plan.strategy}; `import {target.module}` succeeded.")
    print(f"[repo-doctor] OK: installed and imported {target.module}")
    return logstore.STATUS_OK, 0


def _first_error_line(text: str) -> str:
    """Pick the most informative single line for the summary.

    Prefers the LAST error-looking line: pip reports the actual cause at the end
    of its output, after the resolution log.
    """
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in reversed(lines):
        if line.upper().startswith("ERROR") or "Error:" in line or line.endswith("Error"):
            return line[:500]
    return lines[-1][:500] if lines else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="runner.py",
        description="Clone a GitHub repo into a Docker sandbox, attempt install + import, "
                    "and capture everything to a structured log. No LLM, no fixing.",
    )
    parser.add_argument("url", help="https://github.com/<owner>/<repo>")
    parser.add_argument("--config", default="configs/settings.yaml",
                        help="path to settings.yaml (default: configs/settings.yaml)")
    parser.add_argument("--run-id", default=None, help="override the generated run id")
    parser.add_argument("--build", action="store_true",
                        help="rebuild the sandbox image even if it already exists")
    parser.add_argument("--skip-import", action="store_true",
                        help="stop after install; do not attempt the import check")
    parser.add_argument("--diagnose", action="store_true",
                        help="on failure, ask an LLM what went wrong (increment 1). "
                             "Diagnoses only — never applies a fix.")
    args = parser.parse_args(argv)

    # Model output and captured logs routinely contain characters the Windows
    # console codepage cannot encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    project_root = Path(__file__).resolve().parent
    cfg = load_config(args.config, project_root=project_root)
    run_id = args.run_id or new_run_id()

    try:
        status, code = run(args.url, cfg, run_id, args.build, args.skip_import)
    except UsageError as exc:
        print(f"[repo-doctor] {exc}", file=sys.stderr)
        return 2

    print(f"[repo-doctor] status: {status}")

    if args.diagnose and status != logstore.STATUS_OK:
        _diagnose_after_run(cfg, run_id)

    return code


def _diagnose_after_run(cfg: Config, run_id: str) -> None:
    """Increment 1: explain the failure. Strictly read-only — no fix is applied."""
    # Imported here so increment 0's harness keeps working with no LLM
    # configured, and so a diagnosis failure can never fail the run itself.
    from diagnose import load_run, print_human, read_install_file
    from repo_doctor.config import build_llm_client
    from repo_doctor.diagnosis import diagnose_run
    from repo_doctor.llm import LLMError

    run_dir = cfg.results_root / run_id
    try:
        client = build_llm_client(cfg)
        if not client.configured_providers:
            print("[repo-doctor] --diagnose: no LLM provider configured "
                  "(see .env.example); skipping.", file=sys.stderr)
            return
        run_doc = load_run(run_dir)
        print()
        diag = diagnose_run(
            run_doc, client,
            install_file_content=read_install_file(run_doc, run_dir),
            tail_lines=cfg.llm.tail_lines,
            max_chars=cfg.llm.max_chars,
        )
        print_human(run_doc, diag)
        run_doc["diagnosis"] = diag.as_dict()
        (run_dir / "run.json").write_text(
            json.dumps(run_doc, indent=2, ensure_ascii=False), encoding="utf-8")
    except (LLMError, OSError, ValueError) as exc:
        # A failed diagnosis must not change the harness's verdict about the repo.
        print(f"[repo-doctor] --diagnose failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

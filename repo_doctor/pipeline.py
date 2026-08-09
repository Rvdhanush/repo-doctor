"""The install -> import sequence, shared by the initial run and the fix loop.

Extracted from runner.py so increment 2's fix loop (repo_doctor/fix_loop.py) can
re-run "detect install, install, detect import target, import check" after
applying a fix, without duplicating the sequence or re-cloning the repo. Cloning
stays in runner.py: it happens exactly once per run, fix or no fix.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from . import logstore
from .detect import detect_import_target, detect_install

if TYPE_CHECKING:
    from .config import Config
    from .logstore import RunLog
    from .sandbox import Sandbox


def first_error_line(text: str) -> str:
    """Pick the most informative single line for a summary.

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


def install_and_import(sandbox: "Sandbox", cfg: "Config", log: "RunLog",
                       repo_dir: str, repo: str, skip_import: bool,
                       step_prefix: str = "") -> tuple[str, int]:
    """Detect an install strategy, install, then detect and check an import.

    `step_prefix` namespaces step names in run.json (e.g. "attempt2_install")
    so the fix loop's repeated attempts stay distinguishable from the initial
    one and from each other, while the very first call (prefix "") keeps the
    step names increment 0/1 already produced.
    """
    plan = detect_install(sandbox, repo_dir)
    # Capture what the repo currently declares. Re-recorded on every attempt so
    # this always reflects the latest state, including any fix that rewrote it;
    # the fix loop's own attempt history is what preserves prior versions.
    if plan.chosen:
        declared = sandbox.read_file(f"{repo_dir}/{plan.chosen}", max_bytes=20_000)
        if declared:
            log.set_declared_dependencies(plan.chosen, declared)
    log.set_install_detection(plan)
    print(f"[repo-doctor] install strategy: {plan.strategy}"
          + (f" ({plan.chosen})" if plan.chosen else ""))

    if not plan.supported:
        log.add_skipped_step(f"{step_prefix}install", plan.reason)
        log.add_skipped_step(f"{step_prefix}import_check", "no install was attempted")
        log.set_outcome(logstore.STATUS_UNSUPPORTED, failing_step="detect_install",
                        summary_line=plan.reason)
        print(f"[repo-doctor] UNSUPPORTED: {plan.reason}")
        return logstore.STATUS_UNSUPPORTED, 1

    # --- install ---
    print(f"[repo-doctor] installing (timeout {cfg.timeouts.install}s) ...", flush=True)
    started = time.monotonic()
    install = sandbox.exec(plan.argv, cwd=repo_dir, timeout=cfg.timeouts.install)
    log.add_step(f"{step_prefix}install", install, note=plan.reason)
    print(f"[repo-doctor] install exit={install.exit_code} "
          f"({install.duration_s:.1f}s, {time.monotonic() - started:.1f}s wall)")

    if not install.ok:
        log.add_skipped_step(f"{step_prefix}import_check", "install failed")
        summary = first_error_line(install.stderr) or first_error_line(install.stdout) \
            or f"pip exited {install.exit_code}"
        if install.timed_out:
            summary = f"install exceeded the {cfg.timeouts.install}s timeout"
        log.set_outcome(logstore.STATUS_INSTALL_FAILED, failing_step=f"{step_prefix}install",
                        exit_code=install.exit_code, summary_line=summary)
        print(f"[repo-doctor] install FAILED: {summary}")
        return logstore.STATUS_INSTALL_FAILED, 1

    # --- import check ---
    if skip_import:
        log.add_skipped_step(f"{step_prefix}import_check", "--skip-import was passed")
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
        log.add_skipped_step(f"{step_prefix}import_check", target.reason)
        log.set_outcome(logstore.STATUS_IMPORT_FAILED, failing_step=f"{step_prefix}import_check",
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
    log.add_step(f"{step_prefix}import_check", check, note=target.reason)

    if not check.ok:
        summary = first_error_line(check.stderr) or f"import {target.module} exited {check.exit_code}"
        if target.confidence == "low":
            # Be explicit that the module name was a guess, so a downstream reader
            # does not treat a ModuleNotFoundError as a fact about the repo.
            summary += f" (module name was guessed via {target.source}; low confidence)"
        log.set_outcome(logstore.STATUS_IMPORT_FAILED, failing_step=f"{step_prefix}import_check",
                        exit_code=check.exit_code, summary_line=summary)
        print(f"[repo-doctor] import FAILED: {summary}")
        return logstore.STATUS_IMPORT_FAILED, 1

    log.set_outcome(logstore.STATUS_OK,
                    summary_line=f"Installed via {plan.strategy}; `import {target.module}` succeeded.")
    print(f"[repo-doctor] OK: installed and imported {target.module}")
    return logstore.STATUS_OK, 0

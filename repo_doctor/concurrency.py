"""Bounds how many runs may hold Docker/host resources at once. Hardening pass.

runner.py is a CLI invocation, not a daemon -- there is no shared process to
hold an in-memory semaphore across concurrent `python runner.py` invocations,
so the slots are directories on disk. `Path.mkdir()` (no `exist_ok`) is atomic
on both POSIX and Windows, so it doubles as a portable mutex without `fcntl`
(POSIX-only) or `msvcrt` (Windows-only) -- consistent with this project's
"stdlib only" discipline (see requirements.txt).

Each container already carries its own memory/cpu/pids ceiling
(SandboxConfig, sandbox.py) so a single run cannot take the host down. This
guards the OTHER failure mode: N users each starting a run at once, each
individually well-behaved, collectively exhausting host disk/memory before
any single container ceiling is hit. A run that cannot get a slot waits
(the queue), it is not turned away.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

# A crashed run (killed -9, host reboot) leaves its slot directory behind
# with nothing left to release it. Reclaim after this long rather than
# requiring an OS-specific liveness check (no `psutil` dependency). Generous
# relative to the longest a run can legitimately hold a slot: install timeout
# (900s) * attempt_cap (5) is 4500s worst case for --fix alone.
_STALE_AFTER_S = 2 * 3600


def _slots_root() -> Path:
    root = Path(tempfile.gettempdir()) / "repo-doctor-slots"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _try_acquire(slot_dir: Path, run_id: str) -> bool:
    """Attempt to claim one slot directory. True if this call claimed it."""
    try:
        slot_dir.mkdir()
    except FileExistsError:
        info_path = slot_dir / "info.json"
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            age_s = time.time() - float(info.get("acquired_at", time.time()))
        except (OSError, ValueError, TypeError):
            # Unreadable info -- e.g. mid-write by another process -- treat as
            # live rather than risk stealing an active slot.
            return False
        if age_s <= _STALE_AFTER_S:
            return False
        # Stale: the run that held this either crashed or is stuck well past
        # any legitimate duration. Reclaim it.
        shutil.rmtree(slot_dir, ignore_errors=True)
        try:
            slot_dir.mkdir()
        except FileExistsError:
            return False  # lost the race to another waiter

    (slot_dir / "info.json").write_text(
        json.dumps({"run_id": run_id, "acquired_at": time.time()}), encoding="utf-8"
    )
    return True


@contextmanager
def acquire_slot(max_concurrent: int, run_id: str, *,
                 poll_s: float = 2.0,
                 on_wait: Callable[[int], None] | None = None) -> Iterator[None]:
    """Block until one of `max_concurrent` slots is free, then hold it.

    `max_concurrent <= 0` disables the guard entirely (yields immediately) --
    same "0 means unlimited" convention as `limits.token_budget`.
    """
    if max_concurrent <= 0:
        yield
        return

    root = _slots_root()
    held: Path | None = None
    announced = False
    while held is None:
        for index in range(max_concurrent):
            candidate = root / f"slot-{index}"
            if _try_acquire(candidate, run_id):
                held = candidate
                break
        if held is None:
            if not announced and on_wait is not None:
                on_wait(max_concurrent)
                announced = True
            time.sleep(poll_s)

    try:
        yield
    finally:
        shutil.rmtree(held, ignore_errors=True)

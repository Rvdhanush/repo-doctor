"""The Docker sandbox.

The one load-bearing rule of this project (SPEC.md section 4): target-repo code
runs inside the container, never on the host. This module is where that rule is
enforced, and the enforcement is mostly in the `docker run` flags:

  * No bind mounts, no volumes. Nothing on the host filesystem is reachable from
    inside the container. Output comes back over captured exec streams. Mounting
    the results directory in would have handed target-repo code a write path onto
    the host, which is exactly what the sandbox exists to prevent.
  * Memory / CPU / PID ceilings, so a runaway build cannot starve the host.
  * no-new-privileges.

Networking IS enabled, because cloning and pip both require it. That is an
accepted, necessary exposure rather than an oversight; the resource caps and the
disposable filesystem are the mitigation.

The container is long-lived (`sleep infinity`) and commands run against it via
`docker exec`. One-shot `docker run` per command would be simpler, but state
would not survive between commands, and increment 2's fix loop needs installed
packages to persist.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .config import Config


class DockerNotAvailable(RuntimeError):
    """Docker CLI missing, or the daemon is not running."""


class SandboxError(RuntimeError):
    """The sandbox itself failed (as distinct from the target repo failing)."""


@dataclass
class ExecResult:
    """One command executed inside the sandbox."""

    argv: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class SandboxInfo:
    image: str
    image_id: str = ""
    image_digest: str = ""
    container_id: str = ""
    container_name: str = ""
    python_version: str = ""
    pip_version: str = ""
    limits: dict = field(default_factory=dict)


def _run_host(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess:
    """Run a docker CLI command on the host.

    Note this runs the *docker client*, not target-repo code. Target-repo code
    only ever runs via Sandbox.exec, inside the container.
    """
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def check_docker() -> None:
    """Fail early and legibly if Docker is unusable."""
    try:
        proc = _run_host(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
    except FileNotFoundError as exc:
        raise DockerNotAvailable(
            "The `docker` command was not found on PATH. Install Docker Desktop."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerNotAvailable("`docker info` timed out; the daemon may be starting.") from exc

    # `docker info --format ...` exits 0 even when the daemon is unreachable: the
    # template simply renders empty and the real error goes to stderr. Checking
    # the return code alone is not enough, so require actual output.
    server_version = (proc.stdout or "").strip()
    if proc.returncode != 0 or not server_version:
        detail = (proc.stderr or proc.stdout or "").strip() or "no server version reported"
        raise DockerNotAvailable(
            "Docker is installed but the daemon is not responding. "
            "Start Docker Desktop and try again.\n"
            f"docker info said: {detail}"
        )


def image_exists(image: str) -> bool:
    proc = _run_host(["docker", "image", "inspect", image], timeout=60)
    return proc.returncode == 0


def build_image(image: str, context_dir: Path, timeout: int = 1800) -> ExecResult:
    """Build the sandbox image. Returns the build as an ExecResult so it can be logged."""
    argv = ["docker", "build", "-t", image, str(context_dir)]
    started = time.monotonic()
    try:
        proc = _run_host(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ExecResult(argv, str(context_dir), -1, "", "docker build timed out",
                          time.monotonic() - started, timed_out=True)
    return ExecResult(
        argv=argv,
        cwd=str(context_dir),
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration_s=time.monotonic() - started,
    )


class Sandbox:
    """Context manager owning one disposable container for the duration of a run."""

    def __init__(self, config: Config, run_id: str | None = None):
        self.config = config
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.name = f"repo-doctor-{self.run_id}"
        self.info = SandboxInfo(image=config.sandbox.image)
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Sandbox":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Always tear down, including on Ctrl-C and on harness exceptions.
        self.stop()
        return False  # never swallow exceptions

    def start(self) -> None:
        sb = self.config.sandbox
        limits = {
            "memory": sb.memory,
            "cpus": sb.cpus,
            "pids_limit": sb.pids_limit,
            "network": "enabled (required for clone + pip)",
            "mounts": "none",
        }
        argv = [
            "docker", "run", "--detach",
            "--name", self.name,
            # --- isolation flags: see module docstring ---
            "--memory", sb.memory,
            "--memory-swap", sb.memory,      # equal to memory => no swap escape hatch
            "--cpus", str(sb.cpus),
            "--pids-limit", str(sb.pids_limit),
            "--security-opt", "no-new-privileges",
            # Deliberately NO -v / --mount of any kind.
            "--workdir", sb.workdir,
            sb.image,
            "sleep", "infinity",
        ]
        try:
            proc = _run_host(argv, timeout=self.config.timeouts.container_start)
        except subprocess.TimeoutExpired as exc:
            raise SandboxError("Timed out starting the sandbox container.") from exc

        if proc.returncode != 0:
            raise SandboxError(
                f"Could not start the sandbox container: {(proc.stderr or proc.stdout).strip()}"
            )

        self._started = True
        self.info.container_id = (proc.stdout or "").strip()[:12]
        self.info.container_name = self.name
        self.info.limits = limits
        self._record_image_identity()
        self._probe_interpreter()

    def stop(self) -> None:
        """Force-remove the container. Safe to call twice."""
        if not self._started:
            return
        self._started = False
        try:
            _run_host(["docker", "rm", "--force", self.name], timeout=60)
        except Exception:
            # Teardown must never mask the original failure. A leaked container
            # is visible in `docker ps -a`; a swallowed traceback is not.
            pass

    # -- introspection -----------------------------------------------------

    def _record_image_identity(self) -> None:
        """Record what image actually ran, for reproducibility."""
        proc = _run_host(
            ["docker", "image", "inspect", self.config.sandbox.image,
             "--format", "{{json .}}"],
            timeout=60,
        )
        if proc.returncode != 0:
            return
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return
        self.info.image_id = data.get("Id", "")
        digests = data.get("RepoDigests") or []
        # A locally built image has no RepoDigests; Id is the stable local identity.
        self.info.image_digest = digests[0] if digests else ""

    def _probe_interpreter(self) -> None:
        py = self.exec(["python", "--version"], timeout=self.config.timeouts.probe)
        pip = self.exec(["pip", "--version"], timeout=self.config.timeouts.probe)
        # python -V writes to stdout on 3.4+, but tolerate either stream.
        self.info.python_version = (py.stdout or py.stderr).strip()
        self.info.pip_version = (pip.stdout or pip.stderr).strip()

    # -- execution ---------------------------------------------------------

    def exec(
        self,
        argv: Sequence[str],
        cwd: str | None = None,
        timeout: int = 300,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run one command inside the container and capture it fully.

        `argv` is a list, never a shell string: no untrusted value (a repo URL,
        a package name) is ever interpolated into a shell.

        No TTY is allocated. A TTY would merge stdout into stderr and inject ANSI
        control characters, corrupting both the log and increment 1's LLM prompt.
        Without -t the two streams stay cleanly separated.
        """
        if not self._started:
            raise SandboxError("Sandbox is not running.")

        workdir = cwd or self.config.sandbox.workdir
        cmd = ["docker", "exec", "--workdir", workdir]
        for key, value in (env or {}).items():
            cmd += ["--env", f"{key}={value}"]
        cmd.append(self.name)
        cmd += list(argv)

        started = time.monotonic()
        try:
            proc = _run_host(cmd, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Partial output is still evidence — keep whatever was produced.
            return ExecResult(
                argv=list(argv),
                cwd=workdir,
                exit_code=124,  # conventional timeout code
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr) + f"\n[repo-doctor] command exceeded {timeout}s and was killed",
                duration_s=time.monotonic() - started,
                timed_out=True,
            )

        return ExecResult(
            argv=list(argv),
            cwd=workdir,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_s=time.monotonic() - started,
        )

    def path_exists(self, path: str) -> bool:
        return self.exec(["test", "-e", path], timeout=self.config.timeouts.probe).exit_code == 0

    def list_dir(self, path: str) -> list[str]:
        """Names of entries directly under `path` (empty list if unreadable)."""
        result = self.exec(["ls", "-A", "-1", path], timeout=self.config.timeouts.probe)
        if result.exit_code != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def read_file(self, path: str, max_bytes: int = 200_000) -> str | None:
        """Read a small text file from inside the container."""
        result = self.exec(["head", "-c", str(max_bytes), path],
                           timeout=self.config.timeouts.probe)
        if result.exit_code != 0:
            return None
        return result.stdout


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)

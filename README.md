# repo-doctor

An agent that takes a broken Python ML repository and gets it to **install and import
successfully inside a Docker sandbox** — diagnosing dependency, version, and environment
errors, applying fixes, verifying them, and reporting honestly on the repos it cannot fix.

> **Status: increment 0 of 5 complete.** The harness works — it clones, installs, imports, and
> captures everything to a structured log. There is **no LLM and no fixing logic yet**; those
> are increments 1 and 2. Everything below describes what actually runs today.

## Why

Cloning an ML repo and getting it to run is dependency hell: version conflicts, CPU/CUDA build
mismatches, missing system libraries. It is exactly the kind of work an LLM in a chat window
structurally cannot do — it can't clone, install, hit the real error, and iterate. This agent
acts in a real environment and reports what actually happened.

## What works today

```bash
$ python runner.py https://github.com/ageitgey/face_recognition

[repo-doctor] sandbox up: repo-doctor-20260808T164042Z (Python 3.11.15)
[repo-doctor] cloning ...
[repo-doctor] cloned at 9f3061aaeed9
[repo-doctor] install strategy: requirements.txt (requirements.txt)
[repo-doctor] installing (timeout 900s) ...
[repo-doctor] install exit=1 (50.2s)
[repo-doctor] install FAILED: ERROR: Could not build wheels for dlib, ...
[repo-doctor] status: install_failed
```

The real error is captured verbatim, not summarised:

```
subprocess.CalledProcessError: Command '['cmake', ...dlib.../tools/python', ...]'
    returned non-zero exit status 1.
  ERROR: Failed building wheel for dlib
ERROR: Could not build wheels for dlib, which is required to install pyproject.toml-based projects
```

Every run in [`results/`](results/) is committed as evidence, covering all five outcomes:

| Run | Repo | Outcome |
|---|---|---|
| `20260808T164042Z` | ageitgey/face_recognition | `install_failed` — dlib needs a compiler |
| `20260808T164211Z` | bojone/bert4keras | `ok` — installed and imported |
| `neg-unsupported` | karpathy/nanoGPT | `unsupported` — no install file exists |
| `neg-clonefail` | (nonexistent repo) | `clone_failed` |
| `test-docker-down` | — | `harness_error` — daemon not running |

## Quickstart

Requires Docker running and Python 3.11+.

```bash
pip install -r requirements.txt
python runner.py https://github.com/bojone/bert4keras
```

The sandbox image builds automatically on first run (~244 MB).

**In the cloud:** the repo ships a [devcontainer](.devcontainer/devcontainer.json) with
Docker-in-Docker, so *Code → Codespaces → Create codespace* gives you a working environment
with no local Docker at all.

## Design

```
sandbox/Dockerfile        base image the agent operates inside
runner.py                 pipeline: clone -> detect -> install -> import
repo_doctor/sandbox.py    container lifecycle + isolation
repo_doctor/detect.py     deterministic install-file and import-target detection
repo_doctor/logstore.py   structured run logs
results/<run_id>/         run.json, events.jsonl, raw logs
```

Three decisions carry the design:

**All target-repo code runs in the container, never on the host** — and there are **no bind
mounts**. Output returns over captured `docker exec` streams; mounting a host directory in
would hand target-repo code a write path onto the host. Memory, CPU, and PID ceilings mean a
runaway build can't take the machine with it.

**The base image is deliberately lean** — git and certs, no compilers. If it pre-installed
every system library, "pip install succeeded but import fails on a missing shared object"
could never reproduce, and the agent would have no real system-dependency failures to fix.
The `dlib` failure above is that decision working as intended.

**The container is long-lived and `exec`'d against**, so state survives between commands —
which is what the fix loop in increment 2 needs.

### The log is the product

`run.json` is what increment 1's LLM will read, so its shape matters more than anything else
here. Inline output truncates the **middle**, keeping pip's invocation context and its actual
error while dropping download noise; full untruncated output stays in `logs/`. Both the
container teardown and the log write happen in `finally` blocks — no run ends without a
record, and none leaks a container.

Import targets are resolved by asking the *installed distribution* what it provides, not by
guessing from the repo name, and each answer carries a confidence level. A
`ModuleNotFoundError` on a guessed name says nothing about the repo, and increment 1 needs to
know the difference.

## Scope

Success is **install + a basic import**. Not running the model, not the README's example, not
the test suite, not a web product. Python ML/data repos only. Owning one narrow domain
completely is what makes this shippable.

## Roadmap

- [x] **0 — Harness, no LLM.** Clone, install, import, structured logs.
- [ ] **1 — Diagnosis.** LLM turns a captured error into structured JSON.
- [ ] **2 — Fix loop.** Propose → apply → re-run → retry, capped at 5 attempts.
- [ ] **3 — Telemetry.** Every attempt, time, and token cost.
- [ ] **4 — Honest reporting.** Real diagnosis for repos it could not fix.

See [SPEC.md](SPEC.md) for the full brief.

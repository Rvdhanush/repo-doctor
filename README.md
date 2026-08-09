# repo-doctor

An agent that takes a broken Python ML repository and gets it to **install and import
successfully inside a Docker sandbox** — diagnosing dependency, version, and environment
errors, applying fixes, verifying them, and reporting honestly on the repos it cannot fix.

> **Status: increments 0-3 of 5 complete.** The harness clones, installs, imports, and captures
> everything to a structured log; an LLM turns a captured failure into a structured diagnosis;
> (opt-in, via `--fix`) a capped loop proposes a concrete fix, applies it inside the sandbox,
> and re-runs install + import, retrying up to 5 times; and `telemetry.py` aggregates every
> attempt, its time, and its LLM token cost across every run. Polished honest reporting is
> increment 4. Everything below describes what actually runs today.

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
| `e2e-deoldify-fix` | jantic/DeOldify `--fix` | `install_failed` — fix loop resolved the CUDA-index/stale-pin failure (scenario A), then hit a real packaging problem the repo itself has (no `setup.py`) and honestly gave up rather than guess further |

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

## Diagnosis

```bash
python diagnose.py results/<run_id>     # diagnose a captured run — no re-install
python runner.py <url> --diagnose       # run and diagnose in one pass
```

```
CATEGORY     missing_system_package   (confidence: high)
WHAT FAILED  The installation of the dlib package failed due to a missing build toolchain.
WHY          The image lacks a C/C++ compiler, cmake, and other essential build tools,
             which are necessary for compiling dlib from source since no prebuilt wheel
             is available for Python 3.11.
FIX CLASS    install a system package
cost         groq/llama-3.3-70b-versatile  3845 tokens  0.9s
```

Categories are a closed enum, not free text — the first version returned
`"Python Installation"`, which restates the symptom instead of diagnosing it. The prompt
carries the sandbox's actual configuration, because no model can infer "this image has no
compiler" from a stack trace. Grounding rules stop it inventing causes when a run produced
no output at all. Diagnoses run against *stored* logs, so iterating on the prompt costs
seconds rather than re-installing a repo.

Providers are OpenAI-compatible and tried in order, falling through on rate limits and
quota errors. Keys go in `.env` (gitignored); see `.env.example`.

## Fixing

```bash
python runner.py <url> --fix       # on failure: diagnose -> propose -> apply -> re-run, capped at 5
```

Opt-in, like `--diagnose` — the base `runner.py <url>` never spends a token or an extra install
cycle unless asked. Real run against `jantic/DeOldify`, which pins a CUDA build of torch the
sandbox can't satisfy:

```
[repo-doctor] install FAILED: ERROR: No matching distribution found for torch==1.11.0
[repo-doctor] fix attempt 1/5: diagnosing ...
[repo-doctor] fix attempt 1: category=python_version_incompatible (confidence: high)
[repo-doctor] fix attempt 1: proposing edit_dependency_file — remove the version pin so a
              cp311-compatible torch build can be selected
[repo-doctor] install exit=0 (477.5s wall)
[repo-doctor] import FAILED: ModuleNotFoundError: No module named 'deoldify'
[repo-doctor] fix attempt 2/5: diagnosing ...
[repo-doctor] fix attempt 2: category=missing_python_dependency (confidence: high)
...
[repo-doctor] fix attempt 3/5: proposing give_up — No file content was proposed.
[repo-doctor] fix loop stopping: model gave up
```

Attempt 1 fixed the real scenario-A failure. Attempt 3 shows the guardrails working, not
failing: the model's reply claimed `edit_dependency_file` but left `file_content` empty —
`repo_doctor/fix.py`'s `validate()` catches that and forces `give_up` rather than writing an
empty dependency file, and `run.json`'s `fix_loop.attempts[2].proposal.schema_valid` records
exactly why. DeOldify genuinely ships no `setup.py`, so nothing installs the `deoldify` package
itself — a real limitation of the repo, and the honest stop is the correct outcome (SPEC.md
section 6, Scenario E).

The action space proposing a fix can choose from is closed, same discipline as diagnosis's
category enum: `apt_install` (a missing system package/build tool), `edit_dependency_file` (the
model rewrites the complete content of the ONE dependency file it was shown — never a diff, and
never a path other than the one it was given), or `give_up`. Every attempt — diagnosis, proposal,
whether it applied, and the result — is recorded under `run.json`'s `fix_loop` key, so a run that
exhausts the cap still leaves a full, honest trail of what was tried.

## Telemetry

```bash
python telemetry.py                 # a table across every run under results/
python telemetry.py <run_id>        # just one run
python telemetry.py --json          # machine-readable
```

```
RUN ID            STATUS          DURATION  ATTEMPTS  LLM CALLS  TOKENS  STOPPED REASON
----------------  --------------  --------  --------  ---------  ------  --------------
20260808T164042Z  install_failed  58.9s     1         1          3845
20260808T164211Z  ok              25.0s     1         0          0
e2e-deoldify-fix  install_failed  528.4s    4         6          10294   give_up

7 run(s) — 1 clone_failed, 1 harness_error, 3 install_failed, 1 ok, 1 unsupported —
10 LLM call(s), 20066 token(s) total
```

Purely a read-only aggregator over `run.json` files already on disk — it never touches Docker or
an LLM, it just adds up what `runner.py`, `diagnose.py`, and the fix loop already recorded (one
LLM call per diagnosis and per fix proposal, each carrying its own token count and latency). Cost
is reported in tokens, not dollars: `repo_doctor/llm.py` never records a price, and inventing one
from an unconfigured rate would be exactly the kind of unearned precision the honesty requirement
(SPEC.md section 7) warns against. "Dashboard" means a CLI table here, not a web product — out of
scope for v1, and every other tool in this project is a CLI for the same reason.

## Design

```
sandbox/Dockerfile        base image the agent operates inside
runner.py                 CLI: clone -> install/import (-> diagnose) (-> fix loop)
repo_doctor/sandbox.py    container lifecycle + isolation + write_file/apt_install
repo_doctor/detect.py     deterministic install-file and import-target detection
repo_doctor/pipeline.py   install -> import, shared by the initial run and each fix attempt
repo_doctor/diagnosis.py  LLM: captured failure -> structured diagnosis (closed category enum)
repo_doctor/fix.py        LLM: diagnosis -> one concrete action (closed action enum)
repo_doctor/fix_loop.py   orchestrates diagnose -> propose -> apply -> re-run, capped at 5
repo_doctor/logstore.py   structured run logs
telemetry.py              CLI: aggregate every run's attempts/time/token cost into a table
repo_doctor/telemetry.py  read-only aggregation over run.json — no Docker, no LLM
results/<run_id>/         run.json, events.jsonl, raw logs
```

Three decisions carry the design:

**All target-repo code runs in the container, never on the host** — and there are **no bind
mounts**. Output returns over captured `docker exec` streams; mounting a host directory in
would hand target-repo code a write path onto the host. Memory, CPU, and PID ceilings mean a
runaway build can't take the machine with it. The fix loop's file edits and package installs go
through the same `docker exec` boundary (`Sandbox.write_file`/`apt_install`), never a shell
string built from LLM output — the same argv-list discipline as everything else in the sandbox.

**The base image is deliberately lean** — git and certs, no compilers. If it pre-installed
every system library, "pip install succeeded but import fails on a missing shared object"
could never reproduce, and the agent would have no real system-dependency failures to fix.
The `dlib` failure above is that decision working as intended, and now `apt_install` is how the
fix loop closes that gap at runtime instead of the image shipping it upfront.

**The container is long-lived and `exec`'d against**, so state survives between commands —
which is what the fix loop needs: each attempt's `pip install` builds on whatever the previous
attempt already changed, in the same filesystem, without re-cloning.

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
- [x] **1 — Diagnosis.** LLM turns a captured error into structured JSON.
- [x] **2 — Fix loop.** Propose → apply → re-run → retry, capped at 5 attempts.
- [x] **3 — Telemetry.** Every attempt, time, and token cost.
- [ ] **4 — Honest reporting.** Real diagnosis for repos it could not fix.

See [SPEC.md](SPEC.md) for the full brief.

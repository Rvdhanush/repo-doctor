# CLAUDE.md — repo-doctor

> Concise session context. The full brief is in **SPEC.md** — read it before any design or
> scope decision.

## What this is
An agent that gets a broken **Python ML/data repo** to **install + import successfully inside a
Docker sandbox**. It diagnoses dependency/version/CUDA/system errors, applies fixes, verifies
them, and reports honestly — including on repos it cannot fix.

## Scope discipline (do not cross)
- **Success = install succeeds AND a basic import works.** Nothing more.
- **NOT v1:** running the model/training, running the README example, passing tests, or a web
  product. Those are later phases. Do not build them now.
- **Domain: Python ML/data repos only.** Not JS, not arbitrary repos.

## Safety
- **All target-repo code runs inside the Docker sandbox — never on the host.** Cloning, install,
  and execution all happen in the container.

## Cost & control
- **Cap fix attempts (default 5).** Never loop unbounded.
- Track token/API cost per run; stop at a configurable ceiling. Start with a free/cheap model tier.

## Honesty
- **Never fake success.** A "could not fix" report must state the fixes tried, the specific
  blocker, and the human next step — as informative as a success report.

## Build order (one increment at a time; commit each)
1. Harness, **NO LLM** — clone into sandbox, attempt install, capture raw failure output.
2. Diagnosis only — LLM turns captured error into structured JSON (what/why/fix-category).
3. Fix loop (core) — propose -> apply in sandbox -> re-run -> read -> retry to cap.
4. Telemetry — every attempt, time, and token cost as a dashboard.
5. Honest reporting — final per-repo report, real diagnosis when unfixable.

## Conventions
- Python 3.11+. Structured JSON logs. Config-driven runs (attempt cap, base image, model).
- Test against the five scenarios in SPEC.md §6 (CUDA mismatch, version conflict, missing system
  package, stale dependency, genuinely unfixable).

## Commands
- Host deps: `pip install -r requirements.txt` (PyYAML only)
- Build sandbox: `docker build -t repo-doctor-sandbox:0.1 ./sandbox`
- Run on a repo: `python runner.py https://github.com/<owner>/<repo>`
  - `--build` rebuild the image first; `--skip-import` stop after install;
    `--run-id X` name the run; `--config` point at a different settings.yaml;
    `--diagnose` explain a failure (increment 1); `--fix` diagnose -> propose ->
    apply -> re-run, capped at 5 attempts (increment 2); `--report` print the
    final honest verdict after the run (increment 4). All three are opt-in —
    the base command never calls an LLM.
  - The image is built automatically if missing. Requires the Docker daemon running.
- Read a log: `results/<run_id>/run.json` (structured, UTF-8 — open with
  `encoding="utf-8"`), `events.jsonl` (live stream), `logs/*.txt` (raw, untruncated)
- Telemetry across all runs: `python telemetry.py` (table) / `--json`

## Exit codes
`0` repo installed + imported · `1` repo failed (clone/unsupported/install/import) ·
`2` the harness itself failed. A broken target repo is a *successful* harness run;
never conflate the two.

## Increment status
- [x] 0 — harness, no LLM. `runner.py` + `sandbox/` + structured logs.
- [x] 1 — diagnosis. `diagnose.py` + `repo_doctor/{llm,diagnosis}.py`. Diagnose only.
- [x] 2 — fix loop. `repo_doctor/{fix,fix_loop,pipeline}.py`, wired via `runner.py --fix`.
      Propose -> apply -> re-run -> retry, capped at `limits.attempt_cap` (default 5).
- [x] 3 — telemetry. `telemetry.py` + `repo_doctor/telemetry.py`. Read-only aggregation of
      every attempt/time/token cost across `results/` into a CLI table.
- [x] 4 — honest reporting. `report.py` + `repo_doctor/report.py`, wired via `runner.py
      --report`. Final per-repo verdict: fixed/not, every fix tried, cost, and — if still
      broken — the specific diagnosis and human next step. All 5 increments now shipped.

**Gate checks were human-judged** (see SPEC.md §5's "Gate checks" and "Where increment 1
actually landed"). Increment 2's gate — watch it fix a repo, and watch it fail one honestly —
cleared against `results/e2e-deoldify-fix`, `results/e2e-mimo-version-conflict`, and
`results/e2e-facerecognition-system-package` (the last one a full "watch it fix a repo" —
`FIXED` after 3 attempts, not just a partial fix).

Known gap carried forward from the increment-2 design notes: `Diagnosis.secondary_issues`
(problems the primary failure is currently masking, e.g. DeOldify's CUDA index waiting behind
its stale torch pin) is captured but not specially prioritized by `fix_loop.py` — each attempt
just re-diagnoses fresh, so a masked issue surfaces one attempt later rather than being
anticipated. Works fine within the attempt cap; would matter more with a tighter cap.

## Diagnosis (increment 1)
- `python diagnose.py results/<run_id>` — diagnose a run already captured (no re-install)
- `python diagnose.py --all --save` — every failed run, written back into run.json
- `python runner.py <url> --diagnose` — run and diagnose in one pass
- Keys live in `.env` (gitignored); `.env.example` is the template. Providers are
  tried in order and fall through on 402/429/5xx — all OpenAI-compatible, so only
  `base_url`/`model`/key differ.

### Calibration notes (learned the hard way — do not regress these)
- **Categories must stay a closed enum.** Free text produced "Python Installation",
  a restatement rather than a diagnosis.
- **The prompt must carry sandbox facts** (no compiler, CPU-only). The model cannot
  infer them from a traceback.
- **But facts about what the image lacks are not evidence of cause.** Without
  grounding rules the model invented a compiler failure for a repo where no install
  ever ran, and a CUDA index for a repo that has none. `no_install_file` and
  `repo_unavailable` exist so non-install failures have somewhere correct to go.
- **Prompt size is capped separately from the log.** `run.json` keeps a generous
  view for humans; the prompt gets ~120 tail lines / 6000 chars, because a free
  tier allows only ~12k tokens/minute.

## Fixing (increment 2)
- `python runner.py <url> --fix` — on an install/import failure: diagnose ->
  propose one action -> apply it in the sandbox -> re-run install + import ->
  retry, up to `limits.attempt_cap` (default 5). Opt-in, like `--diagnose`.
- The action space is a closed 3-way enum (`repo_doctor/fix.py`), same
  discipline as diagnosis's category enum: `apt_install` (system packages),
  `edit_dependency_file` (the model rewrites the COMPLETE content of the one
  dependency file it was shown — never a diff), `give_up`.
- `clone_failed`/`unsupported` never enter the loop — those aren't install/import
  problems this loop can act on, checked in `runner.py` before an LLM is even
  called. Diagnosis categories `unfixable`/`repo_unavailable`/`no_install_file`
  short-circuit the loop for the same reason, one level in.
- Every attempt (diagnosis + proposal + whether it applied + the result) is
  recorded under `run.json`'s `fix_loop` key — a run that hits the cap still
  leaves a complete, honest trail, which is what increment 4's reporting needs.

### Calibration notes (learned the hard way — do not regress these)
- **The action schema must stay closed, same as diagnosis's category enum.**
  A free-text "what command should I run" invites a shell pipeline built from
  model output; three fixed actions bound the blast radius to a package list,
  a full-file rewrite of one named file, or nothing.
- **An `edit_dependency_file` proposal must be rejected unless `file_path`
  exactly matches the one file the model was shown.** Nothing stops a model
  from naming a different path; `fix.py`'s `validate()` forces `give_up`
  instead of trusting an LLM-chosen write target.
- **A malformed reply must fail closed, not silently.** A real run against
  `jantic/DeOldify` had the model claim `edit_dependency_file` while leaving
  `file_content` empty; `validate()` catches that and forces `give_up` rather
  than writing an empty dependency file — see `results/e2e-deoldify-fix`.
- **`apt_install` package names are shape-checked, not trusted.** Anything
  that doesn't look like a real Debian package name is dropped before
  `apt-get` ever sees it, not executed and hoped for the best.
- **File writes into the sandbox never go through a shell.** `Sandbox.write_file`
  base64-encodes the content and decodes it with `python -c`, passed as argv —
  the same "argv is a list, never a shell string" rule `Sandbox.exec` already
  enforces, extended to LLM-authored file content.

## Telemetry (increment 3)
- `python telemetry.py` — table across every run under `results/`: status,
  duration, install attempts used, LLM calls, total tokens, fix-loop stop
  reason. `python telemetry.py <run_id>` for one run; `--json` for machine use.
- Strictly read-only aggregation over `run.json` files already on disk — no
  Docker, no LLM call, no re-diagnosis. Cost is reported in **tokens**, not
  dollars: `repo_doctor/llm.py` never records a price, so a $ figure would be
  invented, not measured — don't add one without a real per-provider rate table.
- "Dashboard" means the CLI table above, not a web product — CLAUDE.md's scope
  discipline rules that out for v1.

## Reporting (increment 4)
- `python report.py results/<run_id>` / `--all` / `--json` — the final,
  honest per-repo verdict: fixed or not, every fix attempted with what was
  diagnosed and tried, the cost, and — if still broken — the specific
  diagnosis and what a human needs to do. `runner.py <url> --fix --report`
  runs the two in one pass.
- Strictly read-only, same discipline as `telemetry.py`: makes no LLM call of
  its own. A run with no captured diagnosis (`--diagnose`/`--fix` never
  passed) gets an honest "No diagnosis was captured for this run" and names
  the command that would, rather than silently spending a token to fill the
  gap.
- `clone_failed` shows a diagnosis section if one was captured (it's a real
  verdict about the target repo); `harness_error` deliberately never does —
  there is nothing about the repo to explain when the harness itself broke.

## Running in GitHub Codespaces (cloud-first)
`.devcontainer/devcontainer.json` configures a Codespace with a real Docker daemon
(the `docker-in-docker` feature — without it the harness cannot start a sandbox).
- Launch: repo page -> Code -> Codespaces -> Create codespace.
- Claude Code is preinstalled there via a devcontainer feature; run `claude` and
  authenticate once per Codespace. The whole workflow can live in the cloud.
- API keys arrive as **Codespaces secrets**, not .env (.env is gitignored and never
  reaches the container). build_llm_client reads the process environment first.
- Free tier (GitHub Free): 120 core-hours/month (~60h on the default 2-core box) and
  15 GB-month storage.
- **Stop** a codespace to halt compute billing; **delete** it to stop storage billing.
  Stopping alone still consumes the storage allowance.
- Disk (32 GB default), not CPU, is the binding constraint: ML installs land in the
  sandbox container layer. `docker builder prune` reclaims space.

## Evidence on disk (`results/`, committed deliberately)
| run | repo | outcome | diagnosis / fix loop |
|---|---|---|---|
| `20260808T164042Z` | ageitgey/face_recognition | `install_failed` | `missing_system_package` (dlib needs cmake/g++) |
| `20260808T164211Z` | bojone/bert4keras | `ok` | — |
| `deoldify` | jantic/DeOldify | `install_failed` | `python_version_incompatible` + masked `cu113` CUDA index |
| `e2e-deoldify-fix` | jantic/DeOldify `--fix` | `install_failed` | attempt 1 fixed the CUDA/pin failure (scenario A); honest give-up on a real second problem (no `setup.py`) |
| `e2e-mimo-version-conflict` | menyifang/MIMO `--fix` | `install_failed` | attempt 1 fully resolved a real `version_conflict` (scenario B, 711.6s reinstall, exit 0); honest give-up on an unrelated real problem (no installable package) |
| `e2e-facerecognition-system-package` | ageitgey/face_recognition `--fix` | `ok` | **fully fixed** in 3 attempts (scenario C): `apt_install` got dlib compiling, `edit_dependency_file` then installed the package itself; 1803.9s / 6 LLM calls |
| `neg-unsupported` | karpathy/nanoGPT | `unsupported` | `no_install_file` |
| `neg-clonefail` | (nonexistent) | `clone_failed` | `repo_unavailable` |
| `test-docker-down` | — | `harness_error` | — |

Fast repos for iterating: **bert4keras** (~20s, passes), **DeOldify** (~15s, fails at
resolution), **face_recognition** (~50s, fails at build). Avoid stable-diffusion —
it downloads torch for 14 minutes. A live `--fix` run that actually reinstalls a heavy ML
stack (torch/transformers) after a successful edit can run 10-15 minutes on its own — run
those in the background, not inline.

## Gotchas already paid for (do not rediscover these)
- **`docker info --format` exits 0 when the daemon is down.** The template renders empty
  and the error goes to stderr. Check for actual output, not the return code.
- **Cloudflare rejects the default `Python-urllib` User-Agent** with error 1010 — a 403
  that looks exactly like an auth failure. `llm.py` sets its own UA.
- **Credentials collect invisible whitespace.** A Codespaces secret uploaded through a
  shell pipeline kept a trailing `\r`; `http.client` then raised "Invalid header value",
  naming neither the key nor the whitespace. `Provider.__post_init__` strips it now.
- **`run.json` is UTF-8; the Windows console is cp1252.** Read logs with
  `encoding="utf-8"`, and reconfigure stdout before printing model output.
- **No TTY on `docker exec`.** A TTY merges stdout into stderr and injects ANSI codes.
- **Import checks run from `cwd=/`**, never the repo dir — otherwise a source folder on
  `sys.path` lets an import "pass" for a package that never installed.
- **Cerebras currently returns 402** (no free quota on this account). Fallback path is
  wired and correct; everything runs on Groq today.

## Sandbox notes (do not undo casually)
- The base image is **deliberately lean** (git + ca-certificates + curl, no compilers).
  Adding build tooling here would make SPEC §6 Scenario C unreproducible.
- **No bind mounts.** Logs come back over captured `docker exec` streams. Mounting a
  host directory into the sandbox would give target-repo code a write path to the host.
- The container is long-lived (`sleep infinity`) and `exec`'d against, so state
  survives between commands — the fix loop (increment 2) depends on that: each
  attempt's install builds on whatever the previous attempt already changed.

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
    `--run-id X` name the run; `--config` point at a different settings.yaml
  - The image is built automatically if missing. Requires the Docker daemon running.
- Read a log: `results/<run_id>/run.json` (structured, UTF-8 — open with
  `encoding="utf-8"`), `events.jsonl` (live stream), `logs/*.txt` (raw, untruncated)

## Exit codes
`0` repo installed + imported · `1` repo failed (clone/unsupported/install/import) ·
`2` the harness itself failed. A broken target repo is a *successful* harness run;
never conflate the two.

## Increment status
- [x] 0 — harness, no LLM. `runner.py` + `sandbox/` + structured logs.
- [x] 1 — diagnosis. `diagnose.py` + `repo_doctor/{llm,diagnosis}.py`. Diagnose only.
- [ ] 2 — **NEXT.** Fix loop: propose -> apply in sandbox -> re-run -> read -> retry, cap 5.
- [ ] 3 — telemetry · [ ] 4 — honest reporting

**Gate checks are human-judged** (see SPEC.md §5). Increment 2's: watch it fix a repo the
user couldn't, *and* watch it fail one — the failure is data for increment 4, not a bug.
Present evidence in the form the gate asks for; don't start the next increment before it clears.

### Notes for increment 2
- `Sandbox.exec` already persists state between commands — that is why the container is
  long-lived. The fix loop applies changes and re-runs against the same container.
- Diagnoses carry `secondary_issues`: problems the primary failure is masking. DeOldify is
  the worked example — fix the `torch==1.11.0` pin and the `cu113` CUDA index bites next.
  A fix loop that ignores this will thrash.
- `confidence` matters. A `low`-confidence diagnosis on a guessed import name is not
  grounds for a fix.
- Cap is `limits.attempt_cap` in settings.yaml (default 5), already plumbed, unused so far.

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
| run | repo | outcome | diagnosis |
|---|---|---|---|
| `20260808T164042Z` | ageitgey/face_recognition | `install_failed` | `missing_system_package` (dlib needs cmake/g++) |
| `20260808T164211Z` | bojone/bert4keras | `ok` | — |
| `deoldify` | jantic/DeOldify | `install_failed` | `python_version_incompatible` + masked `cu113` CUDA index |
| `neg-unsupported` | karpathy/nanoGPT | `unsupported` | `no_install_file` |
| `neg-clonefail` | (nonexistent) | `clone_failed` | `repo_unavailable` |
| `test-docker-down` | — | `harness_error` | — |

Fast repos for iterating: **bert4keras** (~20s, passes), **DeOldify** (~15s, fails at
resolution), **face_recognition** (~50s, fails at build). Avoid stable-diffusion —
it downloads torch for 14 minutes.

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
  survives between commands — increment 2's fix loop depends on that.

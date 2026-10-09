# IssuePilot architecture

IssuePilot is a stateful agent, not a prompt wrapper. The model proposes; deterministic code
decides what is allowed, what is true, and when to stop.

```
          ┌──────────────────────── orchestrator (task lifecycle) ───────────────────────┐
 CLI ───▶ │ create_task ─▶ run_task ─▶ LangGraph workflow ─▶ outcome / status / approval │ ◀─── FastAPI + dashboard
          └──────┬──────────────────────────┬───────────────────────┬────────────────────┘
                 │                          │                       │
          TaskContext.tool()          MeteredProvider          TaskStore (SQLite)
       permission · budget · audit    tokens · $ · limits      tasks + event log
                 │                          │                       │
       ┌─────────┴────────┐          LLMProvider protocol     LangGraph checkpoints
       │ repo tools (read)│          └─ DeepSeekProvider      (same SQLite, thread = task id)
       │ sandbox (Docker) │
       │ GitHub (PR mode) │
       └──────────────────┘
```

## 1. Agent state and the workflow

The workflow is a LangGraph graph: `plan → gather → analyze → propose → verify → finalize`.
`propose → verify` loops back at most `max_attempts` times, each time with the concrete evidence
from the previous attempt (failing test output, "no test proves the fix", anchor not found...).

| Node | What happens | Model? |
|---|---|---|
| plan | structured `Plan` (Pydantic): summary, root-cause hypothesis, steps, risks | yes |
| gather | `search_repo` / `rank_files` / `read_file` (read-only, size-capped) | no |
| analyze | root cause with evidence and confidence (Observe mode ends here) | yes |
| propose | JSON edits: `{path, old, new}`; `old` must match exactly once | yes |
| verify | apply to a copy, run baseline and patched suite in the sandbox | no |
| finalize | outcome, diff, cost report | no |

Every model reply is parsed into a Pydantic model. Invalid JSON or an edit whose anchor text does
not exist is a *retry with feedback*, not a crash and not a success.

## 2. Persistence, checkpoints and recovery

- `persistence/tasks.py` stores one row per task (status, outcome, tokens, cost, tool calls,
  retries, elapsed, limits, approval, branch, PR URL) plus an append-only `events` table that
  powers the log in the dashboard.
- The LangGraph SQLite checkpointer is keyed by `thread_id = task_id`. After a crash,
  `issuepilot resume <id>` continues from the last finished node and `BudgetTracker` is seeded
  with the totals already spent, so a resume can never double the budget.
- On API start-up any task still marked `running` is moved to `interrupted`: its process died.
- Cancellation is a DB flag (`cancel_requested`). A watcher thread trips an in-process
  `CancelToken`; the sandbox kills the running container. Works across processes, for example
  dashboard button to CLI worker.

## 3. Tool permissions: three modes

Every tool call goes through `TaskContext.tool()`, which (1) checks the capability against the
mode, (2) charges the tool-call budget, (3) writes an audit event with duration and result.

| Capability | Observe | Develop | PR Agent |
|---|:-:|:-:|:-:|
| `read` (search, read files) | ✅ | ✅ | ✅ |
| `github_read` (issues, CI results) | ✅ | ✅ | ✅ |
| `sandbox` (edit a copy, run tests in Docker) | ❌ | ✅ | ✅ |
| `github_write` (branch, commit, push, draft PR) | ❌ | ❌ | ✅ after approval |

There is deliberately **no generic shell tool**. The model can only emit structured edits; the only
commands that ever execute are fixed ones (`pytest` in the sandbox, a handful of `git` calls built
by our code). Protected paths (`.git`, `.github`, CI files, `.env`, keys) and path traversal /
absolute paths are rejected in `apply_edits` before anything touches disk.

## 4. Isolation (Docker) and the disposable workspace

1. The repo is copied to a temp directory. **The original is never written to.**
2. Dependencies are installed by a separate, network-enabled `pip --target` step into a cache.
3. Tests run in a second container: `--network none`, non-root, `--cap-drop ALL`,
   `no-new-privileges`, pids/memory/CPU limits, tmpfs `/tmp`, **no environment variables**
   (no API key or token inside), dependencies mounted read-only.

Untrusted code (a repo's tests, plus whatever the model wrote) therefore cannot read secrets or
reach the network. A test proves this against real Docker. A `local` sandbox exists for tests and
development only and is marked unsafe.

## 5. Verification: "a patch exists" is not "it works"

Verification runs the suite on the original code (baseline) and on the patched workspace, then
the new tests on the original code again.

| Outcome | Meaning | Can open a PR? |
|---|---|:-:|
| `verified` | suite passes **and** at least one test fails before the fix and passes after | yes (with approval) |
| `unproven` | suite passes but nothing demonstrates the bug; the agent retries asking for a regression test | no |
| `tests_failed` | the patch breaks tests that passed, or its own tests fail | no |
| `failed` | no usable patch (bad JSON, anchors missing, protected path...) | no |
| `analysis_only` | Observe mode: diagnosis, nothing changed | no |
| `budget_exceeded` / `cancelled` | a hard limit tripped / the user cancelled | no |

The benchmark (see `docs/EVALUATION.md`) goes one step further and checks "verified" claims
against hidden oracle tests the agent never sees, so a false "verified" would be measured, not
assumed away.

## 6. Cost and hard limits

`BudgetTracker` enforces, per task: spend (default $0.10), wall-clock (600 s), tool calls (80),
edit attempts (3) and CI follow-up rounds (2). Before each model call `max_tokens` is capped from
the remaining spend (input cost estimated up front), so the limit holds *before* the money is
spent, not after. Prices are configurable, with DeepSeek peak/off-peak pricing built in. Exceeding
any limit ends the task as `budget_exceeded` with the reason recorded.

## 7. GitHub integration and the approval gate

```
verified (PR mode) ─▶ awaiting_approval ─▶ human clicks Approve ─▶ publish
                                              │
          clone ─▶ branch issuepilot/<id> ─▶ apply edits ─▶ re-verify in sandbox ─▶ commit
          (only edited paths staged) ─▶ push that branch ─▶ open DRAFT PR ─▶ read CI ─▶ ≤2 follow-ups
```

- The branch name must match `issuepilot/<id>`; pushing to the default branch, `--force` and
  merging are not implemented at all (`GitHubClient` has no merge method and sends `draft=true`).
- The token is passed to git through a `GIT_ASKPASS` helper and redacted from all logs.
- Minimum credentials: a fine-grained token scoped to **one repository** with Contents and
  Pull requests read/write (Actions read optional, for CI summaries).
- CI follow-ups are bounded by `max_ci_rounds`; a failing CI is summarised and fed back as new
  evidence rather than retried blindly.

## 8. Proactive discovery

`discover` is read-only (Observe mode). AST rules propose candidates (mutable default arguments,
bare `except`, `== None`, risky patterns), and an optional, metered LLM pass triages them into
`Finding`s with evidence. It reports; it never edits. On real repos it is conservative: it prefers
to reject a candidate over inventing a bug.

## 9. Provider abstraction

`LLMProvider` is a small `Protocol` (`complete(messages, json_mode, max_tokens) → LLMResponse`).
The agent only sees that protocol, so DeepSeek can be replaced by any provider, and tests inject a
scripted provider. `MeteredProvider` wraps any provider to add accounting and limits.

## 10. Interfaces

CLI (`plan`, `fix`, `discover`, `eval`, `tasks`, `task`, `approve`, `reject`, `cancel`, `resume`,
`publish`, `ci`) and a FastAPI service with a single-page dashboard (`/`): task list, cost / tool /
time bars, verification evidence, patch, log, and approve / reject / cancel / publish buttons.
The API has **no authentication** and a repo root allowlist; bind it to `127.0.0.1` only.

## Known limits

Python + pytest only; file selection is keyword-based; discovery is heuristic; the dashboard works
on local paths (no clone-by-URL yet); no authentication on the API.

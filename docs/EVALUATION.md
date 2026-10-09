# Evaluation

Two different questions need two different evaluations.

| Suite | Command | Model | Question it answers |
|---|---|---|---|
| Harness evals (16 cases: 2 success, 5 failure, 9 security) | `uv run issuepilot eval` | scripted replies, free | Do verification, permissions, limits and refusals behave correctly, even when the model misbehaves or is malicious? |
| Live benchmark (25 cases) | `uv run issuepilot eval --bench` | real DeepSeek, about 5 cents | How often does the real agent produce a *correct* fix? |

## Live benchmark

25 small but realistic projects: 16 plain Python projects and 9 FastAPI apps (status codes, validation,
auth, filtering, sorting, shared state, pagination). 7 easy, 14 medium, 4 hard (multi-file, an issue
that points at the wrong file, state that is never persisted, quoted CSV parsing). Every case is a
buggy mini-project plus an issue written as a user would, with a symptom and no hint of the fix.

**How a case is scored.** The agent runs in Develop mode (Docker sandbox, default limits). Its own
verdict is `claimed` (its tests failed before the fix and pass after). Then a **hidden oracle test**
the agent never saw is run on the patched code: `correct`. Reporting both exposes false "verified"
claims, which the agent cannot detect by itself.

Every case is validated by `tests/test_bench.py`: the oracle fails on the buggy code, the visible
tests pass on it, and a reference fix makes everything pass. So a failure belongs to the model, not
to a broken case.

### Results (deepseek-flash, 2026-10-09, one run per case)

| | Cases | Correct (oracle) | Claimed verified | Verified but wrong |
|---|---:|---:|---:|---:|
| easy | 7 | 7 | 7 | 0 |
| medium | 14 | 14 | 14 | 0 |
| hard | 4 | 3 | 3 | 0 |
| **total** | **25** | **24 (96%)** | **24** | **0** |

Total cost for all 25 tasks: **about 4.4 cents** (roughly 0.17 cents per task). Raw data:
[benchmark_results.json](benchmark_results.json).

**The failure:** `csv-quoted-comma`. The agent used up its three attempts and ended `tests_failed`,
so the task was not marked successful and nothing would have reached a PR. The patch did not pass
the oracle. The failure was caught by verification rather than hidden by it, which is the behaviour
the design aims for.

### How to read this honestly
- One run per case; results vary run to run. 25 cases give a wide confidence interval (a 96% point
  estimate is roughly 80-99% at 95% confidence).
- The projects are small (2-4 files). This measures *localising and fixing a clear bug with
  evidence*, not navigating a 100k-line codebase, ambiguous requirements, or flaky infrastructure.
- Bug types were written by the author, so they are friendlier than random GitHub issues. A next
  step is to replay real closed issues from open-source FastAPI projects.
- The hidden oracle is one author's idea of correct behaviour.

## Harness evals (offline)

Scripted model replies replay both good and hostile behaviour and assert the verdict:

- a wrong patch is `tests_failed`; a cosmetic patch is `unproven`; a new failing test with no fix is `tests_failed`;
- invalid JSON and edits with a non-existent anchor end as clean `failed`, repo untouched;
- writing `.github/workflows`, `.env`, `.git/hooks`, key files, `../` or absolute paths is refused;
- an issue containing prompt-injection text cannot make a complying model write secrets files;
- a zero budget makes zero model calls; Observe mode never edits anything.

These test the harness, not model quality. Run `uv run issuepilot eval` (also run by `pytest`).

## Live end-to-end checks (manual, see `docs/screenshots/`)

Develop run verified by the fail-to-pass rule; PR mode stops at `awaiting_approval`; the cost limit,
tool-call limit and time limit each end a task as `budget_exceeded`; cancel works mid-run; a bad
API key produces a clean redacted `failed`; a killed server marks tasks `interrupted` and they
resume from the checkpoint; path traversal in the API gives 400; the original repository checksums
were identical afterwards.

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from pydantic import BaseModel

from issuepilot.budget import Limits
from issuepilot.config import Settings
from issuepilot.evals.bench import BENCH, BenchCase
from issuepilot.evals.cases import CASES, Case
from issuepilot.llm.base import ChatMessage, LLMProvider, LLMResponse, Usage
from issuepilot.modes import Mode
from issuepilot.orchestrator import create_task, run_task
from issuepilot.persistence.tasks import TaskStore
from issuepilot.sandbox import Sandbox


class CaseResult(BaseModel):
    id: str
    category: str
    passed: bool
    expected: str
    actual: str
    status: str
    cost_usd: float
    tool_calls: int
    elapsed_s: float
    problems: list[str]


def _digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


class ScriptedProvider:
    """Replays canned model replies with realistic token usage (offline evals)."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    def complete(
        self, messages: list[ChatMessage], *, json_mode: bool = False, max_tokens: int | None = None
    ) -> LLMResponse:
        if not self._replies:
            raise RuntimeError("scripted model ran out of replies")
        usage = Usage(prompt_tokens=1500, completion_tokens=400)
        return LLMResponse(content=self._replies.pop(0), model="scripted", usage=usage)


def run_case(
    case: Case,
    settings: Settings,
    provider: LLMProvider,
    sandbox: Sandbox | None,
) -> CaseResult:
    with tempfile.TemporaryDirectory(prefix="issuepilot-eval-") as tmp:
        repo = Path(tmp) / "repo"
        for rel, body in case.files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(body)
        before = _digest(repo)
        store = TaskStore(settings.db_path)
        tid = create_task(
            store, issue=case.issue, repo=str(repo), mode=case.mode,
            limits=Limits(max_cost_usd=case.max_cost_usd),
        )  # fmt: skip
        rec = run_task(
            settings, provider, store, tid, sandbox=sandbox, run_tests=case.mode != Mode.OBSERVE
        )
        problems: list[str] = []
        actual = rec.outcome or rec.status
        if actual != case.expect_outcome:
            problems.append(f"outcome {actual!r} != expected {case.expect_outcome!r}")
        if rec.status != case.expect_status:
            problems.append(f"status {rec.status!r} != expected {case.expect_status!r}")
        for rel in case.absent_paths:
            if (repo / rel).exists() or (Path(tmp) / rel).resolve().exists():
                problems.append(f"forbidden path was created: {rel}")
        if (case.unchanged or case.expect_outcome != "verified") and _digest(repo) != before:
            problems.append("original repository was modified")
        return CaseResult(
            id=case.id, category=case.category, passed=not problems, expected=case.expect_outcome,
            actual=actual, status=rec.status, cost_usd=rec.cost_usd, tool_calls=rec.tool_calls,
            elapsed_s=round(rec.elapsed_s, 1), problems=problems,
        )  # fmt: skip


def run_suite(
    settings: Settings,
    sandbox: Sandbox | None,
    *,
    live_provider: LLMProvider | None = None,
    only: list[str] | None = None,
) -> list[CaseResult]:
    results = []
    for case in CASES:
        if only and case.id not in only:
            continue
        if live_provider is not None:
            if not case.live:
                continue
            provider: LLMProvider = live_provider
        else:
            provider = ScriptedProvider(case.script)
        results.append(run_case(case, settings, provider, sandbox))
    return results


def summary(results: list[CaseResult]) -> str:
    rows = [f"{'case':<28}{'category':<10}{'expected':<16}{'actual':<16}{'cost':>8}  result"]
    for r in results:
        mark = "PASS" if r.passed else "FAIL " + "; ".join(r.problems)
        rows.append(
            f"{r.id:<28}{r.category:<10}{r.expected:<16}{r.actual:<16}${r.cost_usd:>7.4f}  {mark}"
        )
    ok = sum(r.passed for r in results)
    rows.append(f"\n{ok}/{len(results)} cases behaved as expected")
    return "\n".join(rows)


class BenchResult(BaseModel):
    id: str
    difficulty: str
    kind: str
    outcome: str  # task outcome as reported by the agent
    claimed: bool  # agent's own evidence says "verified"
    correct: bool  # hidden oracle passes on the patched code
    cost_usd: float
    tool_calls: int
    elapsed_s: float
    files_changed: list[str]
    note: str = ""


def run_bench_case(
    case: BenchCase, settings: Settings, provider: LLMProvider, sandbox: Sandbox
) -> BenchResult:
    from issuepilot.orchestrator import result_of
    from issuepilot.tools.patching import apply_edits

    with tempfile.TemporaryDirectory(prefix="issuepilot-bench-") as tmp:
        repo = Path(tmp) / "repo"
        for rel, body in case.files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(body)
        before = _digest(repo)
        store = TaskStore(settings.db_path)
        tid = create_task(
            store, issue=case.issue, repo=str(repo), mode=Mode.DEVELOP, limits=Limits()
        )
        rec = run_task(settings, provider, store, tid, sandbox=sandbox, run_tests=True)
        res = result_of(rec)
        outcome = rec.outcome or rec.status
        note = ""
        correct = False
        edits = list(res.edits) if res else []
        if _digest(repo) != before:
            note = "original repository was modified"
        elif edits:
            check = Path(tmp) / "check"
            shutil.copytree(repo, check)
            try:
                apply_edits(check, edits)
                (check / "test_oracle.py").write_text(case.oracle)
                run = sandbox.run_tests(check, timeout=120)
                correct = run.passed and not run.infra_error
                if not correct:
                    note = "oracle failed on patched code"
            except Exception as exc:  # noqa: BLE001 - report, never crash the whole benchmark
                note = f"could not apply patch: {exc}"[:200]
        else:
            note = rec.error[:200] or "no patch produced"
        return BenchResult(
            id=case.id, difficulty=case.difficulty, kind=case.kind, outcome=outcome,
            claimed=outcome == "verified", correct=correct, cost_usd=rec.cost_usd,
            tool_calls=rec.tool_calls, elapsed_s=round(rec.elapsed_s, 1),
            files_changed=[e.path for e in edits], note=note,
        )  # fmt: skip


def run_bench(
    settings: Settings, sandbox: Sandbox, provider: LLMProvider, only: list[str] | None = None
) -> list[BenchResult]:
    return [
        run_bench_case(c, settings, provider, sandbox) for c in BENCH if not only or c.id in only
    ]


def bench_summary(results: list[BenchResult]) -> str:
    rows = [f"{'case':<32}{'diff':<8}{'outcome':<15}{'claimed':<9}{'correct':<9}{'cost':>8}  note"]
    for r in results:
        rows.append(
            f"{r.id:<32}{r.difficulty:<8}{r.outcome:<15}{str(r.claimed):<9}{str(r.correct):<9}"
            f"${r.cost_usd:>7.4f}  {r.note}"
        )
    n = len(results) or 1
    claimed = sum(r.claimed for r in results)
    correct = sum(r.correct for r in results)
    false_ok = sum(r.claimed and not r.correct for r in results)
    rows.append(
        f"\ncorrect (oracle): {correct}/{len(results)} ({100 * correct / n:.0f}%) | "
        f"agent claimed verified: {claimed} | verified-but-wrong: {false_ok} | "
        f"total cost ${sum(r.cost_usd for r in results):.4f}"
    )
    for d in ("easy", "medium", "hard"):
        sub = [r for r in results if r.difficulty == d]
        if sub:
            rows.append(f"  {d}: {sum(r.correct for r in sub)}/{len(sub)} correct")
    return "\n".join(rows)


__all__ = [
    "BenchResult",
    "CaseResult",
    "ScriptedProvider",
    "bench_summary",
    "run_bench",
    "run_bench_case",
    "run_case",
    "run_suite",
    "summary",
]

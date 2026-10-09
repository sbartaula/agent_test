"""Evidence-based verification. A patch is only 'verified' if tests prove it fixes something.

verified        suite passes AND >=1 test fails on the original code but passes with the fix
unproven        suite passes but nothing demonstrates the bug was fixed
tests_failed    the fix breaks tests that passed before (or new tests fail)
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from issuepilot.modes import Capability
from issuepilot.runtime import TaskContext
from issuepilot.sandbox import Sandbox
from issuepilot.tools import Edit, apply_edits, sandbox_copy
from issuepilot.tools.testrunner import TestResult

Outcome = Literal["verified", "unproven", "tests_failed", "infra_error"]


class Verification(BaseModel):
    outcome: Outcome
    detail: str = ""
    baseline_failed: list[str] = Field(default_factory=list)
    after_failed: list[str] = Field(default_factory=list)
    fail_to_pass: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    tests_run: int = 0
    output: str = ""


def is_test_path(rel: str) -> bool:
    p = PurePosixPath(rel)
    return p.name.startswith("test_") or p.name.endswith("_test.py") or "tests" in p.parts


def _run(ctx: TaskContext, sb: Sandbox, ws: Path, label: str) -> TestResult:
    timeout = min(180.0, max(5.0, ctx.tracker.remaining_seconds()))
    with ctx.tool("run_tests", Capability.SANDBOX, label=label, sandbox=sb.name):
        res = sb.run_tests(ws, timeout=timeout, cancel=ctx.tracker.cancel)
    ctx.check()  # surface cancellation/time-out that happened inside the container
    ctx.event(
        "test_result", label, passed=res.passed, num_passed=len(res.passed_tests),
        num_failed=len(res.failed_tests), duration_s=res.duration_s, timed_out=res.timed_out,
        infra_error=res.infra_error,
    )  # fmt: skip
    return res


def run_baseline(ctx: TaskContext, sb: Sandbox, root: Path) -> TestResult:
    with sandbox_copy(root) as ws:
        return _run(ctx, sb, ws, "baseline (original code)")


def verify_edits(
    ctx: TaskContext, sb: Sandbox, root: Path, edits: list[Edit], baseline: TestResult
) -> Verification:
    if baseline.infra_error:
        return Verification(outcome="infra_error", detail=baseline.infra_error)
    with sandbox_copy(root) as ws:
        apply_edits(ws, edits)
        after = _run(ctx, sb, ws, "after fix")
    if after.infra_error:
        return Verification(outcome="infra_error", detail=after.infra_error)
    v = Verification(
        outcome="unproven", baseline_failed=baseline.failed_tests,
        after_failed=after.failed_tests, output=after.output,
        tests_run=len(after.passed_tests) + len(after.failed_tests),
    )  # fmt: skip
    v.regressions = [t for t in after.failed_tests if t not in set(baseline.failed_tests)]
    if v.regressions or (not after.passed and not after.failed_tests):
        v.outcome = "tests_failed"
        v.detail = (
            f"{len(v.regressions)} test(s) fail with the patch" if v.regressions
            else "test run failed (collection error, timeout or crash)"
        )  # fmt: skip
        return v
    if not after.passed:
        v.outcome = "tests_failed"
        v.detail = "pre-existing failures remain: " + ", ".join(after.failed_tests[:5])
        return v

    # Proof: tests that fail on the original code but pass with the fix.
    candidates = set(baseline.failed_tests)
    test_edits = [e for e in edits if is_test_path(e.path)]
    if test_edits:
        with sandbox_copy(root) as ws:
            apply_edits(ws, test_edits)
            only_tests = _run(ctx, sb, ws, "new tests on original code")
        if only_tests.infra_error:
            return Verification(outcome="infra_error", detail=only_tests.infra_error)
        candidates |= set(only_tests.failed_tests)
    v.fail_to_pass = sorted(candidates & set(after.passed_tests))
    if v.fail_to_pass:
        v.outcome = "verified"
        v.detail = f"{len(v.fail_to_pass)} test(s) fail before the fix and pass after it"
    else:
        v.detail = "suite passes, but no test demonstrates the bug (none fail on original code)"
    return v

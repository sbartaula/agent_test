"""The benchmark must itself be trustworthy: every case is buggy, solvable, and oracle-checked."""

import shutil
from pathlib import Path

import pytest

from issuepilot.evals.bench import BENCH, BenchCase
from issuepilot.sandbox.local import LocalSandbox


def _materialize(case: BenchCase, root: Path) -> Path:
    for rel, body in case.files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    return root


def test_bench_size_and_variety() -> None:
    assert len(BENCH) >= 24
    assert len({c.id for c in BENCH}) == len(BENCH)
    assert {c.difficulty for c in BENCH} == {"easy", "medium", "hard"}
    assert {c.kind for c in BENCH} == {"python", "fastapi"}


@pytest.mark.parametrize("case", BENCH, ids=[c.id for c in BENCH])
def test_case_is_buggy_and_solvable(case: BenchCase, tmp_path: Path) -> None:
    sb = LocalSandbox()
    buggy = _materialize(case, tmp_path / "buggy")
    (buggy / "test_oracle.py").write_text(case.oracle)
    assert not sb.run_tests(buggy, timeout=60).passed, "oracle must fail on the buggy code"

    # the repo's own tests pass before the fix, except the oracle which we add last
    plain = tmp_path / "plain"
    shutil.copytree(buggy, plain)
    (plain / "test_oracle.py").unlink()
    assert sb.run_tests(plain, timeout=60).passed, "visible tests should pass on buggy code"

    fixed = tmp_path / "fixed"
    shutil.copytree(buggy, fixed)
    for rel, (old, new) in case.reference.items():
        text = (fixed / rel).read_text()
        assert text.count(old) == 1, f"{rel}: reference anchor must be unique"
        (fixed / rel).write_text(text.replace(old, new))
    res = sb.run_tests(fixed, timeout=60)
    assert res.passed, res.output[-800:]

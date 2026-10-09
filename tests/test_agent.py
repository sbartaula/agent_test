import json
from pathlib import Path

import pytest

from issuepilot.agent import run_agent
from issuepilot.config import Settings
from issuepilot.sandbox.local import LocalSandbox
from tests.conftest import FakeProvider

PLAN = json.dumps(
    {"summary": "fix add", "steps": [{"description": "fix", "files": ["calc.py"], "kind": "edit"}]}
)


def edits(new: str) -> str:
    return json.dumps(
        {"explanation": "fix", "edits": [{"path": "calc.py", "old": "a - b", "new": new}]}
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    return r


def test_unverified_without_tests(repo: Path, settings: Settings) -> None:
    res = run_agent(FakeProvider([PLAN, edits("a + b")]), settings, "add is wrong", repo)
    assert res.status == "unverified" and "+    return a + b" in res.diff
    assert res.prompt_tokens == 2000 and res.cost_usd > 0
    assert "a - b" in (repo / "calc.py").read_text()  # original untouched


def test_real_pytest_in_sandbox_verified(repo: Path, settings: Settings) -> None:
    res = run_agent(
        FakeProvider([PLAN, edits("a + b")]),
        settings,
        "add `add` wrong",
        repo,
        run_tests=True,
        sandbox=LocalSandbox(),
    )
    assert res.status == "verified", res.test_output
    assert res.verification and res.verification.fail_to_pass
    assert (Path(settings.checkpoint_path)).exists()


def test_retry_after_failing_tests(repo: Path, settings: Settings) -> None:
    p = FakeProvider([PLAN, edits("a * b"), edits("a + b")])
    res = run_agent(p, settings, "add is wrong", repo, run_tests=True, sandbox=LocalSandbox())
    assert res.status == "verified" and res.attempts == 2
    assert "That failed" in p.calls[2][-1].content  # feedback reached the model


def test_gives_up_after_max_attempts(repo: Path, settings: Settings) -> None:
    p = FakeProvider([PLAN, edits("a * b"), edits("a * b")])
    res = run_agent(p, settings, "x", repo, run_tests=True, sandbox=LocalSandbox())
    assert res.status == "tests_failed" and res.attempts == 2 and res.diff


def test_bad_edit_then_recovery(repo: Path, settings: Settings) -> None:
    bad = json.dumps({"edits": [{"path": "calc.py", "old": "zzz", "new": "q"}]})
    res = run_agent(FakeProvider([PLAN, bad, edits("a + b")]), settings, "x", repo)
    assert res.status == "unverified" and res.attempts == 2


def test_invalid_model_output_fails_cleanly(repo: Path, settings: Settings) -> None:
    res = run_agent(FakeProvider([PLAN, "junk", "junk"]), settings, "x", repo)
    assert res.status == "failed" and "no valid edits" in res.error


def test_context_falls_back_to_repo_files_when_plan_guesses_wrong(
    repo: Path, settings: Settings
) -> None:
    wrong = json.dumps({"summary": "s", "steps": [{"description": "d", "files": ["app/x.py"]}]})
    p = FakeProvider([wrong, edits("a + b")])
    run_agent(p, settings, "something vague", repo)
    user = p.calls[1][1].content
    assert "### calc.py" in user and "calc.py\ntest_calc.py" in user

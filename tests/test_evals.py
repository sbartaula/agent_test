import pytest

from issuepilot.config import Settings
from issuepilot.evals import CASES, run_case
from issuepilot.evals.runner import ScriptedProvider
from issuepilot.sandbox.local import LocalSandbox


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_eval_case(case, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    res = run_case(case, settings, ScriptedProvider(case.script), LocalSandbox())
    assert res.passed, res.problems


def test_suite_covers_all_categories() -> None:
    assert {c.category for c in CASES} == {"success", "failure", "security"}

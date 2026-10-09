import pytest

from issuepilot.llm.base import Usage
from issuepilot.planning import PlanningError, create_plan
from tests.conftest import VALID_PLAN, FakeProvider


def test_valid_plan() -> None:
    p = FakeProvider([VALID_PLAN])
    plan, usage = create_plan(p, "POST /items 500s")
    assert plan.steps[0].files == ["app/main.py"]
    assert usage == Usage(prompt_tokens=1000, completion_tokens=500)
    assert "POST /items 500s" in p.calls[0][1].content


def test_retries_on_invalid_json_and_sums_usage() -> None:
    p = FakeProvider(["not json", VALID_PLAN])
    plan, usage = create_plan(p, "x")
    assert plan.summary
    assert usage.total_tokens == 3000
    assert len(p.calls) == 2


def test_gives_up_after_max_attempts() -> None:
    with pytest.raises(PlanningError):
        create_plan(FakeProvider(["{}", "{}"]), "x")

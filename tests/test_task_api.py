from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from issuepilot.api.app import create_app
from issuepilot.config import Settings
from issuepilot.sandbox.local import LocalSandbox
from tests.conftest import FakeProvider

PLAN = json.dumps({"summary": "s", "steps": [{"description": "d", "files": ["calc.py"]}]})
FIX = json.dumps(
    {"explanation": "e", "edits": [{"path": "calc.py", "old": "a - b", "new": "a + b"}]}
)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    r = tmp_path / "root" / "repo"
    r.mkdir(parents=True)
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "test_calc.py").write_text(
        "from calc import add\n\ndef test_a():\n    assert add(1, 2) == 3\n"
    )
    return tmp_path / "root"


def client(settings: Settings, root: Path, responses: list[str]) -> TestClient:
    app = create_app(
        settings, lambda: FakeProvider(list(responses)), repos_root=root,
        sandbox_factory=LocalSandbox, inline=True,
    )  # fmt: skip
    return TestClient(app)


def test_task_lifecycle_and_events(settings: Settings, repo_root: Path) -> None:
    c = client(settings, repo_root, [PLAN, FIX])
    r = c.post("/tasks", json={"issue": "add is wrong", "repo_path": "repo", "mode": "develop"})
    assert r.status_code == 202
    tid = r.json()["id"]
    t = c.get(f"/tasks/{tid}").json()
    assert t["status"] == "completed" and t["outcome"] == "verified"
    assert t["result"]["verification"]["fail_to_pass"] and t["limits"]["max_cost_usd"] == 0.10
    ev = c.get(f"/tasks/{tid}/events").json()
    assert any(e["kind"] == "tool_end" for e in ev)
    assert c.get(f"/tasks/{tid}/events", params={"after": ev[-1]["id"]}).json() == []
    assert [x["id"] for x in c.get("/tasks").json()] == [tid]


def test_ui_served(settings: Settings, repo_root: Path) -> None:
    c = client(settings, repo_root, [])
    assert "IssuePilot" in c.get("/").text


def test_repo_path_confined_to_root(settings: Settings, repo_root: Path) -> None:
    c = client(settings, repo_root, [])
    for p in ("..", "../..", "/etc"):
        assert c.post("/tasks", json={"issue": "x", "repo_path": p}).status_code == 400


def test_limits_are_validated(settings: Settings, repo_root: Path) -> None:
    c = client(settings, repo_root, [])
    bad = {"issue": "x", "repo_path": "repo", "max_cost_usd": 1000}
    assert c.post("/tasks", json=bad).status_code == 422


def test_approval_gate_and_no_merge_endpoint(settings: Settings, repo_root: Path) -> None:
    c = client(settings, repo_root, [PLAN, FIX])
    tid = c.post("/tasks", json={"issue": "x", "repo_path": "repo", "mode": "pr"}).json()["id"]
    assert c.get(f"/tasks/{tid}").json()["status"] == "awaiting_approval"
    assert c.post(f"/tasks/{tid}/publish").status_code == 409  # not approved yet
    assert c.post(f"/tasks/{tid}/approve").json()["approval"] == "approved"
    assert c.post(f"/tasks/{tid}/reject").status_code == 200
    assert c.get(f"/tasks/{tid}").json()["status"] == "rejected"
    paths = [getattr(rt, "path", "") for rt in c.app.routes]  # type: ignore[attr-defined]
    assert not [p for p in paths if "merge" in p]


def test_develop_task_cannot_be_approved(settings: Settings, repo_root: Path) -> None:
    c = client(settings, repo_root, [PLAN, FIX])
    tid = c.post("/tasks", json={"issue": "x", "repo_path": "repo"}).json()["id"]
    assert c.post(f"/tasks/{tid}/approve").status_code == 409
    assert c.get("/tasks/nope").status_code == 404


def test_cancel_endpoint_sets_flag(settings: Settings, repo_root: Path) -> None:
    c = client(settings, repo_root, [PLAN, FIX])
    tid = c.post("/tasks", json={"issue": "x", "repo_path": "repo"}).json()["id"]
    assert c.post(f"/tasks/{tid}/cancel").json()["status"] == "cancel_requested"


def test_startup_marks_running_tasks_interrupted(settings: Settings, repo_root: Path) -> None:
    from issuepilot.persistence.tasks import TaskStore

    store = TaskStore(settings.db_path)
    tid = store.create(kind="fix", mode="develop", repo="r", issue="i", limits_json="{}")
    store.update(tid, status="running")
    c = client(settings, repo_root, [])
    assert c.get(f"/tasks/{tid}").json()["status"] == "interrupted"

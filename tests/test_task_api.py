from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from issuepilot.api.app import create_app
from issuepilot.budget import Limits
from issuepilot.config import Settings
from issuepilot.github import GitHubClient
from issuepilot.persistence.tasks import TaskStore
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
    html = c.get("/").text
    assert "IssuePilot" in html
    assert "Merge PR" in html and "confirm(" in html and "'merged'" in html


def test_repo_path_confined_to_root(settings: Settings, repo_root: Path) -> None:
    c = client(settings, repo_root, [])
    for p in ("..", "../..", "/etc"):
        assert c.post("/tasks", json={"issue": "x", "repo_path": p}).status_code == 400


def test_limits_are_validated(settings: Settings, repo_root: Path) -> None:
    c = client(settings, repo_root, [])
    bad = {"issue": "x", "repo_path": "repo", "max_cost_usd": 1000}
    assert c.post("/tasks", json=bad).status_code == 422


def test_approval_gate_and_merge_endpoint_requires_published_task(
    settings: Settings, repo_root: Path
) -> None:
    c = client(settings, repo_root, [PLAN, FIX])
    tid = c.post("/tasks", json={"issue": "x", "repo_path": "repo", "mode": "pr"}).json()["id"]
    assert c.get(f"/tasks/{tid}").json()["status"] == "awaiting_approval"
    assert c.post(f"/tasks/{tid}/publish").status_code == 409  # not approved yet
    assert c.post(f"/tasks/{tid}/approve").json()["approval"] == "approved"
    assert c.post(f"/tasks/{tid}/reject").status_code == 200
    assert c.get(f"/tasks/{tid}").json()["status"] == "rejected"
    paths = [getattr(rt, "path", "") for rt in c.app.routes]  # type: ignore[attr-defined]
    assert f"/tasks/{tid}/merge" not in paths
    assert "/tasks/{tid}/merge" in paths
    assert c.post(f"/tasks/{tid}/merge").status_code == 422
    assert c.post(f"/tasks/{tid}/merge", json={"confirm": True}).status_code == 409


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


def test_budget_exceeded_offers_and_applies_extension(settings: Settings, repo_root: Path) -> None:
    c = client(settings, repo_root, [PLAN, FIX])
    tid = c.post("/tasks", json={"issue": "bug", "repo_path": "repo", "max_tool_calls": 1}).json()[
        "id"
    ]
    t = c.get(f"/tasks/{tid}").json()
    assert t["status"] == "budget_exceeded" and t["budget_suggestion"]["extra_tool_calls"] == 79
    assert c.post(f"/tasks/{tid}/extend", json={}).status_code == 202
    t = c.get(f"/tasks/{tid}").json()
    assert t["limits"]["max_tool_calls"] == 80  # suggestion applied, hard cap still enforced
    assert c.post(f"/tasks/{tid}/extend", json={"extra_usd": 1}).status_code in (202, 409)


def test_extend_rejected_for_non_budget_task(settings: Settings, repo_root: Path) -> None:
    c = client(settings, repo_root, [PLAN, FIX])
    tid = c.post("/tasks", json={"issue": "bug", "repo_path": "repo"}).json()["id"]
    assert c.post(f"/tasks/{tid}/extend", json={"extra_usd": 1}).status_code == 409
    assert c.post("/tasks/nope/extend", json={}).status_code == 404


def test_dashboard_merge_requires_green_checks_and_records_completion(
    settings: Settings, repo_root: Path
) -> None:
    def github_response(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/repos/o/r":
            return httpx.Response(200, json={"default_branch": "main"})
        if req.url.path == "/repos/o/r/pulls/7" and req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "state": "open",
                    "merged": False,
                    "draft": True,
                    "head": {"ref": "issuepilot/task123", "sha": "head123"},
                    "base": {"ref": "main"},
                },
            )
        if req.url.path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {"id": 1, "name": "tests", "status": "completed", "conclusion": "success"}
                    ]
                },
            )
        if req.url.path.endswith("/status"):
            return httpx.Response(200, json={"state": "success", "statuses": []})
        if req.url.path == "/repos/o/r/pulls/7" and req.method == "PATCH":
            return httpx.Response(200, json={"draft": False})
        if req.url.path == "/repos/o/r/pulls/7/merge" and req.method == "PUT":
            assert json.loads(req.content)["sha"] == "head123"
            return httpx.Response(200, json={"merged": True, "message": "merged"})
        return httpx.Response(404, json={"message": "unexpected endpoint"})

    def gh() -> GitHubClient:
        return GitHubClient("test-token", transport=httpx.MockTransport(github_response))

    app = create_app(
        settings, lambda: FakeProvider([]), repos_root=repo_root, github_factory=gh, inline=True
    )
    store = TaskStore(settings.db_path)
    tid = store.create(
        kind="fix",
        mode="pr",
        repo=str(repo_root / "repo"),
        issue="fix it",
        limits_json=json.dumps(Limits().to_dict()),
        task_id="task123",
    )
    store.update(
        tid,
        status="pr_opened",
        approval="approved",
        outcome="verified",
        pr_number=7,
        branch="issuepilot/task123",
        pr_url="https://github.com/o/r/pull/7",
    )

    c = TestClient(app)
    denied = c.post(f"/tasks/{tid}/merge", json={"confirm": False})
    assert denied.status_code == 400
    response = c.post(f"/tasks/{tid}/merge", json={"confirm": True})
    assert response.status_code == 200
    task = response.json()
    assert task["status"] == "merged" and task["merge_sha"] == "head123"
    assert task["merged_at"]
    assert any(e["kind"] == "pr_merged" for e in c.get(f"/tasks/{tid}/events").json())

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from issuepilot.budget import Limits
from issuepilot.config import Settings
from issuepilot.github import GitError, GitHubClient, GitHubError, GitWorkspace, parse_slug
from issuepilot.modes import Mode
from issuepilot.orchestrator import create_task, run_task
from issuepilot.persistence.tasks import TaskStore
from issuepilot.publish import NotApproved, approve, ci_followup, merge_task, publish_task, reject
from issuepilot.sandbox.local import LocalSandbox
from issuepilot.tools.patching import Edit
from tests.conftest import FakeProvider

PLAN = json.dumps({"summary": "s", "steps": [{"description": "d", "files": ["calc.py"]}]})
FIX = json.dumps(
    {
        "explanation": "use +",
        "edits": [
            {"path": "calc.py", "old": "a - b", "new": "a + b"},
        ],
    }
)


def git(cwd: Path, *a: str) -> str:
    return subprocess.run(
        ["git", *a], cwd=cwd, check=True, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1"},
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A bare 'GitHub' remote seeded with a buggy repo on `main`."""
    work = tmp_path / "seed"
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "a@b.c")
    git(work, "config", "user.name", "t")
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_a():\n    assert add(1, 2) == 3\n"
    )
    git(work, "add", ".")
    git(work, "commit", "-m", "init")
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(work), str(bare))
    return bare


class FakeGitHub:
    """httpx MockTransport standing in for api.github.com; records every request."""

    def __init__(self, checks: list[dict] | None = None) -> None:
        self.requests: list[tuple[str, str]] = []
        self.checks = checks or []
        self.pr_body: dict = {}
        self.pr_is_draft = True
        self.merged = False
        self.branch = "issuepilot/task"

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append((req.method, req.url.path))
        if req.url.path == "/repos/o/r":
            return httpx.Response(200, json={"default_branch": "main"})
        if req.url.path == "/repos/o/r/pulls/7" and req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "state": "closed" if self.merged else "open",
                    "merged": self.merged,
                    "draft": self.pr_is_draft,
                    "head": {"ref": self.branch, "sha": "head-sha"},
                    "base": {"ref": "main"},
                },
            )
        if req.url.path == "/repos/o/r/pulls/7" and req.method == "PATCH":
            self.pr_is_draft = False
            return httpx.Response(200, json={"draft": False})
        if req.url.path == "/repos/o/r/pulls/7/merge" and req.method == "PUT":
            payload = json.loads(req.content)
            if payload["sha"] != "head-sha":
                return httpx.Response(409, json={"message": "head changed"})
            self.merged = True
            return httpx.Response(
                200, json={"merged": True, "message": "Pull Request successfully merged"}
            )
        if req.url.path == "/repos/o/r/pulls" and req.method == "POST":
            self.pr_body = json.loads(req.content)
            return httpx.Response(
                201,
                json={"html_url": "https://github.com/o/r/pull/7", "number": 7,
                      "draft": self.pr_body["draft"]},
            )  # fmt: skip
        if req.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": self.checks})
        if req.url.path.endswith("/status"):
            state = "none"
            if any(r["status"] != "completed" for r in self.checks):
                state = "pending"
            elif self.checks:
                state = (
                    "failure"
                    if any(
                        r.get("conclusion") not in ("success", "neutral", "skipped")
                        for r in self.checks
                    )
                    else "success"
                )
            return httpx.Response(200, json={"state": state, "statuses": []})
        if "/annotations" in req.url.path:
            return httpx.Response(
                200, json=[{"path": "calc.py", "start_line": 2, "message": "boom"}]
            )
        return httpx.Response(404, json={"message": "nope"})

    def client(self) -> GitHubClient:
        return GitHubClient("ghp_" + "x" * 36, transport=httpx.MockTransport(self))


@pytest.fixture
def store(settings: Settings) -> TaskStore:
    return TaskStore(settings.db_path)


def pr_task(settings, store, remote, tmp_path) -> str:  # type: ignore[no-untyped-def]
    local = tmp_path / "local"
    git(tmp_path, "clone", str(remote), str(local))
    tid = create_task(store, issue="add is wrong", repo=str(local), mode=Mode.PR, limits=Limits())
    rec = run_task(settings, FakeProvider([PLAN, FIX]), store, tid, sandbox=LocalSandbox())
    assert rec.status == "awaiting_approval" and rec.outcome == "verified"
    return tid


def test_pr_requires_approval_and_opens_draft_on_dedicated_branch(
    settings, store, remote, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    tid = pr_task(settings, store, remote, tmp_path)
    gh = FakeGitHub()
    with pytest.raises(NotApproved):
        publish_task(settings, store, tid, gh.client(), slug="o/r", remote=str(remote))
    assert not [r for r in gh.requests if r[0] == "POST"]
    assert git(remote, "branch", "--list").split() == ["*", "main"] or "issuepilot" not in git(
        remote, "branch"
    )

    approve(store, tid)
    rec = publish_task(settings, store, tid, gh.client(), slug="o/r", remote=str(remote),
                       sandbox=LocalSandbox())  # fmt: skip
    assert rec.status == "pr_opened" and rec.pr_number == 7 and rec.branch == f"issuepilot/{tid}"
    assert gh.pr_body["draft"] is True and gh.pr_body["base"] == "main"
    assert gh.pr_body["head"] == rec.branch
    branches = git(remote, "branch", "--list")
    assert rec.branch in branches
    # main is untouched; the fix lives only on the dedicated branch
    assert "a - b" in git(remote, "show", "main:calc.py")
    assert "a + b" in git(remote, "show", f"{rec.branch}:calc.py")
    assert not any(m == "PUT" or "merge" in p for m, p in gh.requests)


def test_merge_task_requires_explicitly_approved_verified_published_task(
    settings, store, remote, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    tid = pr_task(settings, store, remote, tmp_path)
    with pytest.raises(NotApproved):
        merge_task(settings, store, tid, FakeGitHub().client())

    approve(store, tid)
    publish_gh = FakeGitHub()
    publish_task(settings, store, tid, publish_gh.client(), slug="o/r", remote=str(remote))
    gh = FakeGitHub([{"id": 3, "name": "tests", "status": "completed", "conclusion": "success"}])
    gh.branch = f"issuepilot/{tid}"
    merged = merge_task(settings, store, tid, gh.client())
    assert merged.status == "merged"
    assert merged.merge_sha == "head-sha" and merged.merged_at
    assert any(e.kind == "pr_merged" for e in store.events(tid))


def test_merge_failure_leaves_pr_open_and_records_reason(settings, store, remote, tmp_path) -> None:  # type: ignore[no-untyped-def]
    tid = pr_task(settings, store, remote, tmp_path)
    approve(store, tid)
    publish_task(settings, store, tid, FakeGitHub().client(), slug="o/r", remote=str(remote))
    gh = FakeGitHub([{"id": 1, "name": "tests", "status": "completed", "conclusion": "failure"}])
    gh.branch = f"issuepilot/{tid}"
    with pytest.raises(GitHubError, match="checks are failure"):
        merge_task(settings, store, tid, gh.client())
    rec = store.get(tid)
    assert rec and rec.status == "pr_opened" and not gh.merged
    assert "checks are failure" in rec.error
    assert any(e.kind == "merge_failed" for e in store.events(tid))


def test_merge_target_is_derived_from_the_saved_pull_request(
    settings, store, remote, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    tid = pr_task(settings, store, remote, tmp_path)
    approve(store, tid)
    publish_task(settings, store, tid, FakeGitHub().client(), slug="o/r", remote=str(remote))
    store.update(tid, pr_url="https://github.com/other/repo/pull/8")
    gh = FakeGitHub([{"id": 1, "name": "tests", "status": "completed", "conclusion": "success"}])
    gh.branch = f"issuepilot/{tid}"
    with pytest.raises(NotApproved, match="invalid"):
        merge_task(settings, store, tid, gh.client())
    assert not gh.requests


def test_unapproved_or_rejected_cannot_publish(settings, store, remote, tmp_path) -> None:  # type: ignore[no-untyped-def]
    tid = pr_task(settings, store, remote, tmp_path)
    reject(store, tid)
    with pytest.raises(NotApproved):
        publish_task(settings, store, tid, FakeGitHub().client(), slug="o/r", remote=str(remote))


def test_develop_mode_task_cannot_be_approved(settings, store, remote, tmp_path) -> None:  # type: ignore[no-untyped-def]
    tid = create_task(store, issue="x", repo=str(tmp_path), mode=Mode.DEVELOP, limits=Limits())
    with pytest.raises(NotApproved):
        approve(store, tid)


def test_push_to_default_branch_is_refused(remote: Path) -> None:
    with GitWorkspace(str(remote), "", "main") as ws:
        ws.clone()
        with pytest.raises(GitError):
            ws.create_branch("main")
        with pytest.raises(GitError):
            ws.create_branch("feature/x")
        ws.branch = "main"
        with pytest.raises(GitError):
            ws.push()


def test_commit_stages_only_edited_paths(remote: Path) -> None:
    with GitWorkspace(str(remote), "", "main") as ws:
        ws.clone()
        ws.create_branch("issuepilot/t1")
        (ws.path / "stray.txt").write_text("unrelated")
        ws.apply_and_commit([Edit(path="calc.py", old="a - b", new="a + b")], "m")
        files = git(ws.path, "show", "--name-only", "--format=", "HEAD").split()
        assert files == ["calc.py"]


def test_workflow_edits_blocked_in_commit(remote: Path) -> None:
    with GitWorkspace(str(remote), "", "main") as ws:
        ws.clone()
        ws.create_branch("issuepilot/t2")
        with pytest.raises(Exception, match="protected"):
            ws.apply_and_commit([Edit(path=".github/workflows/x.yml", old="", new="x")], "m")


def test_token_never_in_url_or_errors(tmp_path: Path) -> None:
    token = "ghp_" + "S" * 36
    with (
        GitWorkspace("https://127.0.0.1:9/o/r.git", token, "main") as ws,
        pytest.raises(GitError) as ei,
    ):
        ws.clone()
    assert token not in str(ei.value)
    assert "x-access-token" not in ws.remote and token not in ws.remote


def test_github_error_redacts_token() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad credentials ghp_" + "y" * 36)

    c = GitHubClient("ghp_" + "y" * 36, transport=httpx.MockTransport(h))
    with pytest.raises(GitHubError) as ei:
        c.default_branch("o/r")
    assert "ghp_y" not in str(ei.value)


def test_merge_requires_green_checks_and_expected_pr() -> None:
    pass_checks = [{"id": 3, "name": "tests", "status": "completed", "conclusion": "success"}]
    gh = FakeGitHub(pass_checks)
    result = gh.client().merge_pull_request(
        "o/r", 7, expected_branch="issuepilot/task", expected_base="main"
    )
    assert result == {"sha": "head-sha", "message": "Pull Request successfully merged"}
    assert gh.pr_is_draft is False and gh.merged
    assert ("PUT", "/repos/o/r/pulls/7/merge") in gh.requests

    for checks in (
        [],
        [{"id": 4, "name": "test", "status": "in_progress", "conclusion": None}],
        [{"id": 5, "name": "test", "status": "completed", "conclusion": "failure"}],
    ):
        with pytest.raises(GitHubError, match="checks are"):
            FakeGitHub(checks).client().merge_pull_request(
                "o/r", 7, expected_branch="issuepilot/task", expected_base="main"
            )

    with pytest.raises(GitHubError, match="head branch"):
        FakeGitHub(pass_checks).client().merge_pull_request(
            "o/r", 7, expected_branch="issuepilot/other", expected_base="main"
        )


def test_draft_flag_enforced() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"draft": False, "html_url": "u", "number": 1})

    c = GitHubClient("t", transport=httpx.MockTransport(h))
    with pytest.raises(GitHubError, match="draft"):
        c.create_draft_pr("o/r", head="issuepilot/a", base="main", title="t", body="b")


def test_parse_slug() -> None:
    assert parse_slug("https://github.com/sbartaula/Flowtrack.git") == "sbartaula/Flowtrack"
    assert parse_slug("git@github.com:o/r.git") == "o/r"
    assert parse_slug("o/r") == "o/r"


def test_check_summary_states() -> None:
    fail = [{"id": 1, "name": "pytest", "status": "completed", "conclusion": "failure",
             "output": {"title": "1 failed", "summary": "s", "annotations_count": 1}}]  # fmt: skip
    s = FakeGitHub(fail).client().check_summary("o/r", "b")
    assert s.state == "failure" and "calc.py:2 boom" in s.text()
    ok = [{"id": 2, "name": "x", "status": "completed", "conclusion": "success"}]
    assert FakeGitHub(ok).client().check_summary("o/r", "b").state == "success"
    pend = [{"id": 3, "name": "x", "status": "in_progress", "conclusion": None}]
    assert FakeGitHub(pend).client().check_summary("o/r", "b").state == "pending"
    assert FakeGitHub([]).client().check_summary("o/r", "b").state == "none"


def test_ci_failure_triggers_bounded_followup_on_same_branch(
    settings, store, remote, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    tid = pr_task(settings, store, remote, tmp_path)
    approve(store, tid)
    gh = FakeGitHub()
    publish_task(settings, store, tid, gh.client(), slug="o/r", remote=str(remote))
    branch = f"issuepilot/{tid}"
    fail = [{"id": 1, "name": "lint", "status": "completed", "conclusion": "failure",
             "output": {"title": "s", "summary": "needs docstring",
                        "annotations_count": 0}}]  # fmt: skip
    gh2 = FakeGitHub(fail)
    follow = json.dumps({"explanation": "doc", "edits": [
        {"path": "calc.py", "old": "def add(a, b):\n",
         "new": 'def add(a, b):\n    """Add."""\n'}]})  # fmt: skip
    ci_prov = FakeProvider([PLAN, follow, follow, follow])
    s = ci_followup(settings, ci_prov, store, tid, gh2.client(), slug="o/r", remote=str(remote),
                    sandbox=LocalSandbox())  # fmt: skip
    assert s.state == "failure"
    assert '"""Add."""' in git(remote, "show", f"{branch}:calc.py")
    assert "needs docstring" in ci_prov.calls[0][-1].content
    assert git(remote, "rev-list", "--count", f"main..{branch}") == "2"
    rec = store.get(tid)
    assert rec and rec.ci_rounds == 1

    # exhaust the bounded rounds: a third failure must not push again
    for _ in range(3):
        prov = FakeProvider([PLAN, follow, follow, follow])
        ci_followup(settings, prov, store, tid, gh2.client(), slug="o/r", remote=str(remote),
                    sandbox=LocalSandbox())  # fmt: skip
    rec = store.get(tid)
    assert rec and rec.ci_rounds == Limits().max_ci_rounds
    assert git(remote, "rev-list", "--count", f"main..{branch}") == "3"  # no third follow-up


def test_monorepo_subdir_fix_lands_under_the_subfolder(settings, store, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Project lives in <repo>/proj; the PR commit must touch proj/..., not the repo root."""
    from issuepilot.security import resolve_subdir

    seed = tmp_path / "seed"
    (seed / "proj").mkdir(parents=True)
    git(seed, "init", "-b", "main")
    git(seed, "config", "user.email", "a@b.c")
    git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("monorepo\n")
    (seed / "proj" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (seed / "proj" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_a():\n    assert add(1, 2) == 3\n"
    )
    git(seed, "add", ".")
    git(seed, "commit", "-m", "init")
    bare = tmp_path / "mono.git"
    git(tmp_path, "clone", "--bare", str(seed), str(bare))
    local = tmp_path / "local"
    git(tmp_path, "clone", str(bare), str(local))

    work, rel = resolve_subdir(local, "proj")
    tid = create_task(
        store, issue="add is wrong", repo=str(work), mode=Mode.PR, limits=Limits(), subdir=rel
    )
    rec = run_task(settings, FakeProvider([PLAN, FIX]), store, tid, sandbox=LocalSandbox())
    assert rec.outcome == "verified" and rec.subdir == "proj"
    approve(store, tid)
    gh = FakeGitHub()
    done = publish_task(settings, store, tid, gh.client(), slug="o/r", remote=str(bare),
                        sandbox=LocalSandbox())  # fmt: skip
    assert done.status == "pr_opened"
    assert "a + b" in git(bare, "show", f"{done.branch}:proj/calc.py")
    assert "a - b" in git(bare, "show", "main:proj/calc.py")
    changed = git(bare, "diff", "--name-only", f"main..{done.branch}").split()
    assert changed == ["proj/calc.py"]


def test_resolve_subdir_rejects_escapes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from issuepilot.security import resolve_subdir

    (tmp_path / "proj").mkdir()
    assert resolve_subdir(tmp_path, "proj/")[1] == "proj"
    assert resolve_subdir(tmp_path, "") == (tmp_path, "")
    for bad in ("../x", "/etc", ".git", "missing"):
        with pytest.raises(ValueError):
            resolve_subdir(tmp_path, bad)

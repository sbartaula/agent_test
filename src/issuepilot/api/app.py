"""FastAPI app. Local-first: repos are restricted to an allowed root directory."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from issuepilot.agent import FixResult, run_agent
from issuepilot.budget import BudgetExceeded, Cancelled, Limits
from issuepilot.config import Settings, load_dotenv
from issuepilot.github import GitError, GitHubClient, GitHubError, parse_slug
from issuepilot.llm.base import LLMProvider
from issuepilot.llm.deepseek import DeepSeekProvider
from issuepilot.modes import Mode, PermissionDenied
from issuepilot.orchestrator import (
    ExtensionError,
    budget_suggestion,
    create_task,
    extend_budget,
    request_cancel,
    result_of,
    run_task,
)
from issuepilot.persistence import RunStore
from issuepilot.persistence.tasks import TaskStore
from issuepilot.publish import NotApproved, approve, merge_task, publish_task, reject
from issuepilot.sandbox import Sandbox, SandboxUnavailable, default_sandbox
from issuepilot.security import resolve_subdir

UI_FILE = Path(__file__).parent / "static" / "index.html"


class FixRequest(BaseModel):
    issue: str = Field(min_length=1, max_length=20_000)
    repo_path: str
    run_tests: bool = False


class TaskRequest(BaseModel):
    issue: str = Field(min_length=1, max_length=20_000)
    repo_path: str
    subdir: str = Field(default="", max_length=300)
    mode: Mode = Mode.DEVELOP
    max_cost_usd: float = Field(default=Limits.max_cost_usd, gt=0, le=5)
    max_seconds: float = Field(default=Limits.max_seconds, gt=0, le=3600)
    max_tool_calls: int = Field(default=Limits.max_tool_calls, gt=0, le=500)
    max_attempts: int = Field(default=Limits.max_attempts, gt=0, le=6)


class ExtendRequest(BaseModel):
    extra_usd: float = Field(default=0.0, ge=0, le=5)
    extra_seconds: float = Field(default=0.0, ge=0, le=3600)
    extra_tool_calls: int = Field(default=0, ge=0, le=500)


class PublishRequest(BaseModel):
    slug: str | None = None
    remote: str | None = None


class MergeRequest(BaseModel):
    confirm: bool


class FixResponse(BaseModel):
    id: int
    result: FixResult


def create_app(
    settings: Settings,
    provider_factory: Callable[[], LLMProvider] | None = None,
    repos_root: str | Path | None = None,
    sandbox_factory: Callable[[], Sandbox] | None = None,
    github_factory: Callable[[], GitHubClient] | None = None,
    inline: bool = False,
) -> FastAPI:
    app = FastAPI(title="issuepilot", version="0.1.0")
    store = RunStore(settings.db_path)
    root = Path(repos_root or os.environ.get("ISSUEPILOT_REPOS_ROOT", ".")).resolve()
    make_provider = provider_factory or (lambda: DeepSeekProvider(settings))

    tasks = TaskStore(settings.db_path)
    tasks.mark_interrupted()  # anything still 'running' died with the previous process
    pool = ThreadPoolExecutor(max_workers=2)
    make_sandbox = sandbox_factory or default_sandbox

    def _repo(path: str) -> Path:
        repo = (root / path).resolve()
        if not repo.is_relative_to(root) or not repo.is_dir():
            raise HTTPException(400, "repo_path must be a directory inside the allowed root")
        return repo

    def _view(tid: str) -> dict[str, Any]:
        rec = tasks.get(tid)
        if rec is None:
            raise HTTPException(404, "unknown task")
        data = rec.model_dump(exclude={"result_json"})
        res = result_of(rec)
        data["result"] = res.model_dump() if res else None
        data["limits"] = Limits(**json.loads(rec.limits_json)).to_dict()
        data["budget_suggestion"] = budget_suggestion(rec)
        return data

    @app.get("/", include_in_schema=False)
    def ui() -> FileResponse:
        return FileResponse(UI_FILE)

    @app.post("/tasks", status_code=202)
    def start_task(req: TaskRequest) -> dict[str, str]:
        try:
            repo, subdir = resolve_subdir(_repo(req.repo_path), req.subdir)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        limits = Limits(
            max_cost_usd=req.max_cost_usd, max_seconds=req.max_seconds,
            max_tool_calls=req.max_tool_calls, max_attempts=req.max_attempts,
        )  # fmt: skip
        run = req.mode != Mode.OBSERVE
        sandbox = None
        if run:
            try:
                sandbox = make_sandbox()
            except SandboxUnavailable as exc:
                raise HTTPException(503, str(exc)) from exc
        tid = create_task(
            tasks, issue=req.issue, repo=str(repo), mode=req.mode, limits=limits, subdir=subdir
        )

        def job() -> None:
            run_task(settings, make_provider(), tasks, tid, sandbox=sandbox, run_tests=run)

        if inline:
            job()
        else:
            pool.submit(job)
        return {"id": tid}

    @app.get("/tasks")
    def list_tasks() -> list[dict[str, Any]]:
        return [t.model_dump(exclude={"result_json", "limits_json"}) for t in tasks.recent()]

    @app.get("/tasks/{tid}")
    def get_task(tid: str) -> dict[str, Any]:
        return _view(tid)

    @app.get("/tasks/{tid}/events")
    def task_events(tid: str, after: int = 0) -> list[dict[str, Any]]:
        if tasks.get(tid) is None:
            raise HTTPException(404, "unknown task")
        return [e.model_dump() for e in tasks.events(tid, after)]

    @app.post("/tasks/{tid}/cancel")
    def cancel(tid: str) -> dict[str, str]:
        _view(tid)
        request_cancel(tasks, tid)
        return {"status": "cancel_requested"}

    @app.post("/tasks/{tid}/extend", status_code=202)
    def extend(tid: str, req: ExtendRequest | None = None) -> dict[str, Any]:
        """Human approval to continue a budget-stopped task with more budget, then resume it."""
        rec = tasks.get(tid)
        if rec is None:
            raise HTTPException(404, "unknown task")
        sug = budget_suggestion(rec)
        r = req or ExtendRequest()
        if not (r.extra_usd or r.extra_seconds or r.extra_tool_calls) and sug:
            r = ExtendRequest(
                extra_usd=sug["extra_usd"],
                extra_seconds=sug["extra_seconds"],
                extra_tool_calls=sug["extra_tool_calls"],
            )
        try:
            extend_budget(tasks, tid, extra_usd=r.extra_usd, extra_seconds=r.extra_seconds,
                          extra_tool_calls=r.extra_tool_calls)  # fmt: skip
            sandbox = make_sandbox() if rec.mode != "observe" else None
        except ExtensionError as exc:
            raise HTTPException(409, str(exc)) from exc
        except SandboxUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

        def job() -> None:
            run_task(settings, make_provider(), tasks, tid, sandbox=sandbox,
                     run_tests=rec.mode != "observe", resume=True)  # fmt: skip

        if inline:
            job()
        else:
            pool.submit(job)
        return _view(tid)

    @app.post("/tasks/{tid}/approve")
    def approve_task(tid: str) -> dict[str, Any]:
        try:
            approve(tasks, tid)
        except KeyError as exc:
            raise HTTPException(404, "unknown task") from exc
        except NotApproved as exc:
            raise HTTPException(409, str(exc)) from exc
        return _view(tid)

    @app.post("/tasks/{tid}/reject")
    def reject_task(tid: str) -> dict[str, Any]:
        try:
            reject(tasks, tid)
        except KeyError as exc:
            raise HTTPException(404, "unknown task") from exc
        except NotApproved as exc:
            raise HTTPException(409, str(exc)) from exc
        return _view(tid)

    @app.post("/tasks/{tid}/publish")
    def publish(tid: str, req: PublishRequest | None = None) -> dict[str, Any]:
        """Open a DRAFT PR for an approved task."""
        rec = tasks.get(tid)
        if rec is None:
            raise HTTPException(404, "unknown task")
        if rec.approval != "approved" or rec.status != "awaiting_approval":
            raise HTTPException(409, "task has not been approved")
        token = os.environ.get("GITHUB_TOKEN", "")
        try:
            remote = (req and req.remote) or subprocess.run(
                ["git", "-C", rec.repo, "remote", "get-url", "origin"],
                capture_output=True, text=True,
            ).stdout.strip()  # fmt: skip
            slug = (req and req.slug) or parse_slug(remote)
            client = github_factory() if github_factory else GitHubClient(token)
            publish_task(settings, tasks, tid, client, slug=slug, remote=remote, token=token,
                         sandbox=make_sandbox())  # fmt: skip
        except NotApproved as exc:
            raise HTTPException(409, str(exc)) from exc
        except (GitHubError, GitError, SandboxUnavailable) as exc:
            raise HTTPException(502, str(exc)[:500]) from exc
        return _view(tid)

    @app.post("/tasks/{tid}/merge")
    def merge(tid: str, req: MergeRequest) -> dict[str, Any]:
        """Merge only after explicit dashboard approval, verified result and green GitHub checks."""
        if not req.confirm:
            raise HTTPException(400, "explicit merge confirmation is required")
        rec = tasks.get(tid)
        if rec is None:
            raise HTTPException(404, "unknown task")
        if (
            rec.status != "pr_opened"
            or rec.approval != "approved"
            or rec.outcome != "verified"
            or not rec.pr_number
        ):
            raise HTTPException(
                409, "only an approved, verified task with a published PR can merge"
            )
        token = os.environ.get("GITHUB_TOKEN", "")
        try:
            client = github_factory() if github_factory else GitHubClient(token)
            merge_task(settings, tasks, tid, client, token=token)
        except NotApproved as exc:
            raise HTTPException(409, str(exc)) from exc
        except GitHubError as exc:
            raise HTTPException(409, str(exc)[:500]) from exc
        except (BudgetExceeded, Cancelled, PermissionDenied) as exc:
            raise HTTPException(409, str(exc)[:500]) from exc
        return _view(tid)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/fixes", response_model=FixResponse)
    def create_fix(req: FixRequest) -> FixResponse:
        repo = (root / req.repo_path).resolve()
        if not repo.is_relative_to(root) or not repo.is_dir():
            raise HTTPException(400, "repo_path must be a directory inside the allowed root")
        result = run_agent(make_provider(), settings, req.issue, repo, run_tests=req.run_tests)
        fix_id = store.save_fix(
            issue=req.issue,
            repo_path=str(repo),
            status=result.status,
            result_json=result.model_dump_json(),
        )
        return FixResponse(id=fix_id, result=result)

    @app.get("/fixes/{fix_id}", response_model=FixResult)
    def get_fix(fix_id: int) -> FixResult:
        raw = store.get_fix(fix_id)
        if raw is None:
            raise HTTPException(404, "not found")
        return FixResult.model_validate_json(raw)

    return app


def app_factory() -> FastAPI:
    """For `uvicorn issuepilot.api.app:app_factory --factory`."""
    load_dotenv()
    return create_app(Settings.from_env())

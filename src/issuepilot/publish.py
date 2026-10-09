"""PR Agent mode: approval-gated branch/commit/push/draft-PR and bounded CI follow-ups."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from issuepilot.agent.graph import FixResult, run_agent
from issuepilot.budget import BudgetExceeded, Cancelled, Counters, Limits
from issuepilot.config import Settings
from issuepilot.github import CheckSummary, GitError, GitHubClient, GitHubError, GitWorkspace
from issuepilot.llm.base import LLMProvider
from issuepilot.modes import Capability, Mode, PermissionDenied
from issuepilot.orchestrator import cancel_token, result_of
from issuepilot.persistence.tasks import TaskRecord, TaskStore
from issuepilot.runtime import TaskContext
from issuepilot.sandbox import Sandbox
from issuepilot.tools import Edit, ToolError


class NotApproved(RuntimeError):
    pass


def approve(store: TaskStore, task_id: str) -> TaskRecord:
    rec = _get(store, task_id)
    if rec.status != "awaiting_approval" or rec.outcome != "verified":
        raise NotApproved(
            f"task is '{rec.status}/{rec.outcome}'; only verified tasks can be approved"
        )
    store.update(task_id, approval="approved")
    store.add_event(task_id, "approval", "approved by user", {})
    return _get(store, task_id)


def reject(store: TaskStore, task_id: str) -> TaskRecord:
    rec = _get(store, task_id)
    if rec.status != "awaiting_approval":
        raise NotApproved(f"task is '{rec.status}', nothing to reject")
    store.update(task_id, approval="rejected", status="rejected")
    store.add_event(task_id, "approval", "rejected by user", {})
    return _get(store, task_id)


def _get(store: TaskStore, task_id: str) -> TaskRecord:
    rec = store.get(task_id)
    if rec is None:
        raise KeyError(task_id)
    return rec


def _ctx(settings: Settings, store: TaskStore, rec: TaskRecord, token: str) -> TaskContext:
    prior = Counters(
        prompt_tokens=rec.prompt_tokens, completion_tokens=rec.completion_tokens,
        cost_usd=rec.cost_usd, tool_calls=rec.tool_calls, llm_calls=rec.llm_calls,
        retries=rec.retries, elapsed_s=rec.elapsed_s,
    )  # fmt: skip
    limits = Limits(**json.loads(rec.limits_json))
    return TaskContext(
        rec.id, Mode(rec.mode), settings, limits, store, cancel_token(rec.id), prior, (token,)
    )


def _in_subdir(edits: list[Edit], subdir: str) -> list[Edit]:
    """Edits are relative to the project folder; the git clone is rooted at the repository."""
    if not subdir:
        return edits
    return [e.model_copy(update={"path": f"{subdir}/{e.path}"}) for e in edits]


def _pr_body(rec: TaskRecord, res: FixResult) -> str:
    v = res.verification
    lines = [
        "> 🤖 Draft opened by **IssuePilot**. It was verified in an isolated sandbox but needs "
        "human review. It will never be merged automatically.",
        "", "## Issue", rec.issue[:1500], "",
    ]  # fmt: skip
    if res.analysis:
        lines += ["## Root cause", res.analysis.root_cause, ""]
    if res.explanation:
        lines += ["## Change", res.explanation, ""]
    if v:
        lines += [
            "## Verification",
            f"- outcome: **{v.outcome}** ({v.detail})",
            *[f"- fail→pass: `{t}`" for t in v.fail_to_pass[:8]],
            "",
        ]
    lines += [f"_Task `{rec.id}` · cost ${rec.cost_usd:.4f} · {rec.tool_calls} tool calls_"]
    return "\n".join(lines)


def publish_task(
    settings: Settings,
    store: TaskStore,
    task_id: str,
    client: GitHubClient,
    *,
    slug: str,
    remote: str,
    token: str = "",
    sandbox: Sandbox | None = None,
) -> TaskRecord:
    """Push an approved, verified fix to `issuepilot/<task>` and open a DRAFT PR."""
    rec = _get(store, task_id)
    if rec.approval != "approved" or rec.status != "awaiting_approval":
        raise NotApproved("task has not been approved")
    res = result_of(rec)
    if not res or res.status != "verified" or not res.edits:
        raise NotApproved("only verified results with edits can be published")
    ctx = _ctx(settings, store, rec, token)
    try:
        base = client.default_branch(slug)
        with GitWorkspace(remote, token, base) as ws:
            with ctx.tool("git_clone", Capability.GITHUB_READ, remote=slug):
                ws.clone()
            branch = f"issuepilot/{task_id}"
            ws.create_branch(branch)
            ws.apply_and_commit(
                _in_subdir(res.edits, rec.subdir),
                f"fix: {rec.issue.splitlines()[0][:60]}\n\nIssuePilot task {task_id}",
            )
            if (
                sandbox is not None
            ):  # never publish code that was not re-verified on the clean clone
                with ctx.tool("run_tests", Capability.SANDBOX, label="pre-push re-verification"):
                    r = sandbox.run_tests(
                        ws.path / rec.subdir, timeout=ctx.tracker.remaining_seconds() or 1,
                        cancel=ctx.tracker.cancel,
                    )  # fmt: skip
                if r.exit_code != 0 or r.infra_error:
                    raise GitError(f"pre-push verification failed:\n{r.output[-1500:]}")
            with ctx.tool("git_push", Capability.GITHUB_WRITE, branch=branch):
                ws.push()
            with ctx.tool("open_draft_pr", Capability.GITHUB_WRITE, base=base):
                pr = client.create_draft_pr(
                    slug, head=branch, base=base,
                    title=f"[IssuePilot] {rec.issue.splitlines()[0][:80]}", body=_pr_body(rec, res),
                )  # fmt: skip
        store.update(task_id, status="pr_opened", branch=branch, pr_url=pr["html_url"],
                     pr_number=pr["number"], error="")  # fmt: skip
        ctx.event("pr_opened", pr["html_url"], branch=branch, draft=True)
    except (GitError, GitHubError, ToolError, PermissionDenied, BudgetExceeded, Cancelled) as exc:
        store.update(task_id, error=str(exc)[:800])
        ctx.event("publish_failed", str(exc)[:500])
        raise
    finally:
        ctx.flush()
    return _get(store, task_id)


def merge_task(
    settings: Settings,
    store: TaskStore,
    task_id: str,
    client: GitHubClient,
    *,
    token: str = "",
) -> TaskRecord:
    """Merge a published verified PR after explicit user approval and successful CI."""
    rec = _get(store, task_id)
    if (
        rec.status != "pr_opened"
        or rec.approval != "approved"
        or rec.outcome != "verified"
        or not rec.pr_number
        or not rec.branch
    ):
        raise NotApproved("only an approved, verified task with an open PR can be merged")

    pr_url = urlsplit(rec.pr_url)
    parts = pr_url.path.strip("/").split("/")
    if (
        pr_url.scheme != "https"
        or pr_url.netloc != "github.com"
        or len(parts) != 4
        or parts[2] != "pull"
        or not parts[3].isdigit()
        or int(parts[3]) != rec.pr_number
    ):
        raise NotApproved("stored pull request URL is invalid")
    slug = f"{parts[0]}/{parts[1]}"

    ctx = _ctx(settings, store, rec, token)
    try:
        base = client.default_branch(slug)
        with ctx.tool(
            "merge_pull_request",
            Capability.GITHUB_WRITE,
            pull_number=rec.pr_number,
            branch=rec.branch,
            base=base,
        ):
            result = client.merge_pull_request(
                slug, rec.pr_number, expected_branch=rec.branch, expected_base=base
            )
        from datetime import UTC, datetime

        store.update(
            task_id,
            status="merged",
            merged_at=datetime.now(UTC).isoformat(),
            merge_sha=result["sha"],
            error="",
        )
        ctx.event("pr_merged", result["message"], sha=result["sha"], base=base)
    except (GitHubError, ToolError, PermissionDenied, BudgetExceeded, Cancelled) as exc:
        store.update(task_id, error=str(exc)[:800])
        ctx.event("merge_failed", str(exc)[:500])
        raise
    finally:
        ctx.flush()
    return _get(store, task_id)


def ci_followup(
    settings: Settings,
    provider: LLMProvider,
    store: TaskStore,
    task_id: str,
    client: GitHubClient,
    *,
    slug: str,
    remote: str,
    token: str = "",
    sandbox: Sandbox | None = None,
) -> CheckSummary:
    """Read CI for the task's branch; on failure make at most `max_ci_rounds` follow-up fixes."""
    rec = _get(store, task_id)
    if not rec.branch:
        raise NotApproved("task has no published branch")
    ctx = _ctx(settings, store, rec, token)
    with ctx.tool("read_ci", Capability.GITHUB_READ, branch=rec.branch):
        summary = client.check_summary(slug, rec.branch)
    ctx.event("ci_status", summary.state, failures=len(summary.failures))
    try:
        if summary.state != "failure":
            return summary
        if rec.ci_rounds >= ctx.limits.max_ci_rounds:
            ctx.event("ci_round_limit", f"stopped after {rec.ci_rounds} follow-up rounds")
            return summary
        with GitWorkspace(remote, token, client.default_branch(slug)) as ws:
            ws.clone()
            ws.checkout_remote_branch(rec.branch)
            issue = f"{rec.issue}\n\nA previous fix was pushed but CI failed:\n{summary.text()}"
            res = run_agent(
                provider, settings, issue, str(ws.path / rec.subdir), run_tests=sandbox is not None,
                sandbox=sandbox, thread_id=f"{task_id}-ci{rec.ci_rounds + 1}",
                max_attempts=ctx.limits.max_attempts, ctx=ctx,
            )  # fmt: skip
            store.update(task_id, ci_rounds=rec.ci_rounds + 1)
            if res.status not in ("verified", "unproven") or not res.edits:
                ctx.event("ci_followup_failed", f"agent outcome: {res.status}")
                return summary
            ws.apply_and_commit(
                _in_subdir(res.edits, rec.subdir),
                f"fix: address CI failure (round {rec.ci_rounds + 1})",
            )
            with ctx.tool("git_push", Capability.GITHUB_WRITE, branch=rec.branch):
                ws.push()
        ctx.event("ci_followup_pushed", f"round {rec.ci_rounds + 1}", outcome=res.status)
    finally:
        ctx.flush()
    return summary


__all__ = ["NotApproved", "approve", "ci_followup", "publish_task", "reject"]

"""Command line entry point: `issuepilot plan "issue text"`."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import httpx
from openai import OpenAIError

from issuepilot.agent import FixResult
from issuepilot.budget import Limits
from issuepilot.config import ConfigError, Settings, load_dotenv
from issuepilot.cost import estimate_cost_usd, format_usage
from issuepilot.discovery import run_discovery
from issuepilot.github import GitError, GitHubClient, GitHubError, parse_slug
from issuepilot.llm.base import LLMProvider, LLMResponse, Usage
from issuepilot.llm.deepseek import DeepSeekProvider
from issuepilot.modes import Mode
from issuepilot.orchestrator import create_task, request_cancel, result_of, run_task
from issuepilot.persistence.store import RunStore
from issuepilot.persistence.tasks import TaskRecord, TaskStore
from issuepilot.planning.planner import PlanningError, create_plan
from issuepilot.publish import NotApproved, approve, ci_followup, publish_task, reject
from issuepilot.sandbox import Sandbox, SandboxUnavailable, default_sandbox
from issuepilot.security import redact, resolve_subdir
from issuepilot.tools.github import fetch_issue
from issuepilot.tools.repo import ToolError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="issuepilot")
    sub = p.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Produce a structured plan for an issue.")
    src = plan.add_mutually_exclusive_group(required=True)
    src.add_argument("issue", nargs="?", help="Issue description text.")
    src.add_argument("--file", "-f", help="Read the issue description from a file ('-' = stdin).")
    plan.add_argument("--json", action="store_true", help="Print the plan as JSON.")
    plan.add_argument("--no-save", action="store_true", help="Do not record the run in SQLite.")

    fix = sub.add_parser(
        "fix", help="Investigate an issue; in develop mode produce a verified patch."
    )
    fsrc = fix.add_mutually_exclusive_group(required=True)
    fsrc.add_argument("issue", nargs="?", help="Issue description text.")
    fsrc.add_argument("--file", "-f", help="Read the issue from a file ('-' = stdin).")
    fsrc.add_argument("--github-issue", help="Fetch issue text (read-only) from a GitHub URL.")
    fix.add_argument("--repo", default=".", help="Path to the target repository.")
    fix.add_argument(
        "--mode", choices=[m.value for m in Mode], default="develop",
        help="observe = read-only analysis; develop = edit + verify in a sandbox (default); "
             "pr = develop, then wait for approval before opening a DRAFT PR.",
    )  # fmt: skip
    fix.add_argument(
        "--subdir",
        help="Project folder inside --repo (monorepo): only it is analysed and tested, "
        "and PR paths are prefixed with it.",
    )
    fix.add_argument("--no-tests", action="store_true", help="Skip sandbox test verification.")
    fix.add_argument("--sandbox", choices=["docker", "local"], help="Default: docker.")
    fix.add_argument("--python", help="Interpreter for --sandbox local (unsafe; dev only).")
    fix.add_argument("--max-cost", type=float, default=Limits.max_cost_usd, help="USD hard cap.")
    fix.add_argument("--max-seconds", type=float, default=Limits.max_seconds)
    fix.add_argument("--max-tool-calls", type=int, default=Limits.max_tool_calls)
    fix.add_argument("--max-attempts", type=int, default=Limits.max_attempts)
    fix.add_argument("--output", "-o", help="Write the patch to this file (git apply-able).")
    fix.add_argument("--json", action="store_true", help="Print the full result as JSON.")
    fix.add_argument("--verbose", "-v", action="store_true", help="Print the task event log.")

    disc = sub.add_parser("discover", help="Find likely bugs proactively (read-only report).")
    disc.add_argument("--repo", default=".")
    disc.add_argument("--no-model", action="store_true", help="Static scan only, no LLM triage.")
    disc.add_argument("--max-cost", type=float, default=0.05)
    disc.add_argument("--output", "-o", help="Write the markdown report here.")

    ev = sub.add_parser("eval", help="Run the evaluation suite (offline by default).")
    ev.add_argument("--live", action="store_true", help="Use the real model (costs ~$0.003).")
    ev.add_argument("--sandbox", choices=["docker", "local"], default="docker")
    ev.add_argument("--python")
    ev.add_argument("--case", action="append", help="Only run this case id (repeatable).")
    ev.add_argument("--json", action="store_true")
    ev.add_argument(
        "--bench",
        action="store_true",
        help="Live benchmark: realistic bugs, scored by hidden oracle tests (real model, ~$0.05).",
    )
    ev.add_argument("--output", help="With --bench: also write the results as JSON to this file.")

    sub.add_parser("tasks", help="List recent tasks.")
    show = sub.add_parser("task", help="Show one task with its event log.")
    show.add_argument("task_id")
    for name, text in (("approve", "Approve a verified PR-mode task."),
                       ("reject", "Reject a PR-mode task."),
                       ("cancel", "Request cancellation of a running task."),
                       ("resume", "Resume an interrupted task from its checkpoint.")):  # fmt: skip
        sp = sub.add_parser(name, help=text)
        sp.add_argument("task_id")
    for name, text in (("publish", "Push the approved branch and open a DRAFT PR."),
                       ("ci", "Read CI for the branch; bounded follow-up fixes.")):  # fmt: skip
        sp = sub.add_parser(name, help=text)
        sp.add_argument("task_id")
        sp.add_argument("--slug", help="OWNER/REPO (default: derived from the repo's origin).")
        sp.add_argument("--remote", help="Clone URL (default: the repo's origin).")
        sp.add_argument("--sandbox", choices=["docker", "local"])
        sp.add_argument("--python")
    return p


def _read_issue(args: argparse.Namespace) -> str:
    if getattr(args, "github_issue", None):
        return fetch_issue(args.github_issue)
    if args.file == "-":
        return sys.stdin.read()
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            return fh.read()
    return str(args.issue)


def run_plan(
    args: argparse.Namespace, settings: Settings, provider: LLMProvider | None = None
) -> int:
    issue = _read_issue(args).strip()
    if not issue:
        print("error: empty issue description", file=sys.stderr)
        return 2
    provider = provider or DeepSeekProvider(settings)
    plan, usage = create_plan(provider, issue)
    cost = estimate_cost_usd(usage, settings.price_input_per_m, settings.price_output_per_m)
    plan_json = plan.model_dump_json(indent=2)

    if args.json:
        print(plan_json)
    else:
        print(f"Summary: {plan.summary}")
        if plan.root_cause_hypothesis:
            print(f"Root cause (hypothesis): {plan.root_cause_hypothesis}")
        for i, step in enumerate(plan.steps, 1):
            files = f" [{', '.join(step.files)}]" if step.files else ""
            print(f"{i}. ({step.kind}) {step.description}{files}")
        for risk in plan.risks:
            print(f"Risk: {risk}")
        if plan.test_strategy:
            print(f"Tests: {plan.test_strategy}")

    if not args.no_save:
        run_id = RunStore(settings.db_path).save(
            model=settings.model,
            issue=issue,
            plan_json=plan_json,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=cost,
        )
        print(f"saved run #{run_id}", file=sys.stderr)
    print(format_usage(usage, cost), file=sys.stderr)
    return 0


def _print_event_log(store: TaskStore, task_id: str) -> None:
    for e in store.events(task_id):
        extra = {k: v for k, v in e.data.items() if k not in ("duration_s",)}
        dur = f" ({e.data['duration_s']}s)" if "duration_s" in e.data else ""
        print(f"  {e.ts[11:19]} {e.kind:<12} {e.message}{dur} {extra or ''}", file=sys.stderr)


def _print_result(rec: TaskRecord, result: FixResult | None, args: argparse.Namespace) -> None:
    if getattr(args, "json", False):
        payload = {
            "task": rec.model_dump(exclude={"result_json"}),
            "result": result.model_dump() if result else None,
        }
        print(json.dumps(payload, indent=2))
        return
    print(f"task {rec.id}: {rec.status} / outcome={rec.outcome or '-'} (attempts: "
          f"{result.attempts if result else 0})")  # fmt: skip
    if rec.error:
        print(f"error: {rec.error}")
    if result is None:
        return
    if result.analysis:
        a = result.analysis
        print(f"root cause ({a.confidence} confidence): {a.root_cause}")
        for ev in a.evidence:
            print(f"  - {ev.file}:{ev.lines} {ev.why}")
        if a.proposed_fix:
            print(f"proposed fix: {a.proposed_fix}")
    if result.explanation:
        print(f"explanation: {result.explanation}")
    if result.verification:
        v = result.verification
        print(f"verification: {v.outcome} - {v.detail}")
        if v.fail_to_pass:
            print(f"  proof (fail->pass): {', '.join(v.fail_to_pass[:5])}")
    if result.status == "tests_failed" and result.test_output:
        print(result.test_output)
    if result.diff:
        print(result.diff)
    if result.status == "unverified":
        print("note: tests were not run; the patch is UNVERIFIED.", file=sys.stderr)
    if result.status == "unproven":
        print("note: suite passes but no test proves the fix; review carefully.", file=sys.stderr)


def run_fix(
    args: argparse.Namespace,
    settings: Settings,
    provider: LLMProvider | None = None,
    sandbox: Sandbox | None = None,
) -> int:
    issue = _read_issue(args).strip()
    if not issue:
        print("error: empty issue description", file=sys.stderr)
        return 2
    provider = provider or DeepSeekProvider(settings)
    mode = Mode(args.mode)
    limits = Limits(
        max_cost_usd=args.max_cost, max_seconds=args.max_seconds,
        max_tool_calls=args.max_tool_calls, max_attempts=args.max_attempts,
    )  # fmt: skip
    run_tests = not args.no_tests and mode != Mode.OBSERVE
    if run_tests and sandbox is None:
        sandbox = default_sandbox(args.sandbox, args.python)
    store = TaskStore(settings.db_path)
    try:
        work, subdir = resolve_subdir(Path(args.repo).resolve(), args.subdir or "")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    tid = create_task(store, issue=issue, repo=str(work), mode=mode, limits=limits, subdir=subdir)
    rec = run_task(settings, provider, store, tid, sandbox=sandbox, run_tests=run_tests)
    result = result_of(rec)
    if args.output and result and result.diff:
        Path(args.output).write_text(result.diff, encoding="utf-8")
    _print_result(rec, result, args)
    if args.verbose:
        _print_event_log(store, tid)
    usage = Usage(prompt_tokens=rec.prompt_tokens, completion_tokens=rec.completion_tokens)
    print(format_usage(usage, rec.cost_usd), file=sys.stderr)
    print(f"tools: {rec.tool_calls} | llm calls: {rec.llm_calls} | retries: {rec.retries} | "
          f"runtime: {rec.elapsed_s:.1f}s", file=sys.stderr)  # fmt: skip
    if rec.status in ("failed", "cancelled", "budget_exceeded"):
        return 1
    return {"verified": 0, "analysis_only": 0, "unverified": 0, "unproven": 3}.get(rec.outcome, 1)


def run_discover(
    args: argparse.Namespace, settings: Settings, provider: LLMProvider | None = None
) -> int:
    store = TaskStore(settings.db_path)
    repo = str(Path(args.repo).resolve())
    limits = Limits(max_cost_usd=args.max_cost)
    tid = create_task(store, issue="proactive discovery", repo=repo, mode=Mode.OBSERVE,
                      limits=limits, kind="discover")  # fmt: skip
    use_model = not args.no_model
    report = run_discovery(settings, (provider or DeepSeekProvider(settings)) if use_model else
                           _NoModel(), store, tid, use_model=use_model)  # fmt: skip
    rec = store.get(tid)
    assert rec is not None
    if report is None:
        print(f"discovery {rec.status}: {rec.error}", file=sys.stderr)
        return 1
    text = report.markdown()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    usage = Usage(prompt_tokens=rec.prompt_tokens, completion_tokens=rec.completion_tokens)
    print(format_usage(usage, rec.cost_usd), file=sys.stderr)
    return 0


class _NoModel:
    def complete(self, *_: object, **__: object) -> LLMResponse:
        raise RuntimeError("model disabled")


def run_eval(args: argparse.Namespace, settings: Settings) -> int:
    from issuepilot.evals.runner import run_suite, summary

    sandbox = default_sandbox(args.sandbox, args.python)
    if args.bench:
        return _run_bench(args, settings, sandbox)
    live = DeepSeekProvider(settings) if args.live else None
    with tempfile.TemporaryDirectory(prefix="issuepilot-evaldb-") as tmp:
        isolated = dataclasses.replace(settings, db_path=str(Path(tmp) / "eval.db"))
        results = run_suite(isolated, sandbox, live_provider=live, only=args.case)
    if args.json:
        print(json.dumps([r.model_dump() for r in results], indent=2))
    else:
        print(summary(results))
    return 0 if results and all(r.passed for r in results) else 1


def _run_bench(args: argparse.Namespace, settings: Settings, sandbox: Sandbox) -> int:
    from issuepilot.evals.runner import bench_summary, run_bench

    with tempfile.TemporaryDirectory(prefix="issuepilot-evaldb-") as tmp:
        isolated = dataclasses.replace(settings, db_path=str(Path(tmp) / "bench.db"))
        results = run_bench(isolated, sandbox, DeepSeekProvider(settings), only=args.case)
    payload = [r.model_dump() for r in results]
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2) if args.json else bench_summary(results))
    return 0


def run_admin(args: argparse.Namespace, settings: Settings) -> int:
    store = TaskStore(settings.db_path)
    if args.command == "tasks":
        for t in store.recent():
            print(f"{t.id}  {t.status:<17} {t.mode:<8} ${t.cost_usd:.4f}  {t.issue[:60]!r}")
        return 0
    rec = store.get(args.task_id)
    if rec is None:
        print("error: unknown task", file=sys.stderr)
        return 1
    if args.command == "task":
        print(json.dumps(rec.model_dump(exclude={"result_json"}), indent=2))
        _print_event_log(store, rec.id)
    elif args.command == "approve":
        try:
            approve(store, rec.id)
        except NotApproved as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"approved {rec.id}. Next: issuepilot publish {rec.id}")
    elif args.command == "reject":
        try:
            reject(store, rec.id)
        except NotApproved as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"rejected {rec.id}")
    elif args.command in ("publish", "ci"):
        return run_github(args, settings, store, rec)
    elif args.command == "cancel":
        request_cancel(store, rec.id)
        print(f"cancellation requested for {rec.id}")
    elif args.command == "resume":
        if rec.status not in ("interrupted", "failed", "running"):
            print(f"error: task is '{rec.status}', not resumable", file=sys.stderr)
            return 1
        sandbox = default_sandbox() if rec.mode != "observe" else None
        done = run_task(settings, DeepSeekProvider(settings), store, rec.id,
                        sandbox=sandbox, resume=True)  # fmt: skip
        _print_result(done, result_of(done), args)
    return 0


def _origin(repo: str) -> str:
    out = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"],
                         capture_output=True, text=True)  # fmt: skip
    return out.stdout.strip()


def run_github(
    args: argparse.Namespace, settings: Settings, store: TaskStore, rec: TaskRecord
) -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    try:
        remote = args.remote or _origin(rec.repo)
        slug = args.slug or parse_slug(remote)
        client = GitHubClient(token)
        sandbox = default_sandbox(args.sandbox, args.python)
        if args.command == "publish":
            done = publish_task(settings, store, rec.id, client, slug=slug, remote=remote,
                                token=token, sandbox=sandbox)  # fmt: skip
            print(f"draft PR opened: {done.pr_url} (branch {done.branch}; NOT merged)")
        else:
            summary = ci_followup(
                settings, DeepSeekProvider(settings), store, rec.id, client,
                slug=slug, remote=remote, token=token, sandbox=sandbox,
            )  # fmt: skip
            print(f"CI: {summary.state}")
            if summary.failures:
                print(summary.text())
    except (NotApproved, GitHubError, GitError, SandboxUnavailable, ValueError) as exc:
        print(f"error: {redact(str(exc), (token,))}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        load_dotenv()
        settings = Settings.from_env()
        if args.command == "fix":
            return run_fix(args, settings)
        if args.command == "eval":
            return run_eval(args, settings)
        if args.command == "discover":
            return run_discover(args, settings)
        if args.command == "plan":
            return run_plan(args, settings)
        return run_admin(args, settings)
    except (
        ConfigError,
        PlanningError,
        ToolError,
        ValueError,
        httpx.HTTPError,
        OpenAIError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

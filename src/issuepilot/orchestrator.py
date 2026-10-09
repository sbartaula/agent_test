"""Task lifecycle: create -> run (bounded, cancellable, resumable) -> persist outcome."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from typing import Any

from issuepilot.agent.graph import FixResult, run_agent
from issuepilot.budget import BudgetExceeded, Cancelled, CancelToken, Counters, Limits
from issuepilot.config import Settings
from issuepilot.llm.base import LLMProvider
from issuepilot.modes import Mode, PermissionDenied
from issuepilot.persistence.tasks import TaskRecord, TaskStore
from issuepilot.runtime import TaskContext
from issuepilot.sandbox import Sandbox, SandboxUnavailable
from issuepilot.security import redact
from issuepilot.tools import ToolError

_TOKENS: dict[str, CancelToken] = {}
_LOCK = threading.Lock()


def cancel_token(task_id: str) -> CancelToken:
    with _LOCK:
        return _TOKENS.setdefault(task_id, CancelToken())


def request_cancel(store: TaskStore, task_id: str) -> bool:
    """Flag the task for cancellation (works across processes) and trip any local token."""
    store.request_cancel(task_id)
    with _LOCK:
        tok = _TOKENS.get(task_id)
    if tok:
        tok.cancel()
    return True


# Absolute ceilings: even a human-approved extension can never exceed these.
CEILING = Limits(max_cost_usd=5.0, max_seconds=3600.0, max_tool_calls=500)


class ExtensionError(ValueError):
    pass


def budget_suggestion(rec: TaskRecord) -> dict[str, Any] | None:
    """Heuristic estimate for a budget-stopped task: double whichever limits were exhausted."""
    if rec.status != "budget_exceeded":
        return None
    lim = Limits(**json.loads(rec.limits_json))
    dflt = Limits()
    extra: dict[str, Any] = {"extra_usd": 0.0, "extra_seconds": 0.0, "extra_tool_calls": 0}
    if rec.cost_usd >= lim.max_cost_usd:
        extra["extra_usd"] = round(max(lim.max_cost_usd, dflt.max_cost_usd - lim.max_cost_usd), 4)
    if rec.elapsed_s >= lim.max_seconds:
        extra["extra_seconds"] = max(lim.max_seconds, dflt.max_seconds - lim.max_seconds)
    if rec.tool_calls >= lim.max_tool_calls:
        extra["extra_tool_calls"] = max(
            lim.max_tool_calls, dflt.max_tool_calls - lim.max_tool_calls
        )
    if not any(extra.values()):  # reason not attributable: give a modest all-round bump
        extra = {"extra_usd": round(lim.max_cost_usd / 2, 4), "extra_seconds": lim.max_seconds / 2,
                 "extra_tool_calls": lim.max_tool_calls // 2}  # fmt: skip
    new = _extended(lim, **extra)
    return {
        **extra,
        "reason": rec.error,
        "new_limits": new.to_dict(),
        "note": (
            "Heuristic: raises exhausted limit(s) to at least the default, or doubles them. "
            "Spend so far is kept, not reset."
        ),
        "within_ceiling": new == _extended(lim, **extra, clamp=False),
    }


def _cap[T: (int, float)](value: T, ceiling: T, clamp: bool) -> T:
    return min(value, ceiling) if clamp else value


def _extended(lim: Limits, *, extra_usd: float = 0.0, extra_seconds: float = 0.0,
              extra_tool_calls: int = 0, clamp: bool = True) -> Limits:  # fmt: skip
    return replace(
        lim,
        max_cost_usd=_cap(lim.max_cost_usd + extra_usd, CEILING.max_cost_usd, clamp),
        max_seconds=_cap(lim.max_seconds + extra_seconds, CEILING.max_seconds, clamp),
        max_tool_calls=_cap(lim.max_tool_calls + extra_tool_calls, CEILING.max_tool_calls, clamp),
    )


def extend_budget(store: TaskStore, task_id: str, *, extra_usd: float = 0.0,
                  extra_seconds: float = 0.0, extra_tool_calls: int = 0) -> Limits:  # fmt: skip
    """Human-approved budget raise for a budget_exceeded task. Never exceeds CEILING."""
    rec = store.get(task_id)
    if rec is None:
        raise KeyError(task_id)
    if rec.status != "budget_exceeded":
        raise ExtensionError(f"task is '{rec.status}', only budget_exceeded tasks can be extended")
    if min(extra_usd, extra_seconds, extra_tool_calls) < 0 or not (
        extra_usd or extra_seconds or extra_tool_calls
    ):
        raise ExtensionError("provide a positive extra_usd, extra_seconds or extra_tool_calls")
    old = Limits(**json.loads(rec.limits_json))
    new = _extended(old, extra_usd=extra_usd, extra_seconds=extra_seconds,
                    extra_tool_calls=extra_tool_calls)  # fmt: skip
    if new == old:
        raise ExtensionError("hard ceiling already reached; cannot extend further")
    store.update(task_id, limits_json=json.dumps(new.to_dict()))
    store.add_event(task_id, "budget_extended", "human approved a budget extension",
                    {"old": old.to_dict(), "new": new.to_dict()})  # fmt: skip
    return new


def _watch_cancel(store: TaskStore, task_id: str, tok: CancelToken, stop: threading.Event) -> None:
    while not stop.wait(0.5):
        if store.cancel_requested(task_id):
            tok.cancel()
            return


def create_task(
    store: TaskStore, *, issue: str, repo: str, mode: Mode, limits: Limits, kind: str = "fix",
    subdir: str = "",
) -> str:  # fmt: skip
    import json

    return store.create(
        kind=kind, mode=str(mode), repo=repo, issue=issue,
        limits_json=json.dumps(limits.to_dict()), subdir=subdir,
    )  # fmt: skip


def _outcome_to_status(result: FixResult, mode: Mode) -> tuple[str, str]:
    """(task status, approval) for a finished agent run."""
    if result.status == "failed":
        return "failed", "none"
    if result.status == "verified" and mode == Mode.PR:
        return "awaiting_approval", "pending"
    return "completed", "none"


def run_task(
    settings: Settings,
    provider: LLMProvider,
    store: TaskStore,
    task_id: str,
    *,
    sandbox: Sandbox | None = None,
    run_tests: bool = True,
    resume: bool = False,
    extra_secrets: tuple[str, ...] = (),
) -> TaskRecord:
    """Execute an already-created task. Never raises for expected failures; records them."""
    rec = store.get(task_id)
    assert rec is not None, f"unknown task {task_id}"
    limits = Limits(**json.loads(rec.limits_json))
    mode = Mode(rec.mode)
    prior = (
        Counters(
            prompt_tokens=rec.prompt_tokens, completion_tokens=rec.completion_tokens,
            cost_usd=rec.cost_usd, tool_calls=rec.tool_calls, llm_calls=rec.llm_calls,
            retries=rec.retries, elapsed_s=rec.elapsed_s,
        )
        if resume else None
    )  # fmt: skip
    ctx = TaskContext(
        task_id, mode, settings, limits, store, cancel_token(task_id), prior, extra_secrets
    )
    store.update(task_id, status="running", error="", cancel_requested=0)
    stop = threading.Event()
    threading.Thread(
        target=_watch_cancel, args=(store, task_id, ctx.tracker.cancel, stop), daemon=True
    ).start()
    ctx.event("task_start", "resumed" if resume else "started", mode=str(mode), repo=rec.repo)
    try:
        result = run_agent(
            provider, settings, rec.issue, rec.repo, run_tests=run_tests and mode != Mode.OBSERVE,
            sandbox=sandbox, thread_id=task_id, max_attempts=limits.max_attempts, ctx=ctx,
            resume=resume,
        )  # fmt: skip
        status, approval = _outcome_to_status(result, mode)
        store.update(
            task_id, status=status, outcome=result.status, approval=approval,
            error=redact(result.error, (settings.api_key,)), result_json=result.model_dump_json(),
        )  # fmt: skip
        ctx.event("task_end", status, outcome=result.status)
    except Cancelled:
        store.update(task_id, status="cancelled", outcome="cancelled")
        ctx.event("task_end", "cancelled")
    except BudgetExceeded as exc:
        store.update(task_id, status="budget_exceeded", outcome="budget_exceeded", error=exc.reason)
        ctx.event("task_end", "budget_exceeded", reason=exc.reason)
    except (PermissionDenied, SandboxUnavailable, ToolError, OSError, ValueError) as exc:
        msg = redact(f"{type(exc).__name__}: {exc}", (settings.api_key, *extra_secrets))
        store.update(task_id, status="failed", outcome="failed", error=msg)
        ctx.event("task_end", "failed", error=msg)
    except Exception as exc:  # noqa: BLE001 - last-resort: record, keep resumable state
        msg = redact(f"{type(exc).__name__}: {exc}", (settings.api_key, *extra_secrets))
        store.update(task_id, status="failed", outcome="failed", error=msg)
        ctx.event("task_end", "failed", error=msg, unexpected=True)
    finally:
        stop.set()
        ctx.flush()
        with _LOCK:
            _TOKENS.pop(task_id, None)
    final = store.get(task_id)
    assert final is not None
    return final


def result_of(rec: TaskRecord) -> FixResult | None:
    if rec.kind != "fix" or rec.result_json in ("", "{}"):
        return None
    return FixResult.model_validate_json(rec.result_json)

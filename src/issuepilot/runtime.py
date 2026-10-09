"""Per-task runtime: permission checks, budgets, cancellation, metering and the event log.

Every tool call and every model call passes through a TaskContext, so limits and
audit logging cannot be bypassed by individual tools.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from issuepilot.budget import BudgetExceeded, BudgetTracker, CancelToken, Counters, Limits
from issuepilot.config import Settings
from issuepilot.llm.base import ChatMessage, LLMProvider, LLMResponse
from issuepilot.modes import Capability, Mode, require
from issuepilot.persistence.tasks import TaskStore
from issuepilot.security import redact


class TaskContext:
    def __init__(
        self,
        task_id: str,
        mode: Mode,
        settings: Settings,
        limits: Limits | None = None,
        store: TaskStore | None = None,
        cancel: CancelToken | None = None,
        prior: Counters | None = None,
        extra_secrets: tuple[str, ...] = (),
    ) -> None:
        self.task_id = task_id
        self.mode = mode
        self.settings = settings
        self.store = store
        self.tracker = BudgetTracker(limits or Limits(), cancel, prior)
        self._secrets = (settings.api_key, *extra_secrets)
        self.memory_events: list[dict[str, Any]] = []

    @property
    def limits(self) -> Limits:
        return self.tracker.limits

    def check(self) -> None:
        self.tracker.check()

    def event(self, kind: str, message: str, **data: Any) -> None:
        msg = redact(message, self._secrets)
        clean = {k: redact(v, self._secrets) if isinstance(v, str) else v for k, v in data.items()}
        self.memory_events.append({"kind": kind, "message": msg, **clean})
        if self.store:
            self.store.add_event(self.task_id, kind, msg, clean)

    def flush(self) -> None:
        if not self.store:
            return
        c = self.tracker.counters
        self.store.update(
            self.task_id,
            prompt_tokens=c.prompt_tokens,
            completion_tokens=c.completion_tokens,
            cost_usd=c.cost_usd,
            tool_calls=c.tool_calls,
            llm_calls=c.llm_calls,
            retries=c.retries,
            elapsed_s=c.elapsed_s,
        )

    def retry(self, why: str) -> None:
        self.tracker.add_retry()
        self.event("retry", why)
        self.flush()

    @contextmanager
    def tool(self, name: str, cap: Capability, **info: Any) -> Iterator[None]:
        """Gate + meter + audit one tool invocation."""
        require(self.mode, cap, name)
        self.check()
        self.tracker.add_tool_call()
        t0 = time.monotonic()
        self.event("tool_start", name, capability=str(cap), **info)
        try:
            yield
        except BaseException as exc:
            self.event("tool_error", name, error=f"{type(exc).__name__}: {exc}"[:500],
                       duration_s=round(time.monotonic() - t0, 3))  # fmt: skip
            self.flush()
            raise
        self.event("tool_end", name, duration_s=round(time.monotonic() - t0, 3))
        self.flush()


class MeteredProvider:
    """Wraps any LLMProvider: enforces spend/time/cancel before and after each call."""

    def __init__(self, inner: LLMProvider, ctx: TaskContext) -> None:
        self._inner = inner
        self._ctx = ctx

    def complete(
        self, messages: list[ChatMessage], *, json_mode: bool = False, max_tokens: int | None = None
    ) -> LLMResponse:
        ctx, s = self._ctx, self._ctx.settings
        ctx.check()
        remaining = ctx.tracker.remaining_usd()
        est_in = sum(len(m.content) for m in messages) / 3.5  # conservative chars->tokens
        out_budget = (remaining - est_in * s.price_input_per_m / 1e6) * 1e6 / s.price_output_per_m
        if out_budget < 256:
            raise BudgetExceeded("not enough spend budget left for another model call")
        cap = int(min(out_budget, 8192, max_tokens or 8192))  # hard output cap
        t0 = time.monotonic()
        resp = self._inner.complete(messages, json_mode=json_mode, max_tokens=cap)
        ctx.tracker.add_usage(resp.usage, s.price_input_per_m, s.price_output_per_m)
        c = ctx.tracker.counters
        ctx.event(
            "llm_call", f"{resp.model}",
            prompt_tokens=resp.usage.prompt_tokens, completion_tokens=resp.usage.completion_tokens,
            duration_s=round(time.monotonic() - t0, 3), total_cost_usd=round(c.cost_usd, 6),
        )  # fmt: skip
        ctx.flush()
        return resp

"""Hard limits (spend, time, tool calls, retries) and cooperative cancellation."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass

from pydantic import BaseModel

from issuepilot.llm.base import Usage


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Cancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    max_cost_usd: float = 0.10
    max_seconds: float = 600.0
    max_tool_calls: int = 80
    max_attempts: int = 3  # edit -> verify cycles
    max_ci_rounds: int = 2  # follow-up fixes after CI failures

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


class CancelToken:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()


class Counters(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    llm_calls: int = 0
    retries: int = 0
    elapsed_s: float = 0.0


class BudgetTracker:
    """Thread-safe usage counters. `prior` restores totals when resuming a task."""

    def __init__(self, limits: Limits, cancel: CancelToken | None = None,
                 prior: Counters | None = None) -> None:  # fmt: skip
        self.limits = limits
        self.cancel = cancel or CancelToken()
        self._c = (prior or Counters()).model_copy()
        self._base_elapsed = self._c.elapsed_s
        self._t0 = time.monotonic()
        self._lock = threading.Lock()

    @property
    def counters(self) -> Counters:
        with self._lock:
            return self._c.model_copy(update={"elapsed_s": self.elapsed()})

    def elapsed(self) -> float:
        return self._base_elapsed + (time.monotonic() - self._t0)

    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.max_seconds - self.elapsed())

    def remaining_usd(self) -> float:
        return max(0.0, self.limits.max_cost_usd - self._c.cost_usd)

    def check(self) -> None:
        if self.cancel.cancelled:
            raise Cancelled("task cancelled")
        if self.elapsed() >= self.limits.max_seconds:
            raise BudgetExceeded(f"time limit {self.limits.max_seconds:.0f}s reached")
        if self._c.cost_usd >= self.limits.max_cost_usd:
            raise BudgetExceeded(f"spend limit ${self.limits.max_cost_usd:.4f} reached")
        if self._c.tool_calls >= self.limits.max_tool_calls:
            raise BudgetExceeded(f"tool-call limit {self.limits.max_tool_calls} reached")

    def add_usage(self, usage: Usage, price_in: float, price_out: float) -> None:
        with self._lock:
            self._c.prompt_tokens += usage.prompt_tokens
            self._c.completion_tokens += usage.completion_tokens
            self._c.llm_calls += 1
            self._c.cost_usd += (
                usage.prompt_tokens * price_in + usage.completion_tokens * price_out
            ) / 1_000_000

    def add_tool_call(self) -> None:
        with self._lock:
            self._c.tool_calls += 1

    def add_retry(self) -> None:
        with self._lock:
            self._c.retries += 1

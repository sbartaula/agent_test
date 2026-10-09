from __future__ import annotations

from pathlib import Path
from typing import Protocol

from issuepilot.budget import CancelToken
from issuepilot.tools.testrunner import TestResult


class SandboxUnavailable(RuntimeError):
    pass


class Sandbox(Protocol):
    name: str

    def run_tests(
        self, workspace: Path, *, timeout: float, cancel: CancelToken | None = None
    ) -> TestResult: ...

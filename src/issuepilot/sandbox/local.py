"""Host-process runner. UNSAFE for untrusted repos: only for development/tests."""

from __future__ import annotations

from pathlib import Path

from issuepilot.budget import CancelToken
from issuepilot.tools.testrunner import TestResult, run_pytest


class LocalSandbox:
    name = "local"

    def __init__(self, python: str | None = None) -> None:
        self.python = python

    def run_tests(
        self, workspace: Path, *, timeout: float, cancel: CancelToken | None = None
    ) -> TestResult:
        return run_pytest(workspace, timeout=timeout, python=self.python)

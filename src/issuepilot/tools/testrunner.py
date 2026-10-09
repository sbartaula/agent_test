"""Pytest execution helpers: junit parsing and the (unsafe, opt-in) host runner.

The default execution path is the Docker sandbox; `run_pytest` on the host is only
reachable through LocalSandbox, which must be requested explicitly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import BaseModel, Field

JUNIT_NAME = ".issuepilot-junit.xml"
_PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV")


class TestResult(BaseModel):
    __test__ = False  # not a pytest class
    passed: bool
    output: str
    timed_out: bool = False
    cancelled: bool = False
    infra_error: str = ""  # sandbox/tooling failure, not a test failure
    exit_code: int | None = None
    passed_tests: list[str] = Field(default_factory=list)
    failed_tests: list[str] = Field(default_factory=list)
    duration_s: float = 0.0


def parse_junit(path: Path) -> tuple[list[str], list[str]]:
    """Return (passed_ids, failed_ids). Collection errors count as failures."""
    ok: list[str] = []
    bad: list[str] = []
    if not path.is_file():
        return ok, bad
    try:
        root = ET.parse(path).getroot()  # noqa: S314 (file produced in our own sandbox)
    except ET.ParseError:
        return ok, bad
    for tc in root.iter("testcase"):
        tid = f"{tc.get('classname', '')}::{tc.get('name', '')}"
        if tc.find("failure") is not None or tc.find("error") is not None:
            bad.append(tid)
        elif tc.find("skipped") is None:
            ok.append(tid)
    return ok, bad


def finish_result(
    workspace: Path, exit_code: int | None, output: str, duration: float, max_output: int = 6000
) -> TestResult:
    junit = workspace / JUNIT_NAME
    ok, bad = parse_junit(junit)
    junit.unlink(missing_ok=True)
    return TestResult(
        passed=exit_code == 0, output=output[-max_output:], exit_code=exit_code,
        passed_tests=ok, failed_tests=bad, duration_s=round(duration, 2),
    )  # fmt: skip


def run_pytest(
    cwd: Path, timeout: float = 120, max_output: int = 6000, python: str | None = None
) -> TestResult:
    # Scrubbed env: API keys and tokens from our process are not exposed to repo code.
    env = {k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ}
    env["PYTHONPATH"] = str(cwd / "src") + os.pathsep + str(cwd)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [
                python or sys.executable, "-m", "pytest",
                "-q", "--no-header", "-p", "no:cacheprovider", f"--junitxml={JUNIT_NAME}",
            ],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return TestResult(passed=False, output=f"pytest timed out after {timeout}s", timed_out=True)
    return finish_result(
        cwd, proc.returncode, proc.stdout + proc.stderr, time.monotonic() - t0, max_output
    )

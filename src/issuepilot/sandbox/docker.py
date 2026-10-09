"""Docker sandbox: tests run with no network, no secrets, non-root, capped resources.

Dependencies are installed in a separate network-enabled step into a per-requirements
cache directory (pip only; the repo's own code is never executed in that step except
through package build scripts of third-party dependencies, inside the container).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
import tomllib
import uuid
from pathlib import Path

from issuepilot.budget import CancelToken
from issuepilot.sandbox.base import SandboxUnavailable
from issuepilot.tools.testrunner import JUNIT_NAME, TestResult, finish_result

DEFAULT_IMAGE = "python:3.12-slim"
_HARDENING = [
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    "--pids-limit", "256", "--memory", "1g", "--cpus", "2",
    "--tmpfs", "/tmp:rw,size=256m",
]  # fmt: skip


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def dependency_args(ws: Path) -> list[str]:
    """pip args from requirement files or pyproject metadata (the repo itself is not built)."""
    args: list[str] = []
    for name in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt"):
        if (ws / name).is_file():
            args += ["-r", f"/work/{name}"]
    if not args and (ws / "pyproject.toml").is_file():
        try:
            data = tomllib.loads((ws / "pyproject.toml").read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            return args
        proj = data.get("project", {})
        deps = list(proj.get("dependencies", []))
        for grp in ("dev", "test", "tests"):
            deps += proj.get("optional-dependencies", {}).get(grp, [])
            deps += [
                d for d in data.get("dependency-groups", {}).get(grp, []) if isinstance(d, str)
            ]
        args += deps
    return args


class DockerSandbox:
    name = "docker"

    def __init__(self, image: str = DEFAULT_IMAGE, cache_dir: Path | None = None) -> None:
        if not docker_available():
            raise SandboxUnavailable("Docker is not available; refusing to run repo code on host")
        self.image = image
        self.cache_dir = cache_dir or Path.home() / ".cache" / "issuepilot" / "deps"

    def _user(self) -> list[str]:
        return ["--user", f"{os.getuid()}:{os.getgid()}"]

    def _deps(self, ws: Path, cancel: CancelToken | None, timeout: float) -> Path:
        args = dependency_args(ws)
        key = hashlib.sha256(
            (self.image + "|" + "|".join(args) + "|" + "".join(
                (ws / a[len("/work/"):]).read_text(encoding="utf-8")
                for a in args if a.startswith("/work/") and (ws / a[len("/work/"):]).is_file()
            )).encode()
        ).hexdigest()[:16]  # fmt: skip
        target = self.cache_dir / key
        if (target / ".ok").exists():
            return target
        target.mkdir(parents=True, exist_ok=True)
        res = self._run(
            ["--network", "bridge", "-v", f"{ws}:/work:ro", "-v", f"{target}:/deps",
             "-e", "HOME=/tmp", "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1", "-w", "/tmp"],
            ["python", "-m", "pip", "install", "-q", "--target", "/deps", "pytest", *args],
            cancel, timeout,
        )  # fmt: skip
        if res[0] != 0:
            shutil.rmtree(target, ignore_errors=True)
            raise SandboxUnavailable(f"dependency install failed: {res[1][-800:]}")
        (target / ".ok").touch()
        return target

    def _run(
        self, docker_args: list[str], cmd: list[str], cancel: CancelToken | None, timeout: float
    ) -> tuple[int | None, str, bool, bool]:
        name = f"issuepilot-{uuid.uuid4().hex[:10]}"
        argv = ["docker", "run", "--rm", "--name", name, *_HARDENING, *self._user(),
                *docker_args, self.image, *cmd]  # fmt: skip
        log = self.cache_dir.parent / f"{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        timed_out = cancelled = False
        with log.open("wb") as fh:
            proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT)  # noqa: S603
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if cancel and cancel.cancelled:
                    cancelled = True
                elif time.monotonic() > deadline:
                    timed_out = True
                if cancelled or timed_out:
                    subprocess.run(["docker", "kill", name], capture_output=True, check=False)
                    proc.wait(timeout=20)
                    break
                time.sleep(0.1)
        out = log.read_text(errors="replace")
        log.unlink(missing_ok=True)
        return proc.returncode, out, timed_out, cancelled

    def run_tests(
        self, workspace: Path, *, timeout: float, cancel: CancelToken | None = None
    ) -> TestResult:
        t0 = time.monotonic()
        try:
            deps = self._deps(workspace, cancel, min(timeout * 3, 300))
        except SandboxUnavailable as exc:
            return TestResult(passed=False, output="", infra_error=str(exc))
        code, out, timed_out, cancelled = self._run(
            ["--network", "none", "-v", f"{workspace}:/work", "-v", f"{deps}:/deps:ro",
             "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
             "-e", "PYTHONPATH=/work/src:/work:/deps", "-w", "/work"],
            ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
             f"--junitxml={JUNIT_NAME}"],
            cancel, timeout,
        )  # fmt: skip
        if timed_out or cancelled:
            (workspace / JUNIT_NAME).unlink(missing_ok=True)
            return TestResult(
                passed=False, timed_out=timed_out, cancelled=cancelled, exit_code=code,
                output=f"pytest {'timed out' if timed_out else 'cancelled'}", duration_s=timeout,
            )  # fmt: skip
        return finish_result(workspace, code, out, time.monotonic() - t0)

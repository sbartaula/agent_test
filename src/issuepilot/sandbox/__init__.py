from __future__ import annotations

import os

from issuepilot.sandbox.base import Sandbox, SandboxUnavailable
from issuepilot.sandbox.docker import DockerSandbox, docker_available
from issuepilot.sandbox.local import LocalSandbox


def default_sandbox(kind: str | None = None, python: str | None = None) -> Sandbox:
    """Docker unless the user explicitly opts into the unsafe host runner."""
    kind = kind or os.environ.get("ISSUEPILOT_SANDBOX", "docker")
    if kind == "local":
        return LocalSandbox(python)
    if kind != "docker":
        raise SandboxUnavailable(f"Unknown sandbox '{kind}'")
    return DockerSandbox(os.environ.get("ISSUEPILOT_SANDBOX_IMAGE", "python:3.12-slim"))


__all__ = ["DockerSandbox", "LocalSandbox", "Sandbox", "SandboxUnavailable",
           "default_sandbox", "docker_available"]  # fmt: skip

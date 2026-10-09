"""Permission modes. Each tool call declares a capability; the mode decides if it is allowed."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    OBSERVE = "observe"  # read-only: inspect, analyse, report
    DEVELOP = "develop"  # + edit in a disposable workspace and run tests in the sandbox
    PR = "pr"  # + branch/commit/push/draft PR (only after explicit approval)


class Capability(StrEnum):
    READ = "read"
    SANDBOX = "sandbox"
    GITHUB_READ = "github_read"
    GITHUB_WRITE = "github_write"


_ALLOWED: dict[Mode, frozenset[Capability]] = {
    Mode.OBSERVE: frozenset({Capability.READ, Capability.GITHUB_READ}),
    Mode.DEVELOP: frozenset({Capability.READ, Capability.GITHUB_READ, Capability.SANDBOX}),
    Mode.PR: frozenset(Capability),
}


class PermissionDenied(RuntimeError):
    pass


def require(mode: Mode, cap: Capability, tool: str) -> None:
    if cap not in _ALLOWED[mode]:
        raise PermissionDenied(f"'{tool}' needs '{cap}' which mode '{mode}' does not grant")

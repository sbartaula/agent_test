"""Secret redaction and write-path policy shared by every layer."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)\S+"),
]
_PROTECTED_TOP = {".git", ".github", ".circleci", ".gitlab-ci.yml", "Jenkinsfile"}
_SECRET_FILE = re.compile(r"^(\.env(\..*)?|.*\.(pem|key|p12)|id_rsa.*|credentials.*)$", re.I)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for s in secrets:
        if s and len(s) >= 8:
            text = text.replace(s, "[REDACTED]")
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


def is_protected_write(rel: str) -> bool:
    """Paths the agent may never modify: git internals, CI config, secret files."""
    parts = PurePosixPath(rel).parts
    if not parts:
        return True
    if parts[0] in _PROTECTED_TOP or ".git" in parts:
        return True
    return bool(_SECRET_FILE.match(parts[-1])) and parts[-1] != ".env.example"

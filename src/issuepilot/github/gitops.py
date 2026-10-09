"""Fixed-argv git operations for publishing a branch. No shell, no model-chosen arguments.

Safety invariants (enforced here, not by the prompt):
 * pushes only `refs/heads/issuepilot/<id>`; never the default branch, never --force
 * the token reaches git through GIT_ASKPASS, never in a URL, argv or on disk beyond a 0700 temp dir
 * commits stage only the explicitly edited paths
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from issuepilot.security import is_protected_write, redact
from issuepilot.tools.patching import Edit, apply_edits

BRANCH_RE = re.compile(r"^issuepilot/[A-Za-z0-9._-]{1,60}$")
SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_URL_SLUG = re.compile(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


ASKPASS = """#!/bin/sh
case "$1" in
*sername*) echo x-access-token;;
*) echo "$ISSUEPILOT_TOKEN";;
esac
"""


class GitError(RuntimeError):
    pass


def parse_slug(remote: str) -> str:
    if SLUG_RE.match(remote):
        return remote
    m = _URL_SLUG.search(remote)
    if not m:
        raise GitError("Cannot derive OWNER/REPO from remote; pass --slug")
    return m.group(1)


class GitWorkspace:
    """A fresh clone in a temp dir. Use as a context manager; it is deleted afterwards."""

    def __init__(self, remote: str, token: str = "", default_branch: str = "main") -> None:
        self.remote = remote
        self.token = token
        self.default_branch = default_branch
        self._tmp = tempfile.TemporaryDirectory(prefix="issuepilot-git-")
        self.path = Path(self._tmp.name) / "repo"
        self._askpass = Path(self._tmp.name) / "askpass.sh"
        self.branch = ""

    def __enter__(self) -> GitWorkspace:
        if self.token:
            self._askpass.write_text(ASKPASS)
            self._askpass.chmod(stat.S_IRWXU)
        return self

    def __exit__(self, *_: object) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        env = {
            "PATH": os.environ.get("PATH", ""), "HOME": self._tmp.name,
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ALLOW_PROTOCOL": "https:file:ssh",
        }  # fmt: skip
        if self.token:
            env.update(GIT_ASKPASS=str(self._askpass), ISSUEPILOT_TOKEN=self.token)
        cmd = ["git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", *args]
        try:
            proc = subprocess.run(
                cmd, cwd=cwd or self.path, env=env, capture_output=True, text=True, timeout=180,
            )  # fmt: skip
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} timed out") from exc
        if proc.returncode != 0:
            err = redact((proc.stderr or proc.stdout)[:500], (self.token,))
            raise GitError(f"git {args[0]} failed: {err}")
        return proc.stdout.strip()

    def clone(self) -> None:
        self._git(
            "clone",
            "--depth",
            "50",
            "--no-tags",
            self.remote,
            str(self.path),
            cwd=Path(self._tmp.name),
        )
        self._git("config", "user.name", "IssuePilot")
        self._git("config", "user.email", "issuepilot@users.noreply.github.com")

    def create_branch(self, name: str) -> None:
        if not BRANCH_RE.match(name) or name == self.default_branch:
            raise GitError(f"Refusing branch name {name!r}")
        self._git("checkout", "-b", name)
        self.branch = name

    def checkout_remote_branch(self, name: str) -> None:
        if not BRANCH_RE.match(name):
            raise GitError(f"Refusing branch name {name!r}")
        self._git("fetch", "origin", f"{name}:{name}")
        self._git("checkout", name)
        self.branch = name

    def apply_and_commit(self, edits: list[Edit], message: str) -> str:
        """Apply edits, stage ONLY those paths, commit. Returns the new commit sha."""
        apply_edits(self.path, edits)
        paths = sorted({e.path for e in edits if not is_protected_write(e.path)})
        if not paths:
            raise GitError("nothing to commit")
        self._git("add", "--", *paths)
        if not self._git("diff", "--cached", "--name-only"):
            raise GitError("edits produced no changes")
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD")

    def push(self) -> None:
        if not BRANCH_RE.match(self.branch) or self.branch == self.default_branch:
            raise GitError("Refusing to push: not a dedicated issuepilot/ branch")
        self._git("push", "origin", f"refs/heads/{self.branch}:refs/heads/{self.branch}")

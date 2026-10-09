"""Apply exact-match edits to a throwaway copy of the repo and render a unified diff.

The user's working tree is never modified; the result is a patch to review and apply.
"""

from __future__ import annotations

import difflib
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel

from issuepilot.security import is_protected_write
from issuepilot.tools.repo import IGNORED_DIRS, RepoTools, ToolError, is_secret_path


class Edit(BaseModel):
    """Replace `old` (must occur exactly once) with `new`. Empty `old` creates a new file."""

    path: str
    old: str = ""
    new: str


def _ignore(_dir: str, names: list[str]) -> list[str]:
    return [n for n in names if n in IGNORED_DIRS or is_secret_path(Path(n))]


@contextmanager
def sandbox_copy(root: Path) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="issuepilot-") as tmp:
        dest = Path(tmp) / "repo"
        shutil.copytree(root, dest, ignore=_ignore, symlinks=True)
        yield dest


MAX_EDITS = 20
MAX_EDIT_BYTES = 200_000


def apply_edits(sandbox: Path, edits: list[Edit]) -> None:
    if len(edits) > MAX_EDITS or sum(len(e.new) for e in edits) > MAX_EDIT_BYTES:
        raise ToolError("Edit set too large")
    tools = RepoTools(sandbox)
    for e in edits:
        if is_protected_write(e.path):
            raise ToolError(f"Writing to protected path is not allowed: {e.path}")
        target = tools.resolve(e.path)
        if e.old == "":
            if target.exists():
                raise ToolError(f"Cannot create {e.path}: already exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(e.new, encoding="utf-8")
            continue
        if not target.is_file():
            raise ToolError(f"Cannot edit missing file: {e.path}")
        text = target.read_text(encoding="utf-8")
        count = text.count(e.old)
        if count != 1:
            raise ToolError(
                f"Edit for {e.path}: 'old' text matched {count} times (need exactly 1). "
                f"Current file contents:\n{text[:6000]}"
            )
        target.write_text(text.replace(e.old, e.new, 1), encoding="utf-8")


def render_diff(original_root: Path, sandbox: Path, edits: list[Edit]) -> str:
    chunks: list[str] = []
    for path in dict.fromkeys(e.path for e in edits):
        before = original_root / path
        old = (
            before.read_text(encoding="utf-8").splitlines(keepends=True) if before.is_file() else []
        )
        new = (sandbox / path).read_text(encoding="utf-8").splitlines(keepends=True)
        fromfile = f"a/{path}" if old else "/dev/null"
        chunks.extend(difflib.unified_diff(old, new, fromfile, f"b/{path}"))
        if chunks and not chunks[-1].endswith("\n"):
            chunks[-1] += "\n\\ No newline at end of file\n"
    return "".join(chunks)

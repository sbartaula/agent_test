"""Read-only repository inspection. All paths are confined to the repo root."""

from __future__ import annotations

import re
from pathlib import Path

IGNORED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".issuepilot",
}  # fmt: skip
SECRET_NAMES = re.compile(r"^(\.env(\..*)?|.*\.(pem|key|p12)|id_rsa.*|credentials.*)$", re.I)
MAX_FILE_BYTES = 200_000


class ToolError(RuntimeError):
    pass


def is_secret_path(path: Path) -> bool:
    return bool(SECRET_NAMES.match(path.name)) and path.name != ".env.example"


class RepoTools:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ToolError(f"Not a directory: {root}")

    def resolve(self, rel: str) -> Path:
        """Resolve a repo-relative path; reject escapes, ignored dirs and secret files."""
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            raise ToolError(f"Path escapes repository: {rel}")
        parts = p.relative_to(self.root).parts
        if any(part in IGNORED_DIRS for part in parts) or is_secret_path(p):
            raise ToolError(f"Path not allowed: {rel}")
        return p

    def list_files(self, suffixes: tuple[str, ...] = (".py",), limit: int = 2000) -> list[str]:
        out: list[str] = []
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if any(part in IGNORED_DIRS for part in rel.parts) or not p.is_file():
                continue
            if p.suffix in suffixes and not is_secret_path(p) and not p.is_symlink():
                out.append(rel.as_posix())
                if len(out) >= limit:
                    break
        return out

    def read_file(self, rel: str) -> str:
        p = self.resolve(rel)
        if not p.is_file() or p.is_symlink():
            raise ToolError(f"Not a regular file: {rel}")
        if p.stat().st_size > MAX_FILE_BYTES:
            raise ToolError(f"File too large: {rel}")
        return p.read_text(encoding="utf-8", errors="replace")

    def search(self, term: str, limit: int = 20) -> list[str]:
        """Files containing `term` (case-sensitive substring), most hits first."""
        hits: list[tuple[int, str]] = []
        for rel in self.list_files():
            try:
                n = self.read_file(rel).count(term)
            except ToolError:
                continue
            if n:
                hits.append((n, rel))
        hits.sort(key=lambda h: (-h[0], h[1]))
        return [rel for _, rel in hits[:limit]]

    def rank(self, issue: str, limit: int = 15) -> list[str]:
        """Rank files by overlap with issue keywords (path hits weigh more than body hits)."""
        words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", issue)}
        words -= _STOPWORDS
        routes = re.findall(r"/[a-z][\w/{}-]*", issue.lower())
        scored: list[tuple[int, str]] = []
        for rel in self.list_files():
            try:
                body = self.read_file(rel).lower()
            except ToolError:
                continue
            path = rel.lower()
            score = sum(3 for w in words if w in path) + sum(1 for w in words if w in body)
            score += sum(4 for r in routes if r in body)
            if "test" in path:
                score = score // 2 + 1 if score else 0  # tests rank below the code they cover
            if score:
                scored.append((score, rel))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [rel for _, rel in scored[:limit]]


_STOPWORDS = {
    "this", "that", "with", "from", "have", "when", "should", "returns", "return", "instead",
    "error", "issue", "expected", "actual", "which", "there", "their", "would", "does", "doesn",
    "into", "ather", "than", "then", "also", "after", "before", "because",
}  # fmt: skip

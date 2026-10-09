"""Minimal GitHub REST client. Only the endpoints IssuePilot needs; no merge endpoint exists.

Use a fine-grained token scoped to ONE repository with: Contents: write (push the branch),
Pull requests: write (draft PR), Checks/Actions: read, Issues: read, Metadata: read.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from issuepilot.security import redact

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class CheckSummary(BaseModel):
    sha: str
    state: str  # pending | success | failure | none
    failures: list[str] = Field(default_factory=list)  # one readable block per failed check

    def text(self) -> str:
        return "\n\n".join(self.failures)[:6000]


class GitHubClient:
    def __init__(
        self, token: str, *, transport: httpx.BaseTransport | None = None, base_url: str = API
    ) -> None:
        if not token:
            raise GitHubError("A GitHub token is required (set GITHUB_TOKEN)")
        self._token = token
        self._http = httpx.Client(
            base_url=base_url, timeout=20, transport=transport,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )  # fmt: skip

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise GitHubError(redact(f"GitHub request failed: {exc}", (self._token,))) from exc
        if resp.status_code >= 400:
            msg = redact(resp.text[:300], (self._token,))
            raise GitHubError(f"GitHub {resp.status_code} on {method} {path}: {msg}")
        return resp.json() if resp.content else {}

    def default_branch(self, slug: str) -> str:
        return str(self._req("GET", f"/repos/{slug}")["default_branch"])

    def create_draft_pr(
        self, slug: str, *, head: str, base: str, title: str, body: str
    ) -> dict[str, Any]:
        if head == base:
            raise GitHubError("head and base branch must differ")
        data = self._req(
            "POST", f"/repos/{slug}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": True},
        )  # fmt: skip
        if not data.get("draft"):
            raise GitHubError("GitHub did not create the pull request as a draft")
        return dict(data)

    def check_summary(self, slug: str, ref: str) -> CheckSummary:
        """Summarise check runs for a commit; failed ones include annotations."""
        data = self._req("GET", f"/repos/{slug}/commits/{ref}/check-runs", params={"per_page": 50})
        runs = data.get("check_runs", [])
        if not runs:
            return CheckSummary(sha=ref, state="none")
        if any(r["status"] != "completed" for r in runs):
            return CheckSummary(sha=ref, state="pending")
        bad = [r for r in runs if r["conclusion"] not in ("success", "neutral", "skipped")]
        if not bad:
            return CheckSummary(sha=ref, state="success")
        failures = []
        for r in bad:
            out = r.get("output") or {}
            lines = [f"check '{r['name']}' {r['conclusion']}: {out.get('title') or ''}",
                     (out.get("summary") or "")[:800]]  # fmt: skip
            if out.get("annotations_count"):
                ann = self._req("GET", f"/repos/{slug}/check-runs/{r['id']}/annotations")
                lines += [f"{a['path']}:{a['start_line']} {a['message']}"[:300] for a in ann[:15]]
            failures.append(redact("\n".join(x for x in lines if x), (self._token,)))
        return CheckSummary(sha=ref, state="failure", failures=failures)

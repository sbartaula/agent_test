"""Minimal GitHub REST client. Only the endpoints IssuePilot needs.

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
        """Summarise check runs and legacy commit statuses for a commit."""
        data = self._req("GET", f"/repos/{slug}/commits/{ref}/check-runs", params={"per_page": 50})
        runs = data.get("check_runs", [])
        bad = [
            r
            for r in runs
            if r["status"] == "completed"
            and r["conclusion"] not in ("success", "neutral", "skipped")
        ]
        run_pending = any(r["status"] != "completed" for r in runs)
        failures: list[str] = []
        for r in bad:
            out = r.get("output") or {}
            lines = [f"check '{r['name']}' {r['conclusion']}: {out.get('title') or ''}",
                     (out.get("summary") or "")[:800]]  # fmt: skip
            if out.get("annotations_count"):
                ann = self._req("GET", f"/repos/{slug}/check-runs/{r['id']}/annotations")
                lines += [f"{a['path']}:{a['start_line']} {a['message']}"[:300] for a in ann[:15]]
            failures.append(redact("\n".join(x for x in lines if x), (self._token,)))

        status_data = self._req("GET", f"/repos/{slug}/commits/{ref}/status")
        status_state = status_data.get("state", "none")
        statuses = status_data.get("statuses", [])
        status_failures = [
            f"status '{s.get('context', 'unknown')}' {s.get('state', 'unknown')}: "
            f"{s.get('description') or ''}"
            for s in statuses
            if s.get("state") in ("failure", "error")
        ]
        failures.extend(redact(message, (self._token,)) for message in status_failures)

        if bad or status_state in ("failure", "error"):
            return CheckSummary(sha=ref, state="failure", failures=failures)
        if run_pending or status_state == "pending":
            return CheckSummary(sha=ref, state="pending")
        if runs or statuses or status_state == "success":
            return CheckSummary(sha=ref, state="success")
        return CheckSummary(sha=ref, state="none")

    def merge_pull_request(
        self, slug: str, number: int, *, expected_branch: str, expected_base: str
    ) -> dict[str, Any]:
        """Merge the expected open PR only after all GitHub check runs pass.

        The head SHA is sent to GitHub as an optimistic concurrency guard so a
        newly pushed commit cannot be merged based on stale check results.
        """
        path = f"/repos/{slug}/pulls/{number}"
        pr = self._req("GET", path)
        if pr.get("state") != "open":
            raise GitHubError("pull request is not open")
        if pr.get("merged"):
            raise GitHubError("pull request is already merged")
        if (pr.get("head") or {}).get("ref") != expected_branch:
            raise GitHubError("pull request head branch does not match this task")
        if (pr.get("base") or {}).get("ref") != expected_base:
            raise GitHubError("pull request base branch does not match the expected default branch")
        sha = str((pr.get("head") or {}).get("sha", ""))
        if not sha:
            raise GitHubError("pull request has no head commit SHA")

        checks = self.check_summary(slug, sha)
        if checks.state != "success":
            raise GitHubError(
                f"cannot merge: GitHub checks are {checks.state}"
                + (f": {checks.text()}" if checks.failures else "")
            )

        if pr.get("draft"):
            ready = self._req("PATCH", path, json={"draft": False})
            if ready.get("draft") is not False:
                raise GitHubError("GitHub did not mark the draft pull request ready")

        result = self._req(
            "PUT",
            f"{path}/merge",
            json={"sha": sha, "merge_method": "merge"},
        )
        if result.get("merged") is not True:
            raise GitHubError(f"GitHub did not merge the pull request: {result.get('message', '')}")
        return {"sha": sha, "message": str(result.get("message", "Pull request merged"))}

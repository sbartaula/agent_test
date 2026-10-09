"""Read-only GitHub access: fetch an issue's text. No write endpoints exist here."""

from __future__ import annotations

import os
import re

import httpx

ISSUE_URL = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)/?$")


def fetch_issue(url: str, client: httpx.Client | None = None) -> str:
    m = ISSUE_URL.match(url)
    if not m:
        raise ValueError("Expected URL like https://github.com/OWNER/REPO/issues/123")
    owner, repo, num = m.groups()
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    c = client or httpx.Client(timeout=20)
    resp = c.get(f"https://api.github.com/repos/{owner}/{repo}/issues/{num}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return f"{data['title']}\n\n{data.get('body') or ''}".strip()

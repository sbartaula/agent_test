"""GitHub integration. Deliberately has no merge, no force-push and no default-branch write."""

from issuepilot.github.client import CheckSummary, GitHubClient, GitHubError
from issuepilot.github.gitops import GitError, GitWorkspace, parse_slug

__all__ = ["CheckSummary", "GitError", "GitHubClient", "GitHubError", "GitWorkspace", "parse_slug"]

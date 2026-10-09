from pathlib import Path

import httpx
import pytest

from issuepilot.tools import Edit, RepoTools, ToolError, apply_edits, render_diff, sandbox_copy
from issuepilot.tools.github import fetch_issue


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def f():\n    return 1\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "x.py").write_text("x = 1\n")
    return tmp_path


def test_list_and_search_skip_ignored(repo: Path) -> None:
    t = RepoTools(repo)
    assert t.list_files() == ["app/main.py"]
    assert t.search("return") == ["app/main.py"]


@pytest.mark.parametrize("bad", ["../etc/passwd", ".env", ".venv/x.py", "/etc/passwd"])
def test_path_confinement(repo: Path, bad: str) -> None:
    with pytest.raises(ToolError):
        RepoTools(repo).read_file(bad)


def test_edits_do_not_touch_original_and_diff(repo: Path) -> None:
    edits = [
        Edit(path="app/main.py", old="return 1", new="return 2"),
        Edit(path="new.py", new="y\n"),
    ]
    with sandbox_copy(repo) as box:
        assert not (box / ".env").exists()
        apply_edits(box, edits)
        diff = render_diff(repo, box, edits)
    assert "-    return 1" in diff and "+    return 2" in diff and "/dev/null" in diff
    assert "return 1" in (repo / "app" / "main.py").read_text()
    assert not (repo / "new.py").exists()


def test_ambiguous_or_missing_edit_rejected(repo: Path) -> None:
    with sandbox_copy(repo) as box:
        with pytest.raises(ToolError):
            apply_edits(box, [Edit(path="app/main.py", old="nope", new="x")])
        with pytest.raises(ToolError):
            apply_edits(box, [Edit(path="app/main.py", old="", new="x")])  # exists
        with pytest.raises(ToolError):
            apply_edits(box, [Edit(path="../evil.py", new="x")])


def test_fetch_issue_validates_url_and_is_read_only() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"title": "T", "body": "B"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch_issue("https://github.com/o/r/issues/5", client) == "T\n\nB"
    assert [r.method for r in seen] == ["GET"]
    with pytest.raises(ValueError):
        fetch_issue("https://evil.com/o/r/issues/5", client)


def test_rank_prefers_issue_relevant_code(tmp_path: Path) -> None:
    (tmp_path / "routers").mkdir()
    (tmp_path / "routers" / "items.py").write_text('@router.post("/items")\ndef create(): ...\n')
    (tmp_path / "routers" / "users.py").write_text("def users(): ...\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_items.py").write_text("def test_items(): ...\n")
    assert RepoTools(tmp_path).rank("POST /items returns 500")[0] == "routers/items.py"


def test_mismatch_error_includes_file_contents(repo: Path) -> None:
    with sandbox_copy(repo) as box, pytest.raises(ToolError, match="def f"):
        apply_edits(box, [Edit(path="app/main.py", old="nope", new="x")])

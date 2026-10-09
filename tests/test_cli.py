import json

from issuepilot.cli import build_parser, run_plan
from issuepilot.persistence import RunStore
from tests.conftest import VALID_PLAN, FakeProvider


def test_cli_plan_json_saves_and_reports(settings, capsys) -> None:  # type: ignore[no-untyped-def]
    args = build_parser().parse_args(["plan", "bug", "--json"])
    assert run_plan(args, settings, FakeProvider([VALID_PLAN])) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["summary"] == "Fix 500 on empty body"
    assert "1500 total" in err and "est. cost" in err
    rec = RunStore(settings.db_path).get(1)
    assert rec and rec.prompt_tokens == 1000


def test_cli_empty_issue(settings) -> None:  # type: ignore[no-untyped-def]
    args = build_parser().parse_args(["plan", "  "])
    assert run_plan(args, settings, FakeProvider([])) == 2


def test_cli_fix_writes_patch(settings, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    import json

    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "m.py").write_text("x = 1\n")
    plan = json.dumps({"summary": "s", "steps": [{"description": "d", "files": ["m.py"]}]})
    ed = json.dumps({"edits": [{"path": "m.py", "old": "1", "new": "2"}]})
    out_file = tmp_path / "fix.patch"
    from issuepilot.cli import run_fix

    args = build_parser().parse_args(
        ["fix", "bug", "--repo", str(repo), "--no-tests", "-o", str(out_file)]
    )
    assert run_fix(args, settings, FakeProvider([plan, ed])) == 0
    assert "+x = 2" in out_file.read_text()
    assert "est. cost" in capsys.readouterr().err

from __future__ import annotations

import json
from pathlib import Path

from issuepilot.budget import Limits
from issuepilot.cli import build_parser, run_discover
from issuepilot.config import Settings
from issuepilot.discovery import run_discovery, scan_source
from issuepilot.modes import Mode
from issuepilot.orchestrator import create_task
from issuepilot.persistence.tasks import TaskStore
from tests.conftest import FakeProvider

BUGGY = """
import subprocess, yaml, requests
API_KEY = "sk-live-9f8e7d6c5b4a3921"
PASSWORD = "your-password-here"

def add(item, bucket=[]):
    bucket.append(item)
    try:
        run(item)
    except:
        pass
    if item == None:
        return eval(item)
    subprocess.run(item, shell=True)
    db.execute(f"SELECT * FROM t WHERE id={item}")
    requests.get("http://x")
    return yaml.load(item)
"""


def test_static_rules_fire() -> None:
    rules = {f.rule for f in scan_source("m.py", BUGGY)}
    assert {
        "mutable-default", "bare-except", "eq-none", "eval-exec", "shell-true",
        "sql-injection", "no-timeout", "unsafe-deserialize", "hardcoded-secret",
    } <= rules  # fmt: skip


def test_secret_is_redacted_and_placeholders_ignored() -> None:
    secrets = [f for f in scan_source("m.py", BUGGY) if f.rule == "hardcoded-secret"]
    assert len(secrets) == 1 and "9f8e7d6c" not in secrets[0].snippet


def test_clean_code_has_no_findings() -> None:
    clean = "def f(x: int, y: list[int] | None = None) -> int:\n    return x if y is None else 0\n"
    assert scan_source("ok.py", clean) == []


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    (r / "m.py").write_text(BUGGY)
    (r / "test_m.py").write_text("x = eval('1')\n")  # tests are not scanned
    return r


def test_discovery_without_model_is_read_only(settings: Settings, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = sorted(p.name for p in repo.iterdir())
    store = TaskStore(settings.db_path)
    tid = create_task(
        store, issue="d", repo=str(repo), mode=Mode.OBSERVE, limits=Limits(), kind="discover"
    )
    rep = run_discovery(settings, FakeProvider([]), store, tid, use_model=False)
    assert rep and not rep.triaged and rep.files_scanned == 1
    assert all(f.file == "m.py" for f in rep.findings)
    assert sorted(p.name for p in repo.iterdir()) == before
    rec = store.get(tid)
    assert rec and rec.status == "completed" and rec.cost_usd == 0


def test_triage_marks_false_positives_and_tracks_cost(settings: Settings, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    store = TaskStore(settings.db_path)
    tid = create_task(
        store, issue="d", repo=str(repo), mode=Mode.OBSERVE, limits=Limits(), kind="discover"
    )
    triage = json.dumps(
        {
            "items": [
                {
                    "id": 0,
                    "verdict": "real",
                    "severity": "high",
                    "title": "eval on input",
                    "reasoning": "r",
                },
                {
                    "id": 1,
                    "verdict": "false_positive",
                    "severity": "low",
                    "title": "t",
                    "reasoning": "r",
                },
            ]
        }
    )
    rep = run_discovery(settings, FakeProvider([triage]), store, tid)
    assert rep and rep.triaged
    verdicts = [f.verdict for f in rep.findings]
    assert "real" in verdicts and "false_positive" in verdicts
    assert "eval on input" in rep.markdown()
    rec = store.get(tid)
    assert rec and rec.llm_calls == 1 and rec.cost_usd > 0


def test_discovery_budget_cap(settings: Settings, tmp_path: Path) -> None:
    store = TaskStore(settings.db_path)
    tid = create_task(store, issue="d", repo=str(_repo(tmp_path)), mode=Mode.OBSERVE,
                      limits=Limits(max_cost_usd=0.0), kind="discover")  # fmt: skip
    assert run_discovery(settings, FakeProvider(["{}"]), store, tid) is None
    rec = store.get(tid)
    assert rec and rec.status == "budget_exceeded"


def test_cli_discover_static(settings: Settings, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    args = build_parser().parse_args(["discover", "--repo", str(_repo(tmp_path)), "--no-model"])
    assert run_discover(args, settings) == 0
    assert "mutable default" in capsys.readouterr().out

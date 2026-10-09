import json
from pathlib import Path

from fastapi.testclient import TestClient

from issuepilot.api.app import create_app
from issuepilot.config import Settings
from tests.conftest import FakeProvider

PLAN = json.dumps({"summary": "s", "steps": [{"description": "d", "files": ["m.py"]}]})
EDITS = json.dumps({"edits": [{"path": "m.py", "old": "1", "new": "2"}]})


def test_fix_roundtrip_and_path_restriction(tmp_path: Path, settings: Settings) -> None:
    root = tmp_path / "repos"
    (root / "r").mkdir(parents=True)
    (root / "r" / "m.py").write_text("x = 1\n")
    client = TestClient(create_app(settings, lambda: FakeProvider([PLAN, EDITS]), root))

    assert client.get("/health").json() == {"status": "ok"}
    r = client.post("/fixes", json={"issue": "bug", "repo_path": "r"})
    assert r.status_code == 200 and r.json()["result"]["status"] == "unverified"
    got = client.get(f"/fixes/{r.json()['id']}")
    assert "+x = 2" in got.json()["diff"]
    assert client.get("/fixes/99").status_code == 404
    assert client.post("/fixes", json={"issue": "b", "repo_path": ".."}).status_code == 400

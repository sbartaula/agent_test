import pytest

from issuepilot.config import ConfigError, Settings
from issuepilot.cost import estimate_cost_usd, format_usage
from issuepilot.llm.base import Usage
from issuepilot.persistence import RunStore


def test_cost() -> None:
    u = Usage(prompt_tokens=1_000_000, completion_tokens=500_000)
    assert estimate_cost_usd(u, 0.14, 0.28) == pytest.approx(0.28)
    assert "total" in format_usage(u, 0.28)


def test_settings_requires_key() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({})


def test_settings_defaults_and_secret_not_in_repr() -> None:
    s = Settings.from_env({"DEEPSEEK_API_KEY": "sk-secret"})
    assert s.model == "deepseek-flash"
    assert "sk-secret" not in repr(s)


def test_bad_price() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"DEEPSEEK_API_KEY": "k", "ISSUEPILOT_PRICE_INPUT_PER_M": "abc"})


def test_store_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = RunStore(tmp_path / "sub" / "r.db")
    rid = store.save(
        model="m", issue="i", plan_json="{}", prompt_tokens=1, completion_tokens=2, cost_usd=0.1
    )
    rec = store.get(rid)
    assert rec and rec.completion_tokens == 2
    assert store.get(999) is None


def test_load_dotenv_does_not_override(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from issuepilot.config import load_dotenv

    f = tmp_path / ".env"
    f.write_text('# c\nA_TEST_VAR="x"\nB_TEST_VAR=y\n')
    monkeypatch.setenv("B_TEST_VAR", "keep")
    monkeypatch.delenv("A_TEST_VAR", raising=False)
    load_dotenv(f)
    import os

    assert os.environ["A_TEST_VAR"] == "x" and os.environ["B_TEST_VAR"] == "keep"
    monkeypatch.delenv("A_TEST_VAR")


def test_peak_pricing() -> None:
    from datetime import UTC, datetime

    from issuepilot.config import default_prices

    assert default_prices(datetime(2026, 10, 9, 7, tzinfo=UTC)) == (0.30, 1.20)  # Fri peak
    assert default_prices(datetime(2026, 10, 9, 12, tzinfo=UTC)) == (0.15, 0.60)
    assert default_prices(datetime(2026, 10, 10, 7, tzinfo=UTC)) == (0.15, 0.60)  # Sat

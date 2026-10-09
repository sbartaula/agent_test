from __future__ import annotations

import pytest

from issuepilot.config import Settings
from issuepilot.llm.base import ChatMessage, LLMResponse, Usage


class FakeProvider:
    """Returns scripted responses; records the messages it was sent."""

    def __init__(self, contents: list[str], usage: Usage | None = None) -> None:
        self._contents = list(contents)
        self._usage = usage or Usage(prompt_tokens=1000, completion_tokens=500)
        self.calls: list[list[ChatMessage]] = []

    def complete(
        self, messages: list[ChatMessage], *, json_mode: bool = False, max_tokens: int | None = None
    ) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(content=self._contents.pop(0), model="fake", usage=self._usage)


VALID_PLAN = (
    '{"summary": "Fix 500 on empty body", "steps": '
    '[{"description": "Validate body", "files": ["app/main.py"], "kind": "edit"}]}'
)


@pytest.fixture
def settings(tmp_path: pytest.TempPathFactory) -> Settings:
    return Settings(api_key="test-key", db_path=str(tmp_path / "runs.db"))  # type: ignore[operator]

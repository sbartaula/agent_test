"""DeepSeek via its OpenAI-compatible API."""

from __future__ import annotations

from typing import cast

from openai import OpenAI, omit
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared_params import ResponseFormatJSONObject

from issuepilot.config import Settings
from issuepilot.llm.base import ChatMessage, LLMResponse, Usage


class DeepSeekProvider:
    def __init__(self, settings: Settings, client: OpenAI | None = None) -> None:
        self._model = settings.model
        self._client = client or OpenAI(
            api_key=settings.api_key, base_url=settings.base_url, timeout=120, max_retries=2
        )

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        msgs: list[ChatCompletionMessageParam] = [
            {"role": m.role, "content": m.content}  # type: ignore[misc]
            for m in messages
        ]
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=msgs,
            temperature=0,
            max_tokens=max_tokens if max_tokens is not None else omit,
            response_format=cast(ResponseFormatJSONObject, {"type": "json_object"})
            if json_mode
            else omit,
        )
        u = resp.usage
        return LLMResponse(
            content=resp.choices[0].message.content or "",
            model=resp.model or self._model,
            usage=Usage(
                prompt_tokens=u.prompt_tokens if u else 0,
                completion_tokens=u.completion_tokens if u else 0,
            ),
        )

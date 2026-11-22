from functools import lru_cache
import json
import os
from typing import TypeVar

from groq import APIStatusError, APITimeoutError, Groq
from groq.types.chat import ChatCompletionMessage
from pydantic import BaseModel

from app.core.config import settings


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class StructuredResponseError(ValueError):
    """The response contained tool calls without usable arguments."""


def _tool_definition(schema: type[BaseModel]) -> dict:
    source = schema.model_json_schema()
    definitions = source.pop("$defs", {})
    name = source.pop("title")
    description = source.pop("description", "")

    def expand(node):
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            reference = node["$ref"].rsplit("/", 1)[-1]
            node = {**definitions[reference], **{key: value for key, value in node.items() if key != "$ref"}}
        # Preserve properties named "title" while stripping schema display titles.
        return {key: expand(value) for key, value in node.items()
                if key != "title" or isinstance(value, dict)}

    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": expand(source),
    }}


class GroqProvider:
    def __init__(self, client: Groq, model: str):
        self._client = client
        self._model = model

    def _complete(self, prompt: str, **options) -> ChatCompletionMessage:
        response = self._client.chat.completions.create(
            model=self._model, messages=[{"role": "user", "content": prompt}],
            temperature=0.1, n=1, stop=None, stream=False, **options,
        )
        return response.choices[0].message

    def generate_text(self, prompt: str) -> str:
        return self._complete(prompt).content or ""

    def generate_structured(self, prompt: str, schema: type[ResponseModel]) -> ResponseModel | None:
        tool = _tool_definition(schema)
        name = tool["function"]["name"]
        message = self._complete(
            prompt, tools=[tool], tool_choice={"type": "function", "function": {"name": name}},
        )
        if not message.tool_calls:
            return None

        # The old parser chose the first decodable call, then validated its schema.
        for call in message.tool_calls:
            try:
                arguments = json.loads(call.function.arguments, strict=False)
            except (AttributeError, TypeError, json.JSONDecodeError):
                continue
            if call.function.name != name:
                raise StructuredResponseError("Groq returned an unexpected response tool.")
            return schema.model_validate(arguments or {})
        raise StructuredResponseError("Groq returned no decodable tool arguments.")


def _matches_provider_error(error: Exception, status: int, codes: set[str]) -> bool:
    if not isinstance(error, APIStatusError):
        return False
    if error.status_code == status:
        return True
    if not isinstance(error.body, dict):
        return False
    details = error.body.get("error", error.body)
    if not isinstance(details, dict):
        return False
    return any(
        isinstance(value, str) and value.lower() in codes
        for value in (details.get("code"), details.get("type"))
    )


def is_quota_error(error: Exception) -> bool:
    return _matches_provider_error(error, 429, {
        "rate_limit_exceeded", "quota_exceeded", "insufficient_quota", "resource_exhausted",
    })


def is_transient_error(error: Exception) -> bool:
    return isinstance(error, APITimeoutError) or _matches_provider_error(error, 503, {
        "service_unavailable", "unavailable", "timeout", "deadline_exceeded",
    })


@lru_cache(maxsize=8)
def get_llm(model: str | None = None) -> GroqProvider:
    """Reuse a client for each model in the fallback chain."""
    client = Groq(
        api_key=settings.GROQ_API_KEY, base_url=os.getenv("GROQ_API_BASE"),
        timeout=None, max_retries=2,
    )
    return GroqProvider(client, model or settings.LLM_MODEL)

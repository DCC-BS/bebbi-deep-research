"""The real LLM class driven by a scripted pydantic-ai FunctionModel (no network)."""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from bebbi_deep_research.engine.llm import LLM, LlmSettings


def say(content: str = "", calls: Sequence[tuple[str, dict[str, Any]]] = ()) -> ModelResponse:
    """A model turn: text and/or tool calls."""
    parts: list[Any] = [ToolCallPart(name, args, tool_call_id=f"c{i}") for i, (name, args) in enumerate(calls)]
    if content or not parts:
        parts.insert(0, TextPart(content))
    return ModelResponse(parts=parts)


def first_user_prompt(messages: list[ModelMessage]) -> str:
    """Text of the first user prompt of a run."""
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    return part.content
    return ""


class ScriptedLLM(LLM):
    """LLM whose model answers come from `turns` in order; records tools, instructions and prompt of each request.

    Attributes:
        calls: One entry per model request: {"tools", "instructions", "user", "messages"}.
    """

    def __init__(
        self,
        turns: Sequence[ModelResponse] = (),
        embed: Callable[[list[str]], Awaitable[list[list[float]]]] | None = None,
    ) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []
        self._embed = embed
        super().__init__(LlmSettings(base_url="http://llm/v1"), model=FunctionModel(self._respond))

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls.append(
            {
                "tools": [t.name for t in info.function_tools],
                "instructions": info.instructions or "",
                "user": first_user_prompt(messages),
                "messages": messages,
            }
        )
        return self.turns.pop(0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._embed is None:
            return [[1.0] for _ in texts]
        return await self._embed(texts)

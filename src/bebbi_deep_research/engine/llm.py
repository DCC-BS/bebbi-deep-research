"""LLM access through backend-common's pydantic-ai agents.

Two roles (think / fast), JSON repair, tool loops with budgets, embeddings, and usage counters per step.
"""

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, override

import json_repair
from dcc_backend_common.config import log_secret
from dcc_backend_common.config.app_config import LlmConfig
from dcc_backend_common.llm_agent import BaseAgent
from dcc_backend_common.logger import get_logger
from openai import AsyncOpenAI
from pydantic import Field
from pydantic_ai import Agent, RunContext, Tool, UnexpectedModelBehavior
from pydantic_ai.embeddings import Embedder
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.models import Model
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.usage import RunUsage, UsageLimits

logger = get_logger(__name__)

ROLES = ("think", "fast")
EMBED_BATCH = 32
EMBED_CHARS = 4000
JSON_REMINDER = "\n\nReturn ONLY the JSON object, no prose, no code fences."
SKIPPED = "skipped: tool budget used up"


@dataclass(slots=True)
class Usage:
    """Token usage of one research, in total and per step (clarify, plan, focus, ...)."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    by_step: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Prompt plus completion tokens."""
        return self.prompt_tokens + self.completion_tokens

    def add(self, step: str, usage: RunUsage) -> None:
        """Count the usage of one agent run under `step`."""
        self.calls += usage.requests
        self.prompt_tokens += usage.input_tokens
        self.completion_tokens += usage.output_tokens
        self.by_step[step] = self.by_step.get(step, 0) + usage.input_tokens + usage.output_tokens


class RoleLlmConfig(LlmConfig):
    """LLM settings of one role (built from the service configuration, not from the environment).

    Attributes:
        max_tokens: Completion budget per request; thinking tokens count towards it.
        extra_body: Extra request body, merged over backend-common's ``chat_template_kwargs``.
        enable_thinking: Whether the role reasons before it answers.
    """

    max_tokens: int = 4000
    extra_body: dict[str, Any] = Field(default_factory=dict)
    enable_thinking: bool = False

    @override
    def __str__(self) -> str:
        return f"RoleLlmConfig(llm_model={self.llm_model}, llm_api_key={log_secret(self.llm_api_key)})"


@dataclass(frozen=True, slots=True, kw_only=True)
class LlmSettings:
    """Endpoint, models and limits of the engine's language models."""

    base_url: str
    api_key: str = ""
    models: dict[str, str] = field(
        default_factory=lambda: {"think": "gemma-think", "fast": "gemma-fast", "embed": "qwen-embed"}
    )
    max_tokens: dict[str, int] = field(default_factory=lambda: {"think": 16000, "fast": 4000})
    extra_body: dict[str, dict[str, Any]] = field(default_factory=dict)
    timeout: int = 1800
    max_retries: int = 3

    def role(self, role: str) -> RoleLlmConfig:
        """Return the backend-common configuration of one role."""
        return RoleLlmConfig(
            llm_model=self.models[role],
            llm_url=self.base_url,
            llm_api_key=self.api_key or "none",
            llm_timeout=self.timeout,
            llm_max_retries=self.max_retries,
            max_tokens=self.max_tokens[role],
            extra_body=self.extra_body.get(role, {}),
            enable_thinking=role == "think",
        )


class RoleAgent(BaseAgent[None, str]):
    """Text agent of one role; instructions and tools are given per run."""

    def __init__(self, config: RoleLlmConfig, model: Model | None = None) -> None:
        """Build the agent.

        Args:
            config: Role configuration.
            model: Model to use instead of the OpenAI-compatible endpoint (tests: a pydantic-ai FunctionModel).
        """
        self._model_override = model
        self.role_config = config
        super().__init__(config, deps_type=None, output_type=str, enable_thinking=config.enable_thinking)

    @override
    def create_agent(self, model: Model) -> Agent[None, str]:
        return Agent(model=self._model_override or model, output_type=str)

    async def complete(
        self,
        user: str,
        instructions: str | Callable[[RunContext[None]], str],
        *,
        max_tokens: int | None = None,
        toolsets: Sequence[AbstractToolset[None]] = (),
        usage_limits: UsageLimits | None = None,
    ) -> tuple[str, RunUsage]:
        """Run the agent once (with tools: until it answers without a tool call).

        Args:
            user: The user message.
            instructions: System instructions, or a function re-evaluated before every model request.
            max_tokens: Completion budget per request (default: the role's).
            toolsets: Tools the model may call.
            usage_limits: Request limit of the run.

        Returns:
            The answer, postprocessed by backend-common (ß becomes ss), and the usage of the run.
        """
        settings = self._extract_model_settings(
            {"model_settings": {"max_tokens": max_tokens or self.role_config.max_tokens}}
        )
        if self.role_config.extra_body:
            body: dict[str, Any] = {**(settings.get("extra_body") or {})}  # ty: ignore[invalid-argument-type]
            settings["extra_body"] = {**body, **self.role_config.extra_body}
        result = await self._agent.run(
            user,
            instructions=instructions,
            toolsets=list(toolsets),
            model_settings=settings,
            usage_limits=usage_limits,
        )
        self._log_result(result)
        return self._postprocess(result.output or ""), result.usage


@dataclass(slots=True)
class ToolBudget:
    """Shared state of a tool loop: calls made and the limits.

    Attributes:
        max_calls: Tool calls allowed in the run.
        max_tokens: Token budget of the whole research; tools are hidden above it.
        calls: Tool calls made so far.
    """

    max_calls: int
    max_tokens: int | None = None
    calls: int = 0

    def take(self) -> bool:
        """Count a tool call; False if the budget is used up (the call is answered with "skipped")."""
        self.calls += 1
        return self.calls <= self.max_calls


class LLM:
    """The engine's access to the language models.

    Attributes:
        usage: Token usage of this LLM object (one research).
    """

    def __init__(self, settings: LlmSettings, model: Model | None = None) -> None:
        """Create the role agents and the embedder.

        Args:
            settings: Endpoint, models and limits.
            model: Model for both roles instead of the endpoint (tests).
        """
        self.settings = settings
        self.agents = {role: RoleAgent(settings.role(role), model) for role in ROLES}
        self._embed_client = AsyncOpenAI(
            base_url=settings.base_url, api_key=settings.api_key or "none", timeout=settings.timeout
        )
        self.embedder = Embedder(
            OpenAIEmbeddingModel(settings.models["embed"], provider=OpenAIProvider(openai_client=self._embed_client))
        )
        self.usage = Usage()

    async def aclose(self) -> None:
        """Close the HTTP connections of the role agents and the embedder."""
        for agent in self.agents.values():
            await agent._openai_client.close()
        await self._embed_client.close()

    async def text(self, role: str, system: str, user: str, step: str, max_tokens: int | None = None) -> str:
        """Return the model's answer to one prompt.

        An empty answer usually means the reasoning used up the budget: it is retried once with twice the budget.
        """
        budget = max_tokens or self.settings.max_tokens[role]
        for attempt in range(2):
            try:
                output, usage = await self.agents[role].complete(user, system, max_tokens=budget)
            except UnexpectedModelBehavior as e:
                if attempt:
                    raise
                logger.warning("empty LLM answer, retrying with a larger budget", step=step, role=role, error=str(e))
                budget *= 2
                continue
            self.usage.add(step, usage)
            if output.strip() or attempt:
                return output.strip()
            budget *= 2
        return ""

    async def json(self, role: str, system: str, user: str, step: str, default: Any = None) -> Any:
        """Return a JSON object from the model (repaired if needed); one retry with a stricter reminder.

        Returns:
            The parsed object, or `default` if both attempts fail.
        """
        for attempt in range(2):
            out = await self.text(role, system, user if attempt == 0 else user + JSON_REMINDER, step)
            parsed = parse_json(out, want=dict)
            if isinstance(parsed, dict):
                return parsed
        return default

    async def run_tools(
        self,
        role: str,
        system: str,
        user: str,
        tools: Sequence[Tool[None]],
        step: str,
        *,
        budget: ToolBudget,
        final_prompt: str = "",
    ) -> str:
        """Let the model call tools until it answers; return the answer.

        The tools are hidden once `budget` is used up (calls or tokens); `final_prompt` is then added to the
        instructions. Tools must call `budget.take()` and answer "skipped" when it returns False.

        Raises:
            pydantic_ai.UnexpectedModelBehavior: If the model gives no answer even when asked again.
        """
        used_before = self.usage.total

        def exhausted(ctx: RunContext[None]) -> bool:
            over_tokens = budget.max_tokens is not None and (
                used_before + ctx.usage.input_tokens + ctx.usage.output_tokens > budget.max_tokens
            )
            return budget.calls >= budget.max_calls or over_tokens

        def instructions(ctx: RunContext[None]) -> str:
            return system + ("\n\n" + final_prompt if final_prompt and exhausted(ctx) else "")

        toolset = FunctionToolset[None](list(tools)).filtered(lambda ctx, _tool: not exhausted(ctx))
        output, usage = await self.agents[role].complete(
            user,
            instructions,
            toolsets=[toolset],
            usage_limits=UsageLimits(request_limit=budget.max_calls + 4),
        )
        self.usage.add(step, usage)
        return output.strip()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding per text (texts cut at 4000 characters, batches of 32)."""
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = [t[:EMBED_CHARS] for t in texts[i : i + EMBED_BATCH]]
            result = await self.embedder.embed(batch, input_type="document")
            out += [list(v) for v in result.embeddings]
        return out


def budgeted_tools(
    specs: Sequence[dict[str, Any]], dispatch: Callable[[str, dict[str, Any]], Awaitable[str]], budget: ToolBudget
) -> list[Tool[None]]:
    """Turn OpenAI-style tool definitions into pydantic-ai tools that respect a shared budget.

    Args:
        specs: ``{"type": "function", "function": {"name", "description", "parameters"}}`` entries (MCP tools keep
            the JSON schema of their server).
        dispatch: ``dispatch(name, args)`` runs a tool and returns its text for the model.
        budget: Calls over the budget are answered with "skipped" instead of running.
    """

    def make(name: str) -> Callable[..., Awaitable[str]]:
        async def run(**args: Any) -> str:
            if not budget.take():
                return SKIPPED
            return await dispatch(name, args)

        return run

    tools = []
    for spec in specs:
        fn = spec["function"]
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        description = fn.get("description")
        tools.append(Tool.from_schema(make(fn["name"]), name=fn["name"], description=description, json_schema=schema))
    return tools


def parse_json(text: str, want: type | None = None) -> Any:
    """Return the first JSON value in a model answer.

    With ``want=dict`` the outermost ``{...}`` span is preferred over an earlier ``[...]`` in the prose, and a
    non-dict result counts as no result.
    """
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    patterns = [r"\{.*\}", r"[\[{].*[\]}]"] if want is dict else [r"[\[{].*[\]}]"]
    for pattern in patterns:
        m = re.search(pattern, text, re.S)
        if not m:
            continue
        try:
            value = json.loads(m.group(0))
        except json.JSONDecodeError:
            try:
                value = json_repair.loads(m.group(0))
            except Exception:  # json_repair raises various errors on garbage
                continue
        if value in ("", None) or (want is not None and not isinstance(value, want)):
            continue
        return value
    return None

"""The engine interface and the registry that picks an engine by the preset's `engine` value.

Adding an engine: write a class that fits `Engine` (a module next to pipeline.py and focus.py, using only the
services of `EngineContext`), add it to `ENGINES`, and set `engine: <name>` in a preset. Nothing else dispatches on
the engine name: ResearchAgent.run and the daytime rule in services/jobs.py both ask this registry.
"""

from typing import ClassVar, Protocol

from .config import Budget, Preset
from .context import EngineContext, Result
from .focus import FocusEngine
from .pipeline import PipelineEngine


class Engine(Protocol):
    """One research run: built per run from the agent's shared services, then `run` once.

    Planning is shared: an engine that gets no approved plan calls `ChatTurns.plan` (engine/chat.py) itself, or runs
    without a plan. The chat turns (clarify, plan, edit) do not depend on the engine.
    """

    name: ClassVar[str]  # the preset's `engine` value
    runs_by_day: ClassVar[bool]  # quick runs may start during office hours (services/jobs.py runs_by_day)

    def __init__(self, ctx: EngineContext) -> None:
        """Keep the shared services; `ctx.own` holds the run's own sources (already filtered by the preset)."""
        ...

    async def run(self, task: str, preset: Preset, budget: Budget, plan: dict | None, requirements: str) -> Result:
        """Research `task` and return the report, the plan used, the ledger, the LLM usage and statistics.

        Args:
            task: The research question.
            preset: The preset (routes, MCP tools, report rules).
            budget: The depth's budget with the preset's overrides applied.
            plan: The approved plan, or None.
            requirements: Report-format requirements only the writer sees.
        """
        ...


ENGINES: dict[str, type[Engine]] = {e.name: e for e in (PipelineEngine, FocusEngine)}
DEFAULT_ENGINE = "pipeline"


def engine_class(name: str) -> type[Engine]:
    """Return the engine for a preset's `engine` value; an unknown value runs the default (pipeline) engine."""
    return ENGINES.get(name, ENGINES[DEFAULT_ENGINE])

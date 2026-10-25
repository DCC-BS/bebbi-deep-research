"""ResearchAgent: the engine's entry point for services (chat turns and one research run).

It owns the shared services (`EngineContext`: config, LLM, tools, MCP pool, events, own sources) and hands a run to
the engine the preset names: the pipeline engine (pipeline.py) or the focus engine (focus.py).
"""

import dataclasses

from . import own as O
from .chat import INTENTS, ChatTurns, own_for
from .config import Budget, Config, Preset
from .context import EngineContext, Event, Result
from .focus import FocusEngine
from .llm import LLM
from .mcp import McpPool
from .pipeline import PipelineEngine
from .tools import MCPClient, Tools

__all__ = ["INTENTS", "Event", "ResearchAgent", "Result"]


class ResearchAgent:
    """Plans and runs one research (pipeline or focus engine) with one LLM, tool set and MCP connections."""

    def __init__(self, cfg: Config, llm: LLM, tools: Tools, on_event: Event | None = None, concurrency: int = 3):
        """Create the agent.

        Args:
            cfg: Presets, budgets and MCP servers.
            llm: Language model access (usage is counted per research).
            tools: Web search and page fetching.
            on_event: Progress callback ``on_event(kind, text)``.
            concurrency: Sub-questions researched at the same time.
        """
        self.cfg, self.llm, self.tools = cfg, llm, tools
        self.ctx = EngineContext(cfg, llm, tools, McpPool(cfg), on_event=on_event, concurrency=concurrency)
        self.chat = ChatTurns(llm, tools)

    @property
    def _mcp_clients(self) -> dict[str, MCPClient]:
        """The connected MCP clients (services.jobs.close_agent closes them after the job)."""
        return self.ctx.mcp.clients

    # ------------------------------------------------------- chat turns
    async def clarify(
        self, task: str, own: "O.OwnSources | None" = None, presets: dict[str, str] | None = None
    ) -> dict:
        """At most 2 clarifying questions (see `ChatTurns.clarify`)."""
        return await self.chat.clarify(task, own=own, presets=presets)

    async def edit_plan(self, plan: dict, instruction: str, task: str, preset: Preset, budget: Budget) -> dict:
        """Change a plan as the user asked (see `ChatTurns.edit_plan`)."""
        return await self.chat.edit_plan(plan, instruction, task, preset, budget)

    async def plan(self, task: str, preset: Preset, budget: Budget, own: "O.OwnSources | None" = None) -> dict:
        """Plan a research (see `ChatTurns.plan`)."""
        return await self.chat.plan(task, preset, budget, own=own)

    # ------------------------------------------------------------------ run
    async def run(
        self,
        task: str,
        preset_name: str,
        depth: str = "standard",
        plan: dict | None = None,
        requirements: str = "",
        own: "O.OwnSources | None" = None,
    ) -> Result:
        """Research a task and return the report with its sources.

        Args:
            task: The research question (used for planning and research).
            preset_name: Preset to use.
            depth: Budget name (quick, standard, deep).
            plan: Approved plan; planned here if missing.
            requirements: Report-format requirements (structure, citation style) only the writer sees.
            own: The user's files and knowledge bases.
        """
        preset, budget = self.cfg.presets[preset_name], self.cfg.budgets[depth]
        self.ctx.own = own_for(own, preset)
        if preset.budget:  # per-preset budget overrides (e.g. more pages for surveys)
            budget = dataclasses.replace(budget, **preset.budget)
        engine = FocusEngine if preset.engine == "focus" else PipelineEngine
        return await engine(self.ctx).run(task, preset, budget, plan, requirements)

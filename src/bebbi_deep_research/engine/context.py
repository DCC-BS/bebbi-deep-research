"""What every engine shares: the services of one research agent (`EngineContext`), the result and small helpers."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .config import Config
from .llm import LLM
from .mcp import McpPool
from .own import OwnSources
from .tools import Tools

Event = Callable[[str, str], Awaitable[None] | None]


@dataclass
class Result:
    """Outcome of one research: report, plan, evidence ledger, token usage and statistics."""

    report: str
    plan: dict
    ledger: dict
    usage: dict
    stats: dict = field(default_factory=dict)


@dataclass
class EngineContext:
    """The services an engine works with, owned by one ResearchAgent (one job or one chat turn).

    Engines read these attributes and call `emit`; they never reach into ResearchAgent.
    """

    cfg: Config  # presets, budgets, MCP servers and their hints and tool descriptions
    llm: LLM  # language model; usage is counted per agent
    tools: Tools  # web search and page fetching
    mcp: McpPool  # connected MCP clients, with the last error per server
    on_event: Event | None = None  # progress callback on_event(kind, text)
    own: OwnSources | None = None  # the user's files / knowledge bases of the current run (engine/own.py)
    concurrency: int = 3  # pipeline engine: sub-questions researched at the same time

    async def emit(self, kind: str, text: str) -> None:
        """Send a progress event (e.g. "status", "Recherche: ...") to the callback, if there is one."""
        if self.on_event:
            r = self.on_event(kind, text)
            if asyncio.iscoroutine(r):
                await r


def today() -> str:
    """Today's date as YYYY-MM-DD (prompts state it)."""
    return time.strftime("%Y-%m-%d")

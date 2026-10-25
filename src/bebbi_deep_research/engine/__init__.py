"""research-agent: lean deep research for Bebbi Bot (see docs/research-agent-design.md)."""

from .agent import ResearchAgent, Result
from .config import load_config
from .context import EngineContext
from .engines import ENGINES, Engine, engine_class
from .llm import LLM
from .tools import Tools

__all__ = [
    "ENGINES",
    "Engine",
    "EngineContext",
    "ResearchAgent",
    "Result",
    "engine_class",
    "load_config",
    "LLM",
    "Tools",
]

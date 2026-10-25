"""What every engine shares: the research result and small helpers."""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

Event = Callable[[str, str], Awaitable[None] | None]


@dataclass
class Result:
    """Outcome of one research: report, plan, evidence ledger, token usage and statistics."""

    report: str
    plan: dict
    ledger: dict
    usage: dict
    stats: dict = field(default_factory=dict)


def today() -> str:
    """Today's date as YYYY-MM-DD (prompts state it)."""
    return time.strftime("%Y-%m-%d")

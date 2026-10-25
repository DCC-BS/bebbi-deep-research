"""Process-wide counters of the engine in Prometheus text format (LLM retries, tool and MCP failures).

The service has no metrics library; `/metrics` writes its text by hand. `render()` returns the engine's part of it.
Counters only grow while the process runs (Prometheus `counter` semantics); gauges are read when rendered.
"""

import threading
from collections import Counter
from collections.abc import Callable

HELP = {
    "research_llm_retries_total": "LLM requests retried after a timeout, dropped connection, 5xx or 429",
    "research_tool_errors_total": "Failed web searches and page fetches by HTTP status or error type",
    "research_mcp_errors_total": "MCP request retries, failed connects and tool errors by server host",
    "research_llm_requests_running": "LLM and embedding requests in flight (RA_LLM_CONCURRENCY caps it)",
    "research_llm_requests_waiting": "LLM and embedding requests waiting for a free slot",
}

_lock = threading.Lock()  # MCP calls count from worker threads
_counts: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
_gauges: dict[str, Callable[[], float]] = {}


def inc(name: str, **labels: str) -> None:
    """Add 1 to counter `name` with the given labels."""
    key = (name, tuple(sorted(labels.items())))
    with _lock:
        _counts[key] += 1


def gauge(name: str, read: Callable[[], float]) -> None:
    """Register a gauge read when rendered (the last registration of a name wins)."""
    _gauges[name] = read


def value(name: str, **labels: str) -> int:
    """Return the current value of a counter (tests)."""
    with _lock:
        return _counts[(name, tuple(sorted(labels.items())))]


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render() -> str:
    """Return all counters and gauges in Prometheus text format (counters appear after their first increment)."""
    with _lock:
        counts = sorted(_counts.items())
    lines: list[str] = []
    for name in sorted({n for (n, _), _ in counts}):
        lines += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} counter"]
        for (n, labels), v in counts:
            if n == name:
                lab = ",".join(f'{k}="{_escape(x)}"' for k, x in labels)
                lines.append(f"{name}{{{lab}}} {v}" if lab else f"{name} {v}")
    for name, read in sorted(_gauges.items()):
        lines += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} gauge", f"{name} {read()}"]
    return "\n".join(lines) + "\n" if lines else ""

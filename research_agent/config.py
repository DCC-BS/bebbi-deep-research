"""Presets and budgets (config/presets.yaml)."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Budget:
    rounds: int
    max_subq: int
    queries_per_subq: int
    fetch_per_subq: int
    max_follow_up: int
    max_tokens: int
    words_per_part: int
    parts: list[int] = field(default_factory=lambda: [3, 5])   # min/max report parts in the plan
    focus_calls: int = 14                                       # focus engine: tool calls per run
    focus_words: int = 900                                      # focus engine: typical report length


@dataclass
class Preset:
    name: str
    description: str
    language: str                                   # "de", "en" or "auto" (language of the task)
    routes: dict[str, list[str]]                    # intent -> ["web", "mcp:<server>"]
    mcp: dict[str, list[str]] = field(default_factory=dict)          # server -> tool allowlist
    prefer_domains: dict[str, list[str]] = field(default_factory=dict)  # intent -> domains
    avoid_domains: list[str] = field(default_factory=list)
    query_rules: str = ""
    report: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)      # overrides of the depth budget for this preset
    verify_entailment: bool = True
    engine: str = "pipeline"                        # pipeline (plan, rounds, section-wise writing) | focus (tool loop)
    plan_first: bool = True                         # focus engine: plan before the loop (the plan is the checklist)
    own_sources: str = "prefer"                     # the user's files / knowledge bases: prefer (own first, web for the rest) | only | off


@dataclass
class Config:
    presets: dict[str, Preset]
    budgets: dict[str, Budget]
    mcp_servers: dict[str, str]                     # enabled servers: name -> URL
    mcp_hints: dict[str, str] = field(default_factory=dict)      # name -> how to use its tools (prompt text)
    mcp_disabled: dict[str, str] = field(default_factory=dict)   # configured but switched off (e.g. outage)
    mcp_tool_descriptions: dict[str, dict[str, str]] = field(default_factory=dict)  # server -> tool -> text the model sees


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())
    presets = {name: Preset(name=name, **{k: v for k, v in p.items() if k in Preset.__dataclass_fields__})
               for name, p in raw["presets"].items()}
    budgets = {name: Budget(**b) for name, b in raw["budgets"].items()}
    servers, hints, disabled, descs = {}, {}, {}, {}
    for name, entry in (raw.get("mcp_servers") or {}).items():
        entry = {"url": entry} if isinstance(entry, str) else entry
        (servers if entry.get("enabled", True) else disabled)[name] = entry["url"]
        if entry.get("hint"):
            hints[name] = entry["hint"].strip()
        if entry.get("tools"):
            descs[name] = {t: " ".join(str(d).split()) for t, d in entry["tools"].items()}
    return Config(presets=presets, budgets=budgets, mcp_servers=servers, mcp_hints=hints, mcp_disabled=disabled,
                  mcp_tool_descriptions=descs)

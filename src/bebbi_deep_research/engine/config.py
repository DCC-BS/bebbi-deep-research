"""Presets and budgets (config/presets.yaml).

The file is validated when it is loaded (at service start): unknown keys, unknown engine, language or own_sources
values, routes and MCP allowlists that name no configured server, budget overrides of fields that do not exist and
missing depths stop the start with a message that names every problem. Check a file without starting the service:
`uv run python -m bebbi_deep_research.engine.config path/to/presets.yaml`.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


@dataclass
class Budget:
    """Limits of one research depth (quick, standard, deep): rounds, queries, fetches, tokens, report size."""

    rounds: int
    max_subq: int
    queries_per_subq: int
    fetch_per_subq: int
    max_follow_up: int
    max_tokens: int
    words_per_part: int
    parts: list[int] = field(default_factory=lambda: [3, 5])  # min/max report parts in the plan
    focus_calls: int = 14  # focus engine: tool calls per run
    focus_words: int = 900  # focus engine: typical report length


@dataclass
class Preset:
    """A research preset: engine, routes per intent, MCP tools, domain rules, report rules, own-sources mode."""

    name: str
    description: str
    language: str  # "de", "en" or "auto" (language of the task)
    routes: dict[str, list[str]]  # intent -> ["web", "mcp:<server>"]
    mcp: dict[str, list[str]] = field(default_factory=dict)  # server -> tool allowlist
    prefer_domains: dict[str, list[str]] = field(default_factory=dict)  # intent -> domains
    avoid_domains: list[str] = field(default_factory=list)
    query_rules: str = ""
    report: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)  # overrides of the depth budget for this preset
    verify_entailment: bool = True
    engine: str = "pipeline"  # pipeline (plan, rounds, section-wise writing) | focus (tool loop)
    plan_first: bool = True  # focus engine: plan before the loop (the plan is the checklist)
    house_style: bool = True  # German reports follow the Hausstil Kanton Basel-Stadt (engine/house_style.py)
    own_sources: str = "prefer"  # the user's files / knowledge bases: prefer (own first, web for the rest) | only | off


@dataclass
class Config:
    """The loaded presets file: presets, budgets per depth and the MCP servers with their hints."""

    presets: dict[str, Preset]
    budgets: dict[str, Budget]
    mcp_servers: dict[str, str]  # enabled servers: name -> URL
    mcp_hints: dict[str, str] = field(default_factory=dict)  # name -> how to use its tools (prompt text)
    mcp_disabled: dict[str, str] = field(default_factory=dict)  # configured but switched off (e.g. outage)
    mcp_tool_descriptions: dict[str, dict[str, str]] = field(
        default_factory=dict
    )  # server -> tool -> text the model sees


DEPTHS = ("quick", "standard", "deep")  # the pipe offers these (DEFAULT_DEPTH, "schnell", "gründlich")
INTENTS = ("primary", "data", "secondary")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BudgetSpec(_Strict):
    """One entry under `budgets:`."""

    rounds: int = Field(ge=1)
    max_subq: int = Field(ge=1)
    queries_per_subq: int = Field(ge=1)
    fetch_per_subq: int = Field(ge=0)
    max_follow_up: int = Field(ge=0)
    max_tokens: int = Field(ge=1)
    words_per_part: int = Field(ge=1)
    parts: list[int] = Field(default_factory=lambda: [3, 5], min_length=2, max_length=2)
    focus_calls: int = Field(14, ge=1)
    focus_words: int = Field(900, ge=1)


class BudgetOverride(_Strict):
    """`budget:` of a preset: single fields of the depth budget."""

    rounds: int | None = None
    max_subq: int | None = None
    queries_per_subq: int | None = None
    fetch_per_subq: int | None = None
    max_follow_up: int | None = None
    max_tokens: int | None = None
    words_per_part: int | None = None
    parts: list[int] | None = None
    focus_calls: int | None = None
    focus_words: int | None = None


class ReportSpec(_Strict):
    """`report:` of a preset (read by the writers of both engines)."""

    parts_hint: list[str] = Field(default_factory=list)
    words_per_part: int | None = None
    analysis_part: str | None = None
    closing_part: str | None = None
    writer_rules: str = ""
    length_rule: str | None = None
    format_rule: str | None = None


class PresetSpec(_Strict):
    """One entry under `presets:` (fields: docs/presets.md)."""

    description: str = Field(min_length=1)
    language: Literal["de", "en", "auto"]
    routes: dict[Literal["primary", "data", "secondary"], list[str]]
    mcp: dict[str, list[str]] = Field(default_factory=dict)
    prefer_domains: dict[Literal["primary", "data", "secondary"], list[str]] = Field(default_factory=dict)
    avoid_domains: list[str] = Field(default_factory=list)
    query_rules: str = ""
    report: ReportSpec = Field(default_factory=ReportSpec)
    budget: BudgetOverride = Field(default_factory=BudgetOverride)
    verify_entailment: bool = True
    engine: str = "pipeline"  # a key of engine.engines.ENGINES
    plan_first: bool = True
    house_style: bool = True
    own_sources: Literal["prefer", "only", "off"] = "prefer"

    @field_validator("engine")
    @classmethod
    def _known_engine(cls, name: str) -> str:
        from .engines import ENGINES  # engines imports this module

        if name not in ENGINES:
            raise ValueError(f"unknown engine {name!r} (known: {', '.join(sorted(ENGINES))})")
        return name


class McpServerSpec(_Strict):
    """One entry under `mcp_servers:` (or just its URL)."""

    url: str = Field(pattern=r"^https?://")
    hint: str = ""
    enabled: bool = True
    tools: dict[str, str] = Field(default_factory=dict)  # tool -> description the model sees


class PresetsFile(_Strict):
    """The whole presets file, with the references between its parts checked."""

    mcp_servers: dict[str, McpServerSpec] = Field(default_factory=dict)
    budgets: dict[str, BudgetSpec]
    defaults: dict[str, Any] | None = None  # YAML anchor that the presets merge (`<<: *defaults`)
    presets: dict[str, PresetSpec] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _url_shorthand(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("mcp_servers"), dict):
            servers = {k: {"url": v} if isinstance(v, str) else v for k, v in data["mcp_servers"].items()}
            data = {**data, "mcp_servers": servers}
        return data

    @model_validator(mode="after")
    def _references(self) -> "PresetsFile":
        problems = [
            f"budgets.{d}: missing (the pipe offers {', '.join(DEPTHS)})" for d in DEPTHS if d not in self.budgets
        ]
        for name, p in self.presets.items():
            for intent, route in p.routes.items():
                for r in route:
                    server = r.removeprefix("mcp:")
                    if r != "web" and not (r.startswith("mcp:") and server in self.mcp_servers):
                        problems.append(
                            f"presets.{name}.routes.{intent}: {r!r} is neither 'web' nor 'mcp:<server of mcp_servers>'"
                        )
            for server, tools in p.mcp.items():
                spec = self.mcp_servers.get(server)
                if spec is None:
                    problems.append(f"presets.{name}.mcp.{server}: no such server in mcp_servers")
                    continue
                unknown = [t for t in tools if spec.tools and t not in spec.tools]
                if unknown:
                    problems.append(
                        f"presets.{name}.mcp.{server}: {', '.join(unknown)} not among the tools of mcp_servers.{server}"
                    )
        if problems:
            raise ValueError("; ".join(problems))
        return self


class PresetsError(ValueError):
    """The presets file cannot be used; the message names every problem."""


def _explain(path: Path, e: ValidationError) -> str:
    lines = []
    for err in e.errors():
        where = ".".join(str(x) for x in err["loc"])
        msg = "unknown key (typo?)" if err["type"] == "extra_forbidden" else err["msg"].removeprefix("Value error, ")
        lines.append(f"  - {where}: {msg}" if where else f"  - {msg}")
    return f"presets file {path} is invalid:\n" + "\n".join(lines)


def _preset(name: str, p: PresetSpec) -> Preset:
    return Preset(
        name=name,
        description=p.description,
        language=p.language,
        routes={str(k): v for k, v in p.routes.items()},
        mcp=p.mcp,
        prefer_domains={str(k): v for k, v in p.prefer_domains.items()},
        avoid_domains=p.avoid_domains,
        query_rules=p.query_rules,
        report=p.report.model_dump(exclude_unset=True),
        budget=p.budget.model_dump(exclude_none=True),
        verify_entailment=p.verify_entailment,
        engine=p.engine,
        plan_first=p.plan_first,
        house_style=p.house_style,
        own_sources=p.own_sources,
    )


def load_config(path: Path) -> Config:
    """Load and validate presets, budgets and MCP servers from a presets YAML file.

    Raises:
        PresetsError: If the file is not valid YAML or breaks a rule (the message lists every problem).
    """
    try:
        raw = yaml.safe_load(Path(path).read_text())
        spec = PresetsFile.model_validate(raw)
    except yaml.YAMLError as e:
        raise PresetsError(f"presets file {path} is not valid YAML: {e}") from e
    except ValidationError as e:
        raise PresetsError(_explain(path, e)) from None
    presets = {name: _preset(name, p) for name, p in spec.presets.items()}
    budgets = {name: Budget(**b.model_dump()) for name, b in spec.budgets.items()}
    servers, hints, disabled, descs = {}, {}, {}, {}
    for name, entry in spec.mcp_servers.items():
        (servers if entry.enabled else disabled)[name] = entry.url
        if entry.hint:
            hints[name] = entry.hint.strip()
        if entry.tools:
            descs[name] = {t: " ".join(str(d).split()) for t, d in entry.tools.items()}
    return Config(
        presets=presets,
        budgets=budgets,
        mcp_servers=servers,
        mcp_hints=hints,
        mcp_disabled=disabled,
        mcp_tool_descriptions=descs,
    )


if __name__ == "__main__":  # check a presets file: python -m bebbi_deep_research.engine.config FILE
    try:
        cfg = load_config(Path(sys.argv[1]))
    except (PresetsError, IndexError, OSError) as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    print(f"ok: {len(cfg.presets)} presets, budgets {', '.join(cfg.budgets)}, MCP servers {', '.join(cfg.mcp_servers)}")

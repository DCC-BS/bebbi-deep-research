"""Presets and budgets (config/presets.yaml).

The file is validated when it is loaded (at service start): unknown keys, unknown engine, language or own_sources
values, routes and MCP allowlists that name no configured server, budget overrides of fields that do not exist and
missing depths stop the start with a message that names every problem. Check a file without starting the service:
`uv run python -m bebbi_deep_research.engine.config path/to/presets.yaml`.
"""

import sys
from dataclasses import dataclass, field, replace
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
    web: bool = True  # web search and page fetch (off only in a mixed preset, see Config.preset)


@dataclass
class Choice:
    """A source (step 1) or report style (step 2) the user picks in the Deep Research card (`choices:`)."""

    label: str
    description: str
    preset: str | None = None  # source: preset whose search and writing rules come with it; style: base preset


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
    sources: dict[str, Choice] = field(default_factory=dict)  # "web" and MCP servers the user can switch on
    styles: dict[str, Choice] = field(default_factory=dict)  # report styles; preset = base preset of the style
    own_only_preset: str | None = None  # base preset of a focus style when the user's documents are the only source

    def preset(self, name: str) -> Preset | None:
        """The preset `name`: one of the file, or a mixed preset (`mix:...`, see `mix_name`), else None."""
        return self.presets.get(name) or (mixed_preset(self, name) if name.startswith(MIX + ":") else None)

    def require(self, name: str) -> Preset:
        """The preset `name` (see `preset`).

        Raises:
            KeyError: `name` is no preset of the file and no valid mixed preset.
        """
        p = self.preset(name)
        if p is None:
            raise KeyError(f"unknown preset {name!r}")
        return p

    def offered_sources(self) -> dict[str, Choice]:
        """The sources of `choices:` that can be used now (web and enabled MCP servers)."""
        return {k: c for k, c in self.sources.items() if k == "web" or k in self.mcp_servers}

    def selection_of(self, preset: Preset) -> dict:
        """What a preset uses, as a selection of the card: web, MCP sources, own-sources mode, style."""
        offered = self.offered_sources()
        uses_web = preset.web and any("web" in r for r in preset.routes.values())
        only = preset.own_sources == "only"  # the engines drop web and MCP tools then
        style = next((k for k, c in self.styles.items() if c.preset == preset.name), None)
        if style is None:
            style = next(
                (
                    k
                    for k, c in self.styles.items()
                    if c.preset in self.presets and self.presets[c.preset].engine == preset.engine
                ),
                next(iter(self.styles), ""),
            )
        return {
            "web": bool(uses_web and "web" in offered and not only),
            "mcp": [] if only else sorted(s for s in preset.mcp if s in offered and s != "web"),
            "own": preset.own_sources,
            "style": style,
        }

    def resolve_selection(self, web: bool, mcp: list[str], own: str, style: str) -> str:
        """The preset name for a selection: a preset of the file when it uses exactly this, else a mixed preset.

        Raises:
            ValueError: unknown style, source or own-sources mode, or nothing to research with.
        """
        offered = self.offered_sources()
        if style not in self.styles:
            raise ValueError(f"unknown style {style!r}")
        if own not in OWN_MODES:
            raise ValueError(f"unknown own-sources mode {own!r}")
        unknown = [s for s in mcp if s == "web" or s not in offered]
        if unknown:
            raise ValueError(f"unknown source {', '.join(unknown)}")
        if web and "web" not in offered:
            raise ValueError("web search is not offered")
        if own == "only":
            web, mcp = False, []
        if not web and not mcp and own != "only":
            raise ValueError("no source selected")
        wanted = {"web": bool(web), "mcp": sorted(set(mcp)), "own": own, "style": style}
        for p in self.presets.values():
            if self.selection_of(p) == wanted:
                return p.name
        return mix_name(style, bool(web), own, wanted["mcp"])

    def describe_selection(self, name: str, own_attached: bool = True) -> str:
        """Short German line for the plan and the Word file: sources and report style of preset `name`.

        `own_attached=False`: the chat has no files or knowledge bases, so the line does not mention them.
        """
        p = self.preset(name)
        if p is None or not self.styles:
            return name
        sel = self.selection_of(p) if not name.startswith(MIX + ":") else _parse_mix(name) or {}
        offered = self.offered_sources()
        names = [offered["web"].label] if sel.get("web") and "web" in offered else []
        names += [offered[s].label for s in sel.get("mcp", []) if s in offered]
        own = {"prefer": "eigene Dokumente zuerst", "only": "nur eigene Dokumente", "off": ""}.get(
            sel.get("own", ""), ""
        )
        if sel.get("own") == "only":
            names = []
        names += [own] if own and own_attached else []
        style = self.styles.get(sel.get("style", ""))
        return f"Quellen: {', '.join(names) or '–'} · Bericht: {style.label if style else p.name}"


DEPTHS = ("quick", "standard", "deep")  # the pipe offers these (DEFAULT_DEPTH, "schnell", "gründlich")
INTENTS = ("primary", "data", "secondary")
OWN_MODES = ("prefer", "only", "off")
MIX = "mix"  # prefix of a mixed preset: mix:<style>:<web 0|1>:<own>:<server+server>


def mix_name(style: str, web: bool, own: str, mcp: list[str]) -> str:
    """Name of the mixed preset for a selection (stored with drafts and jobs, resolved again by Config.preset)."""
    return f"{MIX}:{style}:{int(bool(web))}:{own}:{'+'.join(sorted(set(mcp)))}"


def _parse_mix(name: str) -> dict | None:
    parts = name.split(":")
    if len(parts) != 5 or parts[0] != MIX or parts[2] not in ("0", "1") or parts[3] not in OWN_MODES:
        return None
    return {"style": parts[1], "web": parts[2] == "1", "own": parts[3], "mcp": [s for s in parts[4].split("+") if s]}


def _join(texts: list[str]) -> str:
    out: list[str] = []
    for t in texts:
        t = (t or "").strip()
        if t and t not in out:
            out.append(t)
    return "\n".join(out)


def mixed_preset(cfg: "Config", name: str) -> Preset | None:
    """The preset of a selection the user made in the card (sources + report style), None if `name` is not valid.

    Base: the style's preset; for a focus style the preset of the one ticked data source when there is exactly one
    (its tuned search and writing rules), or `own_only_preset` when the user's documents are the only source. The
    ticked sources' presets add their search rules, writing rules, preferred domains and MCP tool allowlists. Web
    search, MCP servers and the own-sources mode are exactly what the user ticked.
    """
    sel = _parse_mix(name)
    if sel is None or sel["style"] not in cfg.styles or cfg.styles[sel["style"]].preset not in cfg.presets:
        return None
    if any(s == "web" or s not in cfg.sources for s in sel["mcp"]):
        return None
    servers, web = sel["mcp"], sel["web"]
    homes = []
    for s in servers:
        home = cfg.presets.get(cfg.sources[s].preset or "")
        if home and home not in homes:
            homes.append(home)
    base = cfg.presets[cfg.styles[sel["style"]].preset or ""]  # checked above: the style's preset exists
    if base.engine == "focus":
        if sel["own"] == "only" and cfg.own_only_preset in cfg.presets:
            base = cfg.presets[cfg.own_only_preset]
        elif len(homes) == 1:
            base = homes[0]
    rules = [base] + [h for h in homes if h is not base]
    mcp = {}
    for s in servers:
        tools = []
        for p in rules:
            tools += [t for t in p.mcp.get(s, []) if t not in tools]
        mcp[s] = tools or list(cfg.mcp_tool_descriptions.get(s, {}))
    routes = {}
    for intent in INTENTS:
        known = [r for p in rules for r in p.routes.get(intent, []) if r.startswith("mcp:") and r[4:] in servers]
        extra = [f"mcp:{s}" for s in servers] if intent != "secondary" or not web else []
        route = list(dict.fromkeys(known + extra))
        routes[intent] = route + (["web"] if web else [])
    domains = {intent: _join_list([p.prefer_domains.get(intent, []) for p in rules]) for intent in INTENTS}
    report = dict(base.report)
    writer = _join([p.report.get("writer_rules", "") for p in rules])
    if writer:
        report["writer_rules"] = writer
    query = _join(
        [p.query_rules for p in rules]
        + (
            []
            if web
            else ["Keine Websuche in dieser Recherche: nur die Datenwerkzeuge und die Dokumente des Benutzers."]
        )
    )
    offered = cfg.offered_sources()
    labels = ([offered["web"].label] if web and "web" in offered else []) + [cfg.sources[s].label for s in servers]
    return replace(
        base,
        name=name,
        description=f"{cfg.styles[sel['style']].label}: {', '.join(labels) or 'eigene Dokumente'}",
        routes=routes,
        mcp=mcp,
        prefer_domains={k: v for k, v in domains.items() if v},
        query_rules=query,
        report=report,
        own_sources=sel["own"],
        web=web,
    )


def _join_list(lists: list[list[str]]) -> list[str]:
    return list(dict.fromkeys(x for xs in lists for x in xs))


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
    # shown to the user in the Deep Research card (one question per enabled server); preset: whose rules come with it
    label: str = Field("", max_length=80)
    description: str = Field("", max_length=1000)
    preset: str | None = None


class ChoiceSpec(_Strict):
    """One source or report style under `choices:`."""

    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    preset: str | None = None


class ChoicesSpec(_Strict):
    """`choices:`: what the user picks in the two steps of the Deep Research card (docs/presets.md)."""

    sources: dict[str, ChoiceSpec] = Field(default_factory=dict)
    styles: dict[str, ChoiceSpec] = Field(min_length=1)
    own_only_preset: str | None = None


class PresetsFile(_Strict):
    """The whole presets file, with the references between its parts checked."""

    mcp_servers: dict[str, McpServerSpec] = Field(default_factory=dict)
    budgets: dict[str, BudgetSpec]
    defaults: dict[str, Any] | None = None  # YAML anchor that the presets merge (`<<: *defaults`)
    presets: dict[str, PresetSpec] = Field(min_length=1)
    choices: ChoicesSpec | None = None

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
        for name, spec in self.mcp_servers.items():
            if spec.preset and spec.preset not in self.presets:
                problems.append(f"mcp_servers.{name}.preset: no such preset {spec.preset!r}")
        if self.choices:
            for key, c in self.choices.sources.items():
                if key != "web":
                    problems.append(
                        f"choices.sources.{key}: only 'web' "
                        "(MCP servers are offered from mcp_servers: label, description, preset)"
                    )
                if c.preset and c.preset not in self.presets:
                    problems.append(f"choices.sources.{key}.preset: no such preset {c.preset!r}")
            for key, c in self.choices.styles.items():
                if not c.preset or c.preset not in self.presets:
                    problems.append(f"choices.styles.{key}.preset: needs a preset of presets (got {c.preset!r})")
            own = self.choices.own_only_preset
            if own and own not in self.presets:
                problems.append(f"choices.own_only_preset: no such preset {own!r}")
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
    choices = spec.choices
    return Config(
        presets=presets,
        budgets=budgets,
        mcp_servers=servers,
        mcp_hints=hints,
        mcp_disabled=disabled,
        mcp_tool_descriptions=descs,
        sources=(
            {k: Choice(**c.model_dump()) for k, c in choices.sources.items()}
            | {  # every MCP server is a source of the card: a new server shows up without further configuration
                name: Choice(
                    label=entry.label or name,
                    description=entry.description or (entry.hint.strip().splitlines() or [name])[0],
                    preset=entry.preset,
                )
                for name, entry in spec.mcp_servers.items()
            }
        )
        if choices
        else {},
        styles={k: Choice(**c.model_dump()) for k, c in choices.styles.items()} if choices else {},
        own_only_preset=choices.own_only_preset if choices else None,
    )


if __name__ == "__main__":  # check a presets file: python -m bebbi_deep_research.engine.config FILE
    try:
        cfg = load_config(Path(sys.argv[1]))
    except (PresetsError, IndexError, OSError) as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    print(f"ok: {len(cfg.presets)} presets, budgets {', '.join(cfg.budgets)}, MCP servers {', '.join(cfg.mcp_servers)}")

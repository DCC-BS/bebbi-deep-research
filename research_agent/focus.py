"""Focus engine: a bounded tool-calling loop (web search, page fetch, allowlisted MCP tools) for
administrative questions, with the citation layer done in code.

The loop is the approach that won the German set (docs/bebbi-bot-deep-research.md): few calls, concise
report. What code adds: every tool result item is a source with an ID, a public URL, a title and a date;
the model cites IDs; code checks numbers against the cited sources, re-points or removes citations and
builds the source list with Stand. See docs/research-agent-design.md, "Focus engine"."""

import asyncio
import json
import re
import time

from . import own as O
from . import prompts as P
from .config import Budget, Preset
from .sources import Registry, Source, mcp_to_sources, search_to_sources
from .text import Evidence, checked_numbers, fix_sr_labels, split_units
from .tools import focus

WEB_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web (Google). Returns titles, URLs, snippets and source IDs.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "3-8 keywords"},
            "language": {"type": "string", "enum": ["de", "fr", "it", "en"], "description": "language of the query"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page",
        "description": "Read a web page or PDF (the passages most relevant to the question).",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
]
_SOURCE_SECTION = re.compile(r"\n#{1,3}\s*(?:Quellen|Quellenverzeichnis|Quellenangaben|Sources|References|Referenzen|"
                             r"Literatur|Belege)\b.*\Z", re.S | re.I)
LIST_LIMITS = {"find_geschaefte": 30, "search_documents": 10, "get_datasets": 8, "search_indicators": 10}
_IDS = re.compile(r"\[S(\d+)(?:\s*[,;/]\s*S?(\d+))*\]")


class FocusEngine:
    def __init__(self, agent):
        self.agent = agent                      # ResearchAgent: llm, tools, MCP clients, events, config
        self.llm, self.tools, self.cfg = agent.llm, agent.tools, agent.cfg

    # ---------------------------------------------------------------- tools
    async def _tool_specs(self, preset: Preset) -> tuple[list[dict], dict, list[str]]:
        own = self.agent.own
        specs, routes, hints = ([] if own and own.mode == "only" else list(WEB_TOOLS)), {}, []
        if own:
            specs += O.tool_specs(own)
        for server, allow in preset.mcp.items():
            if own and own.mode == "only":
                break                             # only the user's own sources: no data tool either
            if server not in self.cfg.mcp_servers:
                continue                          # disabled or unknown server: the preset does without it
            client = await asyncio.to_thread(self.agent._mcp_client, server, preset)
            if not client:
                continue
            try:
                tools = await asyncio.to_thread(client.tools)
            except Exception as e:
                self.agent._mcp_errors[server] = f"{type(e).__name__}: {str(e)[:200]}"
                continue
            prefix = server.replace("-", "_")
            for t in tools:
                name = f"{prefix}__{t['name']}"
                routes[name] = (server, client, t["name"])
                specs.append({"type": "function", "function": {
                    "name": name, "description": self.cfg.mcp_tool_descriptions.get(server, {}).get(t["name"])
                                                 or (t.get("description") or "")[:700],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}})
            if self.cfg.mcp_hints.get(server):
                hints.append("- " + self.cfg.mcp_hints[server].replace("\n", "\n  ").replace(f"{server} (", f"{prefix}__* ("))
        return specs, routes, hints

    async def _call(self, name: str, args: dict, routes: dict, reg: Registry, question: str, lang: str,
                    ctx: dict, trail: list) -> str:
        started = time.time()
        try:
            if name == "web_search":
                q = str(args.get("query", "")).strip()
                if not q:
                    return "error: empty query"
                await self.agent._emit("status", f"Suche: {q[:80]}")
                hits = await self.tools.search(q, args.get("language") or lang, num=8)
                out, added = search_to_sources(hits, reg)
            elif name == "fetch_page":
                url = str(args.get("url", "")).strip()
                if not url.startswith("http"):
                    return "error: give a full URL from a search result"
                await self.agent._emit("status", f"Lese: {url[:80]}")
                title, text, date = await self.tools.fetch_meta(url)
                if len(text) < 200:
                    return f"could not read {url}"
                text = await focus(self.llm, text, [question], budget_chars=6000)
                src = reg.add(url, title, "web", text, date)
                added = [src]
                out = f"[{src.id}] {src.title}{' (' + src.date + ')' if src.date else ''}\n{text}"
            elif name in O.OWN_TOOLS and self.agent.own:
                await self.agent._emit("status", "Lese eigene Quellen" if name == "read_upload" else "Durchsuche Wissensbasis")
                out, added = await O.call(name, args, self.agent.own, reg, self.llm, question)
            elif name in routes:
                server, client, tool = routes[name]
                if tool in LIST_LIMITS and "limit" not in args:   # long candidate lists: the model sees ~30
                    args = {**args, "limit": LIST_LIMITS[tool]}
                await self.agent._emit("status", f"{server}: {tool}")
                raw = await client.acall(tool, args)
                out, added = mcp_to_sources(server, tool, args, raw, reg, self.cfg.mcp_servers[server], ctx)
            else:
                return f"error: unknown tool {name}"
        except Exception as e:                     # tool errors go back to the model
            trail.append({"tool": name, "args": args, "error": str(e)[:300]})
            return f"tool error: {str(e)[:300]}"
        trail.append({"tool": name, "args": args, "sources": [s.id for s in added], "chars": len(out),
                      "s": round(time.time() - started, 1)})
        return out

    # ---------------------------------------------------------------- run
    async def run(self, task: str, preset: Preset, budget: Budget, plan: dict | None, requirements: str):
        from .agent import Result, _today        # late import: agent imports this module
        started = time.time()
        if plan is None and preset.plan_first:
            await self.agent._emit("status", "Erstelle Rechercheplan")
            plan = await self.agent.plan(task, preset, budget, own=self.agent.own)
        lang = (plan or {}).get("language") or (preset.language if preset.language != "auto" else "de")
        de = lang == "de"
        specs, routes, hints = await self._tool_specs(preset)
        reg, ctx, trail = Registry(), {}, []
        own_system, own_first = O.prompt_block(self.agent.own, reg, de) if self.agent.own else ("", "")
        system = P.FOCUS_SYSTEM.format(
            today=_today(), language_name="German" if de else "English",
            spelling="Use Swiss Standard German spelling: never write ß, always ss." if de else "",
            max_calls=budget.focus_calls, tool_hints=("DATA TOOLS\n" + "\n".join(hints) + "\n") if hints else "",
            query_rules=preset.query_rules.strip(), words=budget.focus_words,
            summary="Zusammenfassung" if de else "Summary",
            open_points="Offene Punkte und Grenzen der Recherche" if de else "Open questions and limitations",
            writer_rules=preset.report.get("writer_rules", "")) + own_system
        plan_text = ""
        if plan:
            lines = ["", "RESEARCH PLAN (approved; use it as the checklist and the report structure):"]
            for p in plan["parts"]:
                lines.append(f"- {p['title']}: {p['goal']}")
                lines += [f"  - {q['text']}" for q in plan["sub_questions"] if q["part"] == p["id"]]
            if plan.get("must_name"):
                lines.append("Must be covered: " + ", ".join(plan["must_name"]))
            plan_text = "\n".join(lines) + "\n"
        req = ""
        if requirements:
            req = ("\nREPORT REQUIREMENTS:\n" + requirements.strip()
                   + "\n(The URLs of the sources are added automatically from your source IDs.)\n")
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": P.FOCUS_USER.format(task=task, plan=plan_text, requirements=req) + own_first}]
        calls, final = 0, ""
        for _turn in range(budget.focus_calls + 4):
            out_of_budget = calls >= budget.focus_calls or self.llm.usage.total > budget.max_tokens * 0.8
            msg = await self.llm.chat("think", messages, None if out_of_budget else specs, step="focus")
            if not msg.tool_calls:
                final = (msg.content or "").strip()
                if final or out_of_budget:
                    break
                messages.append({"role": "user", "content": P.FOCUS_FINAL})
                calls = budget.focus_calls
                continue
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            todo = msg.tool_calls[:max(0, budget.focus_calls - calls)]
            calls += len(todo)
            outs = await asyncio.gather(*(self._call(tc.function.name, _args(tc), routes, reg, task, lang, ctx, trail)
                                          for tc in todo))
            for tc, out in zip(todo, outs):
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
            for tc in msg.tool_calls[len(todo):]:  # every tool call needs an answer, or the next request is invalid
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": "skipped: tool budget used up"})
            if calls >= budget.focus_calls:
                messages.append({"role": "user", "content": P.FOCUS_FINAL})
        if not final:
            msg = await self.llm.chat("think", messages + [{"role": "user", "content": P.FOCUS_FINAL}], None, step="focus")
            final = (msg.content or "").strip()

        await self.agent._emit("status", "Prüfe Belege")
        stats: dict = {"tool_calls": calls, "sources": len(reg.sources)}
        report = finalize(final, reg, task, de, stats)
        stats.update({"runtime_s": round(time.time() - started, 1), "engine": "focus",
                      "tools_used": _count(t["tool"] for t in trail),
                      "mcp_errors": dict(self.agent._mcp_errors)})
        u = self.llm.usage
        await self.agent._emit("status", "Bericht fertig")
        return Result(report=report, plan=plan or {}, ledger={"sources": reg.to_list(), "trail": trail, "draft": final},
                      usage={"calls": u.calls, "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                             "by_step": u.by_step}, stats=stats)


def _args(tc) -> dict:
    try:
        a = json.loads(tc.function.arguments or "{}")
        return a if isinstance(a, dict) else {}
    except json.JSONDecodeError:
        return {}


def _count(items) -> dict:
    out: dict = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


# -------------------------------------------------------------------- citations
def finalize(text: str, reg: Registry, task: str, de: bool, stats: dict) -> str:
    """Check citations against the sources, resolve IDs to numbers and append the source list."""
    text = _SOURCE_SECTION.sub("", text.strip())
    text = re.sub(r"\(\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\)", r"[\1]", text)               # (S3) -> [S3]
    text = _IDS.sub(lambda m: "".join(f"[S{i}]" for i in re.findall(r"\d+", m.group(0))), text)  # [S3, S5] -> [S3][S5]
    # bare ID lists in prose ("z. B. S8, S9, S12") -> citations; single bare "S1" may be an S-Bahn line
    text = re.sub(r"(?<![\[\w])S\d+(?:\s*(?:,|und|and)\s*S\d+)+(?![\w\]])",
                  lambda m: "".join(f"[S{i}]" for i in re.findall(r"\d+", m.group(0))), text)
    text = verify(text, reg, Evidence(task), de, stats)
    report = resolve(text, reg, de, stats)
    report = fix_sr_labels(report, reg.cantonal_numbers)
    return report.replace("ß", "ss") if de else report


def verify(text: str, reg: Registry, task_ev: Evidence, de: bool, stats: dict) -> str:
    """Per sentence: unknown IDs go; numbers must be in a cited source. If not, re-point to a source that
    has them (paragraph first, then report, then any source); bare years never decide, they occur in too
    many sources. If no source at all has any of the numbers, a cited prose sentence goes (reliable signal),
    a table row is marked. Sentences without citation are never removed; they get a citation only from a
    source the report already cites. Every change is logged in stats["citation_changes"]."""
    def bump(k):
        stats[k] = stats.get(k, 0) + 1

    report_ids = list(dict.fromkeys(f"S{i}" for i in re.findall(r"\[S(\d+)\]", text) if f"S{i}" in reg.sources))
    out_lines = []
    open_section = False
    for line in text.split("\n"):
        line_ids = list(dict.fromkeys(f"S{i}" for i in re.findall(r"\[S(\d+)\]", line) if f"S{i}" in reg.sources))
        if line.lstrip().startswith("#"):
            open_section = bool(re.search(r"offene punkte|grenzen|open questions|limitations", line, re.I))
            out_lines.append(line)
            continue
        if open_section or not line.strip():     # limits section: own statements, not checked
            out_lines.append(line)
            continue
        is_row = line.lstrip().startswith("|")
        units = [line] if is_row else split_units(line)
        new_units = []
        for u in units:
            ids = [f"S{i}" for i in re.findall(r"\[S(\d+)\]", u)]
            for sid in ids:
                if sid not in reg.sources:
                    u = u.replace(f"[{sid}]", "")
                    bump("unknown_ids")
            ids = list(dict.fromkeys(i for i in ids if i in reg.sources))
            if is_row and not ids:               # header, separator or a row without source: left alone
                new_units.append(u)
                continue
            nums = checked_numbers(re.sub(r"\[S\d+\]", "", u), task_ev)
            if not nums:
                if ids:
                    bump("cited_ok")
                new_units.append(u)
                continue
            cited = [reg.sources[i] for i in ids]
            missing = [(tok, f) for tok, f in nums if not any(s.ev().supports(f) for s in cited)]
            if not missing:
                bump("cited_ok" if ids else "uncited_numbers")
                new_units.append(u)
                continue
            if not any(_distinctive(tok) for tok, _ in missing):
                # only years are missing: too common to pick another source by (a letter on e-mobility that
                # mentions "2022" is not a source for a 2022 vote); leave the model's citation as it is
                bump("years_only" if ids else "uncited_numbers")
                new_units.append(u)
                continue
            # where code may look: for a cited sentence the sources of this paragraph, then of the report, then
            # any; for an uncited one only sources the report already cites (code never introduces a source)
            near = [reg.sources[i] for i in line_ids if i not in ids]
            pool = near + [reg.sources[i] for i in report_ids if i not in ids and i not in line_ids]
            if ids:
                pool += [x for x in reg.sources.values() if x not in pool and x not in cited]
            fix = _find_source(pool, [f for _, f in missing]) or \
                _find_source(pool, [f for tok, f in missing if _distinctive(tok)])
            if fix:
                tag = f"[{fix.id}]"
                before = u
                if ids and len(ids) >= 2:
                    u = u.replace(f"[{ids[-1]}]", tag, 1)
                elif ids:
                    u = u.replace(f"[{ids[-1]}]", f"[{ids[-1]}]{tag}", 1)
                else:
                    u = _append_tag(u, tag)
                bump("repaired" if ids else "added_citation")
                stats.setdefault("citation_changes", []).append({
                    "action": "repaired" if ids else "added", "sentence": before.strip()[:240],
                    "numbers": [tok for tok, _ in missing], "from": [c.title[:100] for c in cited], "to": fix.title[:100]})
                new_units.append(u)
                continue
            anywhere = [tok for tok, f in nums if any(s.ev().supports(f) for s in reg.sources.values())]
            if ids and not anywhere:
                if is_row:
                    u = re.sub(r"\[S\d+\]", "", u).rstrip()
                    u = (u[:-1].rstrip() + " (nicht belegt) |") if u.endswith("|") else u + (" (nicht belegt)" if de else " (no evidence)")
                    bump("marked_rows")
                    new_units.append(u)
                else:
                    bump("dropped_sentences")
                    stats.setdefault("dropped", []).append(u.strip()[:200])
                    stats.setdefault("citation_changes", []).append({"action": "dropped", "sentence": u.strip()[:240]})
                continue
            bump("partly_supported" if ids else "uncited_numbers")
            new_units.append(u)
        out_lines.append(" ".join(x.strip() for x in new_units) if not is_row else (new_units[0] if new_units else ""))
    return "\n".join(out_lines)


def _append_tag(u: str, tag: str) -> str:
    m = re.search(r"([.!?:;]?)\s*$", u)
    return u[:m.start()] + f" {tag}" + m.group(1) if m else u + f" {tag}"


def _find_source(pool: list[Source], forms: list[dict]) -> Source | None:
    return next((s for s in pool if forms and all(s.ev().supports(f) for f in forms)), None)


def _distinctive(tok: str) -> bool:
    """A number that can pick a source: amounts, percentages, identifiers, full dates; not a bare year."""
    return not re.fullmatch(r"(?:19|20)\d{2}", re.sub(r"['’\u202f\u2009 ]", "", tok))


def resolve(text: str, reg: Registry, de: bool, stats: dict) -> str:
    order: dict[str, int] = {}
    for m in re.finditer(r"\[S(\d+)\]", text):
        order.setdefault(f"S{m.group(1)}", len(order) + 1)
    text = re.sub(r"\[S(\d+)\]", lambda m: f"[{order[f'S{m.group(1)}']}]", text)
    text = re.sub(r"(\[\d+\])(\s*\1)+", r"\1", text)
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text)
    text = re.sub(r"(?<=\S)[ \t]{2,}", " ", text)
    lines = []
    for sid, n in sorted(order.items(), key=lambda x: x[1]):
        s = reg.sources[sid]
        when = []
        if s.date:
            when.append(f"Stand {s.date}" if de else f"dated {s.date}")
        when.append(f"abgerufen am {s.fetched_at}" if de else f"accessed {s.fetched_at}")
        where = s.url or ("keine öffentliche URL" if de else "no public URL")
        lines.append(f"[{n}] {s.title}. {where} ({'; '.join(when)})")
    stats["cited_sources"] = len(order)
    stats["cited_mcp_sources"] = sum(1 for sid in order if reg.sources[sid].kind.startswith("mcp:"))
    if not lines:
        return text.strip() + "\n"
    return text.strip() + "\n\n" + ("## Quellen" if de else "## Sources") + "\n\n" + "\n".join(lines) + "\n"

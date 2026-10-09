"""research-agent core: plan -> research rounds (queries, MCP, search, triage, fetch, extract, reflect)
-> section-wise writing -> verification. See docs/research-agent-design.md."""

import asyncio
import dataclasses
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import prompts as P
from .config import Budget, Config, Preset
from .ledger import Card, Ledger, SubQ, quote_in_page
from .llm import LLM
from . import own as O
from .sources import Registry, mcp_to_sources
from .text import Evidence, checked_numbers, fix_sr_labels, split_units, unsupported
from .tools import MCPClient, Tools, domain, domain_match, focus

Event = Callable[[str, str], Awaitable[None] | None]
INTENTS = ("primary", "data", "secondary")


@dataclass
class Result:
    report: str
    plan: dict
    ledger: dict
    usage: dict
    stats: dict = field(default_factory=dict)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


class ResearchAgent:
    def __init__(self, cfg: Config, llm: LLM, tools: Tools, on_event: Event | None = None, concurrency: int = 3):
        self.cfg, self.llm, self.tools = cfg, llm, tools
        self.on_event = on_event
        self.sem = asyncio.Semaphore(concurrency)
        self._mcp_clients: dict[str, MCPClient] = {}
        self._mcp_failed: dict[str, float] = {}
        self._mcp_errors: dict[str, str] = {}
        self._mcp_lock = threading.Lock()
        self._de = False
        self._evs: dict[str, Evidence] = {}      # card id -> number forms (verify)
        self._task_ev = Evidence()               # numbers of the task itself need no evidence
        self._mcp_ctx: dict = {}                 # per-run MCP lookups (dataset titles)
        self.own: O.OwnSources | None = None     # the user's files / knowledge bases of the current run (research_agent/own.py)

    async def _emit(self, kind: str, text: str) -> None:
        if self.on_event:
            r = self.on_event(kind, text)
            if asyncio.iscoroutine(r):
                await r

    # ------------------------------------------------------- chat turns
    async def clarify(self, task: str, own: "O.OwnSources | None" = None) -> dict:
        """At most 2 clarifying questions; none for detailed tasks (they already state their scope)."""
        if len(task) > 600:
            return {"questions": [], "assumptions": []}
        if own and (own.files or own.collections):
            task += ("\n\n(The user has ALREADY ATTACHED these own sources: "
                     + "; ".join([f'file "{f.name}"' for f in own.files] + [f'knowledge base "{c.name}"' for c in own.collections])
                     + ". Phrases like 'my document' or 'the attached file' mean them. Never ask the user to upload or "
                       "provide them again.)")
        data = await self.llm.json("fast", "You decide whether a research task needs clarification. Output JSON only.",
                                   P.CLARIFY.format(task=task), step="clarify", default={}) or {}
        return {"questions": [str(q) for q in data.get("questions") or []][:2],
                "assumptions": [str(a) for a in data.get("assumptions") or []][:5]}

    async def edit_plan(self, plan: dict, instruction: str, task: str, preset: Preset, budget: Budget) -> dict:
        raw = await self.llm.json("fast", "You edit research plans. Output JSON only.",
                                  P.EDIT_PLAN.format(plan=json.dumps(plan, ensure_ascii=False), instruction=instruction,
                                                     max_subq=budget.max_subq), step="edit_plan", default=None)
        return self._validate_plan(raw, task, preset, budget) if isinstance(raw, dict) else plan

    # ------------------------------------------------------------------ plan
    @staticmethod
    def _own_for(own: "O.OwnSources | None", preset: Preset) -> "O.OwnSources | None":
        """The user's own sources as the preset wants them: ignored for own_sources: off, web-less for `only`."""
        if own is None or preset.own_sources == "off" or not (own.files or own.collections):
            return None
        own.mode = "only" if preset.own_sources == "only" else "prefer"
        return own

    async def plan(self, task: str, preset: Preset, budget: Budget, own: "O.OwnSources | None" = None) -> dict:
        hint = ""
        if preset.report.get("parts_hint"):
            hint = f"Typical parts for this kind of research: {', '.join(preset.report['parts_hint'])} (adapt to the task)."
        own = self._own_for(own, preset)
        if own:
            hint += (" The user supplied own sources: " + "; ".join(
                [f'file "{f.name}"' + (f" (begins: {f.text[:300]!r})" if f.text else "") for f in own.files]
                + [f'knowledge base "{c.name}"' for c in own.collections])
                + (". Answer ONLY from these; plan sub-questions the documents can answer." if own.mode == "only"
                   else ". Plan sub-questions so that these sources are used wherever they are relevant."))
        hits = [] if own and own.mode == "only" else \
            await self.tools.search(task[:200], "de" if preset.language == "de" else "en", num=8)
        discovery = "\n".join(f"- {h['title']} ({domain(h['url'])}): {h['snippet'][:180]}" for h in hits) or "-"
        raw = await self.llm.json("think", "You are a careful research planner. Output JSON only.",
                                  P.PLAN.format(today=_today(), task=task, discovery=discovery, parts_hint=hint, max_subq=budget.max_subq,
                                                min_parts=budget.parts[0], max_parts=budget.parts[1]),
                                  step="plan", default={}) or {}
        return self._validate_plan(raw, task, preset, budget)

    def _validate_plan(self, raw: dict, task: str, preset: Preset, budget: Budget) -> dict:
        parts = [p for p in raw.get("parts") or [] if isinstance(p, dict) and p.get("title")]
        parts = [p for p in parts if not re.search(r"summary|zusammenfassung|fazit|limitation|offene punkte|grenzen",
                                                   p["title"], re.I)] or [{"title": "Findings", "goal": task}]
        for i, p in enumerate(parts, 1):
            p["id"] = f"P{i}"
            p.setdefault("goal", p["title"])
        part_ids = {p["id"] for p in parts}
        raw_ids = {str(p.get("id")): pid for p, pid in zip(raw.get("parts") or [], [p["id"] for p in parts])}
        subqs = []
        for q in raw.get("sub_questions") or []:
            if not isinstance(q, dict) or not q.get("text"):
                continue
            part = raw_ids.get(str(q.get("part")), q.get("part"))
            subqs.append({"id": f"Q{len(subqs) + 1}", "part": part if part in part_ids else parts[0]["id"],
                          "text": str(q["text"]).strip(),
                          "intent": q.get("intent") if q.get("intent") in INTENTS else "secondary"})
        subqs = subqs[:budget.max_subq] or [{"id": "Q1", "part": parts[0]["id"], "text": task[:300], "intent": "secondary"}]
        lang = preset.language if preset.language != "auto" else (raw.get("language") if raw.get("language") in ("de", "en") else "en")
        return {"title": str(raw.get("title") or task[:80]), "task_core": str(raw.get("task_core") or task[:400]),
                "language": lang, "parts": parts, "sub_questions": subqs,
                "must_name": [str(x) for x in raw.get("must_name") or [] if str(x).strip()][:25],
                "constraints": [str(x) for x in raw.get("constraints") or [] if str(x).strip()][:15]}

    # ------------------------------------------------------------- research
    def _mcp_client(self, server: str, preset: Preset) -> MCPClient | None:
        """Connect with 3 attempts; a failure is remembered for 2 minutes only (transient outages happen),
        and callers fall back to web search meanwhile."""
        with self._mcp_lock:
            client = self._mcp_clients.get(server)
            if client is not None:
                return client
            if time.time() - self._mcp_failed.get(server, 0) < 120:
                return None
            url = self.cfg.mcp_servers.get(server)
            if not url:
                return None
            for attempt in range(3):
                try:
                    self._mcp_clients[server] = MCPClient(url, preset.mcp.get(server))
                    return self._mcp_clients[server]
                except Exception as e:
                    self._mcp_errors[server] = f"{type(e).__name__}: {str(e)[:200]}"
                    time.sleep(2 * (attempt + 1))
            self._mcp_failed[server] = time.time()
            return None

    async def _queries(self, sq: SubQ, plan: dict, preset: Preset, budget: Budget, led: Ledger) -> list[tuple[str, str]]:
        default_lang = "de" if plan["language"] == "de" else "en"
        out = await self.llm.text("fast", "You write precise web search queries. Output only the requested lines.",
                                  P.QUERIES.format(today=_today(), task_core=plan["task_core"], intent=sq.intent,
                                                   subq=sq.text, previous="; ".join(led.queries[-40:]) or "-",
                                                   gap=led.gaps.get(sq.id) or "-", default_lang=default_lang,
                                                   rules=preset.query_rules.strip(), n=budget.queries_per_subq),
                                  step="queries", max_tokens=600)
        queries = []
        for line in out.splitlines():
            parts = [x.strip() for x in line.strip("-* ").split("|")]
            if len(parts) >= 2 and parts[0].lower() in ("de", "fr", "it", "en") and parts[1]:
                lang, q = parts[0].lower(), parts[1].strip()
            elif line.strip() and "|" not in line:
                lang, q = default_lang, line.strip("-* ").strip()
            else:
                continue
            if q.count('"') % 2:  # unbalanced quotes make Google return nothing: drop them all
                q = q.replace('"', "")
            if q.startswith('"') and q.endswith('"') and q.count('"') == 2 and " " in q:
                q = q[1:-1]       # the whole query quoted: search as keywords, not as one exact phrase
            if q and not led.asked(q):
                queries.append((lang, q))
                led.queries.append(q)
        return queries[:budget.queries_per_subq]

    async def _web(self, sq: SubQ, plan: dict, preset: Preset, budget: Budget, led: Ledger) -> int:
        queries = await self._queries(sq, plan, preset, budget, led)
        preferred = preset.prefer_domains.get(sq.intent) or []
        searches = []
        for i, (lang, q) in enumerate(queries):
            if i == 0 and preferred:  # first query restricted to the preset's primary domains
                # long site: OR chains return nothing half the time: use the 3 most important domains
                searches.append(self.tools.search(q + " " + " OR ".join(f"site:{d}" for d in preferred[:3]), lang))
            searches.append(self.tools.search(q, lang))
        results, seen = [], set()
        for batch in await asyncio.gather(*searches):
            for r in batch:
                u = r["url"].split("#")[0]
                if u in seen or u in led.fetched or domain_match(u, preset.avoid_domains) or u.lower().endswith((".mp4", ".zip")):
                    continue
                seen.add(u)
                results.append(r)
        all_pref = [d for v in preset.prefer_domains.values() for d in v]
        results.sort(key=lambda r: 0 if domain_match(r["url"], all_pref) else 1)  # stable: preferred first
        led.log("search", subq=sq.id, queries=[q for _, q in queries], results=len(results))
        if not results:
            return 0
        pick = results[:budget.fetch_per_subq]
        if len(results) > budget.fetch_per_subq:
            listing = "\n".join(f"[{i}] {r['title']} | {domain(r['url'])} | {r['snippet'][:220]}" for i, r in enumerate(results[:24]))
            out = await self.llm.text("fast", "You select relevant search results. Answer only with indices.",
                                      P.TRIAGE.format(task_core=plan["task_core"], subq=sq.text, results=listing,
                                                      k=budget.fetch_per_subq), step="triage", max_tokens=100)
            idx = [int(x) for x in re.findall(r"\d+", out) if int(x) < min(24, len(results))]
            idx = list(dict.fromkeys(idx))[:budget.fetch_per_subq]
            if idx or "none" in out.lower():
                pick = [results[i] for i in idx]
        led.fetched.update(r["url"] for r in pick)
        counts = await asyncio.gather(*(self._read(sq, r["url"], r["title"], plan, led) for r in pick))
        return sum(counts)

    async def _read(self, sq: SubQ, url: str, title: str, plan: dict, led: Ledger) -> int:
        page_title, text = await self.tools.fetch(url)
        if len(text) < 300:
            led.log("fetch_failed", subq=sq.id, url=url)
            return 0
        excerpt = await focus(self.llm, text, [sq.text, plan["task_core"]])
        return await self._extract(sq, url, page_title or title, excerpt, plan, led, "web")

    async def _extract(self, sq: SubQ, url: str, title: str, text: str, plan: dict, led: Ledger, kind: str,
                       sources: Registry | None = None) -> int:
        prompt = P.EXTRACT.format(task_core=plan["task_core"], subq=sq.text, title=title, url=url, text=text,
                                  lang="German" if plan["language"] == "de" else "English")
        if sources is not None:
            prompt += ('\n- Add "source" to each finding: the ID in square brackets (e.g. "S4") of the item the quote '
                       'comes from.')
        data = await self.llm.json("fast", "You extract verifiable evidence. Output JSON only.", prompt,
                                   step="extract", default={}) or {}
        added, rejected = 0, 0
        for f in (data.get("findings") or []) if isinstance(data, dict) else []:
            if not isinstance(f, dict) or not f.get("quote") or not f.get("finding"):
                continue
            if not quote_in_page(str(f["quote"]), text):
                rejected += 1
                continue
            card_url, card_title = url, title
            date = str(data.get("date") or "") if isinstance(data, dict) else ""
            if sources is not None:              # MCP: the item's own URL, title and date, never the endpoint
                src = sources.get(str(f.get("source", "")).strip("[] "))
                if src is None:
                    src = next((s for s in sources.sources.values() if s.url and str(f["quote"])[:60] in s.text), None)
                if src is not None:
                    card_url, card_title, date = src.url, src.title, src.date or date
            if led.add_card(sq.id, card_url, card_title, str(f["quote"])[:600], str(f["finding"]), kind, date=date):
                added += 1
        led.log("extract", subq=sq.id, url=url, added=added, rejected_quotes=rejected)
        return added

    async def _mcp(self, sq: SubQ, server: str, plan: dict, preset: Preset, led: Ledger, max_calls: int = 3) -> int:
        client = await asyncio.to_thread(self._mcp_client, server, preset)
        if not client:
            led.log("mcp_unavailable", subq=sq.id, server=server, error=self._mcp_errors.get(server, ""))
            return 0
        own = self.cfg.mcp_tool_descriptions.get(server, {})
        tools = [{"type": "function", "function": {"name": t["name"], "description": own.get(t["name"]) or (t.get("description") or "")[:900],
                                                   "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}}
                 for t in await asyncio.to_thread(client.tools)]
        messages = [{"role": "system", "content": P.MCP_AGENT.format(today=_today(), task_core=plan["task_core"],
                                                                      subq=sq.text, max_calls=max_calls)},
                    {"role": "user", "content": sq.text}]
        added, calls = 0, 0
        while calls < max_calls:
            msg = await self.llm.chat("fast", messages, tools, step="mcp")
            if not msg.tool_calls:
                break
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls[:max_calls - calls]:
                calls += 1
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                key = f"{server}:{tc.function.name}:{json.dumps(args, sort_keys=True)}"
                if not args or key in led.mcp_seen:
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": "skipped: empty or repeated call - use different, specific arguments"})
                    continue
                led.mcp_seen.add(key)
                try:
                    out = await client.acall(tc.function.name, args)
                except Exception as e:
                    out = f"tool error: {e}"
                led.log("mcp_call", subq=sq.id, server=server, tool=tc.function.name, args=args, chars=len(out))
                if out.startswith("tool error"):
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": out[:1000]})
                    continue
                reg = Registry()                 # per call: the IDs the extractor sees
                out, _ = mcp_to_sources(server, tc.function.name, args, out, reg, self.cfg.mcp_servers[server],
                                        self._mcp_ctx, max_chars=14000)
                # catalogue listings are not evidence (German set analysis: 54 of 64 extracts gave nothing)
                if len(out) > 200 and tc.function.name not in ("get_datasets", "get_dataset", "get_facets", "list_facets"):
                    added += await self._extract(sq, "", f"{server}: {tc.function.name}", out, plan, led,
                                                 f"mcp:{server}", sources=reg)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": out[:8000]})
            # tool calls beyond the budget still need a tool message, or the next chat call is invalid
            answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
            for tc in msg.tool_calls:
                if tc.id not in answered:
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": "skipped: tool budget used up"})
        return added

    async def _own(self, sq: SubQ, plan: dict, led: Ledger) -> int:
        """Evidence cards from the user's uploads and knowledge bases (pipeline engine)."""
        items = await O.evidence_items(self.own, self.llm, [sq.text, plan["task_core"]])
        led.log("own_sources", subq=sq.id, items=[i["title"] for i in items])
        counts = await asyncio.gather(*(self._extract(sq, i["url"], i["title"], i["text"], plan, led, i["kind"])
                                        for i in items))
        return sum(counts)

    async def _research_subq(self, sq: SubQ, plan: dict, preset: Preset, budget: Budget, led: Ledger) -> int:
        async with self.sem:
            await self._emit("status", f"Recherche: {sq.text[:90]}")
            routes = preset.routes.get(sq.intent) or ["web"]
            added = 0
            if self.own:
                try:
                    added += await self._own(sq, plan, led)
                except Exception as e:
                    led.log("error", subq=sq.id, route="own", error=str(e)[:300])
                if self.own.mode == "only":
                    return added
            for route in routes:
                try:
                    if route.startswith("mcp:"):
                        added += await self._mcp(sq, route[4:], plan, preset, led)
                    elif route == "web":
                        added += await self._web(sq, plan, preset, budget, led)
                except Exception as e:  # one failing source must not stop the run
                    led.log("error", subq=sq.id, route=route, error=str(e)[:300])
            return added

    async def _reflect(self, rnd: int, plan: dict, budget: Budget, led: Ledger) -> list[SubQ]:
        lines = []
        for sq in led.subqs.values():
            cards = [c for c in led.cards.values() if c.subq == sq.id][:4]
            found = "; ".join(c.finding[:160] for c in cards) or "nothing"
            lines.append(f"{sq.id} [{led.coverage(sq.id)} sources] {sq.text}\n   found: {found}")
        data = await self.llm.json("fast", "You assess research coverage. Output JSON only.",
                                   P.REFLECT.format(round=rnd, task_core=plan["task_core"],
                                                    must_name=", ".join(plan["must_name"]) or "-",
                                                    status="\n".join(lines), max_follow_up=budget.max_follow_up),
                                   step="reflect", default={}) or {}
        todo = []
        for s in data.get("status") or []:
            if isinstance(s, dict) and s.get("id") in led.subqs:
                if s.get("missing"):
                    led.gaps[s["id"]] = str(s["missing"])[:300]
                if s.get("status") in ("partial", "missing") and led.coverage(s["id"]) < 3:
                    todo.append(led.subqs[s["id"]])
        part_ids = {p["id"] for p in plan["parts"]}
        for f in (data.get("follow_up") or [])[:budget.max_follow_up]:
            if not isinstance(f, dict) or not f.get("text"):
                continue
            sq = SubQ(id=f"Q{len(led.subqs) + 1}", part=f.get("part") if f.get("part") in part_ids else plan["parts"][0]["id"],
                      text=str(f["text"]), intent=f.get("intent") if f.get("intent") in INTENTS else "secondary", round=rnd + 1)
            led.subqs[sq.id] = sq
            plan["sub_questions"].append({"id": sq.id, "part": sq.part, "text": sq.text, "intent": sq.intent})
            todo.append(sq)
        led.log("reflect", round=rnd, todo=[s.id for s in todo], gaps=led.gaps)
        return todo[:budget.max_subq]

    # ---------------------------------------------------------------- write
    def _lang(self, plan: dict) -> tuple[str, str]:
        if plan["language"] == "de":
            return "German", "Use Swiss Standard German spelling: never write ß, always ss."
        return "English", ""

    def _card_lines(self, cards: list[Card]) -> str:
        return "\n".join(f'[{c.id}] {c.finding} | quote: "{c.quote[:450]}" | source: {c.title[:80]} ({domain(c.url)})'
                         for c in cards)

    async def _write_part(self, part: dict, plan: dict, budget: Budget, led: Ledger, task: str,
                          preset: Preset, requirements: str) -> str:
        sq_ids = {q["id"] for q in plan["sub_questions"] if q["part"] == part["id"]}
        cards = led.cards_for(sq_ids)
        if len(cards) > 45:  # keep source diversity: round-robin over URLs
            by_url: dict[str, list[Card]] = {}
            for c in cards:
                by_url.setdefault(c.url, []).append(c)
            cards, i = [], 0
            while len(cards) < 45:
                added = False
                for lst in by_url.values():
                    if i < len(lst) and len(cards) < 45:
                        cards.append(lst[i])
                        added = True
                if not added:
                    break
                i += 1
        lang, spelling = self._lang(plan)
        async with self.sem:
            await self._emit("status", f"Schreibe: {part['title']}")
            rules = (spelling + " " + preset.report.get("writer_rules", "")).strip()
            text = await self.llm.text("think", "You are a precise research writer.",
                                       P.WRITE_PART.format(language_name=lang, spelling=rules, task=task, title=plan["title"],
                                                           part_title=part["title"], part_goal=part["goal"],
                                                           other_parts=", ".join(p["title"] for p in plan["parts"] if p is not part),
                                                           constraints="; ".join(plan["constraints"]) or "-",
                                                           requirements=requirements or "-",
                                                           must_name=", ".join(plan["must_name"]) or "-",
                                                           cards=self._card_lines(cards) or "(no evidence found)",
                                                           length_rule=(preset.report.get("length_rule") or P.LENGTH_RULE).format(
                                                               words=preset.report.get("words_per_part") or budget.words_per_part),
                                                           format_rule=preset.report.get("format_rule") or P.FORMAT_RULE),
                                       step="write", max_tokens=8000)
        return re.sub(r"^#{1,2}\s+.*\n", "", text.strip(), count=1) if text.lstrip().startswith(("# ", "## ")) else text

    # --------------------------------------------------------------- verify
    def _card_ev(self, card: Card) -> Evidence:
        ev = self._evs.get(card.id)
        if ev is None:
            ev = self._evs[card.id] = Evidence(card.quote + " " + card.finding + " " + card.title + " " + card.date)
        return ev

    async def _verify(self, text: str, led: Ledger, stats: dict, entail: bool) -> str:
        """v2 behaviour plus a tolerant number check (decimal comma, thousands separators, dates, rounding).
        A failed check removes the citation, the sentence stays; only a cited sentence whose numbers occur in
        no card at all is dropped (reliable signal). A number found in another card re-points the citation."""
        # collapse stacked citations ([E1][E2], [E1, E2]) to the first one
        text = re.sub(r"\[E(\d+)(?:\s*[,;/&]\s*E?\d+)+\]",
                      lambda m: (stats.__setitem__("stacked", stats.get("stacked", 0) + 1), f"[E{m.group(1)}]")[1], text)
        text = re.sub(r"(\[E\d+\])(?:\s*\[E\d+\])+", lambda m: (stats.__setitem__("stacked", stats.get("stacked", 0) + 1), m.group(1))[1], text)
        units = split_units(text)
        checks = []
        for u in units:
            for m in re.finditer(r"\[E(\d+)\]", u):
                cid = f"E{m.group(1)}"
                card = led.cards.get(cid)
                if not card:
                    checks.append((u, m.group(0), None, "unknown"))
                    continue
                missing = unsupported(re.sub(r"\[E\d+\]", "", u), self._card_ev(card), self._task_ev)
                checks.append((u, m.group(0), card, "number" if missing else "ok"))
        if entail:
            pending = [c for c in checks if c[3] == "ok"]
            for i in range(0, len(pending), 20):
                batch = pending[i:i + 20]
                pairs = "\n".join(f"{j}. SENTENCE: {re.sub(r'\[E\d+\]', '', u).strip()[:400]}\n   QUOTE: {c.quote[:450]}"
                                  for j, (u, _, c, _) in enumerate(batch))
                out = await self.llm.text("fast", "You check citations. Answer in the requested format only.",
                                          P.ENTAIL.format(pairs=pairs), step="verify", max_tokens=400)
                for j, verdict in re.findall(r"(\d+)\s*:\s*(yes|no)", out.lower()):
                    if int(j) < len(batch) and verdict == "no":
                        u, tag, c, _ = batch[int(j)]
                        checks[checks.index(batch[int(j)])] = (u, tag, c, "entail")
        for u, tag, card, verdict in checks:
            if verdict == "ok":
                stats["kept"] = stats.get("kept", 0) + 1
                continue
            stats[f"removed_{verdict}"] = stats.get(f"removed_{verdict}", 0) + 1
            if verdict == "number":
                plain = re.sub(r"\[E\d+\]", "", u)
                fix = next((c for c in led.cards.values() if not unsupported(plain, self._card_ev(c), self._task_ev)), None)
                if fix:                               # another card has all its numbers: re-point
                    text = text.replace(u, u.replace(tag, f"[{fix.id}]", 1), 1)
                    stats["repaired"] = stats.get("repaired", 0) + 1
                    continue
                nums = checked_numbers(plain, self._task_ev)
                found = [t for t, f in nums if any(self._card_ev(c).supports(f) for c in led.cards.values())]
                if nums and not found and not u.lstrip().startswith("|"):
                    text = text.replace(u, "", 1)     # no card has any of its numbers: reliable signal
                    stats["dropped_sentences"] = stats.get("dropped_sentences", 0) + 1
                    continue
            if u.lstrip().startswith("|"):           # table row: keep the row, mark the cell
                text = text.replace(u, u.replace(tag, " (nicht belegt)" if self._de else " (no evidence)", 1), 1)
            else:                                     # v2: the citation goes, the sentence stays
                text = text.replace(u, u.replace(tag, "", 1), 1)
        return re.sub(r"(?<=\S)  +", " ", re.sub(r" +([.,;:])", r"\1", text))

    def _resolve(self, sections: list[tuple[str, str]], led: Ledger, plan: dict) -> str:
        url_no: dict[str, int] = {}          # one number per source URL, in order of first citation
        key = lambda c: c.url or f"title:{c.title}"   # sources without public URL: one entry per title
        for _, body in sections:
            for m in re.finditer(r"\[E(\d+)\]", body):
                card = led.cards.get(f"E{m.group(1)}")
                if card:
                    url_no.setdefault(key(card), len(url_no) + 1)

        def num(m):
            card = led.cards.get(f"E{m.group(1)}")
            return f"[{url_no[key(card)]}]" if card else ""

        out = [f"# {plan['title']}"]
        for title, body in sections:
            body = re.sub(r"\[E(\d+)\]", num, body)
            body = re.sub(r"(\[\d+\])(\s*\1)+", r"\1", body)   # same source twice in a row
            out.append(f"## {title}\n\n{body.strip()}")
        de = plan["language"] == "de"
        titles = {key(c): c for c in led.cards.values()}
        def when(c: Card) -> str:
            stand = f"{'Stand' if de else 'dated'} {c.date}; " if c.date else ""
            return f"({stand}{'abgerufen am' if de else 'accessed'} {c.fetched_at})"

        nourl = "keine öffentliche URL" if de else "no public URL"
        lines = [f"[{n}] {titles[u].title.strip()[:160]}: {titles[u].url or nourl} {when(titles[u])}"
                 for u, n in sorted(url_no.items(), key=lambda x: x[1])]
        out.append(("## Quellen" if de else "## Sources") + "\n\n" + "\n".join(lines))
        return "\n\n".join(out) + "\n"

    # ------------------------------------------------------------------ run
    async def run(self, task: str, preset_name: str, depth: str = "standard", plan: dict | None = None,
                  requirements: str = "", own: "O.OwnSources | None" = None) -> Result:
        """task = the research question (used for planning and research); requirements = report-format
        requirements (structure, citation style) that only the writer sees; own = the user's files and knowledge bases."""
        preset, budget = self.cfg.presets[preset_name], self.cfg.budgets[depth]
        self.own = self._own_for(own, preset)
        if preset.budget:                        # per-preset budget overrides (e.g. more pages for surveys)
            budget = dataclasses.replace(budget, **preset.budget)
        if preset.engine == "focus":
            from .focus import FocusEngine
            return await FocusEngine(self).run(task, preset, budget, plan, requirements)
        started = time.time()
        if plan is None:
            await self._emit("status", "Erstelle Rechercheplan")
            plan = await self.plan(task, preset, budget, own=self.own)
        led = Ledger()
        self._task_ev = Evidence(task + " " + requirements)
        for q in plan["sub_questions"]:
            led.subqs[q["id"]] = SubQ(id=q["id"], part=q["part"], text=q["text"], intent=q["intent"])
        led.log("plan", plan=plan)
        todo = list(led.subqs.values())
        rounds_done = 0
        for rnd in range(1, budget.rounds + 1):
            before = len(led.cards)
            await asyncio.gather(*(self._research_subq(sq, plan, preset, budget, led) for sq in todo))
            rounds_done = rnd
            new = len(led.cards) - before
            led.log("round", round=rnd, new_cards=new, tokens=self.llm.usage.total)
            if rnd == budget.rounds or self.llm.usage.total > budget.max_tokens * 0.8:
                break
            if all(led.coverage(s.id) >= 2 for s in led.subqs.values()) and budget.max_follow_up == 0:
                break
            if rnd > 1 and new < 3:
                break
            todo = await self._reflect(rnd, plan, budget, led)
            if not todo:
                break

        # write: parts in parallel, then summary and closing part
        self._de = plan["language"] == "de"
        parts = await asyncio.gather(*(self._write_part(p, plan, budget, led, task, preset, requirements)
                                       for p in plan["parts"]))
        stats: dict = {}
        verified = [await self._verify(t, led, stats, preset.verify_entailment) for t in parts]
        lang, spelling = self._lang(plan)
        joined = "\n\n".join(f"## {p['title']}\n{t}" for p, t in zip(plan["parts"], verified))
        analysis_title = preset.report.get("analysis_part")
        analysis = "" if not analysis_title else await self.llm.text("think", "You are an analytical research writer.",
                                       P.ANALYSIS.format(analysis_title=analysis_title, language_name=lang, spelling=spelling,
                                                         task=task, constraints="; ".join(plan["constraints"]) or "-",
                                                         parts=joined[:70000], words=max(400, budget.words_per_part)),
                                       step="analysis", max_tokens=10000)
        if analysis_title:
            analysis = await self._verify(analysis, led, stats, preset.verify_entailment)
        joined_all = joined + (f"\n\n## {analysis_title}\n{analysis}" if analysis_title else "")
        summary = await self.llm.text("think", "You are a precise research writer.",
                                      P.SUMMARY.format(language_name=lang, spelling=spelling, task=task, parts=joined_all[:70000]),
                                      step="summary", max_tokens=8000)
        summary = await self._verify(summary, led, stats, preset.verify_entailment)
        body = (summary + "\n" + joined_all).lower()
        missing = [m for m in plan["must_name"] if m.lower() not in body]
        closing_title = preset.report.get("closing_part") or ("Offene Punkte" if plan["language"] == "de" else "Open questions")
        gaps = "\n".join(f"- {led.subqs[k].text}: {v}" for k, v in led.gaps.items() if k in led.subqs) or "-"
        closing = await self.llm.text("fast", "You are a precise research writer.",
                                      P.CLOSING.format(closing_title=closing_title, language_name=lang, spelling=spelling,
                                                       task=task, gaps=gaps, missing=", ".join(missing) or "-"),
                                      step="closing", max_tokens=1500)
        closing = re.sub(r"\[E\d+\]", "", closing)
        summary_title = "Zusammenfassung" if plan["language"] == "de" else "Summary"
        sections = ([(summary_title, summary)] + [(p["title"], t) for p, t in zip(plan["parts"], verified)]
                    + ([(analysis_title, analysis)] if analysis_title else []) + [(closing_title, closing)])
        report = self._resolve(sections, led, plan)
        cantonal = {}
        for c in led.cards.values():
            for m in re.finditer(r"\b(SG|SGS|LS|BSG)[ -](?:Nummer:\*\*\s*)?(\d{1,3}(?:\.\d+)+)", c.quote + " " + c.title):
                cantonal.setdefault(m.group(2), m.group(1))
            if m := re.search(r"gesetzessammlung\.bs\.ch/(?:data|app/de/texts_of_law)/(\d{1,3}(?:\.\d+)+)", c.url):
                cantonal.setdefault(m.group(1), "SG")
        report = fix_sr_labels(report, cantonal)
        if plan["language"] == "de":
            report = report.replace("ß", "ss")
        stats.update({"rounds": rounds_done, "subqs": len(led.subqs), "cards": len(led.cards), "fetched": len(led.fetched),
                      "queries": len(led.queries), "must_name_missing": missing, "runtime_s": round(time.time() - started, 1)})
        u = self.llm.usage
        await self._emit("status", "Bericht fertig")
        return Result(report=report, plan=plan, ledger=led.to_dict(),
                      usage={"calls": u.calls, "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                             "by_step": u.by_step}, stats=stats)

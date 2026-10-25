"""Chat turns before a research: clarifying questions, the research plan and plan edits (shared by all engines)."""

import json
import re

from . import own as O
from . import prompts as P
from .config import Budget, Preset
from .context import today
from .llm import LLM
from .tools import Tools, domain

INTENTS = ("primary", "data", "secondary")


def own_for(own: "O.OwnSources | None", preset: Preset) -> "O.OwnSources | None":
    """The user's own sources as the preset wants them: ignored for own_sources: off, web-less for `only`."""
    if own is None or preset.own_sources == "off" or not (own.files or own.collections):
        return None
    own.mode = "only" if preset.own_sources == "only" else "prefer"
    return own


class ChatTurns:
    """The LLM calls of the chat before a research starts; engines use `plan` when they get no approved plan."""

    def __init__(self, llm: LLM, tools: Tools):
        """Use `llm` for the turns and `tools` for the discovery search of `plan`."""
        self.llm, self.tools = llm, tools

    async def clarify(
        self, task: str, own: "O.OwnSources | None" = None, presets: dict[str, str] | None = None
    ) -> dict:
        """At most 2 clarifying questions; none for detailed tasks (they already state their scope).

        With `presets` (name -> description) the answer also has "preset": the profile that fits the task best, or
        None if the model named none of them. Detailed tasks then still get a suggestion, but no questions.
        """
        detailed = len(task) > 600
        if detailed and not presets:
            return {"questions": [], "assumptions": []}
        if own and (own.files or own.collections):
            task += (
                "\n\n(The user has ALREADY ATTACHED these own sources: "
                + "; ".join(
                    [f'file "{f.name}"' for f in own.files] + [f'knowledge base "{c.name}"' for c in own.collections]
                )
                + ". Phrases like 'my document' or 'the attached file' mean them. Never ask the user to upload or "
                "provide them again.)"
            )
        prompt = P.CLARIFY.format(task=task)
        if presets:
            prompt += P.CLARIFY_PRESET.format(presets="\n".join(f"- {n}: {d}" for n, d in presets.items()))
        data = (
            await self.llm.json(
                "fast",
                "You decide whether a research task needs clarification. Output JSON only.",
                prompt,
                step="clarify",
                default={},
            )
            or {}
        )
        if not isinstance(data, dict):
            data = {}
        out = {
            "questions": [] if detailed else [str(q) for q in data.get("questions") or []][:2],
            "assumptions": [] if detailed else [str(a) for a in data.get("assumptions") or []][:5],
        }
        if presets:
            out["preset"] = data.get("preset") if data.get("preset") in presets else None
        return out

    async def edit_plan(self, plan: dict, instruction: str, task: str, preset: Preset, budget: Budget) -> dict:
        """Change a plan as the user asked; keep the old plan if the answer is not a valid plan."""
        raw = await self.llm.json(
            "fast",
            "You edit research plans. Output JSON only.",
            P.EDIT_PLAN.format(
                plan=json.dumps(plan, ensure_ascii=False), instruction=instruction, max_subq=budget.max_subq
            ),
            step="edit_plan",
            default=None,
        )
        return self.validate_plan(raw, task, preset, budget) if isinstance(raw, dict) else plan

    async def plan(self, task: str, preset: Preset, budget: Budget, own: "O.OwnSources | None" = None) -> dict:
        """Plan a research: parts, sub-questions with intents, must-name items, language."""
        hint = ""
        if preset.report.get("parts_hint"):
            parts = ", ".join(preset.report["parts_hint"])
            hint = f"Typical parts for this kind of research: {parts} (adapt to the task)."
        own = own_for(own, preset)
        if own:
            hint += (
                " The user supplied own sources: "
                + "; ".join(
                    [f'file "{f.name}"' + (f" (begins: {f.text[:300]!r})" if f.text else "") for f in own.files]
                    + [f'knowledge base "{c.name}"' for c in own.collections]
                )
                + (
                    ". Answer ONLY from these; plan sub-questions the documents can answer."
                    if own.mode == "only"
                    else ". Plan sub-questions so that these sources are used wherever they are relevant."
                )
            )
        hits = (
            []
            if own and own.mode == "only"
            else await self.tools.search(task[:200], "de" if preset.language == "de" else "en", num=8)
        )
        discovery = "\n".join(f"- {h['title']} ({domain(h['url'])}): {h['snippet'][:180]}" for h in hits) or "-"
        raw = (
            await self.llm.json(
                "think",
                "You are a careful research planner. Output JSON only.",
                P.PLAN.format(
                    today=today(),
                    task=task,
                    discovery=discovery,
                    parts_hint=hint,
                    max_subq=budget.max_subq,
                    min_parts=budget.parts[0],
                    max_parts=budget.parts[1],
                ),
                step="plan",
                default={},
            )
            or {}
        )
        return self.validate_plan(raw if isinstance(raw, dict) else {}, task, preset, budget)

    def validate_plan(self, raw: dict, task: str, preset: Preset, budget: Budget) -> dict:
        """Turn the model's plan JSON into a valid plan: numbered parts and sub-questions, known intents, limits."""
        raw_parts = [p for p in raw.get("parts") or [] if isinstance(p, dict) and p.get("title")]
        raw_parts = [
            p
            for p in raw_parts
            if not re.search(r"summary|zusammenfassung|fazit|limitation|offene punkte|grenzen", p["title"], re.I)
        ]
        parts = raw_parts or [{"title": "Findings", "goal": task}]
        raw_ids = {}  # the model's part ids -> ours, from the parts that were kept
        for i, p in enumerate(parts, 1):
            if p in raw_parts:
                raw_ids[str(p.get("id"))] = f"P{i}"
            p["id"] = f"P{i}"
            p.setdefault("goal", p["title"])
        part_ids = {p["id"] for p in parts}
        subqs = []
        for q in raw.get("sub_questions") or []:
            if not isinstance(q, dict) or not q.get("text"):
                continue
            part = raw_ids.get(str(q.get("part")), q.get("part"))
            subqs.append(
                {
                    "id": f"Q{len(subqs) + 1}",
                    "part": part if part in part_ids else parts[0]["id"],
                    "text": str(q["text"]).strip(),
                    "intent": q.get("intent") if q.get("intent") in INTENTS else "secondary",
                }
            )
        subqs = subqs[: budget.max_subq] or [
            {"id": "Q1", "part": parts[0]["id"], "text": task[:300], "intent": "secondary"}
        ]
        lang = (
            preset.language
            if preset.language != "auto"
            else (raw.get("language") if raw.get("language") in ("de", "en") else "en")
        )
        return {
            "title": str(raw.get("title") or task[:80]),
            "task_core": str(raw.get("task_core") or task[:400]),
            "language": lang,
            "parts": parts,
            "sub_questions": subqs,
            "must_name": [str(x) for x in raw.get("must_name") or [] if str(x).strip()][:25],
            "constraints": [str(x) for x in raw.get("constraints") or [] if str(x).strip()][:15],
        }

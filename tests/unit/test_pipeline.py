"""Pipeline engine: plan validation, verification, URL bookkeeping and one end-to-end run with fakes."""

import asyncio
from types import SimpleNamespace

from bebbi_deep_research.engine import ResearchAgent, load_config
from bebbi_deep_research.engine.ledger import Ledger, SubQ
from bebbi_deep_research.engine.pipeline import PipelineEngine
from bebbi_deep_research.engine.text import distinctive

from .test_own_engine import ROOT, FakeTools, make_own

PAGE = (
    "Die Gebühr für das Gesuch beträgt 250 Franken pro Antrag und wird bei der Einreichung fällig. " * 4
    + "\nDer Kanton hat 2022 die Verordnung angepasst und die Frist auf 30 Tage festgelegt."
)


class ScriptedLLM:
    """Answers by step: no network, deterministic. `json_out` per step, `text_out` per step."""

    def __init__(self, json_out=None, text_out=None):
        self.json_out, self.text_out, self.steps = json_out or {}, text_out or {}, []
        self.usage = SimpleNamespace(total=0, calls=0, prompt_tokens=0, completion_tokens=0, by_step={})

    async def embed(self, texts):
        return [[1.0] for _ in texts]

    async def json(self, role, system, user, step="", default=None):
        self.steps.append(step)
        return self.json_out.get(step, default)

    async def text(self, role, system, user, step="", max_tokens=None):
        self.steps.append(step)
        v = self.text_out.get(step, "")
        return v(user) if callable(v) else v

    async def chat(self, *a, **k):
        raise AssertionError("no chat in the pipeline engine")


def agent_for(llm, tools=None):
    agent = ResearchAgent(load_config(ROOT / "config/presets.yaml"), llm, tools or FakeTools())
    agent.ctx.mcp.client = lambda server, p: None
    return agent


def add_cards(agent, *quotes):
    led = Ledger()
    for i, q in enumerate(quotes, 1):
        led.add_card("Q1", f"https://ex.ch/{i}", f"Quelle {i}", q, q, "web")
    return led


# ---------------------------------------------------------------------------------------- plan
def test_validate_plan_maps_part_ids_through_the_kept_parts_and_skips_non_dicts():
    agent = agent_for(ScriptedLLM())
    preset, budget = agent.cfg.presets["web_survey"], agent.cfg.budgets["quick"]
    raw = {
        "parts": [
            {"id": "a", "title": "Zusammenfassung"},
            "kaputt",
            {"id": "b", "title": "Erstens"},
            {"id": "c", "title": "Zweitens"},
        ],
        "sub_questions": [{"part": "b", "text": "x?"}, {"part": "c", "text": "y?"}, {"part": "a", "text": "z?"}],
    }
    plan = agent.chat.validate_plan(raw, "Aufgabe", preset, budget)
    assert [p["title"] for p in plan["parts"]] == ["Erstens", "Zweitens"]
    assert [(q["text"], q["part"]) for q in plan["sub_questions"]] == [("x?", "P1"), ("y?", "P2"), ("z?", "P1")]


async def test_non_dict_json_answers_do_not_crash_clarify_plan_and_reflect():
    llm = ScriptedLLM(json_out={"clarify": ["a"], "plan": ["b"], "reflect": ["c"]})
    agent = agent_for(llm)
    preset, budget = agent.cfg.presets["web_survey"], agent.cfg.budgets["quick"]
    assert await agent.clarify("kurz") == {"questions": [], "assumptions": []}
    plan = await agent.plan("Aufgabe", preset, budget)
    assert plan["sub_questions"][0]["text"] == "Aufgabe"
    led = Ledger()
    led.subqs["Q1"] = SubQ("Q1", "P1", "Frage?")
    assert await PipelineEngine(agent.ctx)._reflect(1, plan, budget, led) == []


# ------------------------------------------------------------------------------------- verify
async def test_verify_applies_every_check_of_a_sentence_to_its_current_text():
    agent = agent_for(ScriptedLLM())
    led = add_cards(agent, "Es waren 4500 Franken im Jahr.", "Es waren 5555 Einwohner im Dorf.")
    out = await PipelineEngine(agent.ctx)._verify(
        "Es waren 7777 Franken [E1] und 9999 Einwohner [E2].", led, {}, entail=False
    )
    assert "[E1]" not in out and "[E2]" not in out
    # a later check on the same sentence still applies after an earlier one rewrote it (E1 lacks 5555, E2 lacks 4500)
    out = await PipelineEngine(agent.ctx)._verify(
        "Es waren 4500 Franken [E1] und 5555 Einwohner [E2] im Dorf.", led, {}, entail=False
    )
    assert "[E1]" not in out and "[E2]" not in out and out.startswith("Es waren 4500 Franken und 5555 Einwohner")
    out = await PipelineEngine(agent.ctx)._verify("Es waren 4500 Franken [E1] und mehr [E9].", led, {}, entail=False)
    assert out == "Es waren 4500 Franken [E1] und mehr."


async def test_verify_keeps_a_citation_when_only_a_bare_year_is_missing():
    agent = agent_for(ScriptedLLM())
    led = add_cards(
        agent, "Die Verordnung wurde angepasst und die Frist beträgt 30 Tage.", "Im Jahr 2022 stimmte das Volk ab."
    )
    stats = {}
    out = await PipelineEngine(agent.ctx)._verify(
        "Die Frist von 30 Tage wurde 2022 festgelegt [E1].", led, stats, entail=False
    )
    assert "[E1]" in out and "[E2]" not in out and stats.get("years_only") == 1
    # a distinctive number still re-points
    out = await PipelineEngine(agent.ctx)._verify(
        "Die Gebühr von 1250 Franken fiel 2022 an [E1].",
        add_cards(agent, "Frist 30 Tage", "Gebühr 1250 Franken seit 2022"),
        {},
        entail=False,
    )
    assert "[E2]" in out


def test_distinctive_ignores_bare_years():
    assert not distinctive("2022") and not distinctive("1999")
    assert distinctive("1250") and distinctive("12.5") and distinctive("2022-05-01") and distinctive("23.5544")


# ------------------------------------------------------------------------------- bookkeeping
def test_coverage_counts_sources_without_url_by_title():
    led = Ledger()
    led.add_card("Q1", "", "Bericht A", "Zitat eins zwei drei vier fünf", "f", "mcp:x")
    led.add_card("Q1", "", "Bericht B", "Zitat sechs sieben acht neun zehn", "f", "mcp:x")
    led.add_card("Q1", "https://a.ch", "A", "Zitat elf zwölf dreizehn vierzehn", "f", "web")
    assert led.coverage("Q1") == 3


async def test_web_stores_the_url_without_fragment_and_fetches_it_once():
    tools = FakeTools()

    async def search(q, lang="de", num=8):
        return [{"title": "T", "url": "https://example.ch/x#abschnitt", "snippet": "s", "date": ""}]

    tools.search = search
    llm = ScriptedLLM(text_out={"queries": "de | eine Frage"}, json_out={"extract": {"findings": []}})
    agent = agent_for(llm, tools)
    cfg = agent.cfg
    led = Ledger()
    sq = SubQ("Q1", "P1", "Frage?")
    plan = {"task_core": "k", "language": "de"}
    engine = PipelineEngine(agent.ctx)
    # two sub-questions searching at the same time must not fetch the same page twice
    await asyncio.gather(
        *(
            engine._web(s, plan, cfg.presets["web_survey"], cfg.budgets["quick"], led)
            for s in (sq, SubQ("Q2", "P1", "Andere?"))
        )
    )
    assert tools.fetched == ["https://example.ch/x"] and led.fetched == {"https://example.ch/x"}


async def test_own_knowledge_hit_without_text_does_not_raise():
    async def retrieve(query, collection):
        return [
            {"name": "a.txt", "file_id": "1"},
            {"text": "Homeoffice an zwei Tagen.", "name": "a.txt", "file_id": "1"},
        ]

    from bebbi_deep_research.engine.own import evidence_items

    items = await evidence_items(make_own(files=[], retrieve=retrieve), ScriptedLLM(), ["Homeoffice"])
    assert [i["text"] for i in items] == ["Homeoffice an zwei Tagen."]


async def test_a_failing_tools_list_drops_the_cached_mcp_client():
    class Dead:
        def tools(self):
            raise RuntimeError("session gone")

    agent = agent_for(ScriptedLLM())
    del agent.ctx.mcp.client  # the real pool again
    agent.ctx.mcp.clients["grossrat"] = Dead()
    led = Ledger()
    n = await PipelineEngine(agent.ctx)._mcp(
        SubQ("Q1", "P1", "F?"), "grossrat", {"task_core": "k", "language": "de"}, agent.cfg.presets["grossrat"], led
    )
    assert n == 0 and "grossrat" not in agent.ctx.mcp.clients and "session gone" in agent.ctx.mcp.errors["grossrat"]
    assert any(e["kind"] == "mcp_unavailable" for e in led.trail)


# ---------------------------------------------------------------------------------- end to end
async def test_pipeline_run_end_to_end_with_fakes():
    plan = {
        "title": "Gebühren",
        "task_core": "Gebühr für das Gesuch",
        "language": "de",
        "parts": [{"id": "P1", "title": "Gebühren", "goal": "g"}],
        "sub_questions": [{"id": "Q1", "part": "P1", "text": "Wie hoch ist die Gebühr?", "intent": "secondary"}],
        "must_name": [],
        "constraints": [],
    }
    quote = "Die Gebühr für das Gesuch beträgt 250 Franken pro Antrag"
    tools = FakeTools()

    async def search(q, lang="de", num=8):
        return [{"title": "Gebührenordnung", "url": "https://example.ch/gebuehren", "snippet": "s", "date": ""}]

    async def fetch(url):
        tools.fetched.append(url)
        return "Gebührenordnung", PAGE

    tools.search, tools.fetch = search, fetch
    llm = ScriptedLLM(
        json_out={"extract": {"findings": [{"quote": quote, "finding": "Die Gebühr beträgt 250 Franken."}]}},
        text_out={
            "queries": "de | gebühr gesuch",
            "write": "Die Gebühr beträgt 250 Franken [E1]. Erfunden sind 9999 Franken [E1].",
            "verify": "0: yes",
            "summary": "Kurz: 250 Franken [E1].",
            "closing": "Nichts offen.",
            "analysis": "",
        },
    )
    agent = agent_for(llm, tools)
    res = await agent.run("Gebühr?", "web_survey", "quick", plan=plan)
    assert tools.fetched == ["https://example.ch/gebuehren"]
    assert "250 Franken [1]" in res.report and "9999" not in res.report  # the invented number is dropped
    assert "https://example.ch/gebuehren" in res.report.split("## Quellen")[1]
    assert res.stats["cards"] == 1 and [c["id"] for c in res.ledger["cards"]] == ["E1"]

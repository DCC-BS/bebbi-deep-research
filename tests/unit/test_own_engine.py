"""Own sources in the engine (research_agent/own.py): tools with a fake retrieval, source kinds, only/prefer/off, both engines."""

from pathlib import Path
from types import SimpleNamespace

from bebbi_deep_research.engine import ResearchAgent, load_config
from bebbi_deep_research.engine.ledger import Ledger, SubQ
from bebbi_deep_research.engine.own import (
    OwnCollection,
    OwnFile,
    OwnSources,
    PassageIndex,
    evidence_items,
    knowledge_search,
    prompt_block,
    read_upload,
    tool_specs,
)
from bebbi_deep_research.engine.sources import Registry

from .scripted_llm import ScriptedLLM, say

ROOT = Path(__file__).resolve().parents[2]
VOCAB = ["homeoffice", "frist", "gebühr", "reise", "auftragswert"]
PLAN = {
    "title": "Plan",
    "task_core": "Frist und Homeoffice",
    "language": "de",
    "parts": [{"id": "P1", "title": "Teil", "goal": "g"}],
    "sub_questions": [{"id": "Q1", "part": "P1", "text": "Welche Frist gilt?", "intent": "primary"}],
    "must_name": [],
    "constraints": [],
}
BIG = (
    ("Allgemeine Hinweise zur Verwaltung. " * 60 + "\n") * 4
    + "Gesuche sind mindestens 28 Tage vor Beginn einzureichen. Die Frist beträgt 28 Tage.\n"
    + ("Weitere allgemeine Hinweise. " * 80 + "\n") * 6
)


async def keyword_embed(texts):
    """Keyword-count embeddings, so 'similar' means 'shares a keyword'."""
    return [[t.lower().count(w) for w in VOCAB] for t in texts]


def focus_llm(*turns):
    return ScriptedLLM(turns, embed=keyword_embed)


class FakeLLM:
    """Scripted JSON and text answers; keyword-count embeddings."""

    def __init__(self, turns=(), json_out=None):
        self.turns, self.calls, self.json_out, self.prompts = list(turns), [], json_out or {}, []
        self.usage = SimpleNamespace(total=0, calls=0, prompt_tokens=0, completion_tokens=0, by_step={})

    async def embed(self, texts):
        return [[t.lower().count(w) for w in VOCAB] for t in texts]

    async def json(self, role, system, user, step="", default=None):
        self.prompts.append(user)
        return self.json_out.get(step, default)

    async def text(self, *a, **k):
        return "keine"


class FakeTools:
    def __init__(self):
        self.searched, self.fetched = [], []

    async def search(self, q, lang="de", num=8):
        self.searched.append(q)
        return [{"title": "Web", "url": "https://example.ch/x", "snippet": "s", "date": ""}]

    async def fetch_meta(self, url):
        self.fetched.append(url)
        return "Seite", "x" * 500, ""

    async def fetch(self, url):
        self.fetched.append(url)
        return "Seite", "x" * 500


def make_agent(llm, preset=None, own_sources=None):
    cfg = load_config(ROOT / "config/presets.yaml")
    if own_sources:
        cfg.presets[preset].own_sources = own_sources
    agent = ResearchAgent(cfg, llm, FakeTools())
    agent._mcp_client = lambda server, p: None  # no network: the MCP servers are "unreachable"
    return agent


def make_own(retrieve=None, files=None, collections=None):
    async def default(query, collection):
        return [
            {
                "text": "Homeoffice ist an zwei Tagen pro Woche möglich.",
                "name": "Weisung-Homeoffice.txt",
                "file_id": "w1",
                "url": "https://owui/api/v1/files/w1/content",
                "date": "2026-10-01",
                "score": 0.7,
            },
            {
                "text": "Kernzeit ist Dienstag und Donnerstag.",
                "name": "Weisung-Homeoffice.txt",
                "file_id": "w1",
                "url": "https://owui/api/v1/files/w1/content",
                "date": "2026-10-01",
                "score": 0.6,
            },
        ]

    return OwnSources(
        files=files
        if files is not None
        else [OwnFile("f1", "merkblatt.pdf", BIG, "2026-10-09", "https://owui/api/v1/files/f1/content")],
        collections=collections if collections is not None else [OwnCollection("kb1", "Personal")],
        retrieve=retrieve or default,
    )


# ------------------------------------------------------------------------------------------------ tools
def test_tools_exist_only_for_what_is_there():
    assert [t["function"]["name"] for t in tool_specs(make_own())] == ["read_upload", "knowledge_search"]
    assert [t["function"]["name"] for t in tool_specs(make_own(collections=[]))] == ["read_upload"]
    assert [
        t["function"]["name"] for t in tool_specs(OwnSources(collections=[OwnCollection("k", "K")]))
    ] == []  # no retrieval: no tool
    assert "Personal" in tool_specs(make_own())[1]["function"]["description"]


def test_registry_key_merges_sources_without_url():
    reg = Registry()
    a = reg.add("", "merkblatt.pdf", "upload", "erster Teil", key="upload:f1")
    b = reg.add("", "merkblatt.pdf", "upload", "zweiter Teil", key="upload:f1")
    c = reg.add("", "merkblatt.pdf", "upload", "anderer Text", key="upload:f2")
    assert a is b and a.id == "S1" and "erster Teil" in a.text and "zweiter Teil" in a.text and c.id == "S2"
    assert (
        reg.add("https://a.ch/x#frag", "A", "web").id == reg.add("https://a.ch/x", "A", "web").id
    )  # URL keys behave as before


async def test_passage_index_picks_the_matching_chunks_and_embeds_once():
    llm = FakeLLM()
    embedded = []
    real = llm.embed

    async def counting(texts):
        embedded.append(len(texts))
        return await real(texts)

    llm.embed = counting
    idx = PassageIndex(BIG)
    text, score = await idx.top(llm, ["Frist"], budget_chars=1500)
    assert "28 Tage" in text and len(text) < 1800 and score > 0.5
    await idx.top(llm, ["Homeoffice"], budget_chars=1500)
    assert embedded[0] > 5 and embedded.count(embedded[0]) == 1  # the chunks were embedded once, queries (1 text) twice
    short = PassageIndex("kurzer Text")
    assert (await short.top(llm, ["x"])) == ("kurzer Text", 1.0)


async def test_read_upload_registers_the_file_as_source_with_its_url_and_date():
    own, reg = make_own(), Registry()
    out, added = await read_upload(own, {"file": "U1", "query": "Frist"}, reg, FakeLLM(), "Frage")
    src = added[0]
    assert (
        src.kind == "upload"
        and src.title == "merkblatt.pdf"
        and src.url.endswith("/f1/content")
        and src.date == "2026-10-09"
    )
    assert out.startswith("[S1] merkblatt.pdf (2026-10-09)") and "28 Tage" in out
    out2, added2 = await read_upload(own, {"file": "merkblatt.pdf", "query": "Gebühr"}, reg, FakeLLM(), "Frage")
    assert added2[0] is src  # same file: same source and ID
    err, none = await read_upload(own, {"file": "U9"}, reg, FakeLLM(), "Frage")
    assert err.startswith("error: unknown file") and none == []


async def test_knowledge_search_groups_passages_per_document():
    own, reg = make_own(), Registry()
    out, added = await knowledge_search(own, {"query": "Homeoffice"}, reg)
    assert len(added) == 1 and added[0].kind == "knowledge:Personal" and added[0].title == "Weisung-Homeoffice.txt"
    assert added[0].date == "2026-10-01" and "Kernzeit" in added[0].text and "zwei Tagen" in out
    assert out.startswith("[S1] Weisung-Homeoffice.txt (Wissensbasis Personal, 2026-10-01)")


async def test_knowledge_search_uses_only_the_collections_of_the_job():
    asked = []

    async def retrieve(query, collection):
        asked.append(collection.id)
        return []

    own = make_own(retrieve=retrieve, collections=[OwnCollection("kb1", "A"), OwnCollection("kb2", "B")])
    # a model that invents a "collection" argument cannot widen the search: the tool has only `query`
    out, added = await knowledge_search(
        own, {"query": "x", "collection": "kb-secret", "collection_names": ["kb-secret"]}, Registry()
    )
    assert sorted(asked) == ["kb1", "kb2"] and added == [] and "no results" in out
    assert set(tool_specs(own)[1]["function"]["parameters"]["properties"]) == {"query"}


async def test_knowledge_search_reports_unreachable_bases_without_failing():
    async def retrieve(query, collection):
        if collection.id == "kb2":
            raise RuntimeError("down")
        return [{"text": "Text", "name": "a.txt", "file_id": "a"}]

    own = make_own(retrieve=retrieve, collections=[OwnCollection("kb1", "A"), OwnCollection("kb2", "B")])
    out, added = await knowledge_search(own, {"query": "x"}, Registry())
    assert len(added) == 1 and 'knowledge base "B" not reachable' in out

    async def broken(query, collection):
        raise RuntimeError("x")

    out, added = await knowledge_search(make_own(retrieve=broken), {"query": "x"}, Registry())
    assert added == [] and "not reachable" in out


def test_prompt_block_inlines_small_files_and_lists_large_ones():
    reg = Registry()
    own = make_own(files=[OwnFile("s", "klein.txt", "Gebühr CHF 120.", "2026-10-09"), OwnFile("g", "gross.pdf", BIG)])
    system, first = prompt_block(own, reg)
    assert (
        'U1 "klein.txt": complete text below as [S1]' in system
        and 'U2 "gross.pdf"' in system
        and "read_upload" in system
    )
    assert "[S1] klein.txt (2026-10-09)\nGebühr CHF 120." in first and BIG[:40] not in first
    assert list(reg.sources) == ["S1"] and reg.sources["S1"].kind == "upload"
    assert "FIRST" in system and "Use ONLY" not in system  # prefer: own sources first, web for the rest
    own.mode = "only"
    assert "Use ONLY these sources" in prompt_block(own, Registry())[0]


# ------------------------------------------------------------------------------------------- focus engine
async def test_focus_only_mode_has_no_web_and_no_data_tools_and_cites_the_sources():
    llm = focus_llm(
        say(calls=[("read_upload", {"file": "U1", "query": "Frist"}), ("knowledge_search", {"query": "Homeoffice"})]),
        say(
            "# Bericht\n\n## Zusammenfassung\n\nDie Frist beträgt 28 Tage [S1]. Homeoffice ist an zwei Tagen möglich [S2].\n\n"
            "## Offene Punkte und Grenzen der Recherche\n\nKeine."
        ),
    )
    agent = make_agent(llm, "dokumente")
    res = await agent.run("Frist und Homeoffice?", "dokumente", "quick", plan=PLAN, own=make_own())
    names = llm.calls[0]["tools"]
    assert names == ["read_upload", "knowledge_search"]  # no web_search, fetch_page, MCP tool
    assert agent.tools.searched == [] and agent.tools.fetched == []
    assert [t["tool"] for t in res.ledger["trail"]] == ["read_upload", "knowledge_search"]
    assert "Use ONLY these sources" in llm.calls[0]["instructions"]
    assert "[1] merkblatt.pdf. https://owui/api/v1/files/f1/content (Stand 2026-10-09;" in res.report
    assert "[2] Weisung-Homeoffice.txt. https://owui/api/v1/files/w1/content (Stand 2026-10-01;" in res.report
    kinds = {s["title"]: s["kind"] for s in res.ledger["sources"]}
    assert kinds == {"merkblatt.pdf": "upload", "Weisung-Homeoffice.txt": "knowledge:Personal"}
    assert res.stats["cited_mcp_sources"] == 0


async def test_focus_prefer_mode_keeps_web_tools_and_adds_the_own_tools():
    llm = focus_llm(say("# B\n\n## Zusammenfassung\n\nDie Frist beträgt 28 Tage [S1]."))
    agent = make_agent(llm)
    own = make_own(files=[OwnFile("f1", "merkblatt.pdf", "Die Frist beträgt 28 Tage.", "2026-10-09")], collections=[])
    res = await agent.run("Frage", "verwaltung_web", "quick", plan=PLAN, own=own)
    assert llm.calls[0]["tools"] == ["web_search", "fetch_page", "read_upload"]
    assert "Die Frist beträgt 28 Tage." in llm.calls[0]["user"]  # small file: in the first message, no tool call
    assert "[1] merkblatt.pdf." in res.report and res.ledger["trail"] == []


async def test_focus_off_mode_ignores_attached_sources():
    llm = focus_llm(say("# B\n\n## Zusammenfassung\n\nText."))
    agent = make_agent(llm, "verwaltung_web", own_sources="off")
    await agent.run("Frage", "verwaltung_web", "quick", plan=PLAN, own=make_own())
    assert llm.calls[0]["tools"] == ["web_search", "fetch_page"] and agent.own is None
    assert "OWN SOURCES" not in llm.calls[0]["instructions"]


async def test_focus_without_own_sources_is_unchanged():
    llm = focus_llm(say("# B\n\n## Zusammenfassung\n\nText."))
    agent = make_agent(llm)
    await agent.run("Frage", "verwaltung_web", "quick", plan=PLAN)
    assert llm.calls[0]["tools"] == ["web_search", "fetch_page"] and "OWN SOURCES" not in llm.calls[0]["instructions"]


async def test_number_check_still_applies_to_own_sources():
    """A figure the document does not contain is not passed off as sourced: the existing citation check handles uploads too."""
    llm = focus_llm(say("# B\n\n## Zusammenfassung\n\nDie Frist beträgt 99 Tage [S1]."))
    agent = make_agent(llm)
    res = await agent.run(
        "Frage",
        "verwaltung_web",
        "quick",
        plan=PLAN,
        own=make_own(files=[OwnFile("f1", "merkblatt.pdf", "Die Frist beträgt 28 Tage.")], collections=[]),
    )
    assert "99" not in res.report and res.stats.get("dropped_sentences") == 1


# --------------------------------------------------------------------------------------------- planning
async def test_plan_mentions_own_sources_and_only_mode_skips_the_web_discovery():
    llm = FakeLLM(json_out={"plan": {}})
    agent = make_agent(llm, "dokumente")
    await agent.plan("Frage", agent.cfg.presets["dokumente"], agent.cfg.budgets["quick"], own=make_own())
    assert (
        'file "merkblatt.pdf"' in llm.prompts[0]
        and 'knowledge base "Personal"' in llm.prompts[0]
        and "Answer ONLY" in llm.prompts[0]
    )
    assert agent.tools.searched == []
    llm2 = FakeLLM(json_out={"plan": {}})
    agent2 = make_agent(llm2)
    await agent2.plan("Frage", agent2.cfg.presets["verwaltung_web"], agent2.cfg.budgets["quick"], own=make_own())
    assert len(agent2.tools.searched) == 1 and "wherever they are relevant" in llm2.prompts[0]


async def test_clarify_is_told_about_the_attached_sources():
    llm = FakeLLM(json_out={"clarify": {"questions": [], "assumptions": []}})
    agent = make_agent(llm)
    await agent.clarify("Was steht in meinem Merkblatt?", own=make_own())
    assert "ALREADY ATTACHED" in llm.prompts[0] and "merkblatt.pdf" in llm.prompts[0] and "Never ask" in llm.prompts[0]


# ----------------------------------------------------------------------------------- pipeline engine
async def test_pipeline_extracts_evidence_cards_from_uploads_and_knowledge():
    findings = {"findings": [{"quote": "Die Frist beträgt 28 Tage.", "finding": "Frist 28 Tage"}]}
    llm = FakeLLM(json_out={"extract": findings})
    agent = make_agent(llm)
    agent.own = make_own(
        files=[
            OwnFile(
                "f1", "merkblatt.pdf", "Allgemein. Die Frist beträgt 28 Tage. Ende.", "2026-10-09", "https://owui/f1"
            )
        ]
    )
    led = Ledger()
    sq = SubQ("Q1", "P1", "Welche Frist gilt?")
    n = await agent._own(sq, PLAN, led)
    cards = list(led.cards.values())
    assert (
        n == 1
        and cards[0].source_kind == "upload"
        and cards[0].title == "merkblatt.pdf"
        and cards[0].url == "https://owui/f1"
    )
    # the knowledge hit: the quote must be verbatim in the passage the extraction saw
    llm.json_out = {
        "extract": {"findings": [{"quote": "Homeoffice ist an zwei Tagen pro Woche möglich.", "finding": "2 Tage"}]}
    }
    agent.own = make_own(files=[])
    await agent._own(sq, PLAN, led)
    kb_card = [c for c in led.cards.values() if c.source_kind.startswith("knowledge")][0]
    assert (
        kb_card.source_kind == "knowledge:Personal" and kb_card.title == "Weisung-Homeoffice.txt" and kb_card.date == ""
    )
    assert any(e.get("kind") == "own_sources" for e in led.trail)


async def test_pipeline_sub_question_in_only_mode_skips_web_and_mcp():
    llm = FakeLLM(json_out={"extract": {"findings": []}})
    agent = make_agent(llm, "web_survey", own_sources="only")
    agent.own = make_own()
    agent.own.mode = "only"
    cfg = agent.cfg
    led = Ledger()
    await agent._research_subq(SubQ("Q1", "P1", "Frage?"), PLAN, cfg.presets["web_survey"], cfg.budgets["quick"], led)
    assert agent.tools.searched == [] and agent.tools.fetched == []
    assert [e["kind"] for e in led.trail] == ["own_sources", "extract", "extract"]  # no "search", no "mcp_call"


async def test_evidence_items_rank_uploads_and_cap_them():
    files = [OwnFile(f"f{i}", f"doc{i}.txt", ("Reise " if i == 3 else "Nichts ") * 2000) for i in range(6)]
    items = await evidence_items(make_own(files=files, collections=[]), FakeLLM(), ["Reise"], max_files=2)
    assert len(items) == 2 and items[0]["title"] == "doc3.txt" and items[0]["kind"] == "upload"


# ----------------------------------------------------------------------------------- preset suggestion
PRESETS = {"grossrat": "Grosser Rat", "web_survey": "Web"}


async def test_clarify_suggests_one_of_the_presets():
    llm = FakeLLM(json_out={"clarify": {"questions": ["Zeitraum?"], "assumptions": [], "preset": "grossrat"}})
    agent = make_agent(llm)
    out = await agent.clarify("Vorstösse zum Wohnschutz", presets=PRESETS)
    assert out["preset"] == "grossrat" and out["questions"] == ["Zeitraum?"]
    assert "- grossrat: Grosser Rat" in llm.prompts[0] and '"preset"' in llm.prompts[0]


async def test_a_suggestion_outside_the_presets_is_none():
    llm = FakeLLM(json_out={"clarify": {"questions": [], "assumptions": [], "preset": "erfunden"}})
    assert (await make_agent(llm).clarify("Frage", presets=PRESETS))["preset"] is None


async def test_a_detailed_task_gets_a_suggestion_but_no_questions():
    llm = FakeLLM(json_out={"clarify": {"questions": ["Zeitraum?"], "assumptions": ["a"], "preset": "web_survey"}})
    out = await make_agent(llm).clarify("x" * 700, presets=PRESETS)
    assert out == {"questions": [], "assumptions": [], "preset": "web_survey"}
    # Without presets a detailed task needs no model call at all.
    llm2 = FakeLLM()
    assert await make_agent(llm2).clarify("x" * 700) == {"questions": [], "assumptions": []} and not llm2.prompts

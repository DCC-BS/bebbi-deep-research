import asyncio
from types import SimpleNamespace

import pytest

from bebbi_deep_research.engine.focus import FocusEngine, finalize
from bebbi_deep_research.engine.sources import Registry

from .scripted_llm import ScriptedLLM, say


def _reg():
    reg = Registry()
    reg.add("https://a.ch/x", "A", "web", "Es gibt 1234 Fälle")
    return reg


def test_unknown_ids_in_heading_and_open_points_do_not_crash():
    for t in [
        "## Fazit\nText [S1].\n\n## Offene Punkte\nUnklar [S9].",
        "## Fazit [S7]\nText [S1].",
        "Text [S1].\n\n## Offene Punkte\nUnklar [S9][S1].",
    ]:
        stats = {}
        out = finalize(t, _reg(), "task", True, stats)
        assert "S9" not in out and "S7" not in out and stats["unknown_ids"] >= 1


def test_leading_whitespace_kept():
    out = finalize("- Punkt [S1].\n  - Unter eins. Zwei [S1].\n    - tiefer [S1].", _reg(), "t", True, {})
    assert "\n  - Unter eins." in out and "\n    - tiefer" in out
    assert finalize("- a\n  - b [S1]", _reg(), "t", True, {}).startswith("- a\n  - b [1]")


def test_source_section_stops_at_next_heading():
    out = finalize("Text [S1].\n\n## Quellen und Methodik\nx\n\n## Offene Punkte\nfoo", _reg(), "t", True, {})
    assert "Offene Punkte" in out and "foo" in out and "Methodik" not in out
    out = finalize("Text [S1].\n\n## Quellen\n[1] foo", _reg(), "t", True, {})
    assert "foo" not in out and out.count("## Quellen") == 1


class _Tools:
    def __init__(self):
        self.fetched = []

    async def fetch_meta(self, url):
        self.fetched.append(url)
        return "T", "x" * 500, ""


def _engine():
    async def emit(*a):
        pass

    ctx = SimpleNamespace(llm=None, tools=_Tools(), cfg=None, own=None, emit=emit)
    return FocusEngine(ctx), ctx


def test_fetch_page_only_known_urls():
    eng, agent = _engine()
    reg = Registry()
    reg.add("https://seen.ch/a", "Seen", "web", "snippet")
    eng._task_urls = {"https://task.ch/doc"}

    async def call(url):
        return await eng._call("fetch_page", {"url": url}, {}, reg, "q", "de", {}, [])

    assert "search first" in asyncio.run(call("http://169.254.169.254/latest"))
    assert agent.tools.fetched == []
    asyncio.run(call("https://seen.ch/a/"))
    asyncio.run(call("https://task.ch/doc"))
    assert len(agent.tools.fetched) == 2


def test_empty_final_answer_raises():
    eng, agent = _engine()

    eng.llm = ScriptedLLM([say("")] * 6)
    preset = SimpleNamespace(plan_first=False, language="de", mcp={}, query_rules="", report={}, house_style=True)
    agent.own = None
    agent.cfg = SimpleNamespace(mcp_servers={})
    eng.cfg = agent.cfg
    with pytest.raises(RuntimeError):
        asyncio.run(
            eng.run("Frage", preset, SimpleNamespace(focus_calls=2, focus_words=500, max_tokens=10**6), None, "")
        )

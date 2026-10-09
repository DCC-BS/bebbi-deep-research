from service.report import build_sources, owui_source_events, split_report, ui_markdown

FOCUS = """# Stadtklima

Der Grosse Rat hat ein Konzept verlangt [1]. Die Regierung antwortete im Mai [2][3].

## Quellen

[1] Motion Meier betreffend Stadtklima. https://grosserrat.bs.ch/geschaefte/23.5001 (Stand 2023-05-01; abgerufen am 2026-10-09)
[2] Klimabericht 2024. keine öffentliche URL (abgerufen am 2026-10-09)
[3] Statistik: Wohnen. https://www.statistik.bs.ch/wohnen (abgerufen am 2026-10-09)
"""
FOCUS_LEDGER = {"sources": [{"id": "S1", "url": "https://grosserrat.bs.ch/geschaefte/23.5001", "title": "x", "kind": "mcp:grossrat"},
                            {"id": "S2", "url": "", "title": "Klimabericht 2024", "kind": "mcp:statistik"}], "trail": []}


def test_split_report():
    body, heading, lines = split_report(FOCUS)
    assert body.endswith("im Mai [2][3].") and heading == "Quellen" and len(lines) == 3
    assert split_report("kein Verzeichnis") == ("kein Verzeichnis", "", [])


def test_sources_from_focus_report_and_ledger():
    s1, s2, s3 = build_sources(FOCUS, FOCUS_LEDGER)
    assert (s1["n"], s1["title"], s1["stand"], s1["fetched"], s1["kind"]) == (1, "Motion Meier betreffend Stadtklima", "2023-05-01", "2026-10-09", "mcp:grossrat")
    assert s1["excerpt"] == "Der Grosse Rat hat ein Konzept verlangt." and s1["excerpt_kind"] == "claim"
    assert s2["url"] == "" and s2["kind"] == "mcp:statistik" and s2["excerpt"] == "Die Regierung antwortete im Mai."
    assert s3["kind"] == "web" and s3["stand"] == ""


def test_sources_from_pipeline_report_prefer_the_verbatim_quote():
    report = "# T\n\nText [1].\n\n## Quellen\n\n[1] Titel mit Punkt. Abs. 2: https://a.ch/x (Stand 2020; abgerufen am 2026-10-09)\n"
    ledger = {"cards": [{"url": "https://a.ch/x", "title": "t", "quote": "  Wörtliche   Stelle aus der Quelle. " * 3, "finding": "f", "source_kind": "mcp:opencaselaw"}]}
    (s,) = build_sources(report, ledger)
    assert s["title"] == "Titel mit Punkt. Abs. 2" and s["excerpt_kind"] == "quote" and s["kind"] == "mcp:opencaselaw"
    assert "  " not in s["excerpt"] and len(s["excerpt"]) <= 400


def test_english_report():
    report = "# T\n\nText [1].\n\n## Sources\n\n[1] Title. https://a.org (dated 2021-01-01; accessed 2026-10-09)\n"
    (s,) = build_sources(report)
    assert (s["stand"], s["fetched"]) == ("2021-01-01", "2026-10-09")
    assert "1. Title. https://a.org (dated 2021-01-01; accessed 2026-10-09)" in ui_markdown(report, [s])


def test_unparsable_or_gapped_list_gives_no_sources():
    assert build_sources("Text\n\n## Quellen\n\n[1] kaputt") == []
    assert build_sources("Text\n\n## Quellen\n\n[2] Nur. https://a.ch (abgerufen am 2026-10-09)") == []
    assert build_sources("") == []


def test_ui_markdown_list_and_stray_brackets():
    report = FOCUS.replace("im Mai", "im Mai (siehe [2024] und [7])")
    out = ui_markdown(report, build_sources(report))
    body, _, tail = out.partition("## Quellen")
    assert "[1]" in body and "[2][3]" in body and "\\[2024\\]" in body and "\\[7\\]" in body
    assert "​" not in out
    assert tail.strip().splitlines()[0].startswith("1. Motion Meier betreffend Stadtklima. https://grosserrat.bs.ch/geschaefte/23.5001 (Stand 2023-05-01; abgerufen am")
    assert "2. Klimabericht 2024. keine öffentliche URL (abgerufen am 2026-10-09)" in tail
    assert not any(ln.startswith("[") for ln in tail.splitlines())     # no line may start with [n]: it would become a badge
    assert ui_markdown(report, []) == report


def test_source_events_are_ordered_and_unique():
    sources = build_sources(FOCUS, FOCUS_LEDGER)
    sources.append({**sources[0], "n": 4})                            # same URL and title again
    events = owui_source_events(sources)
    assert [e["type"] for e in events] == ["source"] * 4
    ids = [e["data"]["metadata"][0]["source"] for e in events]
    names = [e["data"]["metadata"][0]["name"] for e in events]
    assert ids[:3] == ["https://grosserrat.bs.ch/geschaefte/23.5001", "quelle-2", "https://www.statistik.bs.ch/wohnen"]
    assert len(set(ids)) == len(set(names)) == 4                      # Open WebUI groups by these: duplicates would shift [n]
    d = events[0]["data"]
    assert d["source"] == {"name": "Motion Meier betreffend Stadtklima", "url": "https://grosserrat.bs.ch/geschaefte/23.5001"}
    assert "Stand: 2023-05-01" in d["document"][0] and "abgerufen am 2026-10-09" in d["document"][0] and "> Der Grosse Rat" in d["document"][0]
    assert "url" not in events[1]["data"]["source"]

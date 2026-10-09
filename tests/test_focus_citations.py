from research_agent.focus import finalize
from research_agent.sources import Registry


def _reg():
    reg = Registry()
    reg.add("https://grosserrat.bs.ch/?gnr=23.5544", "Motion Christ betreffend Stadtklima (23.5544)", "mcp:grossrat",
            '{"signatur_ges":"23.5544","beginn_ges":"2023-11-08","status_ges":"In Bearbeitung"}', "2023-11-08")
    reg.add("https://statistik.bs.ch/x", "Leerwohnungen", "web", "Leerwohnungsziffer 2024: 1,1 %; 1'176 leere Wohnungen")
    reg.add("https://www.gesetzessammlung.bs.ch/data/153.260/de", "IDG", "mcp:opencaselaw", "§ 17 ... eine Woche")
    return reg


def test_resolve_numbers_and_source_list():
    stats = {}
    out = finalize("# T\n\n## Zusammenfassung\n\nDie Motion 23.5544 ist in Bearbeitung [S1]. Es gab 1'176 leere Wohnungen [S2].\n"
                   "\n## Quellen\n[1] whatever", _reg(), "Frage", True, stats)
    assert "[1]" in out and "[2]" in out and "whatever" not in out
    assert "[1] Motion Christ betreffend Stadtklima (23.5544). https://grosserrat.bs.ch/?gnr=23.5544 (Stand 2023-11-08;" in out
    assert stats["cited_ok"] == 2 and stats["cited_mcp_sources"] == 1


def test_repoint_and_drop():
    stats = {}
    out = finalize("Es gab 1'176 leere Wohnungen [S1]. Die Quote betrug 7,7 % [S2]. Das IDG gilt [S9].", _reg(), "Frage", True, stats)
    assert stats["repaired"] == 1                      # 1'176 is in S2, not S1: S2 added
    assert "7,7" not in out and stats["dropped_sentences"] == 1   # 7,7 % is in no source
    assert stats["unknown_ids"] == 1


def test_uncited_numbers_get_citation_only_from_cited_sources_and_sr_fix():
    stats = {}
    out = finalize("Die Leerwohnungsziffer lag 2024 bei 1,1 %. Das IDG (SR 153.260) regelt dies [S3].", _reg(), "Frage", True, stats)
    assert "added_citation" not in stats            # S2 has 1,1 % but the report does not cite it: no new source
    assert "SG 153.260" in out and "SR 153.260" not in out
    stats = {}
    out = finalize("Es gab 1'176 leere Wohnungen [S2]. Die Leerwohnungsziffer lag 2024 bei 1,1 %.", _reg(), "Frage", True, stats)
    assert stats["added_citation"] == 1 and "1,1 % [1]" in out
    assert stats["citation_changes"][0]["action"] == "added"


def test_years_never_pick_another_source():
    reg = _reg()
    reg.add("https://example.org/e-mobility", "Masterplan Elektromobilität", "web", "Bericht vom März 2022 zur Elektromobilität")
    stats = {}
    out = finalize("Die Motion wurde 2022 überwiesen [S1].", reg, "Frage", True, stats)
    assert "repaired" not in stats and stats["years_only"] == 1
    assert "Elektromobilität" not in out


def test_repair_prefers_source_of_same_paragraph():
    reg = _reg()
    reg.add("https://example.org/other", "Fremdes Dokument", "web", "irgendwo 1'176 erwähnt")      # S4
    stats = {}
    out = finalize("Die Ziffer betrug 1,1 % [S2]. Es gab 1'176 leere Wohnungen [S1].", reg, "Frage", True, stats)
    assert stats["citation_changes"][0]["to"] == "Leerwohnungen"
    assert "Fremdes Dokument" not in out


def test_table_row_marked():
    stats = {}
    out = finalize("| Kanton | Quote |\n|---|---|\n| BS | 9,9 % [S2] |", _reg(), "Frage", True, stats)
    assert "(nicht belegt) |" in out and stats["marked_rows"] == 1


def test_table_header_left_alone():
    stats = {}
    out = finalize("| Grösse | Quote (2023) | Quelle |\n|---|---|---|\n| 1 Zimmer | 1,1 % | [S2] |", _reg(), "Frage", True, stats)
    assert out.split("\n")[0] == "| Grösse | Quote (2023) | Quelle |"

from research_agent.text import Evidence, cantonal_labels, fix_sr_labels, split_units, unsupported


def test_split_keeps_legal_abbreviations():
    s = ("Gemäss § 17 Abs. 4 IDG müssen Aufzeichnungen i.d.R. innert einer Woche vernichtet werden [S3]. "
         "Der Regierungsrat beschloss am 1. Jan. 2024 die Revision [S4].")
    assert len(split_units(s)) == 2
    assert len(split_units("Laut Art. 13 BV bzw. Art. 6 ff. DSG gilt dies. Dr. Müller (Prof. an der Univ. Basel) widerspricht.")) == 2
    assert len(split_units("The GDP grew by 2.5% (approx. USD 3.1 bn) [S1]. Mr. Smith confirmed it [S2].")) == 2


def test_split_after_year():
    assert split_units("Das Gesetz gilt seit 2019. Danach folgte die Revision.") == [
        "Das Gesetz gilt seit 2019.", "Danach folgte die Revision."]
    assert len(split_units("Die 3. Etappe startet 2026. Im 2. Quartal folgt die Vernehmlassung.")) == 2


def test_numbers_tolerant():
    ev = Evidence("Die Wohnbevölkerung betrug 201971 Personen am 31. Dezember 2024; Anteil 2,34 %; Kosten 1'234'567 Fr.")
    assert unsupported("Die Bevölkerung stieg auf 201'971 Personen (Stand 31.12.2024).", ev) == []
    assert unsupported("Der Anteil lag bei rund 2,3 %.", ev) == []
    assert unsupported("Die Kosten betrugen 1,2 Mio. Franken.", ev) == []
    assert unsupported("Die Kosten betrugen 4,5 Mio. Franken.", ev) == ["4,5"]
    assert unsupported("Drei Kantone und 5 Gemeinden.", ev) == []          # small counts are not checked


def test_identifiers_and_task_numbers():
    ev = Evidence("Motion 23.5544 überwiesen am 2023-12-06")
    assert unsupported("Die Motion 23.5544 wurde am 6. Dezember 2023 überwiesen.", ev) == []
    assert unsupported("Die Motion 23.5545 wurde überwiesen.", ev) == ["23.5545"]
    assert unsupported("Seit 2020 gab es Vorstösse.", ev, ignore=Evidence("seit 2020")) == []


def test_cantonal_labels():
    out = cantonal_labels("**1. [BS] § None** — Verordnung über die Information (SR 153.270) — [LexFind](x)\n"
                          "Bundesgesetz (SR 235.1)")
    assert "(SG 153.270)" in out and "(SR 235.1)" in out
    assert "**SG-Nummer:** 153.260" in cantonal_labels("# Gesetz\n**SR Number:** 153.260\n", canton="BS")
    assert fix_sr_labels("IDG (SR 153.260) und DSG (SR 235.1)", {"153.260": "SG"}) == "IDG (SG 153.260) und DSG (SR 235.1)"


def test_dates_across_languages():
    ev = Evidence("Regolamento sulla gestione dei rifiuti del 13 maggio 2019")
    assert unsupported("Das Regolamento (13. Mai 2019) ist verlinkt.", ev) == []

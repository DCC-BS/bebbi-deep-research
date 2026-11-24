from pathlib import Path

from bebbi_deep_research.engine.config import load_config

ROOT = Path(__file__).resolve().parents[2]


def test_presets_load_with_tool_descriptions():
    cfg = load_config(ROOT / "config" / "presets.yaml")
    assert "statistik" in cfg.mcp_disabled and "statistik" not in cfg.mcp_servers
    descs = cfg.mcp_tool_descriptions
    # every allowlisted tool of an enabled server has a short description of our own
    for preset in cfg.presets.values():
        for server, tools in preset.mcp.items():
            for tool in tools:
                assert tool in descs.get(server, {}), f"{server}.{tool} has no description"
                assert len(descs[server][tool]) <= 300
    assert cfg.presets["web_survey"].engine == "pipeline"
    assert cfg.presets["web_survey"].budget == {"fetch_per_subq": 6}


# ---------------------------------------------------------------- choices: two-step card, mixed presets
def test_every_preset_is_its_own_selection():
    cfg = load_config(ROOT / "config" / "presets.yaml")
    for name, preset in cfg.presets.items():
        sel = cfg.selection_of(preset)
        assert cfg.resolve_selection(**sel) == name, (name, sel)
    assert cfg.selection_of(cfg.presets["dokumente"]) == {"web": False, "mcp": [], "own": "only", "style": "kompakt"}
    assert "statistik" not in cfg.offered_sources()  # a disabled server is not offered


def test_a_mixed_preset_uses_exactly_the_ticked_sources_and_their_rules():
    cfg = load_config(ROOT / "config" / "presets.yaml")
    name = cfg.resolve_selection(web=False, mcp=["grossrat", "data-bs"], own="prefer", style="kompakt")
    assert name == "mix:kompakt:0:prefer:data-bs+grossrat"
    p = cfg.preset(name)
    assert p.name == name and p.engine == "focus" and p.web is False
    assert set(p.mcp) == {"grossrat", "data-bs"} and "get_document" in p.mcp["grossrat"]
    assert all("web" not in r and r for r in p.routes.values())
    assert "Zahlen nur" in p.report["writer_rules"] and "Geschäftsnummer" in p.report["writer_rules"]
    assert "Keine Websuche" in p.query_rules
    # one data source and a short answer: that source's tuned preset is the base
    one = cfg.preset(cfg.resolve_selection(web=False, mcp=["opencaselaw"], own="off", style="kompakt"))
    assert one.report["parts_hint"] == cfg.presets["rechtsrecherche"].report["parts_hint"] and one.own_sources == "off"
    assert set(one.mcp) == {"opencaselaw"}
    # the analysis style keeps its engine whatever is ticked
    wide = cfg.preset(cfg.resolve_selection(web=True, mcp=["grossrat"], own="prefer", style="analyse"))
    assert wide.engine == "pipeline" and wide.routes["primary"] == ["mcp:grossrat", "web"]
    # own documents only: no web, no data source, the documents preset as base
    only = cfg.resolve_selection(web=True, mcp=["grossrat"], own="only", style="kompakt")
    assert only == "dokumente"
    assert "Bericht: Kurze Verwaltungsantwort" in cfg.describe_selection(name)
    assert "eigene Dokumente" in cfg.describe_selection(name)
    assert "eigene Dokumente" not in cfg.describe_selection(name, own_attached=False)


def test_wrong_selections_and_names_are_refused():
    import pytest

    cfg = load_config(ROOT / "config" / "presets.yaml")
    for kw in (
        {"web": False, "mcp": [], "own": "prefer", "style": "kompakt"},  # nothing to research with
        {"web": True, "mcp": ["statistik"], "own": "prefer", "style": "kompakt"},  # disabled server
        {"web": True, "mcp": [], "own": "prefer", "style": "erfunden"},
        {"web": True, "mcp": [], "own": "immer", "style": "kompakt"},
    ):
        with pytest.raises(ValueError):
            cfg.resolve_selection(**kw)
    for bad in ("mix:kompakt:2:prefer:", "mix:erfunden:1:prefer:", "mix:kompakt:1:prefer:nope", "mix:x", "nope"):
        assert cfg.preset(bad) is None


def test_choices_must_name_known_servers_and_presets(tmp_path):
    import pytest

    from bebbi_deep_research.engine.config import PresetsError

    text = (ROOT / "config" / "presets.yaml").read_text()
    broken = (
        text.replace("  sources:\n    web:", "  sources:\n    grossrat: {label: x, description: y}\n    web:")
        .replace("      preset: web_survey\n  own_only_preset", "      preset: gibtsnicht\n  own_only_preset")
        .replace("    preset: rechtsrecherche\n", "    preset: erfunden\n", 1)
    )
    f = tmp_path / "p.yaml"
    f.write_text(broken)
    with pytest.raises(PresetsError) as e:
        load_config(f)
    msg = str(e.value)
    assert "choices.sources.grossrat: only 'web'" in msg and "choices.styles.analyse.preset" in msg
    assert "mcp_servers.opencaselaw.preset: no such preset 'erfunden'" in msg


def test_every_enabled_mcp_server_is_offered_with_its_own_texts():
    cfg = load_config(ROOT / "config" / "presets.yaml")
    offered = cfg.offered_sources()
    assert set(offered) == {"web"} | set(cfg.mcp_servers)  # a new server needs no entry under choices
    assert offered["grossrat"].label == "Grosser Rat Basel-Stadt" and offered["grossrat"].preset == "grossrat"

from pathlib import Path

from research_agent.config import load_config

ROOT = Path(__file__).resolve().parent.parent


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

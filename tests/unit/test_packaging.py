"""Generated deployment assets must match their sources."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

from bebbi_deep_research.engine import load_config

ROOT = Path(__file__).resolve().parents[2]


def test_openwebui_assets_up_to_date():
    r = subprocess.run(
        [sys.executable, "scripts/build_openwebui_assets.py", "--check"], cwd=ROOT, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stdout
    fn = json.loads((ROOT / "deploy/openwebui/function.json").read_text())[0]
    assert fn["type"] == "pipe" and fn["valves"]["SERVICE_TOKEN"] == "" and "class Pipe" in fn["content"]


def test_chart_ships_same_presets():
    assert (ROOT / "deploy/helm/bebbi-deep-research/files/presets.yaml").read_text() == (
        ROOT / "config/presets.yaml"
    ).read_text()


def test_presets_load_and_statistik_mcp_stays_off():
    cfg = load_config(ROOT / "config/presets.yaml")
    assert "statistik" not in cfg.mcp_servers and "statistik" in cfg.mcp_disabled
    assert {"quick", "standard", "deep"} <= set(cfg.budgets)
    pipe_default = "rechtsrecherche grossrat statistik verwaltung_web web_survey dokumente".split()
    assert set(pipe_default) == set(cfg.presets)


def test_pipe_valve_presets_exist():
    import importlib.util

    spec = importlib.util.spec_from_file_location("pipe", ROOT / "pipe/deep_research.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = yaml.safe_load((ROOT / "config/presets.yaml").read_text())
    pipe = mod.Pipe()
    assert set(pipe._labels()) == set(cfg["presets"])
    pipe.valves.PRESET_MODELS = True
    assert {m["id"] for m in pipe.pipes()} <= set(cfg["presets"]) | {mod.AUTO}

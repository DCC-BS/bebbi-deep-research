"""Engine registry: one place maps the preset's `engine` value to an engine class."""

from pathlib import Path

import pytest

from bebbi_deep_research.engine import ENGINES, engine_class, load_config
from bebbi_deep_research.engine.focus import FocusEngine
from bebbi_deep_research.engine.pipeline import PipelineEngine
from bebbi_deep_research.services.jobs import runs_by_day

ROOT = Path(__file__).resolve().parents[2]


def test_registry_keys_are_the_engine_names_and_unknown_values_run_the_pipeline():
    assert ENGINES == {"pipeline": PipelineEngine, "focus": FocusEngine}
    assert all(cls.name == name for name, cls in ENGINES.items())
    assert engine_class("focus") is FocusEngine and engine_class("typo") is PipelineEngine


def test_every_preset_names_a_registered_engine():
    cfg = load_config(ROOT / "config/presets.yaml")
    assert {p.engine for p in cfg.presets.values()} <= set(ENGINES)


def test_daytime_rule_asks_the_engine():
    cfg = load_config(ROOT / "config/presets.yaml")
    for name, p in cfg.presets.items():
        assert runs_by_day(cfg, name, "quick") is (p.engine == "focus")
        assert runs_by_day(cfg, name, "deep") is False
    assert runs_by_day(cfg, "no-such-preset", "quick") is False


def test_a_preset_with_an_unknown_engine_is_refused(tmp_path):
    import yaml

    from bebbi_deep_research.engine import load_config
    from bebbi_deep_research.engine.config import PresetsError
    from bebbi_deep_research.utils.app_config import DEFAULT_PRESETS

    cfg = yaml.safe_load(DEFAULT_PRESETS.read_text())
    next(iter(cfg["presets"].values()))["engine"] = "quantum"
    bad = tmp_path / "presets.yaml"
    bad.write_text(yaml.safe_dump(cfg, allow_unicode=True))
    with pytest.raises(PresetsError, match="unknown engine 'quantum'"):
        load_config(bad)


async def test_metrics_include_the_engine_counters(tmp_path):
    from bebbi_deep_research.engine import metrics

    from .conftest import running

    metrics.inc("research_tool_errors_total", tool="search", reason="http_403")
    async with running(tmp_path) as (c, _):
        text = (await c.get("/metrics")).text
    assert "research_jobs{" in text and 'research_tool_errors_total{reason="http_403",tool="search"}' in text

"""The presets file is validated at load time: typos and wrong references stop the start with a clear message."""

from pathlib import Path

import pytest
import yaml

from bebbi_deep_research.app import create_app
from bebbi_deep_research.engine.config import PresetsError, load_config

from .conftest import make_container, make_settings

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = yaml.safe_load((ROOT / "config/presets.yaml").read_text())


def _write(tmp_path, change) -> Path:
    raw = yaml.safe_load((ROOT / "config/presets.yaml").read_text())
    change(raw)
    path = tmp_path / "presets.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True))
    return path


def _error(tmp_path, change) -> str:
    with pytest.raises(PresetsError) as e:
        load_config(_write(tmp_path, change))
    return str(e.value)


@pytest.mark.parametrize("path", ["config/presets.yaml", "deploy/helm/bebbi-deep-research/files/presets.yaml"])
def test_shipped_presets_are_valid(path):
    cfg = load_config(ROOT / path)
    assert set(cfg.presets) == set(SHIPPED["presets"])
    assert cfg.presets["grossrat"].budget == {"focus_words": 550}
    assert cfg.presets["grossrat"].report["analysis_part"] is None  # explicit null is kept (no analysis part)
    assert cfg.presets["dokumente"].own_sources == "only"


def test_misspelled_key_is_named(tmp_path):
    def change(raw):
        raw["presets"]["dokumente"]["own_source"] = raw["presets"]["dokumente"].pop("own_sources")

    msg = _error(tmp_path, change)
    assert "presets.dokumente.own_source: unknown key (typo?)" in msg


def test_enums(tmp_path):
    def change(raw):
        raw["presets"]["grossrat"]["engine"] = "fokus"
        raw["presets"]["grossrat"]["own_sources"] = "nur"
        raw["presets"]["grossrat"]["language"] = "fr"

    msg = _error(tmp_path, change)
    assert "presets.grossrat.engine" in msg and "presets.grossrat.own_sources" in msg
    assert "presets.grossrat.language" in msg


def test_references_to_mcp_servers_and_budget_fields(tmp_path):
    def change(raw):
        raw["presets"]["grossrat"]["routes"]["primary"] = ["mcp:grosrat", "web"]
        raw["presets"]["grossrat"]["mcp"]["nope"] = ["x"]
        raw["presets"]["grossrat"]["mcp"]["grossrat"].append("delete_everything")
        raw["presets"]["web_survey"]["budget"] = {"fetch_per_sub": 6}
        del raw["budgets"]["deep"]

    msg = _error(tmp_path, change)
    assert "presets.web_survey.budget.fetch_per_sub: unknown key" in msg
    # the cross references are checked once every entry has the right shape
    fixed = _write(tmp_path, lambda raw: (change(raw), raw["presets"]["web_survey"].pop("budget")))
    with pytest.raises(PresetsError) as e:
        load_config(fixed)
    msg = str(e.value)
    assert "budgets.deep: missing" in msg
    assert "presets.grossrat.routes.primary: 'mcp:grosrat' is neither 'web' nor" in msg
    assert "presets.grossrat.mcp.nope: no such server" in msg
    assert "delete_everything not among the tools of mcp_servers.grossrat" in msg


def test_disabled_server_counts_as_configured(tmp_path):
    cfg = load_config(_write(tmp_path, lambda raw: None))
    assert "statistik" in cfg.mcp_disabled and "statistik" in cfg.presets["statistik"].mcp


def test_server_url_shorthand_and_bad_yaml(tmp_path):
    def change(raw):
        raw["mcp_servers"]["opencaselaw"] = "https://mcp.opencaselaw.ch/"
        raw["presets"]["rechtsrecherche"]["mcp"]["opencaselaw"] = ["anything"]  # no tool list: not checked

    assert load_config(_write(tmp_path, change)).mcp_servers["opencaselaw"] == "https://mcp.opencaselaw.ch/"
    bad = tmp_path / "bad.yaml"
    bad.write_text("presets: [\n")
    with pytest.raises(PresetsError, match="not valid YAML"):
        load_config(bad)


async def test_service_does_not_start_with_an_invalid_presets_file(tmp_path):
    path = _write(tmp_path, lambda raw: raw["presets"]["grossrat"].update(engin="focus"))
    app = create_app(make_container(make_settings(tmp_path, presets_path=path)))
    with pytest.raises(PresetsError, match="presets.grossrat.engin: unknown key"):
        async with app.router.lifespan_context(app):
            pass

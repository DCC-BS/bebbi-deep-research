#!/usr/bin/env python3
"""Generate deploy/openwebui/function.json and models.json from pipe/deep_research.py and config/presets.yaml.

  uv run python scripts/build_openwebui_assets.py          # write the files
  uv run python scripts/build_openwebui_assets.py --check  # exit 1 if they are out of date (CI, tests)

function.json  one entry in the format of Open WebUI's /api/v1/functions/sync (seeder: POST it with an admin key).
models.json    one model entry per preset ("Deep Research · Recht", ...) in the format of /api/v1/models/import.
"""

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy" / "openwebui"
FUNCTION_ID = "bebbi_deep_research"
LABELS = {"rechtsrecherche": "Recht", "grossrat": "Grosser Rat", "statistik": "Statistik",
          "verwaltung_web": "Verwaltung", "web_survey": "Web"}      # same as the PRESETS valve default of the pipe
SUGGESTIONS = {
    "rechtsrecherche": ["Welche Rechtsgrundlagen regeln Videoüberwachung durch Kantonsbehörden in Basel-Stadt?"],
    "grossrat": ["Welche Vorstösse zum Stadtklima hat der Grosse Rat in den letzten Jahren behandelt?"],
    "statistik": ["Wie hat sich die Leerwohnungsziffer in Basel-Stadt seit 2015 entwickelt?"],
    "verwaltung_web": ["Wie organisieren andere Kantone die Open-Data-Strategie?"],
    "web_survey": ["Which approaches exist for citizen participation platforms in European cities?"],
}


def build() -> dict[str, str]:
    cfg = yaml.safe_load((ROOT / "config" / "presets.yaml").read_text())
    content = (ROOT / "pipe" / "deep_research.py").read_text()
    function = [{
        "id": FUNCTION_ID, "name": "Deep Research (Bebbi Bot)", "type": "pipe", "content": content,
        "meta": {"description": "Deep research with clarifying questions, plan approval and night queue."},
        # SERVICE_TOKEN is a secret: the seeder fills it from its own secret store (see docs/integration.md)
        "valves": {"SERVICE_URL": "http://bebbi-deep-research:8088", "SERVICE_TOKEN": ""},
        "is_active": True, "is_global": True, "user_id": None, "updated_at": 0, "created_at": 0,
    }]
    models = []
    for name, p in cfg["presets"].items():
        models.append({
            "id": f"{FUNCTION_ID}.{name}", "base_model_id": None, "name": f"Deep Research · {LABELS.get(name, name)}",
            "params": {}, "access_grants": [{"principal_type": "user", "principal_id": "*", "permission": "read"}], "is_active": True,
            "meta": {"description": p["description"], "capabilities": {"vision": False, "file_upload": False, "citations": False},
                     "suggestion_prompts": [{"content": s} for s in SUGGESTIONS.get(name, [])]},
        })
    return {"function.json": json.dumps(function, indent=2, ensure_ascii=False) + "\n",
            "models.json": json.dumps(models, indent=2, ensure_ascii=False) + "\n"}


def main() -> int:
    files = build()
    if "--check" in sys.argv:
        stale = [n for n, c in files.items() if not (OUT / n).exists() or (OUT / n).read_text() != c]
        print("stale: " + ", ".join(stale) if stale else "up to date")
        return 1 if stale else 0
    OUT.mkdir(parents=True, exist_ok=True)
    for n, c in files.items():
        (OUT / n).write_text(c)
        print("wrote", OUT / n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

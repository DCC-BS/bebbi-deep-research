#!/usr/bin/env python3
"""Install (or update) the Deep Research pipe and the Word download action in a running Open WebUI and set its valves.

Idempotent.

  OWUI_URL=http://localhost:3000 OWUI_API_KEY=sk-... RA_TOKEN=... uv run python scripts/install_pipe.py

Instead of OWUI_API_KEY you can pass OWUI_ADMIN_EMAIL / OWUI_ADMIN_PASSWORD (local stack): the script signs in and,
with --create-api-key, creates an admin API key and prints it (put it into .env as OWUI_API_KEY for the service).
Options: --service-url URL (default http://service:8088, the compose service name), --models (also import models.json).
"""

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
FUNCTION_ID = "bebbi_deep_research"
ACTION_ID = "bebbi_deep_research_download"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service-url", default=os.environ.get("RA_SERVICE_URL", "http://service:8088"))
    ap.add_argument("--create-api-key", action="store_true")
    ap.add_argument("--models", action="store_true", help="also import deploy/openwebui/models.json")
    args = ap.parse_args()
    base = os.environ.get("OWUI_URL", "http://localhost:3000").rstrip("/")
    token = os.environ.get("RA_TOKEN")
    if not token:
        sys.exit("RA_TOKEN (the service token for the SERVICE_TOKEN valve) is not set")
    http = httpx.Client(base_url=base, timeout=60)

    key = os.environ.get("OWUI_API_KEY")
    if not key:
        r = http.post(
            "/api/v1/auths/signin",
            json={"email": os.environ["OWUI_ADMIN_EMAIL"], "password": os.environ["OWUI_ADMIN_PASSWORD"]},
        )
        r.raise_for_status()
        key = r.json()["token"]
    http.headers["Authorization"] = f"Bearer {key}"
    if args.create_api_key:
        r = http.post("/api/v1/auths/api_key")
        r.raise_for_status()
        print("OWUI_API_KEY=" + r.json()["api_key"])

    for function_id, name, file in (
        (FUNCTION_ID, "Deep Research (Bebbi Bot)", "deep_research.py"),
        (ACTION_ID, "Word-Datei (Deep Research)", "deep_research_download.py"),
    ):
        form = {
            "id": function_id,
            "name": name,
            "content": (ROOT / "pipe" / file).read_text(),
            "meta": {
                "description": "Deep research with clarifying questions, plan approval and night queue."
                if function_id == FUNCTION_ID
                else "Attaches the research report as a Word file to the answer."
            },
        }
        existing = http.get(f"/api/v1/functions/id/{function_id}")
        if existing.status_code == 200 and existing.json():  # already installed: update the code
            r = http.post(f"/api/v1/functions/id/{function_id}/update", json=form)
        else:
            r = http.post("/api/v1/functions/create", json=form)
        r.raise_for_status()
        fn = r.json()
        if not fn["is_active"]:
            http.post(f"/api/v1/functions/id/{function_id}/toggle").raise_for_status()
        # The pipe is global; the download action is not (the Deep Research model lists it in meta.actionIds). The
        # endpoint flips the flag, so compare first. An install made before 1.2.2 has the action global: this undoes it.
        if fn["is_global"] != (function_id == FUNCTION_ID):
            http.post(f"/api/v1/functions/id/{function_id}/toggle/global").raise_for_status()
        cur = http.get(
            f"/api/v1/functions/id/{function_id}/valves"
        )  # Open WebUI stores only the fields sent: merge, keep the operator's other valves
        current = cur.json() if cur.status_code == 200 and isinstance(cur.json(), dict) else {}
        r = http.post(
            f"/api/v1/functions/id/{function_id}/valves/update",
            json={**current, "SERVICE_URL": args.service_url, "SERVICE_TOKEN": token},
        )
        r.raise_for_status()
        print(f"{function_id} installed and active; valves set:", json.dumps({**r.json(), "SERVICE_TOKEN": "***"}))

    if args.models:
        models = json.loads((ROOT / "deploy" / "openwebui" / "models.json").read_text())
        r = http.post("/api/v1/models/import", json={"models": models})
        r.raise_for_status()
        print("models imported:", len(models))
    return 0


if __name__ == "__main__":
    sys.exit(main())

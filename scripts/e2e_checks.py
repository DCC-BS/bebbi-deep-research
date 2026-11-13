#!/usr/bin/env python3
"""Asserting checks against a local Open WebUI stack (same .env and accounts as e2e_owui.py). Exit code 1 on a failure.

scripts/e2e_checks.py admin-key FILE.txt
    the service's admin API key (OWUI_API_KEY) still reads a knowledge base of a normal user
    (POST /api/v1/retrieval/query/collection, response documents/metadatas/distances with file_id and name) and a file
    date (GET /api/v1/files/{id}, created_at), and a normal user is still refused on another user's knowledge base.
    Needs the permission workspace.knowledge for the test user.
scripts/e2e_checks.py hidden-context
    Open WebUI 0.13.1 appends hidden tool results (here: the knowledge base attached to the model) after the user's
    message, so the pipe sees a tool message last. A chat with a clarifying question must still get its second turn as
    the answer to that question: the turn goes on to the plan (the model "auto" shows the plan card), it does not
    start over with the preset card. Needs a knowledge base attached to the model bebbi_deep_research.auto (Workspace,
    Models; restart Open WebUI after changing the model: it caches the model list) and the service running.
"""

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_owui as e  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool) -> None:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


def admin_key(path: str) -> None:
    env = e.load_env()
    user = e.Client(env)
    kb = user.create_kb("E2E admin key", [path])["id"]
    admin = httpx.Client(
        base_url=env.get("OWUI_PUBLIC_URL", "http://localhost:3000"),
        headers={"Authorization": "Bearer " + env["OWUI_API_KEY"]},
        timeout=60,
    )
    r = admin.post("/api/v1/retrieval/query/collection", json={"collection_names": [kb], "query": "Frist", "k": 3})
    check(f"admin key reads a user's knowledge base ({r.status_code})", r.status_code == 200)
    d = r.json() if r.status_code == 200 else {}
    check("response has documents, metadatas, distances", all(k in d for k in ("documents", "metadatas", "distances")))
    md = (d.get("metadatas") or [[]])[0]
    check("metadata has file_id and name", bool(md) and "file_id" in md[0] and "name" in md[0])
    if md:
        f = admin.get(f"/api/v1/files/{md[0]['file_id']}")
        check(
            f"admin key reads a file record with created_at ({f.status_code})",
            f.status_code == 200 and "created_at" in f.json(),
        )
    other = e.Client(env, admin=True).create_kb("E2E admin only", [path])["id"]
    r = user.http.post("/api/v1/retrieval/query/collection", json={"collection_names": [other], "query": "x"})
    check(f"a normal user is refused on another user's knowledge base ({r.status_code})", r.status_code == 403)


def hidden_context() -> None:
    env = e.load_env()
    cl = e.Client(env)
    model = "bebbi_deep_research.auto"
    cid = cl.new_chat(model)
    sio = cl.open_session()
    try:
        _, first = cl.say(cid, "Welche Rechtsgrundlagen gelten für Videoüberwachung?", model, session_id=sio.session_id)
        check("first turn asks clarifying questions", "Bevor ich den Rechercheplan erstelle" in first)
        _, second = cl.say(cid, "weiter", model, session_id=sio.session_id)
    finally:
        sio.disconnect()
    check("second turn goes on to the queued job (not a new clarify/preset round)", "Auftrag eingereiht" in second)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "admin-key" and len(sys.argv) > 2:
        admin_key(sys.argv[2])
    elif cmd == "hidden-context":
        hidden_context()
    else:
        sys.exit(__doc__)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

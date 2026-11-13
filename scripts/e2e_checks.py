#!/usr/bin/env python3
"""Asserting checks against a local Open WebUI stack (same .env and accounts as e2e_owui.py). Exit code 1 on a failure.

scripts/e2e_checks.py admin-key FILE.txt
    the service's admin API key (OWUI_API_KEY) still reads a knowledge base of a normal user
    (POST /api/v1/retrieval/query/collection, response documents/metadatas/distances with file_id and name) and a file
    date (GET /api/v1/files/{id}, created_at), and a normal user is still refused on another user's knowledge base.
    Needs the permission workspace.knowledge for the test user.
scripts/e2e_checks.py hidden-context
    Open WebUI 0.13.1 appends hidden tool results (here: the knowledge base attached to the model) after the user's
    message, so the pipe sees a tool message last, and stores a queued message with hidden tool results only. Four
    turns: clarifying question, "weiter" (job queued), status question, "abbrechen". Fails if no hidden tool results
    occur (attach a knowledge base to bebbi_deep_research.auto and leave its builtin tools on; restart Open WebUI after
    changing the model: it caches the model list), if the queued message would render empty (`output` renders in
    preference to `content`), or if a later turn does not find the job.
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
    admin_user = e.Client(env, admin=True)
    created: list[tuple[e.Client, str]] = []
    try:
        kb = user.create_kb("E2E admin key", [path])["id"]
        created.append((user, kb))
        admin = httpx.Client(
            base_url=env.get("OWUI_PUBLIC_URL", "http://localhost:3000"),
            headers={"Authorization": "Bearer " + env["OWUI_API_KEY"]},
            timeout=60,
        )
        r = admin.post("/api/v1/retrieval/query/collection", json={"collection_names": [kb], "query": "Frist", "k": 3})
        check(f"admin key reads a user's knowledge base ({r.status_code})", r.status_code == 200)
        d = r.json() if r.status_code == 200 else {}
        check(
            "response has documents, metadatas, distances", all(k in d for k in ("documents", "metadatas", "distances"))
        )
        md = (d.get("metadatas") or [[]])[0]
        check("metadata has file_id and name", bool(md) and "file_id" in md[0] and "name" in md[0])
        if md:
            f = admin.get(f"/api/v1/files/{md[0]['file_id']}")
            ok = f.status_code == 200 and "created_at" in f.json()
            check(f"admin key reads a file record with created_at ({f.status_code})", ok)
        other = admin_user.create_kb("E2E admin only", [path])["id"]
        created.append((admin_user, other))
        r = user.http.post("/api/v1/retrieval/query/collection", json={"collection_names": [other], "query": "x"})
        check(f"a normal user is refused on another user's knowledge base ({r.status_code})", r.status_code == 403)
    finally:
        for owner, kb_id in created:  # leave the instance as found
            owner.http.delete(f"/api/v1/knowledge/{kb_id}/delete")


def _output_kinds(message: dict) -> list[str]:
    return [("hidden " if o.get("hidden") else "") + str(o.get("type")) for o in message.get("output") or []]


def hidden_context() -> None:
    """Three turns on a model with hidden tool results (knowledge base attached to the model, builtin tools on)."""
    env = e.load_env()
    cl = e.Client(env)
    model = "bebbi_deep_research.auto"
    cid = cl.new_chat(model)
    sio = cl.open_session()

    def turn(text: str) -> tuple[str, dict]:
        aid, answer = cl.say(cid, text, model, session_id=sio.session_id)
        return answer, cl.get_chat(cid)["chat"]["history"]["messages"][aid]

    try:
        first, m1 = turn("Welche Rechtsgrundlagen gelten für Videoüberwachung?")
        check("turn 1 asks clarifying questions", "Bevor ich den Rechercheplan erstelle" in first)
        hidden = any(k.startswith("hidden function_call") for k in _output_kinds(m1))
        check(f"hidden tool results were present (output: {_output_kinds(m1)})", hidden)
        second, m2 = turn("weiter")
        check("turn 2 goes on to the queued job", "Auftrag eingereiht" in second)
        kinds = _output_kinds(m2)
        # Open WebUI renders `output` in preference to `content` (ContentRenderer.svelte): a non-empty output without a
        # text item shows an empty bubble
        check(f"queued message renders: output is empty or has a text item ({kinds})", not kinds or "message" in kinds)
        third, m3 = turn("Ist der Bericht schon fertig?")
        check(
            "turn 3 reports the queued job (state survived the hidden tool results)",
            "Die Recherche läuft noch" in third,
        )
        fourth, _ = turn("abbrechen")
        check("turn 4 cancels the job", "abgebrochen" in fourth)
    finally:
        sio.disconnect()


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

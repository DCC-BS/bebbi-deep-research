#!/usr/bin/env python3
"""Asserting checks against a local Open WebUI stack (same .env and accounts as e2e_owui.py). Exit code 1 on a failure.

scripts/e2e_checks.py admin-key FILE.txt
    the service's admin API key (OWUI_API_KEY) still reads a knowledge base of a normal user
    (POST /api/v1/retrieval/query/collection, response documents/metadatas/distances with file_id and name) and a file
    date (GET /api/v1/files/{id}, created_at), and a normal user is still refused on another user's knowledge base.
    Needs the permission workspace.knowledge for the test user.
scripts/e2e_checks.py queued-message
    Open WebUI 0.13.4 holds a message the user types while a reply is still running and answers it after the reply. The
    pipe's reply is open for as long as its plan card is (a card has no free text), so the pipe polls for a held message
    and ends its turn at once, keeping the plan in its state marker. Two chats, each with two messages typed while the
    plan card is open (they arrive as one text, joined by a blank line): change requests ("Teil 3 weglassen",
    "Nur Basel-Stadt") must edit the plan and queue the job; a status question followed by "abbrechen" must discard it.
scripts/e2e_checks.py night CHAT_ID [--pipe]
    After a night run was delivered into CHAT_ID (from `e2e_owui.py say --approve`): the stored message has the
    report, unique sources in order, no marker and an empty `output`; the Word action
    attaches one file the owner can download, a second click attaches nothing. With --pipe (daytime path: the service
    delivers nothing, the user asks again) the last answer is checked and the Word file must be attached already.
scripts/e2e_checks.py hidden-context
    Open WebUI 0.13.1 appends hidden tool results (here: the knowledge base attached to the model) after the user's
    message, so the pipe sees a tool message last, and stores a queued message with hidden tool results only. Four
    turns: clarifying question, "weiter" (job queued), status question, "abbrechen". Fails if no hidden tool results
    occur (attach a knowledge base to bebbi_deep_research.auto and leave its builtin tools on; restart Open WebUI after
    changing the model: it caches the model list), if the queued message would render empty (`output` renders in
    preference to `content`), or if a later turn does not find the job.
"""

import re
import sys
import threading
import time
import uuid
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


def _post_turn(cl: "e.Client", chat_id: str, text: str, model: str, session_id: str) -> httpx.Response:
    """A user turn sent like the browser does while a reply is running: parent is the open assistant message."""
    hist = cl.get_chat(chat_id)["chat"]["history"]
    parent = hist.get("currentId")
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    user = {
        "id": uid,
        "parentId": parent,
        "childrenIds": [],
        "role": "user",
        "content": text,
        "timestamp": int(time.time()),
        "models": [model],
    }
    msgs = [{"role": m["role"], "content": m["content"]} for m in cl.path(cl.get_chat(chat_id))]
    payload = {
        "model": model,
        "messages": msgs + [{"role": "user", "content": text}],
        "stream": True,
        "chat_id": chat_id,
        "id": aid,
        "message_ids": [{"model_id": model, "message_id": aid, "modelIdx": 0}],
        "session_id": session_id,
        "parent_id": parent,
        "user_message": user,
        "background_tasks": {"follow_up_generation": False},
        "features": {},
        "params": {},
    }
    return cl.http.post("/api/chat/completions", json=payload)


def _wait(cond, timeout: float = 120.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(1)
    return False


def _queued_case(env: dict, model: str, held: list[str], label: str, check_turns) -> None:
    """First turn of a new chat with the plan card held open for 40 s; `held` are typed meanwhile, one after the other.
    The pipe must end its turn at once (not wait for the card), keeping the plan in its marker; `check_turns(first,
    second, sio)` then judges the answer to the held messages."""
    cl = e.Client(env)
    cid = cl.new_chat(model)
    sio = cl.open_session(delay=40)
    first: dict = {}

    def run_first() -> None:
        aid, text = cl.say(cid, "Welche Hitzemassnahmen plant Basel?", model, session_id=sio.session_id)
        first.update(aid=aid, text=text)

    def messages() -> dict:
        return cl.get_chat(cid)["chat"]["history"]["messages"]

    def answered() -> bool:
        return any(
            m["role"] == "assistant" and m.get("done") and m["id"] != first.get("aid") and m.get("content")
            for m in messages().values()
        )

    t = threading.Thread(target=run_first)
    t.start()
    try:
        _wait(lambda: any("Rechercheplan" in c for c in sio.replaced), 60)  # the plan card is open
        time.sleep(3)
        started = time.time()
        for text in held:
            r = _post_turn(cl, cid, text, model, sio.session_id)
            body = r.json() if r.status_code == 200 else {}
            check(
                f"[{label}] {text!r} typed while the card is open is held ({r.status_code} {body})",
                body.get("queued") is True,
            )
        pending = [
            m for m in messages().values() if m["role"] == "user" and (m.get("meta") or {}).get("status") == "pending"
        ]
        check(
            f"[{label}] held messages are stored with meta.status=pending ({len(pending)})", len(pending) == len(held)
        )
        t.join(120)
        took = time.time() - started
        check(f"[{label}] the first reply ends without waiting for the card ({took:.0f} s of 40 s)", took < 25)
        check(
            f"[{label}] it keeps the plan for the held message ({first.get('text', '')[-60:]!r})",
            "Änderungswunsch" in first.get("text", ""),
        )
        check(f"[{label}] held messages are answered afterwards", _wait(answered, 90))
        h = messages()
        second = [m for m in h.values() if m["role"] == "assistant" and m["id"] != first.get("aid")]
        text = second[0]["content"] if second else ""
        check_turns(first, text, sio)
        check(
            f"[{label}] no message is left pending",
            not [m for m in h.values() if (m.get("meta") or {}).get("status") == "pending"],
        )
    finally:
        sio.disconnect()


def queued_message() -> None:
    env = e.load_env()
    model = "bebbi_deep_research.auto"

    def as_change(first: dict, text: str, sio) -> None:
        # two held messages arrive as one text, "A\n\nB"; the mock/real service edits the plan with it
        edited = [c for c in sio.replaced if "Teil 3 weglassen" in c and "Nur Basel-Stadt" in c]
        check("[change request] the plan was edited with both held messages as one instruction", bool(edited))
        check(
            f"[change request] the answer is the queued job, not a new research ({text[:60]!r})",
            "Auftrag eingereiht" in text,
        )

    _queued_case(env, model, ["Teil 3 weglassen", "Nur Basel-Stadt"], "change request", as_change)

    def as_discard(first: dict, text: str, sio) -> None:
        check(f"[cancel word] 'abbrechen' as the last paragraph discards the plan ({text[:60]!r})", "verworfen" in text)

    _queued_case(env, model, ["Wie ist der Stand?", "abbrechen"], "cancel word", as_discard)


def night(chat_id: str, pipe: bool = False) -> None:
    env = e.load_env()
    cl = e.Client(env)
    chat = cl.get_chat(chat_id)
    # delivered report: the first assistant message; pipe path (no delivery): the answer of the later turn
    m = [x for x in cl.path(chat) if x["role"] == "assistant"][-1 if pipe else 0]
    check("message is done", m.get("done") is True)
    check("report text delivered (starts with the report heading)", m.get("content", "").lstrip().startswith("# "))
    check("state marker is gone from the delivered text", "research-agent:" not in m.get("content", ""))
    if not pipe:  # the pipe path returns the text, which Open WebUI stores as the `output` message item too
        out = m.get("output") or []
        texts = [
            part.get("text")
            for item in out
            if item.get("type") == "message"
            for part in item.get("content") or []
            if part.get("type") == "output_text"
        ]
        # empty; or, with hidden tool results (a knowledge base on the model), only hidden calls with their outputs plus
        # the report as the text item: Open WebUI renders `output` in preference to `content`, so the delivery must
        # have replaced the text item
        hidden_calls = {i.get("call_id") for i in out if i.get("type") == "function_call" and i.get("hidden")}
        only_hidden_and_text = all(i.get("type") == "message" or i.get("call_id") in hidden_calls for i in out)
        check(
            f"output is empty or hidden items + the report as its text item ({len(out)} items)",
            not out or (texts == [m.get("content")] and only_hidden_and_text),
        )
    srcs = m.get("sources") or []
    ids = [(s.get("source") or {}).get("name") or (s.get("source") or {}).get("id") for s in srcs]
    check(
        f"sources stored once each ({len(srcs)} sources, {len(set(ids))} unique)",
        len(srcs) > 0 and len(ids) == len(set(ids)),
    )
    n_cited = max([int(x) for x in re.findall(r"\[(\d+)\]", m.get("content", ""))] or [0])
    check(f"every cited [n] has a source (highest [n] {n_cited})", n_cited <= len(srcs))
    if pipe:  # the pipe attached the Word file itself and returned the report with the sources events
        files = [f for f in (m.get("files") or []) if str(f.get("name", "")).endswith(".docx")]
        check(f"pipe attached one Word file ({len(files)})", len(files) == 1)
        d = cl.http.get(f"/api/v1/files/{files[0]['id']}/content") if files else None
        check(
            f"owner downloads it ({d.status_code if d is not None else None})",
            d is not None and d.status_code == 200 and d.content[:2] == b"PK",
        )
        return
    sio = cl.open_session()
    try:

        def action() -> dict:
            r = cl.http.post(
                "/api/chat/actions/bebbi_deep_research_download",
                json={
                    "model": "bebbi_deep_research.auto",
                    "messages": [{"role": "assistant", "content": m["content"], "id": m["id"]}],
                    "chat_id": chat_id,
                    "id": m["id"],
                    "session_id": sio.session_id,
                },
            )
            return {"status": r.status_code}

        check("Word action ok (first click)", action()["status"] == 200)
        time.sleep(2)
        files = [
            f
            for f in (cl.get_chat(chat_id)["chat"]["history"]["messages"][m["id"]].get("files") or [])
            if str(f.get("name", "")).endswith(".docx")
        ]
        check(f"one Word file attached ({len(files)})", len(files) == 1)
        action()
        time.sleep(2)
        files2 = [
            f
            for f in (cl.get_chat(chat_id)["chat"]["history"]["messages"][m["id"]].get("files") or [])
            if str(f.get("name", "")).endswith(".docx")
        ]
        check(f"second click attaches nothing ({len(files2)})", len(files2) == 1)
        if files:
            d = cl.http.get(f"/api/v1/files/{files[0]['id']}/content")
            check(
                f"owner downloads the file ({d.status_code}, {len(d.content)} bytes, PK={d.content[:2] == b'PK'})",
                d.status_code == 200 and d.content[:2] == b"PK",
            )
    finally:
        sio.disconnect()


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "admin-key" and len(sys.argv) > 2:
        admin_key(sys.argv[2])
    elif cmd == "queued-message":
        queued_message()
    elif cmd == "night" and len(sys.argv) > 2:
        night(sys.argv[2], pipe="--pipe" in sys.argv)
    elif cmd == "hidden-context":
        hidden_context()
    else:
        sys.exit(__doc__)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

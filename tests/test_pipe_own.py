"""Own sources in the pipe: what the user may read (Open WebUI's own checks, here with fakes), what is sent to the service."""

import asyncio
import sys
import types

from .test_pipe import _plan_history, load_pipe

USER = {"id": "u1", "role": "user"}
LONG = "Gesuche sind 28 Tage vor Beginn einzureichen. " * 100          # 4'700 characters


def fake_file(fid, name="merkblatt.pdf", text=LONG, owner="u1", status="completed", ts=1791571774):
    return types.SimpleNamespace(id=fid, user_id=owner, filename=name, meta={"name": name}, data={"content": text, "status": status}, created_at=ts)


class World:
    """Stands in for Open WebUI: files, knowledge bases, who may read what, model knowledge."""

    def __init__(self):
        self.files = {"f1": fake_file("f1"), "f2": fake_file("f2", "fremd.pdf", "Geheim", owner="other"),
                      "f3": fake_file("f3", "scan.pdf", "", status="completed"), "f4": fake_file("f4", "neu.pdf", "", status="pending")}
        self.kbs = {"kb1": types.SimpleNamespace(id="kb1", name="Beschaffung"), "kb2": types.SimpleNamespace(id="kb2", name="Geheim")}
        self.readable_kbs, self.model_knowledge, self.fail_user = {"kb1"}, [], False
        self.checked = []

    def install(self, mod, monkeypatch):
        async def user(uid):
            if self.fail_user:
                raise RuntimeError("db down")
            return types.SimpleNamespace(id=uid, role="user")

        async def file(u, fid):
            self.checked.append(("file", fid))
            f = self.files.get(fid)
            return f if f and f.user_id == u.id else None

        async def kb(u, kid):
            self.checked.append(("kb", kid))
            return self.kbs[kid] if kid in self.readable_kbs else None

        async def model_knowledge(model_id):
            return self.model_knowledge

        async def base(request, override=""):
            return (override or "https://bebbi.example").rstrip("/")

        for name, fn in (("_owui_user", user), ("_owui_file", file), ("_owui_knowledge", kb), ("_owui_model_knowledge", model_knowledge),
                         ("_public_base", base)):
            monkeypatch.setattr(mod, name, fn)


def collect(mod, items, with_text=True, **kw):
    return asyncio.run(mod.collect_own_sources(None, "u1", "bebbi_deep_research.grossrat", items, with_text, **kw))


def test_readable_file_and_knowledge_base_are_collected(monkeypatch):
    mod, w = load_pipe(), World()
    w.install(mod, monkeypatch)
    own = collect(mod, [{"type": "file", "id": "f1", "name": "merkblatt.pdf", "file": {"data": {"content": "NICHT DIESER TEXT"}}},
                        {"type": "collection", "id": "kb1", "name": "vom Client genannt"}])
    f = own["files"][0]
    assert f["id"] == "f1" and f["name"] == "merkblatt.pdf" and f["text"] == LONG.strip() and f["chars"] == len(LONG.strip())
    assert f["url"] == "https://bebbi.example/api/v1/files/f1/content" and f["date"] == "2026-10-09"
    assert own["collections"] == [{"id": "kb1", "name": "Beschaffung"}]            # the real name, not the client's
    assert own["base_url"] == "https://bebbi.example" and own["notes"] == []
    assert "NICHT DIESER" not in str(own)          # the text comes from the file record, never from the browser's payload


def test_plan_step_sends_a_preview_only(monkeypatch):
    mod, w = load_pipe(), World()
    w.install(mod, monkeypatch)
    f = collect(mod, [{"type": "file", "id": "f1", "name": "x"}], with_text=False)["files"][0]
    assert len(f["text"]) == 1500 and f["chars"] == len(LONG.strip())


def test_unreadable_sources_are_rejected_and_named_without_content(monkeypatch):
    mod, w = load_pipe(), World()
    w.install(mod, monkeypatch)
    own = collect(mod, [{"type": "file", "id": "f2", "name": "fremd.pdf"}, {"type": "collection", "id": "kb2", "name": "Geheim"},
                        {"type": "file", "id": "gibt-es-nicht", "name": "x.pdf"}])
    assert own["files"] == [] and own["collections"] == []
    notes = " ".join(own["notes"])
    assert "fremd.pdf" in notes and "Geheim" in notes and "nicht lesbar" in notes and "Geheim\"" not in str(own["files"])
    assert ("kb", "kb2") in w.checked                       # every id went through the access check


def test_files_without_text_pending_files_other_attachments(monkeypatch):
    mod, w = load_pipe(), World()
    w.install(mod, monkeypatch)
    own = collect(mod, [{"type": "file", "id": "f3", "name": "scan.pdf"}, {"type": "file", "id": "f4", "name": "neu.pdf"},
                        {"type": "image", "url": "data:image/png;base64,xx"}, {"type": "url", "url": "https://a.ch", "name": "https://a.ch"},
                        "kaputt"])
    notes = own["notes"]
    assert own["files"] == [] and any("scan.pdf" in n and "keinen lesbaren Text" in n for n in notes)
    assert any("neu.pdf" in n and "noch verarbeitet" in n for n in notes) and any("https://a.ch" in n and "Typ url" in n for n in notes)
    assert len(notes) == 3                                                                 # the image is silently skipped


def test_model_knowledge_is_added_deduplicated_and_checked(monkeypatch):
    mod, w = load_pipe(), World()
    w.install(mod, monkeypatch)
    w.model_knowledge = [{"type": "collection", "id": "kb1", "name": "Beschaffung"}, {"type": "collection", "id": "kb2", "name": "Geheim"},
                         {"type": "file", "id": "f1", "name": "merkblatt.pdf"}, {"collection_name": "legacy"}]
    own = collect(mod, [{"type": "collection", "id": "kb1", "name": "Beschaffung"}])
    assert [c["id"] for c in own["collections"]] == ["kb1"] and [f["id"] for f in own["files"]] == ["f1"]
    assert any("Geheim" in n for n in own["notes"])
    assert w.checked.count(("kb", "kb1")) == 1


def test_nothing_attached_means_nothing_sent(monkeypatch):
    mod, w = load_pipe(), World()
    w.install(mod, monkeypatch)
    assert collect(mod, []) is None and collect(mod, None) is None and collect(mod, [{"type": "image"}]) is None


def test_file_limit_and_fail_closed(monkeypatch):
    mod, w = load_pipe(), World()
    for i in range(5):
        w.files[f"g{i}"] = fake_file(f"g{i}", f"g{i}.txt", "Text " * 20)
    w.install(mod, monkeypatch)
    own = collect(mod, [{"type": "file", "id": f"g{i}", "name": f"g{i}.txt"} for i in range(5)], max_files=2)
    assert len(own["files"]) == 2 and sum("höchstens 2 Dateien" in n for n in own["notes"]) == 3
    cut = collect(mod, [{"type": "file", "id": "f1", "name": "x"}], max_chars=100)["files"][0]
    assert len(cut["text"]) == 100 and cut["chars"] == len(LONG.strip())                  # the service sees the cut and reports it
    w.fail_user = True
    own = collect(mod, [{"type": "file", "id": "f1", "name": "x"}])
    assert own["files"] == [] and own["collections"] == [] and "nicht geprüft" in own["notes"][0]


def test_real_helpers_use_open_webui_functions(monkeypatch):
    """The wiring to Open WebUI v0.11.4 (names pinned in the upgrade checklist): fake modules under the real names."""
    seen = {}
    me = types.SimpleNamespace(id="u1", role="user")

    class Users:
        @staticmethod
        async def get_user_by_id(uid):
            return me

    class Files:
        @staticmethod
        async def get_file_by_id(fid):
            return types.SimpleNamespace(id=fid, user_id="owner", filename="x.pdf", meta={}, data={"content": "T"}, created_at=0)

    async def has_access_to_file(file_id, access_type, user):
        seen["file"] = (file_id, access_type, user)
        return file_id == "shared"

    class Knowledges:
        @staticmethod
        async def get_knowledge_by_id(kid):
            return types.SimpleNamespace(id=kid, name="KB") if kid in ("kb1", "kb2") else None

    async def filter_accessible_collections(names, user):
        seen["kb"] = (set(names), user)
        return {n for n in names if n == "kb1"}

    class Models:
        @staticmethod
        async def get_model_by_id(mid):
            meta = types.SimpleNamespace(model_dump=lambda: {"knowledge": [{"type": "collection", "id": "kb1"}]})
            return types.SimpleNamespace(meta=meta) if mid == "m1" else None

    class Config:
        @staticmethod
        async def get(key):
            seen["config"] = key
            return "https://bebbi.bs.ch/"

    for name, attrs in (("open_webui", {}), ("open_webui.models", {}), ("open_webui.models.users", {"Users": Users}),
                        ("open_webui.models.files", {"Files": Files}), ("open_webui.models.knowledge", {"Knowledges": Knowledges}),
                        ("open_webui.models.models", {"Models": Models}), ("open_webui.utils", {}), ("open_webui.utils.access_control", {}),
                        ("open_webui.utils.access_control.files", {"has_access_to_file": has_access_to_file}),
                        ("open_webui.retrieval", {}), ("open_webui.retrieval.utils", {"filter_accessible_collections": filter_accessible_collections}),
                        ("open_webui.config", {"Config": Config})):
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(**attrs) if attrs else types.ModuleType(name))
    mod = load_pipe()

    async def run():
        user = await mod._owui_user("u1")
        assert await mod._owui_file(user, "other") is None and seen["file"] == ("other", "read", me)
        assert (await mod._owui_file(user, "shared")).id == "shared"                  # access through a knowledge base, channel, ...
        assert await mod._owui_knowledge(user, "kb2") is None and seen["kb"] == ({"kb2"}, me)
        assert (await mod._owui_knowledge(user, "kb1")).name == "KB"
        assert await mod._owui_knowledge(user, "unknown") is None                      # admins pass the filter for any name: it must exist
        assert await mod._owui_model_knowledge("m1") == [{"type": "collection", "id": "kb1"}]
        assert await mod._public_base(None) == "https://bebbi.bs.ch" and seen["config"] == "webui.url"
        assert await mod._public_base(None, "https://override.ch/") == "https://override.ch"

    asyncio.run(run())


# -------------------------------------------------------------------------------------------- chat flow
def _pipe(mod, monkeypatch, service_answer=None):
    World().install(mod, monkeypatch)
    pipe, calls = mod.Pipe(), []
    pipe.valves.MAX_WAIT_MINUTES = 0                  # a "jetzt" job is not streamed in these tests
    pipe.valves.POLL_SECONDS = 0

    async def api(method, path, body=None):
        calls.append((method, path, body))
        if path == "/clarify":
            return {"questions": [], "assumptions": []}
        if path == "/plans":
            return {"draft_id": "d1", "plan": {"title": "T", "task_core": "k", "parts": [{"id": "P1", "title": "Teil", "goal": "g"}],
                                                  "sub_questions": [{"id": "Q1", "part": "P1", "text": "Frage?"}], "must_name": []},
                    "own_sources": {"files": [{"name": "merkblatt.pdf", "chars": 4700, "cut": False}], "collections": [{"name": "Beschaffung"}],
                                    "notes": ["Wissensbasis „Geheim“ ist für Sie nicht lesbar und wird nicht verwendet."], "mode": "prefer"}}
        if path == "/jobs":
            return {"job_id": "j1", "when": body["when"], "window": "20:00-05:00", "own_sources": {"files": [{"name": "merkblatt.pdf", "chars": 4700}],
                                                                                                   "collections": [], "notes": [], "mode": "prefer"}}
        return {"status": "running", "events": [], "report": None}

    pipe._api = api
    return pipe, calls


def test_first_turn_sends_previews_and_the_plan_lists_the_sources(monkeypatch):
    mod = load_pipe()
    pipe, calls = _pipe(mod, monkeypatch)
    files = [{"type": "file", "id": "f1", "name": "merkblatt.pdf"}, {"type": "collection", "id": "kb1", "name": "Beschaffung"}]
    out = asyncio.run(pipe.pipe({"model": "bebbi_deep_research.grossrat", "messages": [{"role": "user", "content": "Frage zum Merkblatt"}]},
                                USER, __files__=files))
    clarify, plan = calls[0], calls[1]
    assert clarify[1] == "/clarify" and clarify[2]["own_sources"]["files"][0]["name"] == "merkblatt.pdf"
    assert len(plan[2]["own_sources"]["files"][0]["text"]) == 1500 and plan[2]["own_sources"]["collections"] == [{"id": "kb1", "name": "Beschaffung"}]
    assert "*Eigene Quellen:* Datei „merkblatt.pdf“ (4'700 Zeichen); Wissensbasis „Beschaffung“. Sie werden zuerst verwendet" in out
    assert "*Hinweis:* Wissensbasis „Geheim“ ist für Sie nicht lesbar" in out


def test_approval_sends_the_whole_text_and_night_message_names_the_sources(monkeypatch):
    mod = load_pipe()
    pipe, calls = _pipe(mod, monkeypatch)
    out = asyncio.run(pipe.pipe(_plan_history(mod, "heute Nacht"), USER, __files__=[{"type": "file", "id": "f1", "name": "merkblatt.pdf"}],
                                __chat_id__="c1", __message_id__="m1"))
    job = next(c for c in calls if c[1] == "/jobs")[2]
    assert job["own_sources"]["files"][0]["text"] == LONG.strip() and job["own_sources"]["base_url"] == "https://bebbi.example"
    assert "Nachtfenster" in out and "*Eigene Quellen:* Datei „merkblatt.pdf“" in out


def test_without_attachments_the_requests_carry_no_own_sources(monkeypatch):
    mod = load_pipe()
    pipe, calls = _pipe(mod, monkeypatch)
    asyncio.run(pipe.pipe({"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}, USER))
    assert all("own_sources" not in (c[2] or {}) for c in calls)


def test_valve_switches_own_sources_off(monkeypatch):
    mod = load_pipe()
    pipe, calls = _pipe(mod, monkeypatch)
    pipe.valves.OWN_SOURCES = False
    asyncio.run(pipe.pipe({"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}, USER,
                          __files__=[{"type": "file", "id": "f1", "name": "x"}]))
    assert all("own_sources" not in (c[2] or {}) for c in calls)


def test_the_users_own_words_are_used_when_open_webui_rewrote_the_message(monkeypatch):
    """With file_context on, Open WebUI wraps the question in a retrieval template and prepends <attached_files>."""
    mod = load_pipe()
    pipe, calls = _pipe(mod, monkeypatch)
    rewritten = "<attached_files>\n<file type=\"file\" id=\"f1\"/>\n</attached_files>\n\n### Task: respond using <context>...Frist 28 Tage...</context>\nQuery: ok jetzt"
    body = _plan_history(mod, rewritten)
    asyncio.run(pipe.pipe(body, USER, __metadata__={"user_message": {"content": "ok"}}, __chat_id__="c", __message_id__="m"))
    assert next(c for c in calls if c[1] == "/jobs")[2]["when"] == "now"                  # "ok" was understood, not the template
    # without the metadata the <attached_files> block alone is removed, so the approval word is still found
    calls.clear()
    asyncio.run(pipe.pipe(_plan_history(mod, "<attached_files>\n<file type=\"file\" id=\"f1\"/>\n</attached_files>\n\nok"), USER,
                          __chat_id__="c", __message_id__="m"))
    assert any(c[1] == "/jobs" for c in calls)


def test_refusal_of_the_service_reaches_the_user_as_it_is(monkeypatch):
    mod = load_pipe()
    pipe, _ = _pipe(mod, monkeypatch)

    async def refuse(method, path, body=None):
        raise mod.UserError("Dieses Modell arbeitet nur mit eigenen Quellen. Bitte laden Sie eine Datei hoch.")

    pipe._api = refuse
    out = asyncio.run(pipe.pipe({"model": "x.dokumente", "messages": [{"role": "user", "content": "Frage"}]}, USER))
    assert out == "Dieses Modell arbeitet nur mit eigenen Quellen. Bitte laden Sie eine Datei hoch."


def test_http_422_becomes_a_user_error(monkeypatch):
    import httpx

    mod = load_pipe()
    pipe = mod.Pipe()
    transport = httpx.MockTransport(lambda request: httpx.Response(422, json={"detail": "nur eigene Quellen"}))
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    out = asyncio.run(pipe.pipe({"model": "x.dokumente", "messages": [{"role": "user", "content": "Frage"}]}, USER))
    assert out == "nur eigene Quellen"

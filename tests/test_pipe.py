import asyncio
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_pipe():
    spec = importlib.util.spec_from_file_location("pipe_mod", ROOT / "pipe/deep_research.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_background_tasks_do_not_start_a_research():
    """Open WebUI sends title/tags/follow-up generation to the chat model; the pipe must answer them locally."""
    mod = load_pipe()
    pipe = mod.Pipe()

    async def boom(*a, **k):
        raise AssertionError("service must not be called for background tasks")

    pipe._api = boom
    marker = mod._marker({"state": "clarify", "task": "Stadtklima im Grossen Rat?", "depth": "quick"})
    body = {"messages": [{"role": "user", "content": "### Chat History:\n<chat_history>\nASSISTANT: Fragen" + marker + "\n</chat_history>"}]}
    title = asyncio.run(pipe.pipe(body, {"id": "u"}, __task__="title_generation"))
    assert json.loads(title) == {"title": "Stadtklima im Grossen Rat?"}
    assert json.loads(asyncio.run(pipe.pipe(body, {"id": "u"}, __task__="tags_generation")))["tags"]
    assert json.loads(asyncio.run(pipe.pipe(body, {"id": "u"}, __task__="follow_up_generation"))) == {"follow_ups": []}
    assert json.loads(asyncio.run(pipe.pipe({"messages": []}, {"id": "u"}, __task__="title_generation")))["title"] == "Deep Research"


def _pipe_with_service(mod, jobs: dict):
    pipe = mod.Pipe()
    pipe.valves.POLL_SECONDS = 0
    calls = []

    async def fake_api(method, path, body=None):
        calls.append((method, path, body))
        if path == "/jobs" and method == "POST":
            return {"job_id": "j1", "when": body["when"], "window": "20:00-05:00"}
        return jobs["j1"]

    pipe._api = fake_api
    return pipe, calls


def _plan_history(mod, text: str, depth: str = "quick"):
    return {"model": "bebbi_deep_research.grossrat", "messages": [
        {"role": "user", "content": "Frage"},
        {"role": "assistant", "content": "Plan" + mod._marker({"state": "plan", "draft": "d1", "depth": depth})},
        {"role": "user", "content": text}]}


def test_finished_report_carries_no_state_marker():
    """A report returned by the pipe must not keep state=job, otherwise the next message returns the report again."""
    mod = load_pipe()
    pipe, calls = _pipe_with_service(mod, {"j1": {"status": "done", "events": [], "report": "# Bericht\n\n## Quellen\n[1] x https://a.ch"}})
    out = asyncio.run(pipe.pipe(_plan_history(mod, "ok jetzt"), {"id": "u"}, __chat_id__="c1", __message_id__="m1"))
    assert out.startswith("# Bericht") and "research-agent" not in out
    post = next(c for c in calls if c[0] == "POST")
    assert post[2]["chat_id"] == "c1" and post[2]["message_id"] == "m1" and post[2]["when"] == "now"


def test_running_job_keeps_marker_and_night_run_is_queued():
    mod = load_pipe()
    pipe, _ = _pipe_with_service(mod, {"j1": {"status": "running", "events": [{"text": "suche"}], "report": None}})
    out = asyncio.run(pipe.pipe(_plan_history(mod, "heute Nacht"), {"id": "u"}, __chat_id__="c1", __message_id__="m1"))
    assert "Nachtfenster" in out and mod._state([{"role": "assistant", "content": out}, {}]) == {"state": "job", "job": "j1"}
    assert "<!--" not in out
    follow_up = {"model": "x.grossrat", "messages": [{"role": "assistant", "content": out}, {"role": "user", "content": "und?"}]}
    again = asyncio.run(pipe.pipe(follow_up, {"id": "u"}))
    assert "läuft noch" in again and mod._state([{"role": "assistant", "content": again}, {}])["job"] == "j1"


def test_title_from_plan():
    mod = load_pipe()
    body = {"messages": [{"role": "user", "content": "<chat_history>\nASSISTANT: **Rechercheplan: Fasnacht im Grossen Rat**\n\n- x\n</chat_history>"}]}
    out = asyncio.run(mod.Pipe().pipe(body, {"id": "u"}, __task__="title_generation"))
    assert json.loads(out)["title"] == "Fasnacht im Grossen Rat"


def test_plan_markdown_separates_parts_and_lists():
    mod = load_pipe()
    plan = {"title": "T", "task_core": "c", "parts": [{"id": "a", "title": "A", "goal": "ga"}, {"id": "b", "title": "B", "goal": "gb"}],
            "sub_questions": [{"part": "a", "text": "q1"}, {"part": "b", "text": "q2"}], "must_name": []}
    text = mod._render_plan(plan, "quick")
    assert "- q1\n\n**B**: gb\n\n- q2" in text


def test_queued_message_is_sent_as_replace_event_with_empty_output():
    """The delivery can only change `content`; Open WebUI shows `output` first, so the queued text must not become output."""
    mod = load_pipe()
    pipe, _ = _pipe_with_service(mod, {"j1": {"status": "queued", "events": [], "report": None}})
    events = []

    async def emit(ev):
        events.append(ev)

    out = asyncio.run(pipe.pipe(_plan_history(mod, "heute Nacht"), {"id": "u"}, __event_emitter__=emit, __chat_id__="c", __message_id__="m"))
    assert out == ""
    replace = [e for e in events if e["type"] == "replace"]
    assert len(replace) == 1 and "Nachtfenster" in replace[0]["data"]["content"]
    assert mod._state([{"role": "assistant", "content": replace[0]["data"]["content"]}, {}])["job"] == "j1"


DONE = {"status": "done", "events": [], "report": "# B\n\nText [1][2].\n\n## Quellen\n\n[1] A. https://a.ch (abgerufen am 2026-10-09)\n[2] B. https://b.ch (abgerufen am 2026-10-09)",
        "ui_report": "# B\n\nText [1][2].\n\n## Quellen\n\n1. A. https://a.ch\n2. B. https://b.ch", "docx": "/jobs/j1/report.docx", "docx_name": "Bebbi-Deep-Research_B_2026-10-09.docx",
        "owui_events": [{"type": "source", "data": {"source": {"name": "A", "url": "https://a.ch"}, "document": ["a"], "metadata": [{"source": "https://a.ch", "name": "A"}]}},
                        {"type": "source", "data": {"source": {"name": "B", "url": "https://b.ch"}, "document": ["b"], "metadata": [{"source": "https://b.ch", "name": "B"}]}}]}


def _run_done(mod, text="ok jetzt", emitter=True, store=None, request=object(), user=None):
    pipe, calls = _pipe_with_service(mod, {"j1": DONE})
    events = []

    async def emit(ev):
        events.append(ev)

    async def download(path):
        calls.append(("GET", path, None))
        return b"PK-docx"

    stored = []

    async def fake_store(req, user_id, filename, data, content_type):
        stored.append((user_id, filename, data, content_type))
        return {"type": "file", "id": "f1", "url": "f1", "name": filename, "size": len(data)}

    pipe._download = download
    mod.store_user_file = store or fake_store
    out = asyncio.run(pipe.pipe(_plan_history(mod, text), user or {"id": "u"}, __event_emitter__=emit if emitter else None,
                                __chat_id__="c1", __message_id__="m1", __request__=request))
    return out, events, stored, calls


def test_report_gets_one_source_event_per_source_in_order_and_the_word_file():
    """Open WebUI shows [n] as the badge of the n-th source event and lists the sources under the message."""
    mod = load_pipe()
    out, events, stored, _ = _run_done(mod)
    assert out == DONE["ui_report"] and "\u200b" not in out
    sources = [e for e in events if e["type"] == "source"]
    assert sources == DONE["owui_events"]
    files = [e for e in events if e["type"] == "files"]
    assert files == [{"type": "files", "data": {"files": [{"type": "file", "id": "f1", "url": "f1", "name": DONE["docx_name"], "size": 7}]}}]
    assert stored == [("u", DONE["docx_name"], b"PK-docx", mod.DOCX)]
    assert events.index(sources[-1]) < events.index(files[0])          # sources first, the file after the text is ready


def test_sources_are_not_emitted_again_when_the_service_delivered_them():
    """The service writes the report with the admin key as well (delivery ok): then the sources are on the message already."""
    mod = load_pipe()
    seen = {"n": 0}
    done = {**DONE, "id": "j1", "delivery": None}

    pipe, _ = _pipe_with_service(mod, {"j1": done})
    real_api = pipe._api

    async def api(method, path, body=None):
        seen["n"] += 1
        return await real_api(method, path, body) if seen["n"] < 3 else {**done, "delivery": "ok"}

    pipe._api = api
    pipe.valves.POLL_SECONDS = 0
    events = []

    async def emit(ev):
        events.append(ev)

    async def download(path):
        return b"x"

    async def store(*a, **k):
        return {"type": "file", "id": "f", "name": "n"}

    pipe._download, mod.store_user_file = download, store
    out = asyncio.run(pipe._publish(done, emit, {"id": "u"}, object()))
    assert out == DONE["ui_report"] and [e["type"] for e in events] == ["files"]      # waited for the delivery, kept only the file


def test_failed_word_file_does_not_lose_the_report():
    mod = load_pipe()

    async def broken(*a, **k):
        raise RuntimeError("storage down")

    out, events, _, _ = _run_done(mod, store=broken)
    assert out.startswith(DONE["ui_report"]) and "Word-Datei konnte nicht angehängt werden" in out
    assert len([e for e in events if e["type"] == "source"]) == 2 and not [e for e in events if e["type"] == "files"]


def test_without_event_emitter_or_request_only_the_text_is_returned():
    mod = load_pipe()
    out, events, stored, calls = _run_done(mod, emitter=False)
    assert out == DONE["ui_report"] and not events and not stored
    out, events, stored, _ = _run_done(mod, request=None)
    assert out == DONE["ui_report"] and len([e for e in events if e["type"] == "source"]) == 2 and not stored       # sources yes, file needs the in-process upload


def test_finished_job_reply_in_a_later_message_has_sources_and_file_too():
    mod = load_pipe()
    pipe, _ = _pipe_with_service(mod, {"j1": DONE})
    events = []

    async def emit(ev):
        events.append(ev)

    async def download(path):
        return b"x"

    async def store(*a, **k):
        return {"type": "file", "id": "f", "name": "n"}

    pipe._download, mod.store_user_file = download, store
    body = {"model": "x.grossrat", "messages": [{"role": "assistant", "content": "queued" + mod._marker({"state": "job", "job": "j1"})},
                                                {"role": "user", "content": "fertig?"}]}
    out = asyncio.run(pipe.pipe(body, {"id": "u"}, __event_emitter__=emit, __request__=object()))
    assert out == DONE["ui_report"] and [e["type"] for e in events] == ["source", "source", "files"]


def test_download_helper_is_identical_in_pipe_and_action():
    import inspect
    action = importlib.util.module_from_spec(spec := importlib.util.spec_from_file_location("action_mod", ROOT / "pipe/deep_research_download.py"))
    spec.loader.exec_module(action)
    assert inspect.getsource(load_pipe().store_user_file).splitlines()[1:] == inspect.getsource(action.store_user_file).splitlines()[1:]


def test_store_user_file_runs_open_webui_upload_as_the_user(monkeypatch):
    """The upload goes through Open WebUI's own handler with the chat user, never with the service's admin key."""
    import sys
    import types

    seen = {}

    async def upload_file_handler(request, file, metadata, process, process_in_background, user):
        seen.update(request=request, name=file.filename, ctype=file.content_type, data=file.file.read(), process=process, user=user)
        return types.SimpleNamespace(id="file-1")

    class Users:
        @staticmethod
        async def get_user_by_id(uid):
            return {"id": uid}

    for name, attrs in (("open_webui", {}), ("open_webui.models", {}), ("open_webui.models.users", {"Users": Users}),
                        ("open_webui.routers", {}), ("open_webui.routers.files", {"upload_file_handler": upload_file_handler})):
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(**attrs) if attrs else types.ModuleType(name))
    mod = load_pipe()
    item = asyncio.run(mod.store_user_file("REQ", "user-7", "b.docx", b"abc", mod.DOCX))
    assert seen == {"request": "REQ", "name": "b.docx", "ctype": mod.DOCX, "data": b"abc", "process": False, "user": {"id": "user-7"}}
    assert item["type"] == "file" and item["id"] == item["url"] == "file-1" and item["size"] == 3


def test_service_errors_reach_the_user_in_german():
    import httpx

    mod = load_pipe()
    pipe = mod.Pipe()
    pipe.valves.SERVICE_URL = "http://service.invalid:1"
    out = asyncio.run(pipe.pipe({"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}, {"id": "u"}))
    assert out.startswith("Deep Research ist nicht verfügbar: Der Recherche-Dienst ist nicht erreichbar")

    def handler(request):
        return httpx.Response(502, json={"detail": "Das Sprachmodell ist nicht erreichbar."})

    real = httpx.AsyncClient
    mod.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        out = asyncio.run(pipe.pipe({"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}, {"id": "u"}))
    finally:
        mod.httpx.AsyncClient = real
    assert out == "Deep Research ist nicht verfügbar: Das Sprachmodell ist nicht erreichbar."

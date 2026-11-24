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
    assert (
        "läuft noch" in again and mod._state([{"role": "assistant", "content": again}, {"role": "user"}])["job"] == "j1"
    )


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
    assert "Nachtfenster" in queued and mod._state([{"role": "assistant", "content": queued}, {"role": "user"}]) == {
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
        if path.startswith("/options"):  # a service without choices: the one-question preset card
            raise mod.NotFound("no choices configured")
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


# ------------------------------------------- the card: one question per source, files, knowledge, style, depth
OPTIONS = {
    "sources": [
        {"id": "web", "label": "Websuche", "description": "Öffentliche Webseiten."},
        {"id": "grossrat", "label": "Grosser Rat Basel-Stadt", "description": "Geschäfte seit 1973."},
        {"id": "data-bs", "label": "Open Data Basel-Stadt", "description": "Datensätze."},
    ],
    "styles": [
        {"id": "kompakt", "label": "Kurze Verwaltungsantwort", "description": "Plan: Checkliste. Bericht: kurz."},
        {"id": "analyse", "label": "Ausführliche Analyse", "description": "Plan: mehrere Teile. Bericht: lang."},
    ],
    "depths": [
        {"id": "quick", "focus_words": 600, "focus_calls": 8, "rounds": 1},
        {"id": "standard", "focus_words": 900, "focus_calls": 14, "rounds": 2},
        {"id": "deep", "focus_words": 1300, "focus_calls": 20, "rounds": 3},
    ],
    "selected": {"web": True, "mcp": ["grossrat"], "own": "prefer", "style": "kompakt"},
}


def _steps_pipe(mod, answers: list, questions=(), own=None):
    """Model "Deep Research" with a service that offers choices; the fake browser answers the cards in order."""
    pipe, calls, asked, events, call, emit = _card_pipe(mod, answers)
    plan_api = pipe._api

    async def api(method, path, body=None):
        if path == "/clarify":
            calls.append((method, path, body))
            return {"questions": list(questions), "assumptions": [], "preset": "grossrat"}
        if path.startswith("/options/resolve"):
            calls.append((method, path, body))
            plain = {k: v for k, v in body.items() if k != "own_attached"}
            suggested = {"web": True, "mcp": ["grossrat"], "own": "prefer", "style": "kompakt"}
            name = "grossrat" if plain == suggested else "mix:x"
            return {"preset": name, "label": "Quellen: Grosser Rat Basel-Stadt · Bericht: Kurze Verwaltungsantwort"}
        if path.startswith("/options"):
            calls.append((method, path, body))
            return OPTIONS
        return await plan_api(method, path, body)

    async def collect(*a, **k):
        return own

    pipe._api = api
    pipe._collect_own = collect
    return pipe, calls, asked, events, call, emit


def _answers(card: dict, pick: dict) -> dict:
    """Answer a card: `pick` maps question id -> option label (default: the first option, the recommendation)."""
    out = {}
    for q in card["data"]["questions"]:
        labels = [o["label"] for o in q["options"]]
        i = labels.index(pick[q["id"]]) if q["id"] in pick else 0
        out[q["id"]] = {"type": "option", "option_index": i, "label": labels[i]}
    return {"status": "answered", "answers": out}


def _browser(picks: list, rest: list = ()):
    """Answers source cards with `picks` (one dict per card shown), then the rest (plan card, dialogs) in order."""
    queue = list(picks)
    after = list(rest)

    async def call(event, seen):
        seen.append(event)
        if event["type"] == "request:user_input" and event["data"]["questions"][0]["id"].startswith("src:") and queue:
            return _answers(event, queue.pop(0))
        return after.pop(0)

    return call


def _run(mod, picks, rest=(), own=None, questions=()):
    pipe, calls, asked, events, _call, emit = _steps_pipe(mod, [], questions=questions, own=own)
    browser = _browser(picks, rest)

    async def call(event):
        return await browser(event, asked)

    out = asyncio.run(pipe.pipe(AUTO_BODY, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    return out, calls, asked, events


def test_the_card_asks_each_data_source_then_web_files_knowledge_style_and_depth():
    mod = load_pipe()
    own = {"files": [{"name": "Bericht.pdf"}], "collections": [{"name": "Richtlinien"}], "notes": []}
    pick = {
        "src:grossrat": "Ja",
        "src:data-bs": "Ja",
        "src:web": "Nein",
        "style": "Ausführliche Analyse",
        "depth": "Gründlich",
    }
    out, calls, asked, events = _run(mod, [pick], [_pick(2)], own=own)
    card = asked[0]
    assert card["type"] == "request:user_input"
    qs = card["data"]["questions"]
    assert [q["id"] for q in qs] == ["src:grossrat", "src:data-bs", "src:web", "files", "kb", "style", "depth"]
    # the recommendation comes first (the card marks it): the suggested sources yes, the others no
    assert [o["label"] for o in qs[0]["options"]] == ["Ja", "Nein"] and [o["label"] for o in qs[1]["options"]] == [
        "Nein",
        "Ja",
    ]
    assert qs[0]["header"] == "Grosser Rat Basel-Stadt" and "Geschäfte seit 1973" in qs[0]["question"]
    assert "Bericht.pdf" in qs[3]["question"] and "«Richtlinien»" in qs[4]["question"]
    assert [o["label"] for o in qs[6]["options"]] == ["Standard", "Schnell", "Gründlich"]  # the default depth first
    assert "Schnell: 1 Recherche-Runde, bis 8 Abfragen, Kurzantwort ca. 600 Wörter." in qs[6]["question"]
    assert "Kurze Verwaltungsantwort: Plan: Checkliste" in qs[5]["question"]  # option texts are cut to one line
    resolve = next(c for c in calls if c[1] == "/options/resolve")[2]
    assert resolve == {
        "web": False,
        "mcp": ["data-bs", "grossrat"],
        "own": "prefer",
        "style": "analyse",
        "own_attached": True,
    }
    plan = next(c for c in calls if c[1] == "/plans")
    assert plan[2]["preset"] == "mix:x" and plan[2]["depth"] == "deep"
    assert plan[2]["own_sources"]["files"] == [{"name": "Bericht.pdf"}]
    assert "Quellen: Grosser Rat Basel-Stadt" in out


def test_switched_off_files_or_knowledge_are_not_sent_and_documents_only_is_derived():
    mod = load_pipe()
    own = {"files": [{"name": "Bericht.pdf"}], "collections": [{"name": "Richtlinien"}], "notes": []}
    pick = {"src:grossrat": "Nein", "src:data-bs": "Nein", "src:web": "Nein", "kb": "Nein"}
    out, calls, asked, events = _run(mod, [pick], [_pick(0)], own=own)
    resolve = next(c for c in calls if c[1] == "/options/resolve")[2]
    assert resolve["own"] == "only" and resolve["web"] is False and resolve["mcp"] == []
    plan = next(c for c in calls if c[1] == "/plans")[2]
    assert plan["own_sources"]["collections"] == [] and plan["own_sources"]["files"]
    job = next(c for c in calls if c[1] == "/jobs")[2]  # the submit collects the texts again: same filter
    assert job["own_sources"]["collections"] == []


def test_without_attachments_the_card_recommends_no_and_explains_how_to_attach():
    mod = load_pipe()
    out, calls, asked, events = _run(mod, [{}], [_pick(2)])
    qs = {q["id"]: q for q in asked[0]["data"]["questions"]}
    assert [o["label"] for o in qs["files"]["options"]] == ["Nein", "Ja"] and "keine Dateien" in qs["files"]["question"]
    assert "«+»" in qs["files"]["question"] and "«#»" in qs["kb"]["question"]
    resolve = next(c for c in calls if c[1] == "/options/resolve")[2]
    assert resolve["own"] == "prefer" and resolve["own_attached"] is False  # nothing attached: the default mode
    assert next(c for c in calls if c[1] == "/plans")[2]["preset"] == "grossrat"


def test_wanting_files_without_attaching_any_ends_with_how_to_attach():
    mod = load_pipe()
    out, calls, asked, events = _run(mod, [{"files": "Ja"}])
    assert "Es wurde keine Recherche gestartet" in out and "«+»" in out
    assert not any(c[1] in ("/plans", "/options/resolve") for c in calls)


def test_no_source_asks_again_and_no_answer_starts_nothing():
    mod = load_pipe()
    none = {"src:grossrat": "Nein", "src:data-bs": "Nein", "src:web": "Nein"}
    out, calls, asked, events = _run(mod, [none], [{"status": "cancelled"}])
    assert len(asked) == 2 and asked[1]["data"]["questions"][0]["question"].startswith("Bitte wählen Sie mindestens")
    assert "Keine Auswahl" in out and not any(c[1] in ("/plans", "/options/resolve") for c in calls)


def test_the_selection_survives_the_clarifying_questions():
    mod = load_pipe()
    own = {"files": [{"name": "a.pdf"}], "collections": [{"name": "KB"}], "notes": []}
    pick = {"src:web": "Nein", "kb": "Nein", "depth": "Schnell"}
    first, calls, asked, events = _run(mod, [pick], own=own, questions=["Zeitraum?"])
    assert "Zeitraum?" in first
    body = {
        "model": "bebbi_deep_research.auto",
        "messages": [
            *AUTO_BODY["messages"],
            {"role": "assistant", "content": first},
            {"role": "user", "content": "ab 2020"},
        ],
    }
    pipe2, calls2, asked2, events2, call2, emit2 = _steps_pipe(mod, [_pick(2)], own=own)
    out = asyncio.run(pipe2.pipe(body, {"id": "u"}, emit2, "c1", "m2", __event_call__=call2))
    plan = next(c for c in calls2 if c[1] == "/plans")
    assert plan[2]["preset"] == "mix:x" and plan[2]["depth"] == "quick"
    assert plan[2]["own_sources"]["collections"] == []  # the knowledge base stays off after the questions
    assert not any(c[1].startswith("/options") or c[1] == "/clarify" for c in calls2)
    assert "Quellen: Grosser Rat" in out


def test_the_status_trail_shows_the_users_decisions():
    mod = load_pipe()
    out, calls, asked, events = _run(mod, [{"depth": "Gründlich"}], [_pick(1), "nur Basel-Stadt", _pick(0)])
    trail = [e["data"]["description"] for e in events if e["type"] == "status"]
    assert "Quellen: Grosser Rat Basel-Stadt · Bericht: Kurze Verwaltungsantwort · Tiefe: Gründlich" in trail
    assert trail.index("Plan freigegeben") > trail.index("Änderungswunsch: nur Basel-Stadt")
    assert mod._decision_line("Recht", "quick") == "Recherche: Recht · Tiefe: Schnell"


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


def _hidden_tool_results(name: str = "list_attached_knowledge", call_id: str = "fc_1", content=""):
    """What Open WebUI 0.13.1 appends after the user's message with native function calling: memory recall, the skills
    list and the attached knowledge list as an assistant message with a tool call and its tool result."""
    return [
        {
            "role": "assistant",
            "content": content,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": '<attached_knowledge>\n<knowledge type="collection" id="k1"/>\n</attached_knowledge>',
        },
    ]


def _held_history(mod, text: str = "und?", *, marker_in_request: bool):
    """A chat whose last answer is a queued job, with hidden tool results around every turn. Open WebUI rebuilds the
    history from the stored `output` of each assistant message: an answer that the pipe returned has its text in the
    request (`marker_in_request`); a queued message that went out as a `replace` event has only the hidden tool results
    in its `output`, so the text with the marker is not in the request at all (0.13.3, `process_messages_with_output`)."""
    held = [{"role": "assistant", "content": "Eingereiht" + mod._marker({"state": "job", "job": "j1"})}]
    return {
        "model": "bebbi_deep_research.grossrat",
        "messages": [
            {"role": "user", "content": "Frage"},
            *_hidden_tool_results(call_id="fc_0"),
            *(held if marker_in_request else []),
            {"role": "user", "content": text},
            *_hidden_tool_results(call_id="fc_1"),
        ],
    }


def test_hidden_tool_results_after_the_user_message_do_not_hide_the_state():
    """0.13.1 puts hidden tool results after the user message, so a tool message is the last message the pipe sees. The
    queued job must still be reported (the state is read from the answer before the user's message)."""
    mod = load_pipe()
    pipe, calls = _pipe_with_service(mod, {"j1": {"status": "running", "events": [{"text": "suche"}], "report": None}})
    out = asyncio.run(pipe.pipe(_held_history(mod, marker_in_request=True), {"id": "u"}))
    assert "läuft noch" in out and [c[:2] for c in calls] == [("GET", "/jobs/j1?user_id=u")]


def test_a_queued_message_stored_with_hidden_tool_results_only_is_read_from_the_chat(monkeypatch):
    """The message that queued the job is stored with `output` = hidden tool results (the text went out as a `replace`
    event): Open WebUI rebuilds the history from `output`, the marker is gone from the request, and the older clarify
    answer must not count as the state. The next turn reads the queued message from the stored chat (found with the
    review of the 0.13.3 pipe: 'abbrechen' was read as the answer to the clarifying questions)."""
    mod = load_pipe()
    pipe, calls = _pipe_with_service(mod, {"j1": {"status": "running", "events": [], "report": None}})
    older = {
        "role": "assistant",
        "content": "Fragen" + mod._marker({"state": "clarify", "task": "T", "depth": "quick"}),
    }
    body = {
        "model": "bebbi_deep_research.grossrat",
        "messages": [
            {"role": "user", "content": "Frage"},
            *_hidden_tool_results(call_id="a"),
            older,
            {"role": "user", "content": "weiter"},
            *_hidden_tool_results(call_id="b"),  # the queued message: hidden tool results only
            {"role": "user", "content": "abbrechen"},
            *_hidden_tool_results(call_id="c"),
        ],
    }
    asked = []

    async def stored(chat_id, metadata):
        asked.append(metadata["user_message"]["parentId"])
        return True, {"role": "assistant", "content": "Eingereiht" + mod._marker({"state": "job", "job": "j1"})}

    monkeypatch.setattr(mod, "_stored_parent", stored)
    meta = {"user_message": {"id": "u3", "parentId": "a2", "content": "abbrechen"}}
    out = asyncio.run(pipe.pipe(body, {"id": "u"}, __chat_id__="c1", __metadata__=meta))
    assert asked == ["a2"] and out == mod.CANCELLED and calls[0][:2] == ("POST", "/jobs/j1/cancel?user_id=u")


def test_hold_returns_the_text_when_hidden_tool_results_fill_the_output():
    """`output` renders in preference to `content`. With hidden tool results in it, a `replace`-only queue message would
    be an empty bubble (checked in the browser on 0.13.3), so the text is the answer then; without them it stays a
    `replace` event."""
    mod = load_pipe()

    def run(history):
        pipe, _ = _pipe_with_service(mod, {"j1": {"status": "running", "events": [], "report": None}})
        events = []

        async def emit(e):
            events.append(e)

        async def api(method, path, body=None):
            from_service = mod.UserError("kaputt")
            raise from_service

        pipe._api = api
        out = asyncio.run(pipe.pipe(history, {"id": "u"}, __event_emitter__=emit))
        return out, [e["type"] for e in events]

    out, types = run(_held_history(mod, marker_in_request=True))
    assert "kaputt" in out and "research-agent" in out and "replace" not in types
    out, types = run(_job_history(mod))
    assert out == "" and "replace" in types


def test_text_of_the_turn_is_the_user_message_not_the_tool_result():
    mod = load_pipe()
    pipe, calls = _pipe_with_service(mod, {"j1": {"status": "running", "events": [], "report": None}})
    out = asyncio.run(pipe.pipe(_held_history(mod, "abbrechen", marker_in_request=True), {"id": "u"}))
    assert out == mod.CANCELLED and calls[0][:2] == ("POST", "/jobs/j1/cancel?user_id=u")
    # a new task: the clarify request carries the user's question, not the knowledge list
    sent = []

    async def api(method, path, body=None):
        sent.append((path, body))
        return {"questions": [], "assumptions": [], "preset": "grossrat"}

    pipe2 = mod.Pipe()
    pipe2._api = api
    messages = [{"role": "user", "content": "Frage zum Stadtklima"}, *_hidden_tool_results()]
    asyncio.run(pipe2.pipe({"model": "bebbi_deep_research.grossrat", "messages": messages}, {"id": "u"}))
    assert sent[0][0] == "/clarify" and sent[0][1]["task"] == "Frage zum Stadtklima"


def test_no_user_message_is_no_turn():
    mod = load_pipe()
    only_assistant = [{"role": "assistant", "content": "x" + mod._marker({"state": "job", "job": "j1"})}]
    assert mod._last_user(only_assistant) == -1 and mod._last_user([]) == -1
    assert mod._state(only_assistant) == {}  # not "the last message is the turn"


def test_tool_call_messages_without_text_are_no_answers():
    """Whitespace or an empty list as the content of a tool-call message: still only a hidden tool result."""
    mod = load_pipe()
    answer = {"role": "assistant", "content": "Eingereiht" + mod._marker({"state": "job", "job": "j1"})}
    for empty in ("", "  \n", [], None):
        history = [
            {"role": "user", "content": "Frage"},
            answer,
            {"role": "user", "content": "und?"},
            *_hidden_tool_results(content=empty),
        ]
        assert mod._state(history) == {"state": "job", "job": "j1"}
        assert mod._tool_only(_hidden_tool_results(content=empty)[0])


def test_list_content_of_an_answer_carries_the_marker():
    mod = load_pipe()
    marker = mod._marker({"state": "job", "job": "j1"})
    history = [
        {"role": "user", "content": "Frage"},
        {"role": "assistant", "content": [{"type": "text", "text": "Eingereiht" + marker}]},
        {"role": "user", "content": [{"type": "text", "text": "und?"}]},
    ]
    assert mod._state(history) == {"state": "job", "job": "j1"}
    assert mod._text(history[-1]["content"]) == "und?"


def test_guided_regeneration_is_not_the_turn():
    """Guided regeneration (0.13) appends the reply to revise and the instruction as a user message after the turn. The
    turn is still the user message before; the regenerated reply's state is not the chat's state."""
    mod = load_pipe()
    clarify = {
        "role": "assistant",
        "content": "Fragen" + mod._marker({"state": "clarify", "task": "T", "depth": "quick"}),
    }
    job = {"role": "assistant", "content": "Eingereiht" + mod._marker({"state": "job", "job": "j1"})}
    history = [
        {"role": "user", "content": "Frage"},
        clarify,
        {"role": "user", "content": "weiter"},
        job,  # the reply to regenerate
        {"role": "user", "content": "Mach es kürzer"},  # the instruction
    ]
    meta = {"regeneration_message_id": "a2"}
    kept = mod._drop_regeneration(history, meta)
    assert kept[-1]["content"] == "weiter" and mod._state(kept)["state"] == "clarify"
    assert mod._drop_regeneration(history, {}) is history  # an ordinary request is untouched
    sent = []

    async def api(method, path, body=None):
        sent.append((path, body))
        return {"draft_id": "d1", "plan": {"title": "t", "parts": []}}

    pipe = mod.Pipe()
    pipe._api = api

    async def card(*a, **k):
        return "open", None

    pipe._decide = card
    asyncio.run(
        pipe.pipe({"model": "bebbi_deep_research.grossrat", "messages": history}, {"id": "u"}, __metadata__=meta)
    )
    assert sent and sent[0][0] == "/plans" and "weiter" in sent[0][1]["task"] and "kürzer" not in sent[0][1]["task"]


def test_guided_regeneration_with_hidden_tool_results_is_not_the_turn():
    """Open WebUI 0.13.3 appends the regeneration instruction first and the hidden tool results after it
    (middleware.py), so the request ends with a tool message. The instruction is still not the turn: regenerating the
    clarifying questions with "Mach es kürzer" must not take the instruction as the answers."""
    mod = load_pipe()
    clarify = {
        "role": "assistant",
        "content": "Fragen" + mod._marker({"state": "clarify", "task": "T", "depth": "quick"}),
    }
    history = [
        {"role": "user", "content": "Frage"},
        clarify,  # the reply to regenerate
        {"role": "user", "content": "Mach es kürzer"},  # the instruction
        *_hidden_tool_results(),
    ]
    kept = mod._drop_regeneration(history, {"regeneration_message_id": "a1"})
    assert [m.get("content") for m in kept] == ["Frage"]
    assert mod._previous(kept) is None  # the regenerated reply's clarify state is not the chat's state


def test_a_failed_turn_does_not_lose_the_state():
    """A turn that failed before an answer was stored leaves two user messages in a row; the job before them is still
    the chat's state (the pipe reports it instead of starting a new research)."""
    mod = load_pipe()
    pipe, calls = _pipe_with_service(mod, {"j1": {"status": "running", "events": [], "report": None}})
    history = {
        "model": "bebbi_deep_research.grossrat",
        "messages": [
            {"role": "user", "content": "Frage"},
            {"role": "assistant", "content": "Eingereiht" + mod._marker({"state": "job", "job": "j1"})},
            {"role": "user", "content": "Ist er fertig?"},  # no answer stored
            {"role": "user", "content": "Ist der Bericht schon fertig?"},
            *_hidden_tool_results(),
        ],
    }
    out = asyncio.run(pipe.pipe(history, {"id": "u"}))
    assert "läuft noch" in out and [c[:2] for c in calls] == [("GET", "/jobs/j1?user_id=u")]


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
        return True, {"role": "assistant", "content": "Eingereiht" + mod._marker({"state": "job", "job": "j1"})}

    monkeypatch.setattr(mod, "_stored_parent", stored)
    meta = {"user_message": {"id": "u2", "parentId": "a1", "content": "fertig?"}}
    out = asyncio.run(pipe.pipe(_compacted_history(), {"id": "u"}, __chat_id__="c1", __metadata__=meta))
    assert asked == [("c1", "a1")]
    assert "läuft noch" in out and [c[:2] for c in calls] == [("GET", "/jobs/j1?user_id=u")]


def test_the_stored_chat_wins_over_the_request_and_the_request_is_the_fallback(monkeypatch):
    """The stored answer before the user's message is the state, whatever the request holds (compaction, hidden tool
    results and regeneration change the request, not the chat). When the chat cannot tell, the request counts."""
    mod = load_pipe()
    meta = {"user_message": {"id": "u2", "parentId": "a1"}}
    with_marker = _job_history(mod)["messages"]  # the request says: job j1
    clarify = {
        "role": "assistant",
        "content": "Fragen" + mod._marker({"state": "clarify", "task": "T", "depth": "quick"}),
    }

    async def stored(chat_id, metadata):
        return True, clarify

    monkeypatch.setattr(mod, "_stored_parent", stored)
    assert asyncio.run(mod._state_of_chat(with_marker, "c1", meta))["state"] == "clarify"

    async def first(chat_id, metadata):  # the chat's first message: no answer before it
        return True, None

    monkeypatch.setattr(mod, "_stored_parent", first)
    assert asyncio.run(mod._state_of_chat(with_marker, "c1", meta)) == {}

    async def unknown(chat_id, metadata):  # temporary chat, or the message is not in the chat
        return False, None

    monkeypatch.setattr(mod, "_stored_parent", unknown)
    assert asyncio.run(mod._state_of_chat(with_marker, "temporary:x", meta)) == {"state": "job", "job": "j1"}
    no_marker = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "Bericht"},
        {"role": "user", "content": "b"},
    ]
    assert asyncio.run(mod._state_of_chat(no_marker, None, None)) == {}


def test_a_failing_or_foreign_lookup_gives_no_state(monkeypatch):
    mod = load_pipe()
    meta = {"user_message": {"id": "u2", "parentId": "a1"}}

    async def fails(chat_id, metadata):
        raise ImportError("no Open WebUI here")

    monkeypatch.setattr(mod, "_stored_parent", fails)
    assert asyncio.run(mod._state_of_chat(_compacted_history()["messages"], "c1", meta)) == {}

    async def forged(chat_id, metadata):  # a marker with an id that would change the service path
        return True, {"role": "assistant", "content": "x" + mod._marker({"state": "job", "job": "j1/../x"})}

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
    assert asyncio.run(mod._stored_parent("c1", meta("a1"))) == (True, {"role": "assistant", "content": "x"})
    assert asyncio.run(mod._stored_parent("c1", meta("u0"))) == (True, None)  # a user message with no answer before
    assert asyncio.run(mod._stored_parent("c1", meta(None))) == (True, None)  # the chat's first message
    assert seen == [("c1", "a1"), ("c1", "u0")]
    for chat in (None, "temporary:x", "local:x", "channel:x"):  # nothing stored
        assert asyncio.run(mod._stored_parent(chat, meta("a1"))) == (False, None)
    assert asyncio.run(mod._stored_parent("c1", {})) == (False, None)  # the message is not named


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
    assert asyncio.run(mod._stored_parent("c1", {"user_message_id": "u2"}))[1]["role"] == "assistant"
    assert seen == [("c1", "u2"), ("c1", "a1")]  # always the chat of the request
    assert asyncio.run(mod._stored_parent("c1", {"user_message": None, "user_message_id": "u0"})) == (True, None)
    assert asyncio.run(mod._stored_parent("c1", {"user_message_id": "missing"})) == (False, None)
    # `user_message` with a parent wins, no second lookup
    seen.clear()
    assert asyncio.run(mod._stored_parent("c1", {"user_message": {"parentId": "a1"}, "user_message_id": "u2"}))[0]
    assert seen == [("c1", "a1")]
    # a failed turn (user message without an answer) is walked past to the answer before it
    store["u3"] = {"role": "user", "parentId": "u2"}
    assert asyncio.run(mod._stored_parent("c1", {"user_message": {"parentId": "u3"}}))[1]["content"] == "x"
    # a parent that is not in the chat: the chat cannot tell
    assert asyncio.run(mod._stored_parent("c1", {"user_message": {"parentId": "gone"}})) == (False, None)


def test_a_failed_state_lookup_is_logged(monkeypatch, caplog):
    mod = load_pipe()

    async def fails(chat_id, metadata):
        raise ImportError("moved")

    monkeypatch.setattr(mod, "_stored_parent", fails)
    with caplog.at_level("WARNING"):
        assert asyncio.run(mod._state_of_chat([{"role": "user", "content": "x"}], "c1", {})) == {}
    assert "ImportError" in caplog.text and "moved" in caplog.text


# ------------------------------------------------ messages typed while a card is open (Open WebUI 0.13.4)
def test_a_cancel_word_counts_as_the_whole_text_or_its_last_paragraph():
    """0.13.4 joins several messages typed while a reply ran with blank lines (subagents.py `combined_content`)."""
    mod = load_pipe()
    for text in ("abbrechen", "Abbrechen.", "Wie ist der Stand?\n\nabbrechen", "x\n\n  Stopp!  "):
        assert mod._is_word(text, mod.CANCEL_WORDS), text
    for text in (
        "Bitte nicht abbrechen",
        "abbrechen Sie das nicht",
        "Wie ist der Stand?",
        "",
        "Hier ein Log:\nfoo\nStopp\nbar",  # a pasted multi-line message with a line "Stopp"
        "abbrechen\n\nund doch nicht",  # the word is not the last paragraph
        "x\n\nStopp!\n\ny",
    ):
        assert not mod._is_word(text, mod.CANCEL_WORDS), text
    assert not mod._is_word("ok\n\nTeil 3 weglassen", mod.APPROVE_WORDS)  # an "ok" paragraph followed by a change
    assert mod._is_word("Teil 3 weglassen\n\nok", mod.APPROVE_WORDS)


def test_a_click_that_arrives_while_held_is_checked_wins():
    mod = load_pipe()
    mod.HELD_POLL_SECONDS = 0.01
    gate = asyncio.Event()

    async def call(event):
        await gate.wait()
        return "clicked"

    async def held():
        gate.set()  # the user clicks while the check runs
        await asyncio.sleep(0.05)  # the call finishes meanwhile
        return True

    assert asyncio.run(mod._card(call, {}, 5, held)) == "clicked"


def test_a_cancelled_card_call_is_awaited():
    mod = load_pipe()
    mod.HELD_POLL_SECONDS = 0.01
    unwound = []

    async def call(event):
        try:
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0)
            unwound.append(True)

    async def held():
        return True

    assert asyncio.run(mod._card(call, {}, 5, held)) is mod.HELD and unwound


def test_two_held_messages_with_a_cancel_word_cancel_the_job():
    mod = load_pipe()
    pipe, calls = _pipe_with_service(mod, {"j1": {"status": "running", "events": [], "report": None}})
    out = asyncio.run(pipe.pipe(_job_history(mod, "Ist er fertig?\n\nabbrechen"), {"id": "u"}))
    assert out == mod.CANCELLED and calls[0][:2] == ("POST", "/jobs/j1/cancel?user_id=u")


def test_card_race_gives_up_when_a_message_is_held():
    mod = load_pipe()
    mod.HELD_POLL_SECONDS = 0.01
    cancelled = []

    async def never(event):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def quick(event):
        return "answer"

    polls = []

    async def held():
        polls.append(1)
        return len(polls) >= 3

    async def never_held():
        return False

    async def run():
        assert await mod._card(never, {}, 5, held) is mod.HELD
        await asyncio.sleep(0)  # let the cancelled task unwind
        assert await mod._card(quick, {}, 5, never_held) == "answer"
        assert await mod._card(quick, {}, 5, None) == "answer"  # no `held`: plain wait_for
        try:
            await mod._card(never, {}, 0.05, never_held)
        except TimeoutError:
            return True
        return False

    assert asyncio.run(run()) and cancelled and len(polls) == 3


def test_has_queued_message_uses_open_webuis_condition(monkeypatch):
    import sys
    import types

    mod = load_pipe()
    seen = []

    class ChatMessages:
        @staticmethod
        async def has_queued_message(chat_id, message_id):
            seen.append((chat_id, message_id))
            return message_id == "m-held"

    module = types.ModuleType("open_webui.models.chat_messages")
    module.ChatMessages = ChatMessages
    monkeypatch.setitem(sys.modules, "open_webui", types.ModuleType("open_webui"))
    monkeypatch.setitem(sys.modules, "open_webui.models", types.ModuleType("open_webui.models"))
    monkeypatch.setitem(sys.modules, "open_webui.models.chat_messages", module)
    assert asyncio.run(mod._has_queued_message("c1", "m-held")) is True
    assert asyncio.run(mod._has_queued_message("c1", "m-other")) is False
    assert seen == [("c1", "m-held"), ("c1", "m-other")]
    # temporary chats are not stored (no held messages); no ids: nothing to ask
    assert asyncio.run(mod._has_queued_message("temporary:x", "m-held")) is False
    assert asyncio.run(mod._has_queued_message(None, "m-held")) is False and len(seen) == 2

    class Broken:
        @staticmethod
        async def has_queued_message(*a):
            raise RuntimeError("db down")

    module.ChatMessages = Broken
    assert asyncio.run(mod._has_queued_message("c1", "m-held")) is False  # the card is then waited for as before


def _blocked_card_pipe(mod, monkeypatch, held_after: int = 2):
    """A card that nobody answers, and a user who types a message after `held_after` polls."""
    mod.HELD_POLL_SECONDS = 0.01
    pipe, calls, asked, events, _call, emit = _card_pipe(mod, [])
    polls = []

    async def call(event):
        asked.append(event)
        await asyncio.sleep(3600)

    async def has_queued(chat_id, message_id):
        polls.append((chat_id, message_id))
        return len(polls) >= held_after

    monkeypatch.setattr(mod, "_has_queued_message", has_queued)
    return pipe, calls, asked, events, call, emit, polls


def test_a_message_typed_while_the_plan_card_is_open_ends_the_turn_and_keeps_the_plan(monkeypatch):
    mod = load_pipe()
    pipe, calls, asked, events, call, emit, polls = _blocked_card_pipe(mod, monkeypatch)
    body = {"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage schnell"}]}
    out = asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    assert polls[0] == ("c1", "m1")  # the reply being written is the parent of the held message
    assert not any(c[1] == "/jobs" for c in calls) and len(asked) == 1
    assert "Änderungswunsch" in out and "Rechercheplan" in out
    st = mod._state([{"role": "assistant", "content": out}, {"role": "user"}])
    assert st["state"] == "plan" and st["draft"] == "d1" and st["depth"]


def test_a_message_typed_while_the_change_dialog_is_open_also_ends_the_turn(monkeypatch):
    mod = load_pipe()
    mod.HELD_POLL_SECONDS = 0.01
    pipe, calls, asked, events, _call, emit = _card_pipe(mod, [])
    polls = []

    async def call(event):
        asked.append(event["type"])
        if event["type"] == "request:user_input":
            return _pick(1)  # Ändern
        await asyncio.sleep(3600)  # the input dialog stays open

    async def has_queued(chat_id, message_id):
        polls.append(1)
        return True

    monkeypatch.setattr(mod, "_has_queued_message", has_queued)
    body = {"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage"}]}
    out = asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    assert asked == ["request:user_input", "input"] and "Änderungswunsch" in out
    assert mod._state([{"role": "assistant", "content": out}, {"role": "user"}])["state"] == "plan"


def test_a_message_typed_while_the_preset_card_is_open_is_a_new_question(monkeypatch):
    mod = load_pipe()
    pipe, calls, asked, events, call, emit, polls = _blocked_card_pipe(mod, monkeypatch)

    async def api(method, path, body=None):
        calls.append((method, path, body))
        if path == "/clarify":
            return {"questions": [], "assumptions": [], "preset": "grossrat"}
        if path == "/presets":
            return []
        if path.startswith("/options"):
            raise mod.NotFound("no choices configured")
        raise AssertionError(path)

    pipe._api = api
    body = {"model": "bebbi_deep_research.auto", "messages": [{"role": "user", "content": "Frage"}]}
    out = asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    assert "neue Frage" in out and "research-agent" not in out and not any(c[1] == "/plans" for c in calls)


def _plan_history(mod, text: str, edits: int = 0):
    state = {"state": "plan", "draft": "d1", "depth": "quick", **({"edits": edits} if edits else {})}
    return {
        "model": "x.grossrat",
        "messages": [
            {"role": "user", "content": "Frage"},
            {"role": "assistant", "content": "Plan ..." + mod._marker(state)},
            {"role": "user", "content": text},
        ],
    }


def test_the_held_message_is_read_as_a_change_request_on_the_plan():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _card_pipe(mod, [_pick(0)])
    out = asyncio.run(
        pipe.pipe(_plan_history(mod, "Teil 3 weglassen"), {"id": "u"}, emit, "c1", "m2", __event_call__=call)
    )
    edit = next(c for c in calls if c[1].endswith("/edit"))
    assert edit[0:2] == ("POST", "/plans/d1/edit") and edit[2] == {"instruction": "Teil 3 weglassen", "user_id": "u"}
    assert not any(c[1] == "/clarify" or c[1] == "/plans" for c in calls)  # not a new research
    assert asked[0]["type"] == "request:user_input"  # the changed plan is shown with the card again
    job = next(c for c in calls if c[1] == "/jobs")  # approved there
    assert job[2]["draft_id"] == "d1" and out == ""


def test_several_held_messages_are_one_change_request():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _card_pipe(mod, [_pick(2)])
    asyncio.run(
        pipe.pipe(
            _plan_history(mod, "Teil 3 weglassen\n\nNur Basel-Stadt"),
            {"id": "u"},
            emit,
            "c1",
            "m2",
            __event_call__=call,
        )
    )
    edit = next(c for c in calls if c[1].endswith("/edit"))
    assert edit[2]["instruction"] == "Teil 3 weglassen\n\nNur Basel-Stadt"


def test_typed_approval_cancel_and_the_edit_limit_on_a_held_plan():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _card_pipe(mod, [])
    out = asyncio.run(pipe.pipe(_plan_history(mod, "Freigeben"), {"id": "u"}, emit, "c1", "m2", __event_call__=call))
    assert out == "" and next(c for c in calls if c[1] == "/jobs")[2]["draft_id"] == "d1" and not asked

    pipe, calls, asked, events, call, emit = _card_pipe(mod, [])
    out = asyncio.run(
        pipe.pipe(_plan_history(mod, "Egal\n\nabbrechen"), {"id": "u"}, emit, "c1", "m2", __event_call__=call)
    )
    assert "verworfen" in out and calls == [] and "research-agent" not in out

    pipe, calls, asked, events, call, emit = _card_pipe(mod, [])
    out = asyncio.run(
        pipe.pipe(_plan_history(mod, "Noch ein Wunsch", edits=3), {"id": "u"}, emit, "c1", "m2", __event_call__=call)
    )
    assert (
        "dreimal" in out
        and calls == []
        and mod._state([{"role": "assistant", "content": out}, {"role": "user"}])["state"] == "plan"
    )


def test_a_plan_marker_without_a_draft_is_no_state():
    mod = load_pipe()
    assert mod._marker_state(mod._marker({"state": "plan", "depth": "quick"})) == {}
    assert mod._marker_state(mod._marker({"state": "plan", "draft": "d1"})) == {}  # no depth: review would fail
    assert mod._marker_state(mod._marker({"state": "plan", "draft": "d1?user_id=x"})) == {}


def test_hidden_results_before_the_turn_and_none_after_it_are_no_hidden_results():
    """Open WebUI 0.13.4 (#34257) no longer repeats the context lists (memory, skills, attached knowledge) on every
    turn: the request can hold them before an earlier turn and none after the current user message. The state marker
    in the answer before the turn is still found, and `hold` keeps using `replace` (no hidden items in this message)."""
    mod = load_pipe()
    answer = {"role": "assistant", "content": "Eingereiht" + mod._marker({"state": "job", "job": "j1"})}
    history = [
        {"role": "user", "content": "Frage"},
        *_hidden_tool_results(),  # the lists of the first turn
        answer,
        {"role": "user", "content": "Ist er fertig?"},  # no lists after this one
    ]
    assert mod._hidden_results(history) is False
    assert mod._last_user(history) == 4 and mod._state(history) == {"state": "job", "job": "j1"}
    pipe, calls = _pipe_with_service(mod, {"j1": {"status": "running", "events": [], "report": None}})
    out = asyncio.run(pipe.pipe({"model": "x.grossrat", "messages": history}, {"id": "u"}))
    assert "läuft noch" in out and [c[:2] for c in calls] == [("GET", "/jobs/j1?user_id=u")]


def test_the_approved_plan_stays_as_a_collapsed_block_and_the_marker_still_reads():
    mod = load_pipe()
    pipe, calls, asked, events, call, emit = _card_pipe(mod, [_pick(0)])
    body = {"model": "x.grossrat", "messages": [{"role": "user", "content": "Frage schnell"}]}
    asyncio.run(pipe.pipe(body, {"id": "u"}, emit, "c1", "m1", __event_call__=call))
    queued = events[-1]["data"]["content"]
    assert queued.startswith("Auftrag eingereiht")
    block = queued.split("<details>", 1)[1]
    assert block.startswith("\n<summary>Rechercheplan</summary>") and "**Rechercheplan:" in block
    assert "</details>" in block and "research-agent" not in block.split("</details>")[0]
    assert mod._state([{"role": "assistant", "content": queued}, {"role": "user"}])["state"] == "job"
    assert mod._plan_details("") == ""


def test_attached_documents_switched_off_are_mode_off():
    mod = load_pipe()
    own = {"files": [{"name": "a.pdf"}], "collections": [], "notes": []}
    out, calls, asked, events = _run(mod, [{"files": "Nein"}], [_pick(2)], own=own)
    resolve = next(c for c in calls if c[1] == "/options/resolve")[2]
    assert resolve["own"] == "off" and resolve["own_attached"] is False
    assert "own_sources" not in next(c for c in calls if c[1] == "/plans")[2]

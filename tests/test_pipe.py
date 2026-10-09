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

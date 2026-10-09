"""
title: Deep Research (Bebbi Bot)
description: Deep research with clarifying questions, plan approval and a night queue. Talks to the bebbi-deep-research job service.
version: 1.0.0
requirements: httpx
"""

# Open WebUI pipe function (manifold): one model per research preset, e.g. "Deep Research · Recht".
# Conversation flow, tracked with a hidden state marker in the assistant message:
#   1. user task -> clarifying questions (if needed)          state=clarify
#   2. answers / task -> research plan for approval            state=plan
#   3. "ok" / "jetzt" / "heute Nacht" -> job submitted;        state=job
#      free text instead -> plan is edited and shown again (max 3 rounds)
#   4. "jetzt": the pipe polls and streams status, then returns the report (or hands over to the service,
#      which writes the report into this message when done); "heute Nacht": queued for the night window.

import asyncio
import base64
import io
import json
import re
import time

import httpx
from pydantic import BaseModel, Field

# The state marker is a markdown link reference definition with a comment label: valid markdown, never rendered.
# (An HTML comment is shown as text by the Open WebUI renderer.) The payload is base64 so no markdown syntax can break it.
MARK = re.compile(r"^\[//\]: # \(research-agent:([A-Za-z0-9_=-]+)\)[ \t]*$", re.M)
APPROVE = re.compile(r"^\s*(ok|okay|ja|yes|start|starten|los|passt|go|einverstanden|approve|jetzt|heute nacht|nachts?)\b", re.I)
NIGHT = re.compile(r"nacht|night|später|spaeter", re.I)
NOW = re.compile(r"\b(jetzt|sofort|now)\b", re.I)
DEPTH_WORDS = {"quick": re.compile(r"\b(schnell|kurz|quick)\b", re.I), "deep": re.compile(r"\b(gründlich|gruendlich|tief|deep)\b", re.I)}


async def store_user_file(request, user_id: str, filename: str, data: bytes, content_type: str) -> dict:
    """Store `data` in the Open WebUI file store as a file of the chat's user (in-process, like Open WebUI's own upload).
    A file uploaded with the service's admin key would belong to the admin and give the user a 404 on download; this
    code runs inside Open WebUI as the user. Returns the message-file item for a `files` event.
    (The same function is in the other file of pipe/; a test checks that both are identical.)"""
    from fastapi import UploadFile
    from open_webui.models.users import Users
    from open_webui.routers.files import upload_file_handler
    from starlette.datastructures import Headers

    user = await Users.get_user_by_id(user_id)
    upload = UploadFile(file=io.BytesIO(data), filename=filename, headers=Headers({"content-type": content_type}))
    stored = await upload_file_handler(request, file=upload, metadata={}, process=False, process_in_background=False, user=user)
    return {"type": "file", "id": stored.id, "url": stored.id, "name": filename, "size": len(data), "status": "uploaded",
            "meta": {"name": filename, "content_type": content_type, "size": len(data)}}


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _filename(job: dict) -> str:
    """File name of the Word file: the one the service proposes, else a generic one."""
    return job.get("docx_name") or f"Bebbi-Deep-Research_{job.get('id', 'Bericht')}.docx"


def _marker(state: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(state, ensure_ascii=False).encode()).decode()
    return f"\n\n[//]: # (research-agent:{payload})"


def _decode(payload: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except ValueError:
        return {}


def _state(messages: list[dict]) -> dict:
    for m in reversed(messages[:-1]):
        if m.get("role") == "assistant":
            found = MARK.search(m.get("content") or "")
            return _decode(found.group(1)) if found else {}
    return {}


def _render_plan(plan: dict, depth: str) -> str:
    lines = [f"**Rechercheplan: {plan['title']}**", "", f"*Auftrag:* {plan['task_core']}", ""]
    for p in plan["parts"]:
        lines += [f"**{p['title']}**: {p['goal']}", ""]       # blank lines: else the next part title becomes part of the last list item
        lines += [f"- {q['text']}" for q in plan["sub_questions"] if q["part"] == p["id"]]
        lines.append("")
    if plan.get("must_name"):
        lines += ["", f"*Muss abgedeckt werden:* {', '.join(plan['must_name'])}"]
    lines += ["", f"Tiefe: **{depth}**. Antworten Sie mit **ok** (jetzt starten), **heute Nacht** (Nachtlauf) "
              "oder beschreiben Sie, was am Plan geändert werden soll."]
    return "\n".join(lines)


class Pipe:
    class Valves(BaseModel):
        SERVICE_URL: str = Field(default="http://bebbi-deep-research:8088", description="base URL of the bebbi-deep-research job service")
        SERVICE_TOKEN: str = Field(default="", description="bearer token of the job service (= RA_TOKEN of the service)")
        DEFAULT_DEPTH: str = Field(default="standard", description="quick | standard | deep")
        NIGHT_FOR_DEPTH: str = Field(default="deep", description="depths that always run in the night window")
        POLL_SECONDS: int = Field(default=10)
        MAX_WAIT_MINUTES: int = Field(default=25, description="how long the pipe streams a 'now' job before handing over")
        PRESETS: str = Field(default="rechtsrecherche:Recht,grossrat:Grosser Rat,statistik:Statistik,verwaltung_web:Verwaltung,web_survey:Web",
                             description="preset:label pairs shown as models")

    def __init__(self):
        self.type = "manifold"
        self.valves = self.Valves()

    def pipes(self) -> list[dict]:
        out = []
        for pair in self.valves.PRESETS.split(","):
            name, _, label = pair.partition(":")
            out.append({"id": name.strip(), "name": f"Deep Research · {label.strip() or name.strip()}"})
        return out

    async def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=300) as http:
                r = await http.request(method, self.valves.SERVICE_URL.rstrip("/") + path, json=body,
                                       headers={"Authorization": f"Bearer {self.valves.SERVICE_TOKEN}"})
        except httpx.TransportError as e:
            raise RuntimeError(f"Der Recherche-Dienst ist nicht erreichbar ({type(e).__name__}). Bitte später erneut versuchen.") from e
        if r.status_code >= 400:
            is_json = r.headers.get("content-type", "").startswith("application/json")
            raise RuntimeError(r.json().get("detail", r.text) if is_json else r.text)
        return r.json()

    async def _download(self, path: str) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=120) as http:
                r = await http.get(self.valves.SERVICE_URL.rstrip("/") + path, headers={"Authorization": f"Bearer {self.valves.SERVICE_TOKEN}"})
        except httpx.TransportError as e:
            raise RuntimeError(f"Der Recherche-Dienst ist nicht erreichbar ({type(e).__name__})") from e
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.content

    @staticmethod
    def _task_reply(task: str, body: dict) -> str:
        """Open WebUI sends background tasks (chat title, tags, follow-ups) to the model of the chat. They must not start
        a research: answer them locally with the JSON the task expects."""
        if task == "title_generation":
            prompt = " ".join(str(m.get("content", "")) for m in body.get("messages", []))
            found = MARK.search(prompt)       # the chat history of the task carries our state marker (clarify state: the task)
            task_text = _decode(found.group(1)).get("task", "") if found else ""
            plan_title = re.search(r"\*\*Rechercheplan: (.+?)\*\*", prompt)       # plan state: the title of the plan
            title = " ".join((task_text or (plan_title.group(1) if plan_title else "")).split())[:50] or "Deep Research"
            return json.dumps({"title": title}, ensure_ascii=False)
        if task == "tags_generation":
            return json.dumps({"tags": ["Recherche"]}, ensure_ascii=False)
        if task == "follow_up_generation":
            return json.dumps({"follow_ups": []})
        return ""

    async def pipe(self, body: dict, __user__: dict, __event_emitter__=None, __chat_id__=None, __message_id__=None, __task__=None,
                   __request__=None):
        if __task__:
            return self._task_reply(str(__task__), body)

        async def status(text: str, done: bool = False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": text, "done": done}})

        async def hold(text: str) -> str:
            """Text of a message that the service replaces with the report later. Open WebUI v0.11 renders the stored
            `output` of a message in preference to its `content`, and no API lets the service change `output`. So the text
            goes out as a `replace` event (stored as content, output stays empty) and the pipe returns nothing."""
            if not __event_emitter__:
                return text
            await __event_emitter__({"type": "replace", "data": {"content": text}})
            return ""

        messages = body.get("messages") or []
        text = (messages[-1].get("content") or "") if messages else ""
        if isinstance(text, list):  # multimodal content parts
            text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
        preset = body.get("model", "").split(".")[-1]
        st = _state(messages)
        try:
            # --- running / finished job: report status or result
            if st.get("state") == "job":
                job = await self._api("GET", f"/jobs/{st['job']}")
                if job["status"] == "done":
                    return await self._publish(job, __event_emitter__, __user__, __request__)
                if job["status"] == "error":
                    return f"Die Recherche ist fehlgeschlagen: {job.get('error')}"
                last = job["events"][-1]["text"] if job["events"] else "wartet in der Warteschlange"
                return f"Die Recherche läuft noch ({job['status']}: {last}). Der Bericht erscheint in diesem Chat." + _marker(st)

            # --- plan shown: approve or edit
            if st.get("state") == "plan":
                if APPROVE.match(text) or NIGHT.search(text) and len(text) < 40:
                    when = "night" if (NIGHT.search(text) or st["depth"] in self.valves.NIGHT_FOR_DEPTH.split(",")) and not NOW.search(text) else "now"
                    job = await self._api("POST", "/jobs", {"draft_id": st["draft"], "user_id": __user__.get("id", "?"),
                                                            "chat_id": __chat_id__, "message_id": __message_id__, "when": when})
                    st = {"state": "job", "job": job["job_id"]}
                    if job.get("when", when) == "night":   # the service decides: by day only quick focus runs
                        why = (" Tagsüber laufen nur schnelle Recherchen (Tiefe quick) sofort." if when == "now" else "")
                        return await hold(f"Auftrag eingereiht für das Nachtfenster ({job['window']}).{why} Der Bericht erscheint "
                                          "in diesem Chat, sobald er fertig ist." + _marker(st))
                    text, finished, done = await self._stream(job["job_id"], status)
                    if done:
                        text = await self._publish(done, __event_emitter__, __user__, __request__)
                    # a finished report carries no marker: the next message starts anew
                    return text if finished else await hold(text + _marker(st))
                if st.get("edits", 0) >= 3:
                    return "Der Plan wurde bereits dreimal angepasst. Bitte mit **ok** starten oder neu beginnen." + _marker(st)
                await status("Passe den Plan an")
                res = await self._api("POST", f"/plans/{st['draft']}/edit", {"instruction": text})
                st["edits"] = st.get("edits", 0) + 1
                await status("Plan angepasst", done=True)
                return _render_plan(res["plan"], st["depth"]) + _marker(st)

            # --- new task (or answers to clarifying questions)
            depth = next((d for d, rx in DEPTH_WORDS.items() if rx.search(text)), self.valves.DEFAULT_DEPTH)
            if st.get("state") == "clarify":
                task, depth = st["task"] + "\n\nPräzisierungen: " + text, st.get("depth", depth)
            else:
                task = text
                await status("Prüfe, ob Rückfragen nötig sind")
                c = await self._api("POST", "/clarify", {"task": task, "preset": preset, "depth": depth})
                if c["questions"]:
                    await status("Rückfragen", done=True)
                    qs = "\n".join(f"{i}. {q}" for i, q in enumerate(c["questions"], 1))
                    assume = ("\n\nOhne Antwort gehe ich davon aus: " + "; ".join(c["assumptions"])) if c["assumptions"] else ""
                    return (f"Bevor ich den Rechercheplan erstelle:\n\n{qs}{assume}\n\nAntworten Sie kurz "
                            "(oder mit **weiter**, um mit diesen Annahmen fortzufahren).") + _marker({"state": "clarify", "task": task, "depth": depth})
            await status("Erstelle Rechercheplan")
            res = await self._api("POST", "/plans", {"task": task, "preset": preset, "depth": depth})
            await status("Plan erstellt", done=True)
            return _render_plan(res["plan"], depth) + _marker({"state": "plan", "draft": res["draft_id"], "depth": depth})
        except Exception as e:
            await status("Fehler", done=True)
            return f"Deep Research ist nicht verfügbar: {e}" + (_marker(st) if st else "")

    async def _publish(self, job: dict, emitter, user: dict, request) -> str:
        """The report of a finished job for this message: one `source` event per source (Open WebUI shows `[n]` as a
        citation badge of the n-th source and lists the sources under the message) and the Word file as a message file.
        Returns the report text. Sources and file are best effort; the text is always returned."""
        text = job.get("ui_report") or job["report"]
        if emitter:
            # The service delivers the report into the same message with the admin key (also for "jetzt" jobs). If it did,
            # the sources are there already; stored twice they would double the list and shift nothing but confuse.
            for _ in range(10):
                if job.get("delivery") or not job.get("id"):
                    break
                await asyncio.sleep(min(self.valves.POLL_SECONDS, 2))
                job = await self._api("GET", f"/jobs/{job['id']}")
            for event in [] if job.get("delivery") == "ok" else job.get("owui_events") or []:     # [n] is the n-th event
                await emitter(event)
            if job.get("docx") and request is not None and (user or {}).get("id"):
                try:
                    data = await self._download(job["docx"])
                    name = _filename(job)
                    item = await store_user_file(request, user["id"], name, data, DOCX)
                    await emitter({"type": "files", "data": {"files": [item]}})
                except Exception as e:      # the report is more important than the file
                    text += f"\n\n*Die Word-Datei konnte nicht angehängt werden ({type(e).__name__}).*"
        return text

    async def _stream(self, job_id: str, status) -> tuple[str, bool, dict | None]:
        """Stream the status of a running job. Returns (text, finished, job): the report or error when the job ended, else a hint."""
        deadline = time.time() + 60 * self.valves.MAX_WAIT_MINUTES
        last = ""
        while time.time() < deadline:
            job = await self._api("GET", f"/jobs/{job_id}")
            if job["status"] == "done":
                await status("Bericht fertig", done=True)
                return "", True, job
            if job["status"] == "error":
                await status("Fehler", done=True)
                return f"Die Recherche ist fehlgeschlagen: {job.get('error')}", True, None
            if job["events"] and job["events"][-1]["text"] != last:
                last = job["events"][-1]["text"]
                await status(last)
            await asyncio.sleep(self.valves.POLL_SECONDS)
        await status("Läuft im Hintergrund weiter", done=True)
        return "Die Recherche dauert länger. Der Bericht erscheint in diesem Chat, sobald er fertig ist.", False, None

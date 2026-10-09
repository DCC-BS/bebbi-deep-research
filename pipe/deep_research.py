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
ATTACHED = re.compile(r"\A\s*<attached_files>.*?</attached_files>\s*", re.S)   # Open WebUI prepends this to messages with files
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


class UserError(RuntimeError):
    """A refusal with a message meant for the user as it is (no "nicht verfügbar" prefix)."""


# ---------------------------------------------------------------------------------------------- own sources (A4)
# The chat's files and knowledge bases. Everything that needs Open WebUI internals is in the helpers below (imports inside
# the functions, so the module also loads outside Open WebUI and the tests can put fakes in their place). Pinned to v0.11.4:
#   open_webui.models.users.Users.get_user_by_id           open_webui.models.files.Files.get_file_by_id
#   open_webui.utils.access_control.files.has_access_to_file
#   open_webui.retrieval.utils.filter_accessible_collections   (Open WebUI's own rule for knowledge bases)
#   open_webui.models.knowledge.Knowledges.get_knowledge_by_id open_webui.models.models.Models.get_model_by_id
#   open_webui.config.Config.get("webui.url")
# What the user may read is decided here, inside Open WebUI, as the chat user; the service trusts the result.
OWN_MAX_FILES = 10
OWN_MAX_FILE_CHARS = 2_000_000


def _file_date(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
    except (TypeError, ValueError, OverflowError):
        return ""


async def _owui_user(user_id: str):
    from open_webui.models.users import Users

    return await Users.get_user_by_id(user_id)


async def _owui_file(user, file_id: str):
    """The file record if `user` may read the file (owner, admin, or access through a knowledge base, channel, shared
    chat or shared model: Open WebUI's `has_access_to_file`), else None."""
    from open_webui.models.files import Files
    from open_webui.utils.access_control.files import has_access_to_file

    file = await Files.get_file_by_id(file_id)
    if file and (file.user_id == user.id or user.role == "admin" or await has_access_to_file(file_id, "read", user)):
        return file
    return None


async def _owui_knowledge(user, kb_id: str):
    """The knowledge base (id, name) if `user` may read it, else None. Uses Open WebUI's `filter_accessible_collections`,
    the rule the retrieval endpoints apply; it checks the grants of the knowledge base and lets admins through."""
    from open_webui.models.knowledge import Knowledges
    from open_webui.retrieval.utils import filter_accessible_collections

    kb = await Knowledges.get_knowledge_by_id(kb_id)
    if kb and kb_id in await filter_accessible_collections({kb_id}, user):
        return kb
    return None


async def _owui_model_knowledge(model_id: str) -> list[dict]:
    """Knowledge items attached to the model itself (workspace model settings: meta.knowledge)."""
    from open_webui.models.models import Models

    info = await Models.get_model_by_id(model_id)
    items = ((info.meta.model_dump() if hasattr(info.meta, "model_dump") else dict(info.meta or {})).get("knowledge") if info and info.meta else None) or []
    return [i for i in items if isinstance(i, dict)]


async def _public_base(request, override: str = "") -> str:
    """Public base URL of Open WebUI (links to files): the valve, else Open WebUI's WEBUI_URL setting, else the request."""
    if override:
        return override.rstrip("/")
    try:
        from open_webui.config import Config

        url = await Config.get("webui.url")
    except Exception:
        url = ""
    return str(url or getattr(request, "base_url", "") or "").rstrip("/")


async def collect_own_sources(request, user_id: str, model_id: str, chat_files: list, with_text: bool,
                              public_url: str = "", max_files: int = OWN_MAX_FILES, max_chars: int = OWN_MAX_FILE_CHARS) -> dict | None:
    """The user's own sources of this chat turn for the service: uploaded files (with extracted text) and knowledge bases,
    from the chat's attachments (`__files__`) and the model's own knowledge. Only what the user may read; the rest is
    named in `notes` (never with content). Returns None if there is nothing to report. `with_text=False` (plan step)
    sends only the first 1500 characters of each file."""
    wanted: list[tuple[str, str, str]] = []          # (kind, id, name)
    notes: list[str] = []
    for item in chat_files or []:
        if not isinstance(item, dict):
            continue
        kind, name = item.get("type"), item.get("name") or (item.get("file") or {}).get("filename") or item.get("id") or "?"
        if kind in ("file", "collection") and item.get("id"):
            wanted.append((kind, str(item["id"]), str(name)))
        elif kind not in ("image", "filesystem", None):
            notes.append(f"Anhang „{name}“ (Typ {kind}) wird nicht verwendet; unterstützt sind Dateien und Wissensbasen.")
    try:
        for item in await _owui_model_knowledge(model_id):
            if item.get("type") in ("file", "collection") and item.get("id"):
                wanted.append((item["type"], str(item["id"]), str(item.get("name") or item["id"])))
    except Exception:
        pass                                          # no model entry or not inside Open WebUI: nothing attached to the model
    if not wanted and not notes:
        return None
    files, collections, seen = [], [], set()
    try:
        user = await _owui_user(user_id)
        if user is None:
            raise LookupError(user_id)
    except Exception:
        return {"files": [], "collections": [], "notes": ["Eigene Quellen konnten nicht geprüft werden und werden nicht verwendet."]}
    base = await _public_base(request, public_url) if any(k == "file" for k, _, _ in wanted) or wanted else ""
    for kind, ident, name in wanted:
        if (kind, ident) in seen:
            continue
        seen.add((kind, ident))
        try:
            if kind == "collection":
                kb = await _owui_knowledge(user, ident)
                if kb is None:
                    notes.append(f"Wissensbasis „{name}“ ist für Sie nicht lesbar und wird nicht verwendet.")
                else:
                    collections.append({"id": kb.id, "name": kb.name})
                continue
            file = await _owui_file(user, ident)
        except Exception as e:
            notes.append(f"„{name}“ konnte nicht geprüft werden ({type(e).__name__}) und wird nicht verwendet.")
            continue
        if file is None:
            notes.append(f"Datei „{name}“ ist für Sie nicht lesbar und wird nicht verwendet.")
            continue
        data = file.data or {}
        text = (data.get("content") or "").strip()
        label = (file.meta or {}).get("name") or file.filename
        if not text:
            pending = data.get("status") in ("pending", "processing")
            notes.append(f"Datei „{label}“ " + ("wird noch verarbeitet; bitte später erneut fragen." if pending
                                             else "enthält keinen lesbaren Text (die Texterkennung ist fehlgeschlagen) und wird nicht verwendet."))
            continue
        if len(files) >= max_files:
            notes.append(f"Datei „{label}“ wird nicht verwendet: höchstens {max_files} Dateien pro Recherche.")
            continue
        files.append({"id": file.id, "name": label, "chars": len(text), "date": _file_date(file.created_at),
                      "url": f"{base}/api/v1/files/{file.id}/content" if base else "",
                      "text": text[:1500] if not with_text else text[:max_chars]})
    if not (files or collections or notes):
        return None
    return {"files": files, "collections": collections, "base_url": base, "notes": notes}


def _own_lines(info: dict | None) -> list[str]:
    """The plan lines about the user's own sources (from the service's answer: names, sizes, what was cut, remarks)."""
    if not info:
        return []
    parts = [f"Datei „{f['name']}“ ({f['chars']:,} Zeichen{', gekürzt' if f.get('cut') else ''})".replace(",", "'") for f in info.get("files", [])]
    parts += [f"Wissensbasis „{c['name']}“" for c in info.get("collections", [])]
    lines = []
    if parts:
        how = ("Es werden ausschliesslich diese Quellen verwendet." if info.get("mode") == "only"
               else "Sie werden zuerst verwendet, Web und Fachdatenbanken ergänzen." if info.get("mode") != "off" else "")
        lines.append(f"*Eigene Quellen:* {'; '.join(parts)}. {how}".strip())
    lines += [f"*Hinweis:* {n}" for n in info.get("notes", [])]
    return lines


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


def _render_plan(plan: dict, depth: str, own: dict | None = None) -> str:
    lines = [f"**Rechercheplan: {plan['title']}**", "", f"*Auftrag:* {plan['task_core']}", ""]
    for p in plan["parts"]:
        lines += [f"**{p['title']}**: {p['goal']}", ""]       # blank lines: else the next part title becomes part of the last list item
        lines += [f"- {q['text']}" for q in plan["sub_questions"] if q["part"] == p["id"]]
        lines.append("")
    if plan.get("must_name"):
        lines += ["", f"*Muss abgedeckt werden:* {', '.join(plan['must_name'])}"]
    own_lines = _own_lines(own)
    if own_lines:
        lines += [""] + [ln + "  " for ln in own_lines[:-1]] + own_lines[-1:]
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
        PRESETS: str = Field(default="rechtsrecherche:Recht,grossrat:Grosser Rat,statistik:Statistik,verwaltung_web:Verwaltung,web_survey:Web,dokumente:Eigene Dokumente",
                             description="preset:label pairs shown as models")
        OWN_SOURCES: bool = Field(default=True, description="use the chat's uploaded files and knowledge bases as sources")
        PUBLIC_URL: str = Field(default="", description="public base URL of Open WebUI for links to uploaded files (empty = WEBUI_URL setting)")
        OWN_MAX_FILES: int = Field(default=OWN_MAX_FILES, description="files per research")
        OWN_MAX_FILE_CHARS: int = Field(default=OWN_MAX_FILE_CHARS, description="extracted text per file sent to the service")

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
            detail = r.json().get("detail", r.text) if is_json else r.text
            raise (UserError if r.status_code == 422 and isinstance(detail, str) else RuntimeError)(detail)
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

    async def _own(self, request, user: dict, body: dict, files: list, with_text: bool) -> dict | None:
        if not self.valves.OWN_SOURCES or not (user or {}).get("id"):
            return None
        return await collect_own_sources(request, user["id"], body.get("model", ""), files, with_text, self.valves.PUBLIC_URL,
                                         self.valves.OWN_MAX_FILES, self.valves.OWN_MAX_FILE_CHARS)

    async def pipe(self, body: dict, __user__: dict, __event_emitter__=None, __chat_id__=None, __message_id__=None, __task__=None,
                   __request__=None, __files__=None, __metadata__=None):
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
        # Open WebUI rewrites the last message when files are attached (retrieval context, <attached_files> tags). The
        # user's own words are in the message object it received from the browser.
        typed = ((__metadata__ or {}).get("user_message") or {}).get("content")
        text = ATTACHED.sub("", typed if isinstance(typed, str) and typed.strip() else text)
        chat_files = list(__files__ or (__metadata__ or {}).get("files") or body.get("files") or [])
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
                    own = await self._own(__request__, __user__, body, chat_files, with_text=True)
                    job = await self._api("POST", "/jobs", {"draft_id": st["draft"], "user_id": __user__.get("id", "?"),
                                                            "chat_id": __chat_id__, "message_id": __message_id__, "when": when,
                                                            **({"own_sources": own} if own else {})})
                    st = {"state": "job", "job": job["job_id"]}
                    own_note = "\n\n" + "\n".join(_own_lines(job.get("own_sources"))) if _own_lines(job.get("own_sources")) else ""
                    if job.get("when", when) == "night":   # the service decides: by day only quick focus runs
                        why = (" Tagsüber laufen nur schnelle Recherchen (Tiefe quick) sofort." if when == "now" else "")
                        return await hold(f"Auftrag eingereiht für das Nachtfenster ({job['window']}).{why} Der Bericht erscheint "
                                          "in diesem Chat, sobald er fertig ist." + own_note + _marker(st))
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
                return _render_plan(res["plan"], st["depth"], res.get("own_sources")) + _marker(st)

            # --- new task (or answers to clarifying questions)
            depth = next((d for d, rx in DEPTH_WORDS.items() if rx.search(text)), self.valves.DEFAULT_DEPTH)
            own = await self._own(__request__, __user__, body, chat_files, with_text=False)     # previews: enough to clarify and plan
            if st.get("state") == "clarify":
                task, depth = st["task"] + "\n\nPräzisierungen: " + text, st.get("depth", depth)
            else:
                task = text
                await status("Prüfe, ob Rückfragen nötig sind")
                c = await self._api("POST", "/clarify", {"task": task, "preset": preset, "depth": depth,
                                                         **({"own_sources": own} if own else {})})
                if c["questions"]:
                    await status("Rückfragen", done=True)
                    qs = "\n".join(f"{i}. {q}" for i, q in enumerate(c["questions"], 1))
                    assume = ("\n\nOhne Antwort gehe ich davon aus: " + "; ".join(c["assumptions"])) if c["assumptions"] else ""
                    return (f"Bevor ich den Rechercheplan erstelle:\n\n{qs}{assume}\n\nAntworten Sie kurz "
                            "(oder mit **weiter**, um mit diesen Annahmen fortzufahren).") + _marker({"state": "clarify", "task": task, "depth": depth})
            await status("Erstelle Rechercheplan")
            res = await self._api("POST", "/plans", {"task": task, "preset": preset, "depth": depth,
                                                     **({"own_sources": own} if own else {})})
            await status("Plan erstellt", done=True)
            return _render_plan(res["plan"], depth, res.get("own_sources")) + _marker({"state": "plan", "draft": res["draft_id"], "depth": depth})
        except UserError as e:
            await status("Hinweis", done=True)
            return str(e) + (_marker(st) if st else "")
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

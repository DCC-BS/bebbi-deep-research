"""
title: Deep Research (Bebbi Bot)
description: Deep research with clarifying questions, plan approval and a night queue. Talks to the bebbi-deep-research job service.
version: 1.0.0
requirements: httpx
"""

# Open WebUI pipe function (manifold): one model "Deep Research" (id auto); the research preset (Recht, Grosser Rat,
# ...) is chosen per task. The valve PRESET_MODELS also lists one model per preset, which skips the choice.
# Conversation flow, tracked with a hidden state marker in the assistant message:
#   0. model "Deep Research": the service suggests a preset for the task, the user confirms or picks another in
#      Open WebUI's ask-user card (no answer: no research)
#   1. user task -> clarifying questions (if needed)          state=clarify
#   2. answers / task -> research plan for approval            state=plan
#   3. the user decides in Open WebUI's ask-user card, in the same turn: Freigeben -> job queued for the night window
#      (state=job), Ändern -> input dialog, plan edited and shown again (max 3 rounds), Verwerfen -> no job. No answer
#      (card closed or timed out, browser gone) counts as no; the plan is not kept, a new message starts over.
#   4. the service writes the report into this message when the job is done; a later message in the chat returns it too.

import asyncio
import base64
import contextvars
import io
import json
import logging
import re
import time
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

# The state marker is a markdown link reference definition with a comment label: valid markdown, never rendered.
# (An HTML comment is shown as text by the Open WebUI renderer.) The payload is base64 so no markdown syntax can
# break it.
logger = logging.getLogger(__name__)
MARK = re.compile(r"^\[//\]: # \(research-agent:([A-Za-z0-9_=-]+)\)[ \t]*$", re.M)
ATTACHED = re.compile(
    r"\A\s*<attached_files>.*?</attached_files>\s*", re.S
)  # Open WebUI prepends this to messages with files
# The plan is approved, changed or discarded in Open WebUI's ask-user card only (buttons, see Pipe._decide). No answer
# (card closed or timed out, browser gone, no browser session) counts as "no": nothing runs, a new message starts over.
APPROVE, CHANGE, DISCARD = "Freigeben", "Ändern", "Verwerfen"
AUTO = "auto"  # id of the one model that lets the user choose the preset per task
CANCEL_WORDS = {"abbrechen", "recherche abbrechen", "stopp"}  # the whole message, nothing else, cancels a queued job
CANCELLED = "Die Recherche wurde abgebrochen. Es wird kein Bericht erstellt."
# Version of the service API this pipe speaks (service: routers/contract.py). Sent with every call; the service refuses
# another version with a German message, so a pipe and a service that do not fit together fail visibly.
API_VERSION = "1"
# Correlation id of the current chat turn, sent as X-Request-ID: "<chat_id>:<message_id>". The service logs it with
# every request and stores it with the job, so the night run's log lines lead back to the chat message.
REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("dr_request_id", default="")
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def request_id(chat_id, message_id) -> str:
    """The X-Request-ID of a chat turn ("" without a chat, then the service makes its own)."""
    if not chat_id:
        return ""
    return _UNSAFE.sub("", f"{chat_id}:{message_id or ''}")[:200]


DEPTH_WORDS = {
    "quick": re.compile(r"\b(schnell|kurz|quick)\b", re.I),
    "deep": re.compile(r"\b(gründlich|gruendlich|tief|deep)\b", re.I),
}


async def store_user_file(request, user_id: str, filename: str, data: bytes, content_type: str) -> dict:
    """Store `data` in the Open WebUI file store as a file of the chat's user.

    In-process, like Open WebUI's own upload.
    A file uploaded with the service's admin key would belong to the admin and give the user a 404 on download; this
    code runs inside Open WebUI as the user. Returns the message-file item for a `files` event.
    (The same function is in the other file of pipe/; a test checks that both are identical.)"""
    from fastapi import UploadFile
    from open_webui.models.users import Users
    from open_webui.routers.files import upload_file_handler
    from starlette.datastructures import Headers

    user = await Users.get_user_by_id(user_id)
    upload = UploadFile(file=io.BytesIO(data), filename=filename, headers=Headers({"content-type": content_type}))
    stored = await upload_file_handler(
        request, file=upload, metadata={}, process=False, process_in_background=False, user=user
    )
    return {
        "type": "file",
        "id": stored.id,
        "url": stored.id,
        "name": filename,
        "size": len(data),
        "status": "uploaded",
        "meta": {"name": filename, "content_type": content_type, "size": len(data)},
    }


class UserError(RuntimeError):
    """A refusal with a message meant for the user as it is (no "nicht verfügbar" prefix)."""


class NotFound(RuntimeError):
    """The service answered 404 (job or draft is gone)."""


def _jpath(path: str, user_id: str) -> str:
    """Service path with the owner check of the chat user (`?user_id=`)."""
    return path + ("&" if "?" in path else "?") + "user_id=" + quote(str(user_id), safe="")


# ---------------------------------------------------------------------------------------------- own sources (A4)
# The chat's files and knowledge bases. Everything that needs Open WebUI internals is in the helpers below (imports
# inside the functions, so the module also loads outside Open WebUI and the tests can put fakes in their place).
# Checked against v0.12.3 (scripts/check_owui_internals.py):
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
    """The knowledge base (id, name) if `user` may read it, else None.

    Uses Open WebUI's `filter_accessible_collections`,
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
    items = (
        (info.meta.model_dump() if hasattr(info.meta, "model_dump") else dict(info.meta or {})).get("knowledge")
        if info and info.meta
        else None
    ) or []
    return [i for i in items if isinstance(i, dict)]


async def _public_base(request, override: str = "") -> str:
    """Public base URL of Open WebUI (links to files).

    The valve, else Open WebUI's WEBUI_URL setting, else the request.
    """
    if override:
        return override.rstrip("/")
    try:
        from open_webui.config import Config

        url = await Config.get("webui.url")
    except Exception:
        url = ""
    return str(url or getattr(request, "base_url", "") or "").rstrip("/")


async def collect_own_sources(
    request,
    user_id: str,
    model_id: str,
    chat_files: list,
    with_text: bool,
    public_url: str = "",
    max_files: int = OWN_MAX_FILES,
    max_chars: int = OWN_MAX_FILE_CHARS,
) -> dict | None:
    """The user's own sources of this chat turn for the service.

    Uploaded files (with extracted text) and knowledge bases,
    from the chat's attachments (`__files__`) and the model's own knowledge. Only what the user may read; the rest is
    named in `notes` (never with content). Returns None if there is nothing to report. `with_text=False` (plan step)
    sends only the first 1500 characters of each file."""
    wanted: list[tuple[str, str, str]] = []  # (kind, id, name)
    notes: list[str] = []
    for item in chat_files or []:
        if not isinstance(item, dict):
            continue
        kind, name = (
            item.get("type"),
            item.get("name") or (item.get("file") or {}).get("filename") or item.get("id") or "?",
        )
        if kind in ("file", "collection") and item.get("id"):
            wanted.append((kind, str(item["id"]), str(name)))
        elif kind not in ("image", "filesystem", None):
            notes.append(
                f"Anhang „{name}“ (Typ {kind}) wird nicht verwendet; unterstützt sind Dateien und Wissensbasen."
            )
    try:
        for item in await _owui_model_knowledge(model_id):
            if item.get("type") in ("file", "collection") and item.get("id"):
                wanted.append((item["type"], str(item["id"]), str(item.get("name") or item["id"])))
    except Exception:
        pass  # no model entry or not inside Open WebUI: nothing attached to the model
    if not wanted and not notes:
        return None
    files, collections, seen = [], [], set()
    try:
        user = await _owui_user(user_id)
        if user is None:
            raise LookupError(user_id)
    except Exception:
        return {
            "files": [],
            "collections": [],
            "notes": ["Eigene Quellen konnten nicht geprüft werden und werden nicht verwendet."],
        }
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
            notes.append(
                f"Datei „{label}“ "
                + (
                    "wird noch verarbeitet; bitte später erneut fragen."
                    if pending
                    else "enthält keinen lesbaren Text (die Texterkennung ist fehlgeschlagen) und wird nicht verwendet."
                )
            )
            continue
        if len(files) >= max_files:
            notes.append(f"Datei „{label}“ wird nicht verwendet: höchstens {max_files} Dateien pro Recherche.")
            continue
        files.append(
            {
                "id": file.id,
                "name": label,
                "chars": len(text),
                "date": _file_date(file.created_at),
                "url": f"{base}/api/v1/files/{file.id}/content" if base else "",
                "text": text[:1500] if not with_text else text[:max_chars],
            }
        )
    if not (files or collections or notes):
        return None
    return {"files": files, "collections": collections, "base_url": base, "notes": notes}


def _own_lines(info: dict | None) -> list[str]:
    """The plan lines about the user's own sources (from the service's answer: names, sizes, what was cut, remarks)."""
    if not info:
        return []
    parts = [
        f"Datei „{f['name']}“ ({f['chars']:,} Zeichen{', gekürzt' if f.get('cut') else ''})".replace(",", "'")
        for f in info.get("files", [])
    ]
    parts += [f"Wissensbasis „{c['name']}“" for c in info.get("collections", [])]
    lines = []
    if parts:
        how = (
            "Es werden ausschliesslich diese Quellen verwendet."
            if info.get("mode") == "only"
            else "Sie werden zuerst verwendet, Web und Fachdatenbanken ergänzen."
            if info.get("mode") != "off"
            else ""
        )
        lines.append(f"*Eigene Quellen:* {'; '.join(parts)}. {how}".strip())
    lines += [f"*Hinweis:* {n}" for n in info.get("notes", [])]
    return lines


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _filename(job: dict) -> str:
    """File name of the Word file: the one the service proposes, else a generic one."""
    return job.get("docx_name") or f"Bebbi-Deep-Research_{job.get('id', 'Bericht')}.docx"


STATE_KEYS = {"clarify": ("task",), "job": ("job",)}
SERVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # job and draft ids: no "/", "#", "?" or "&" that change the path


def _marker(state: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(state, ensure_ascii=False).encode()).decode()
    return f"\n\n[//]: # (research-agent:{payload})"


def _decode(payload: str) -> dict:
    try:
        value = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _state(messages: list[dict]) -> dict:
    for m in reversed(messages[:-1]):
        if m.get("role") == "assistant":
            found = MARK.search(m.get("content") or "")
            st = _decode(found.group(1)) if found else {}
            # a forged or broken marker is no state; the user can edit the marker, so ids that go into a service path
            # must be plain ids (`<id>#` or `<id>?...` would cut off or replace the user_id owner check)
            ids_ok = all(isinstance(st[k], str) and SERVICE_ID.fullmatch(st[k]) for k in ("job", "draft") if k in st)
            return st if ids_ok and all(k in st for k in STATE_KEYS.get(st.get("state"), ("?",))) else {}
    return {}


async def _stored_parent(chat_id: str | None, metadata: dict | None) -> dict | None:
    """The assistant message that the current user message answers, read from the chat in Open WebUI's database.

    Since Open WebUI 0.12.4 context compaction also runs in short chats (user, assistant, user) that are over the token
    threshold, and it summarizes the assistant message away, together with our state marker. The chat in the database
    still has it. The user message comes from `__metadata__["user_message"]`; when a reply is regenerated the browser
    sends none and Open WebUI sets `__metadata__["user_message_id"]` itself (main.py), so that message is read from the
    same chat to find its parent."""
    if not chat_id:
        return None
    metadata = metadata or {}
    parent = (metadata.get("user_message") or {}).get("parentId")
    uid = metadata.get("user_message_id")
    if not (isinstance(parent, str) and parent) and isinstance(uid, str) and uid:
        from open_webui.models.chats import Chats

        user_message = await Chats.get_message_by_id_and_message_id(chat_id, uid)
        parent = (user_message or {}).get("parentId")
    if not (isinstance(parent, str) and parent):
        return None
    from open_webui.models.chats import Chats

    stored = await Chats.get_message_by_id_and_message_id(chat_id, parent)
    return stored if isinstance(stored, dict) and stored.get("role", "assistant") == "assistant" else None


async def _state_of_chat(messages: list[dict], chat_id: str | None, metadata: dict | None) -> dict:
    """The chat state: from the assistant messages of the request, else (compaction removed them) from the database."""
    if st := _state(messages):
        return st
    if any(m.get("role") == "assistant" for m in messages[:-1]):  # an answer without a marker: no state
        return {}
    try:
        stored = await _stored_parent(chat_id, metadata)
    except Exception as e:  # no Open WebUI around (script), or the lookup failed: as before, no state
        logger.warning("chat state not read from the stored chat (%s: %s)", type(e).__name__, e)
        return {}
    return _state([{"role": "assistant", "content": stored.get("content") or ""}, {}]) if stored else {}


_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>!|~#])")


def _plain(value, limit: int = 1000) -> str:
    """LLM-written text (plan, clarifying questions) as plain text in the chat: one line, Markdown characters escaped.

    The planner sees web snippets, so its text is untrusted: an image (`![](https://...)` loads without a click and
    could carry the task away), a link or raw HTML must not render. Escaped, they show as the characters they are.
    """
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(value or "").split())[:limit])


def _render_plan(plan: dict, depth: str, own: dict | None = None) -> str:
    lines = [f"**Rechercheplan: {_plain(plan['title'], 200)}**", "", f"*Auftrag:* {_plain(plan['task_core'])}", ""]
    for p in plan["parts"]:
        lines += [
            f"**{_plain(p['title'], 200)}**: {_plain(p['goal'])}",
            "",
        ]  # blank lines: else the next part title becomes part of the last list item
        lines += [f"- {_plain(q['text'])}" for q in plan["sub_questions"] if q["part"] == p["id"]]
        lines.append("")
    if plan.get("must_name"):
        lines += ["", f"*Muss abgedeckt werden:* {', '.join(_plain(m, 200) for m in plan['must_name'])}"]
    own_lines = _own_lines(own)
    if own_lines:
        lines += [""] + [ln + "  " for ln in own_lines[:-1]] + own_lines[-1:]
    lines += ["", f"Tiefe: **{depth}**."]
    return "\n".join(lines)


def _with_preset(shown: str, label: str) -> str:
    """The plan with the chosen preset named in its last line (model "Deep Research" only)."""
    return shown[: -len(".")] + f", Recherche: **{label}**." if label else shown


class Pipe:
    class Valves(BaseModel):
        SERVICE_URL: str = Field(
            default="http://bebbi-deep-research:8088", description="base URL of the bebbi-deep-research job service"
        )
        SERVICE_TOKEN: str = Field(
            default="", description="bearer token of the job service (= RA_TOKEN of the service)"
        )
        DEFAULT_DEPTH: str = Field(default="standard", description="quick | standard | deep")
        POLL_SECONDS: int = Field(default=10)
        DECISION_MINUTES: int = Field(
            default=30, description="how long the approve/change/discard card waits for the user"
        )
        PRESETS: str = Field(
            default=(
                "rechtsrecherche:Recht,grossrat:Grosser Rat,statistik:Statistik,verwaltung_web:Verwaltung,"
                "web_survey:Web,dokumente:Eigene Dokumente"
            ),
            description="preset:label pairs offered in the preset choice (order of the card)",
        )
        PRESET_MODELS: bool = Field(
            default=False,
            description="also list one model per preset (Deep Research · Label), which skip the preset choice",
        )
        DEFAULT_PRESET: str = Field(
            default="verwaltung_web",
            description="preset when the service suggests none and no choice is possible (no browser session)",
        )
        OWN_SOURCES: bool = Field(
            default=True, description="use the chat's uploaded files and knowledge bases as sources"
        )
        PUBLIC_URL: str = Field(
            default="",
            description="public base URL of Open WebUI for links to uploaded files (empty = WEBUI_URL setting)",
        )
        OWN_MAX_FILES: int = Field(default=OWN_MAX_FILES, description="files per research")
        OWN_MAX_FILE_CHARS: int = Field(
            default=OWN_MAX_FILE_CHARS, description="extracted text per file sent to the service"
        )

    def __init__(self):
        self.type = "manifold"
        self.valves = self.Valves()

    def _labels(self) -> dict[str, str]:
        """preset name -> label from the PRESETS valve, in its order."""
        out = {}
        for pair in self.valves.PRESETS.split(","):
            name, _, label = pair.partition(":")
            if name.strip():
                out[name.strip()] = label.strip() or name.strip()
        return out

    def pipes(self) -> list[dict]:
        out = [{"id": AUTO, "name": "Deep Research"}]
        if self.valves.PRESET_MODELS:
            out += [{"id": name, "name": f"Deep Research · {label}"} for name, label in self._labels().items()]
        return out

    def _headers(self) -> dict[str, str]:
        rid = REQUEST_ID.get()
        return {
            "Authorization": f"Bearer {self.valves.SERVICE_TOKEN}",
            "X-DR-API-Version": API_VERSION,
            **({"X-Request-ID": rid} if rid else {}),
        }

    async def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=300) as http:
                r = await http.request(
                    method, self.valves.SERVICE_URL.rstrip("/") + path, json=body, headers=self._headers()
                )
        except httpx.TransportError as e:
            raise RuntimeError(
                f"Der Recherche-Dienst ist nicht erreichbar ({type(e).__name__}). Bitte später erneut versuchen."
            ) from e
        if r.status_code >= 400:
            is_json = r.headers.get("content-type", "").startswith("application/json")
            body = r.json() if is_json else {}
            # service errors: {"errorId", "status", "debugMessage"} (German text for the user);
            # request validation: "detail"
            detail = body.get("debugMessage") or body.get("detail") or r.text
            if r.status_code == 404:
                raise NotFound(detail)
            if r.status_code == 422 and not isinstance(detail, str):  # request validation: the API does not fit
                raise RuntimeError(
                    "Der Recherche-Dienst versteht die Anfrage nicht (die Versionen von Pipe und Dienst passen "
                    "nicht zusammen). Bitte wenden Sie sich an den Betrieb."
                )
            # 422: a refusal meant for the user; 409: another API version (German text of the service)
            raise (UserError if r.status_code in (409, 422) and isinstance(detail, str) else RuntimeError)(detail)
        return r.json()

    async def _download(self, path: str) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=120) as http:
                r = await http.get(self.valves.SERVICE_URL.rstrip("/") + path, headers=self._headers())
        except httpx.TransportError as e:
            raise RuntimeError(f"Der Recherche-Dienst ist nicht erreichbar ({type(e).__name__})") from e
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.content

    @staticmethod
    def _task_reply(task: str, body: dict) -> str:
        """Answer Open WebUI's background tasks (chat title, tags, follow-ups) locally.

        Open WebUI sends them to the model of the chat. They must not start
        a research: answer them locally with the JSON the task expects."""
        if task == "title_generation":
            prompt = " ".join(str(m.get("content", "")) for m in body.get("messages", []))
            found = MARK.search(
                prompt
            )  # the chat history of the task carries our state marker (clarify state: the task)
            task_text = _decode(found.group(1)).get("task", "") if found else ""
            task_text = task_text if isinstance(task_text, str) else ""
            # plan state: the title of the plan (rendered escaped, see _plain)
            plan_title = re.search(r"\*\*Rechercheplan: ((?:\\.|[^*])+?)\*\*", prompt)
            shown = re.sub(r"\\(.)", r"\1", plan_title.group(1)) if plan_title else ""
            title = " ".join((task_text or shown).split())[:50] or "Deep Research"
            return json.dumps({"title": title}, ensure_ascii=False)
        if task == "tags_generation":
            return json.dumps({"tags": ["Recherche"]}, ensure_ascii=False)
        if task == "follow_up_generation":
            return json.dumps({"follow_ups": []})
        return ""

    async def _own(self, request, user: dict, body: dict, files: list, with_text: bool) -> dict | None:
        if not self.valves.OWN_SOURCES or not (user or {}).get("id"):
            return None
        return await collect_own_sources(
            request,
            user["id"],
            body.get("model", ""),
            files,
            with_text,
            self.valves.PUBLIC_URL,
            self.valves.OWN_MAX_FILES,
            self.valves.OWN_MAX_FILE_CHARS,
        )

    async def _choose_preset(self, call, suggested: str | None) -> str | None:
        """Let the user confirm the suggested preset or pick another in Open WebUI's ask-user card.

        Returns the preset name, None when there is no answer (card closed or timed out, browser gone). Without a
        browser session (no card possible) the suggestion, else DEFAULT_PRESET, is taken."""
        labels = self._labels()
        if suggested not in labels:
            suggested = None
        if call is None:
            return suggested or self.valves.DEFAULT_PRESET
        try:
            descriptions = {p["name"]: p.get("description", "") for p in await self._api("GET", "/presets")}
        except Exception:
            descriptions = {}
        names = ([suggested] if suggested else []) + [n for n in labels if n != suggested]
        options = [
            {
                "label": labels[n] + (" (empfohlen)" if n == suggested else ""),
                "description": descriptions.get(n, ""),
            }
            for n in names
        ]
        seconds = 60 * max(1, self.valves.DECISION_MINUTES)
        question = {
            "id": "preset",
            "header": "Recherche",
            "question": "Wo soll recherchiert werden?",
            "options": options,
            "allow_other": False,
        }
        try:
            res = await asyncio.wait_for(
                call(
                    {
                        "type": "request:user_input",
                        "data": {"questions": [question], "allow_other": False, "timeout_ms": seconds * 1000},
                    }
                ),
                timeout=seconds + 30,
            )
        except Exception:
            return None
        answer = (
            ((res or {}).get("answers") or {}).get("preset")
            if isinstance(res, dict) and res.get("status") == "answered"
            else None
        )
        index = answer.get("option_index") if isinstance(answer, dict) and answer.get("type") == "option" else None
        return names[index] if isinstance(index, int) and 0 <= index < len(names) else None

    async def _decide(self, call, can_change: bool) -> tuple[str, str]:
        """Ask the user what happens with the plan.

        Open WebUI's ask-user card (`request:user_input`, buttons only, no free text), for "Ändern" followed by an
        input dialog for the change. Returns ("approve" | "change" | "discard", change text), or ("open", "") when
        there is no answer (card cancelled or timed out, browser gone, no browser at all).
        """
        if call is None:
            return "open", ""
        seconds = 60 * max(1, self.valves.DECISION_MINUTES)
        options = [(APPROVE, "Die Recherche wird im Nachtfenster ausgeführt; der Bericht erscheint in diesem Chat.")]
        if can_change:
            options.append(
                (CHANGE, "Sie beschreiben, was anders sein soll; der Plan wird angepasst und erneut vorgelegt.")
            )
        options.append((DISCARD, "Keine Recherche starten."))
        question = {
            "id": "plan",
            "header": "Rechercheplan",
            "question": "Wie möchten Sie mit dem Plan fortfahren?",
            "options": [{"label": label, "description": d} for label, d in options],
            "allow_other": False,
        }

        async def ask(event: dict):
            try:
                return await asyncio.wait_for(
                    call(event), timeout=seconds + 30
                )  # the card closes itself after `seconds`
            except Exception:
                return None

        while True:
            res = await ask(
                {
                    "type": "request:user_input",
                    "data": {"questions": [question], "allow_other": False, "timeout_ms": seconds * 1000},
                }
            )
            answer = (
                ((res or {}).get("answers") or {}).get("plan")
                if isinstance(res, dict) and res.get("status") == "answered"
                else None
            )
            index = answer.get("option_index") if isinstance(answer, dict) and answer.get("type") == "option" else None
            if not isinstance(index, int) or not 0 <= index < len(options):
                return "open", ""
            choice = options[index][0]
            if choice == APPROVE:
                return "approve", ""
            if choice == DISCARD:
                return "discard", ""
            change = await ask(
                {
                    "type": "input",
                    "data": {
                        "title": "Plan ändern",
                        "message": "Was soll am Plan geändert werden?",
                        "placeholder": "z. B. Teil 3 weglassen, nur Basel-Stadt, Zeitraum ab 2020",
                    },
                }
            )
            if isinstance(change, str) and change.strip():
                return "change", change.strip()
            # dialog cancelled or empty: back to the choice

    async def pipe(
        self,
        body: dict,
        __user__: dict,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
        __task__=None,
        __request__=None,
        __files__=None,
        __metadata__=None,
        __event_call__=None,
    ):
        if __task__:
            return self._task_reply(str(__task__), body)
        REQUEST_ID.set(request_id(__chat_id__, __message_id__))  # this turn's task only (Open WebUI runs one per call)

        async def status(text: str, done: bool = False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": text, "done": done}})

        async def hold(text: str) -> str:
            """Text of a message that the service replaces with the report later. Open WebUI v0.11 renders the stored
            `output` of a message in preference to its `content`, and no API lets the service change `output`. So the
            text
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
        choose = preset not in self._labels()  # model "Deep Research": the preset is chosen per task
        st: dict = {}
        uid = (__user__ or {}).get("id") or "?"

        async def submit(st: dict) -> str:
            """Queue the job of the approved plan for the night window (for now every research runs at night)."""
            own = await self._own(__request__, __user__, body, chat_files, with_text=True)
            job = await self._api(
                "POST",
                "/jobs",
                {
                    "draft_id": st["draft"],
                    "user_id": uid,
                    "chat_id": __chat_id__,
                    "message_id": __message_id__,
                    "when": "night",
                    **({"own_sources": own} if own else {}),
                },
            )
            st.clear()
            st.update({"state": "job", "job": job["job_id"]})
            own_note = (
                "\n\n" + "\n".join(_own_lines(job.get("own_sources"))) if _own_lines(job.get("own_sources")) else ""
            )
            return await hold(
                f"Auftrag eingereiht für das Nachtfenster ({job['window']}). Der Bericht erscheint "
                "in diesem Chat, sobald er fertig ist." + own_note + _marker(st)
            )

        async def change(st: dict, instruction: str) -> tuple[dict, dict | None]:
            await status("Passe den Plan an")
            res = await self._api(
                "POST", f"/plans/{quote(st['draft'], safe='')}/edit", {"instruction": instruction, "user_id": uid}
            )
            st["edits"] = st.get("edits", 0) + 1
            await status("Plan angepasst", done=True)
            return res["plan"], res.get("own_sources")

        async def review(plan: dict, own_info: dict | None, st: dict) -> str:
            """Show the plan and let the user decide in the card: start, change (plan shown again) or discard."""
            while True:
                shown = _with_preset(_render_plan(plan, st["depth"], own_info), st.get("label", ""))
                if __event_call__ is not None and __event_emitter__:
                    await __event_emitter__(
                        {"type": "replace", "data": {"content": shown}}
                    )  # visible while the card is open
                choice, instruction = await self._decide(__event_call__, st.get("edits", 0) < 3)
                if choice == "approve":
                    return await submit(st)
                if choice == "discard":
                    return shown + "\n\n*Plan verworfen, es wurde keine Recherche gestartet.*"
                if choice == "open":  # no answer counts as no
                    return (
                        shown + "\n\n*Keine Freigabe erhalten, es wurde keine Recherche gestartet. "
                        "Stellen Sie die Frage erneut, um neu zu beginnen.*"
                    )
                plan, own_info = await change(st, instruction)

        try:
            st = await _state_of_chat(messages, __chat_id__, __metadata__)
            # --- running / finished job: report status or result
            if st.get("state") == "job":
                if text.strip().strip(".!").lower() in CANCEL_WORDS:
                    try:
                        await self._api("POST", _jpath(f"/jobs/{quote(st['job'], safe='')}/cancel", uid))
                        return CANCELLED
                    except UserError:
                        pass  # it has ended already: show what there is
                job = await self._api("GET", _jpath(f"/jobs/{quote(st['job'], safe='')}", uid))
                if job["status"] == "done":
                    return await self._publish(job, __event_emitter__, __user__, __request__)
                if job["status"] == "error":
                    return f"Die Recherche ist fehlgeschlagen: {job.get('error')}"
                if job["status"] == "cancelled":
                    return CANCELLED
                last = job["events"][-1]["text"] if job["events"] else "wartet in der Warteschlange"
                return (
                    f"Die Recherche läuft noch ({job['status']}: {last}). Der Bericht erscheint in diesem Chat. "
                    "Schreiben Sie **abbrechen**, um sie abzubrechen." + _marker(st)
                )

            # --- new task (or answers to clarifying questions)
            depth = next((d for d, rx in DEPTH_WORDS.items() if rx.search(text)), self.valves.DEFAULT_DEPTH)
            own = await self._own(
                __request__, __user__, body, chat_files, with_text=False
            )  # previews: enough to clarify and plan
            if st.get("state") == "clarify":
                task, depth = st["task"] + "\n\nPräzisierungen: " + text, st.get("depth", depth)
                if choose:  # the preset chosen before the questions (a forged one falls back to the default)
                    preset = st.get("preset") if st.get("preset") in self._labels() else self.valves.DEFAULT_PRESET
            else:
                task = text
                await status("Prüfe, ob Rückfragen nötig sind")
                c = await self._api(
                    "POST",
                    "/clarify",
                    {
                        "task": task,
                        **({"suggest_preset": True} if choose else {"preset": preset}),
                        "depth": depth,
                        **({"own_sources": own} if own else {}),
                    },
                )
                if choose:
                    await status("Wählen Sie, wo recherchiert wird", done=True)
                    picked = await self._choose_preset(__event_call__, c.get("preset"))
                    if picked is None:
                        return (
                            "*Keine Auswahl erhalten, es wurde keine Recherche gestartet. "
                            "Stellen Sie die Frage erneut, um neu zu beginnen.*"
                        )
                    preset = picked
                if c["questions"]:
                    await status("Rückfragen", done=True)
                    qs = "\n".join(f"{i}. {_plain(q)}" for i, q in enumerate(c["questions"], 1))
                    assume = (
                        ("\n\nOhne Antwort gehe ich davon aus: " + "; ".join(_plain(a) for a in c["assumptions"]))
                        if c["assumptions"]
                        else ""
                    )
                    return (
                        f"Bevor ich den Rechercheplan erstelle:\n\n{qs}{assume}\n\nAntworten Sie kurz "
                        "(oder mit **weiter**, um mit diesen Annahmen fortzufahren)."
                    ) + _marker(
                        {"state": "clarify", "task": task, "depth": depth, **({"preset": preset} if choose else {})}
                    )
            await status("Erstelle Rechercheplan")
            res = await self._api(
                "POST",
                "/plans",
                {
                    "task": task,
                    "preset": preset,
                    "depth": depth,
                    "user_id": uid,
                    **({"own_sources": own} if own else {}),
                },
            )
            await status("Plan erstellt", done=True)
            st = {"state": "plan", "draft": res["draft_id"], "depth": depth}
            if choose:
                st["label"] = self._labels().get(preset, preset)
            return await review(res["plan"], res.get("own_sources"), st)
        except Exception as e:
            if isinstance(e, NotFound):
                st = {}  # job or draft vanished: drop the marker so the next message starts anew
                msg = "Die Recherche wurde im Dienst nicht gefunden. Bitte stellen Sie die Frage neu."
            else:
                msg = str(e) if isinstance(e, UserError) else f"Deep Research ist nicht verfügbar: {e}"
            await status("Hinweis" if isinstance(e, UserError) else "Fehler", done=True)
            if st.get("state") == "job":  # a job exists: the service delivers its report into this message later
                return await hold(msg + _marker(st))
            return msg + (_marker(st) if st.get("state") == "clarify" else "")  # a plan without an answer is not kept

    async def _publish(self, job: dict, emitter, user: dict, request) -> str:
        """The report of a finished job for this message: one `source` event per source (Open WebUI shows `[n]` as a
        citation badge of the n-th source and lists the sources under the message) and the Word file as a message file.
        Returns the report text. Sources and file are best effort; the text is always returned."""
        text = job.get("ui_report") or job["report"]
        if emitter:
            # The service delivers the report into the same message through the image's delivery route. If it did,
            # the sources are there already; stored twice they would double the list and shift nothing but confuse.
            for _ in range(10):
                if job.get("delivery") or not job.get("id"):
                    break
                await asyncio.sleep(min(self.valves.POLL_SECONDS, 2))
                job = await self._api("GET", _jpath(f"/jobs/{job['id']}", (user or {}).get("id") or "?"))
            for event in [] if job.get("delivery") == "ok" else job.get("owui_events") or []:  # [n] is the n-th event
                await emitter(event)
            if job.get("docx") and request is not None and (user or {}).get("id"):
                try:
                    data = await self._download(_jpath(job["docx"], user["id"]))
                    name = _filename(job)
                    item = await store_user_file(request, user["id"], name, data, DOCX)
                    await emitter({"type": "files", "data": {"files": [item]}})
                except Exception as e:  # the report is more important than the file
                    text += f"\n\n*Die Word-Datei konnte nicht angehängt werden ({type(e).__name__}).*"
        return text

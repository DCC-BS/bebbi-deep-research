"""
title: Word-Datei (Deep Research)
description: Hängt den Recherchebericht als Word-Datei an die Antwort an (Schaltfläche unter der Nachricht).
version: 1.0.0
requirements: httpx
icon_url: data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJjdXJyZW50Q29sb3IiIHN0cm9rZS13aWR0aD0iMS41Ij48cGF0aCBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiIGQ9Ik0zIDE2LjV2Mi4yNUEyLjI1IDIuMjUgMCAwIDAgNS4yNSAyMWgxMy41QTIuMjUgMi4yNSAwIDAgMCAyMSAxOC43NVYxNi41TTE2LjUgMTIgMTIgMTYuNW0wIDBMNy41IDEybTQuNSA0LjVWMyIvPjwvc3ZnPg==
"""

# Open WebUI action function, companion of pipe/deep_research.py. A report that the job service delivers later (night
# run) is written into the chat by the service; a file uploaded with the service's admin key would belong to the admin and give
# the user a 404. This action runs inside Open WebUI as the user: it fetches the Word file of the job from the
# service and stores it as a file of the user, attached to the message. The pipe does the same right away when it
# returns the report itself.

import io

import httpx
from pydantic import BaseModel, Field

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


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


class Action:
    class Valves(BaseModel):
        SERVICE_URL: str = Field(
            default="http://bebbi-deep-research:8088", description="base URL of the bebbi-deep-research job service"
        )
        SERVICE_TOKEN: str = Field(
            default="", description="bearer token of the job service (= RA_TOKEN of the service)"
        )

    def __init__(self):
        self.valves = self.Valves()

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        async with httpx.AsyncClient(timeout=120) as http:
            return await http.get(
                self.valves.SERVICE_URL.rstrip("/") + path,
                params=params,
                headers={"Authorization": f"Bearer {self.valves.SERVICE_TOKEN}"},
            )

    async def action(self, body: dict, __user__: dict | None = None, __event_emitter__=None, __request__=None):
        async def toast(kind: str, text: str):
            if __event_emitter__:
                await __event_emitter__({"type": "notification", "data": {"type": kind, "content": text}})

        chat_id, message_id, user_id = body.get("chat_id"), body.get("id"), (__user__ or {}).get("id")
        if not (chat_id and message_id and user_id and __request__ is not None):
            return await toast("error", "Die Word-Datei kann nur in einem gespeicherten Chat erstellt werden.")
        try:
            found = await self._get("/jobs/lookup", {"chat_id": chat_id, "message_id": message_id, "user_id": user_id})
            if found.status_code == 404:
                return await toast("warning", "Zu dieser Nachricht gehört keine Recherche.")
            found.raise_for_status()
            job = found.json()
            if job["status"] != "done":
                return await toast("warning", "Die Recherche ist noch nicht fertig.")
            info = (await self._get(f"/jobs/{job['job_id']}", {"user_id": user_id})).json()
            name = info.get("docx_name") or f"Bebbi-Deep-Research_{job['job_id']}.docx"
            from open_webui.models.chats import Chats

            attached = await Chats.get_message_metadata(chat_id, message_id, "files")
            if isinstance(attached, list) and any(isinstance(f, dict) and f.get("name") == name for f in attached):
                return await toast("info", "Die Word-Datei hängt bereits an dieser Nachricht.")
            data = await self._get(info["docx"], {"user_id": user_id})
            data.raise_for_status()
            item = await store_user_file(__request__, user_id, name, data.content, DOCX)
            if __event_emitter__:
                await __event_emitter__({"type": "files", "data": {"files": [item]}})
            await toast("success", "Die Word-Datei wurde an die Nachricht angehängt.")
        except Exception as e:
            await toast("error", f"Die Word-Datei konnte nicht erstellt werden ({type(e).__name__}).")

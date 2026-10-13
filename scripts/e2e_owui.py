#!/usr/bin/env python3
"""Drive a local Open WebUI like the browser does (chat create, completion with chat_id/message ids, store the answer).

  scripts/e2e_owui.py new  [--model bebbi_deep_research.grossrat]      -> prints the chat id
  scripts/e2e_owui.py say  CHAT_ID "text"  [--model ...] -> sends a user turn, prints the assistant answer
  scripts/e2e_owui.py show CHAT_ID [--raw]                              -> prints the stored messages
  scripts/e2e_owui.py say CHAT_ID "text" --file doc.pdf --kb KNOWLEDGE_ID
      -> the turn carries an uploaded file / knowledge base
  scripts/e2e_owui.py kb-create NAME doc1.txt doc2.txt [--admin]
      -> creates a knowledge base (as the test user, or as admin), prints its id
  scripts/e2e_owui.py upload doc.pdf
      -> uploads a file like the chat input, prints the file id
Own sources (A4): a file is uploaded with POST /api/v1/files/ (processing runs in the background; the browser polls
/files/{id}/process/status) and attached to the turn as {"type": "file", "id", "name", "file": <record>, ...}; a
knowledge base as {"type": "collection", "id", "name", ...}. Both go into the `files` of the completion request and of
the user message.

Reads .env (TEST_USER_EMAIL, TEST_USER_PASSWORD, for --admin WEBUI_ADMIN_EMAIL/PASSWORD; OWUI_PUBLIC_URL defaults to
http://localhost:3000). Test accounts only.
Assistant message ids are printed so a later `show` can be compared with what the service delivered.
"""

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict:
    env = dict(os.environ)
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    return env


class Client:
    def __init__(self, env: dict | None = None, admin: bool = False):
        env = env or load_env()
        self.http = httpx.Client(
            base_url=env.get("OWUI_PUBLIC_URL", "http://localhost:3000"), timeout=httpx.Timeout(1800, connect=10)
        )
        email, password = (
            (env["WEBUI_ADMIN_EMAIL"], env["WEBUI_ADMIN_PASSWORD"])
            if admin
            else (env["TEST_USER_EMAIL"], env["TEST_USER_PASSWORD"])
        )
        r = self.http.post("/api/v1/auths/signin", json={"email": email, "password": password})
        r.raise_for_status()
        self.http.headers["Authorization"] = "Bearer " + r.json()["token"]

    def new_chat(self, model: str) -> str:
        chat = {
            "title": "Neuer Chat",
            "models": [model],
            "params": {},
            "history": {"messages": {}, "currentId": None},
            "messages": [],
            "tags": [],
            "timestamp": int(time.time() * 1000),
        }
        r = self.http.post("/api/v1/chats/new", json={"chat": chat})
        r.raise_for_status()
        return r.json()["id"]

    def get_chat(self, chat_id: str) -> dict:
        r = self.http.get(f"/api/v1/chats/{chat_id}")
        r.raise_for_status()
        return r.json()

    def path(self, chat: dict) -> list[dict]:
        hist = chat["chat"]["history"]
        out, cur = [], hist.get("currentId")
        while cur:
            m = hist["messages"][cur]
            out.append(m)
            cur = m.get("parentId")
        return out[::-1]

    def upload(self, path: str, timeout: float = 120) -> dict:
        """Upload a file like the chat input does and wait until Open WebUI has extracted its text.

        Returns the chat file item.
        """
        p = Path(path)
        r = self.http.post("/api/v1/files/", params={"process": "true"}, files={"file": (p.name, p.read_bytes())})
        r.raise_for_status()
        rec = r.json()
        end = time.time() + timeout
        while time.time() < end:
            st = self.http.get(f"/api/v1/files/{rec['id']}/process/status").json().get("status")
            if st in ("completed", "failed"):
                break
            time.sleep(1)
        rec = self.http.get(f"/api/v1/files/{rec['id']}").json()
        return {
            "type": "file",
            "file": rec,
            "id": rec["id"],
            "url": f"/api/v1/files/{rec['id']}",
            "name": p.name,
            "collection_name": rec.get("meta", {}).get("collection_name", ""),
            "status": "uploaded",
            "size": p.stat().st_size,
            "error": "",
        }

    def file_item(self, file_id: str) -> dict:
        """The chat item of a file that is already uploaded (the browser sends it again with every turn of the chat)."""
        rec = self.http.get(f"/api/v1/files/{file_id}").json()
        return {
            "type": "file",
            "file": rec,
            "id": rec["id"],
            "url": f"/api/v1/files/{rec['id']}",
            "name": rec["filename"],
            "collection_name": rec.get("meta", {}).get("collection_name", ""),
            "status": "uploaded",
            "size": rec.get("meta", {}).get("size", 0),
            "error": "",
        }

    def create_kb(self, name: str, paths: list[str], description: str = "Testwissensbasis") -> dict:
        """Create a knowledge base with the given files (like Workspace > Knowledge). Returns the chat item for it."""
        r = self.http.post("/api/v1/knowledge/create", json={"name": name, "description": description})
        r.raise_for_status()
        kb = r.json()
        for path in paths:
            item = self.upload(path)
            a = self.http.post(f"/api/v1/knowledge/{kb['id']}/file/add", json={"file_id": item["id"]})
            a.raise_for_status()
        return {"type": "collection", **{k: kb[k] for k in ("id", "name", "description")}, "status": "processed"}

    def say(
        self, chat_id: str, text: str, model: str, timeout: float = 900, files: list[dict] | None = None
    ) -> tuple[str, str]:
        """One user turn exactly as the UI does it. Returns (assistant message id, answer text)."""
        chat = self.get_chat(chat_id)
        prev = self.path(chat)
        parent = chat["chat"]["history"].get("currentId")
        uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
        user = {
            "id": uid,
            "parentId": parent,
            "childrenIds": [aid],
            "role": "user",
            "content": text,
            "timestamp": int(time.time()),
            "models": [model],
            **({"files": files} if files else {}),
        }
        msgs = [{"role": m["role"], "content": m["content"]} for m in prev] + [{"role": "user", "content": text}]
        # Open WebUI 0.11 stores both messages itself: parent_id is the previous assistant message,
        # user_message the new one
        payload = {
            "model": model,
            "messages": msgs,
            "stream": True,
            "chat_id": chat_id,
            "id": aid,
            "session_id": uuid.uuid4().hex[:20],
            "parent_id": parent,
            "user_message": user,
            "background_tasks": {"title_generation": True, "tags_generation": True, "follow_up_generation": True},
            "features": {},
            "params": {},
            **({"files": files} if files else {}),
        }
        # With chat_id + message id Open WebUI runs the completion as a background task and the browser receives the
        # result over the websocket; the backend stores the finished message. We poll the stored chat instead.
        r = self.http.post("/api/chat/completions", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
        answer = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            m = self.get_chat(chat_id)["chat"]["history"]["messages"][aid]
            if m.get("done") and m.get("content"):
                answer = m["content"]
                break
        else:
            raise TimeoutError("assistant message not finished in time")
        return aid, answer


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["new", "say", "show", "upload", "kb-create"])
    ap.add_argument("chat_id", nargs="?", help="chat id; for upload the file, for kb-create the name")
    ap.add_argument("text", nargs="*")
    ap.add_argument("--file", action="append", default=[], help="file to upload and attach to the turn")
    ap.add_argument(
        "--file-id", action="append", default=[], help="id of an already uploaded file to attach again (later turns)"
    )
    ap.add_argument("--kb", action="append", default=[], help="knowledge base id to attach to the turn")
    ap.add_argument("--admin", action="store_true", help="act as the admin (kb-create)")
    ap.add_argument("--model", default="bebbi_deep_research.grossrat")
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()
    cl = Client(admin=a.admin)
    if a.cmd == "new":
        print(cl.new_chat(a.model))
    elif a.cmd == "upload":
        print(cl.upload(a.chat_id)["id"])
    elif a.cmd == "kb-create":
        print(cl.create_kb(a.chat_id, a.text)["id"])
    elif a.cmd == "say":
        files = [cl.upload(f) for f in a.file] + [cl.file_item(i) for i in a.file_id]
        for kb in a.kb:
            info = cl.http.get(f"/api/v1/knowledge/{kb}").json()
            files.append(
                {
                    "type": "collection",
                    "id": kb,
                    "name": info.get("name", kb),
                    "description": info.get("description", ""),
                    "status": "processed",
                }
            )
        aid, ans = cl.say(a.chat_id, " ".join(a.text), a.model, files=files or None)
        print(f"[assistant message {aid}]\n{ans}")
    else:
        for m in cl.path(cl.get_chat(a.chat_id)):
            c = m["content"]
            print(f"--- {m['role']} {m['id']} ({len(c)} chars)\n{c if a.raw or len(c) < 1500 else c[:1500] + ' …'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

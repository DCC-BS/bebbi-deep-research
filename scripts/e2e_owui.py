#!/usr/bin/env python3
"""Drive a local Open WebUI like the browser does (chat create, completion with chat_id/message ids, store the answer).

  scripts/e2e_owui.py new  [--model bebbi_deep_research.grossrat]      -> prints the chat id
  scripts/e2e_owui.py say  CHAT_ID "text"  [--model ...] -> sends a user turn, prints the assistant answer
  scripts/e2e_owui.py show CHAT_ID [--raw]                              -> prints the stored messages

Reads .env (TEST_USER_EMAIL, TEST_USER_PASSWORD; OWUI_URL defaults to http://localhost:3000). Test accounts only.
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
    def __init__(self, env: dict | None = None):
        env = env or load_env()
        self.http = httpx.Client(base_url=env.get("OWUI_PUBLIC_URL", "http://localhost:3000"), timeout=httpx.Timeout(1800, connect=10))
        r = self.http.post("/api/v1/auths/signin", json={"email": env["TEST_USER_EMAIL"], "password": env["TEST_USER_PASSWORD"]})
        r.raise_for_status()
        self.http.headers["Authorization"] = "Bearer " + r.json()["token"]

    def new_chat(self, model: str) -> str:
        chat = {"title": "Neuer Chat", "models": [model], "params": {}, "history": {"messages": {}, "currentId": None},
                "messages": [], "tags": [], "timestamp": int(time.time() * 1000)}
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

    def say(self, chat_id: str, text: str, model: str, timeout: float = 900) -> tuple[str, str]:
        """One user turn exactly as the UI does it. Returns (assistant message id, answer text)."""
        chat = self.get_chat(chat_id)
        prev = self.path(chat)
        parent = chat["chat"]["history"].get("currentId")
        uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
        user = {"id": uid, "parentId": parent, "childrenIds": [aid], "role": "user", "content": text,
                "timestamp": int(time.time()), "models": [model]}
        msgs = [{"role": m["role"], "content": m["content"]} for m in prev] + [{"role": "user", "content": text}]
        # Open WebUI 0.11 stores both messages itself: parent_id is the previous assistant message, user_message the new one
        payload = {"model": model, "messages": msgs, "stream": True, "chat_id": chat_id, "id": aid, "session_id": uuid.uuid4().hex[:20],
                   "parent_id": parent, "user_message": user,
                   "background_tasks": {"title_generation": True, "tags_generation": True, "follow_up_generation": True},
                   "features": {}, "params": {}}
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
    ap.add_argument("cmd", choices=["new", "say", "show"])
    ap.add_argument("chat_id", nargs="?")
    ap.add_argument("text", nargs="?")
    ap.add_argument("--model", default="bebbi_deep_research.grossrat")
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()
    cl = Client()
    if a.cmd == "new":
        print(cl.new_chat(a.model))
    elif a.cmd == "say":
        aid, ans = cl.say(a.chat_id, a.text, a.model)
        print(f"[assistant message {aid}]\n{ans}")
    else:
        for m in cl.path(cl.get_chat(a.chat_id)):
            c = m["content"]
            print(f"--- {m['role']} {m['id']} ({len(c)} chars)\n{c if a.raw or len(c) < 1500 else c[:1500] + ' …'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

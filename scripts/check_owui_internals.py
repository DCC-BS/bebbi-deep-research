#!/usr/bin/env python3
"""Check the Open WebUI internals that pipe/deep_research.py imports, inside a running Open WebUI container.

Run it after an Open WebUI upgrade (integration.md section 9), with a user, a file of that user and a knowledge base of
that user that exist in the instance (optional CHAT_ID: a chat with at least two turns, for the stored-parent lookup):

  docker cp pipe/deep_research.py scripts/check_owui_internals.py <container>:/tmp/
  docker exec -u 0 -w /app/backend -e PYTHONPATH=/app/backend <container> \\
      python /tmp/check_owui_internals.py USER_ID FILE_ID KNOWLEDGE_ID [CHAT_ID]

Prints what each helper returns; a renamed module or a changed signature ends in a traceback.
"""

import asyncio
import importlib.util
import inspect
import sys
from pathlib import Path


def load_pipe():
    spec = importlib.util.spec_from_file_location("pipe", Path(__file__).with_name("deep_research.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def main(user_id: str, file_id: str, kb_id: str, chat_id: str = "") -> None:
    pipe = load_pipe()
    from open_webui.models.chats import Chats
    from open_webui.retrieval.utils import filter_accessible_collections
    from open_webui.routers.files import upload_file_handler
    from open_webui.utils.access_control.files import has_access_to_file

    for fn in (
        has_access_to_file,
        filter_accessible_collections,
        Chats.get_message_metadata,
        Chats.get_message_by_id_and_message_id,
        upload_file_handler,
    ):
        print(fn.__qualname__, inspect.signature(fn))
    user = await pipe._owui_user(user_id)
    print("user:", user.role)
    file = await pipe._owui_file(user, file_id)
    print("file:", file.filename, file.created_at, (file.meta or {}).get("name"), type(file.data).__name__)
    print("knowledge base:", (await pipe._owui_knowledge(user, kb_id)).name)
    # the chat state after context compaction (Open WebUI 0.12.4): the stored parent message of a user message
    chat = await Chats.get_chat_by_id(chat_id) if chat_id else None
    if chat is not None:
        messages = (chat.chat.get("history") or {}).get("messages") or {}
        child = next((m for m in messages.values() if m.get("role") == "user" and m.get("parentId")), None)
        if child:
            parent = await pipe._stored_parent(chat_id, {"user_message": {"parentId": child["parentId"]}})
            regen = await pipe._stored_parent(chat_id, {"user_message_id": child["id"]})
            print("stored parent:", (parent or {}).get("role"), "| via user_message_id:", (regen or {}).get("role"))
    print("model knowledge:", await pipe._owui_model_knowledge("bebbi_deep_research.grossrat"))


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:5]))

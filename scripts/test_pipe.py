#!/usr/bin/env python3
"""Simulate an Open WebUI chat against the pipe + job service (no Open WebUI needed).

  RA_TOKEN=... uv run python scripts/test_pipe.py "<task>" [preset] [--url http://127.0.0.1:8088] [--no-edit] \
      [--wait 30]

Turns: task -> (clarifying questions -> "weiter") -> plan -> ask-user card: "Ändern" + change (unless --no-edit), then
"Freigeben" -> job queued for the night window -> "Status?" every 15 s until the report is there or --wait minutes
passed.
The pipe queues every job for the night: run the service with a window that covers now (RA_NIGHT_WINDOW=00:00-23:59).
Put "schnell" into the task for depth quick. Exit code 0 if a report came back, 1 otherwise.
"""

import argparse
import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "pipe", Path(__file__).resolve().parent.parent / "pipe" / "deep_research.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


async def main(task: str, preset: str, url: str, edit: bool, wait_minutes: float) -> int:
    pipe = mod.Pipe()
    pipe.valves.SERVICE_URL = url
    pipe.valves.SERVICE_TOKEN = os.environ["RA_TOKEN"]
    pipe.valves.POLL_SECONDS = 5
    messages: list[dict] = []

    replaced: list[str] = []
    answers = (
        [
            {"status": "answered", "answers": {"plan": {"type": "option", "option_index": 1}}},
            "Bitte zusätzlich die Praxis des Datenschutzbeauftragten berücksichtigen.",
        ]
        if edit
        else []
    )
    answers.append({"status": "answered", "answers": {"plan": {"type": "option", "option_index": 0}}})

    async def emit(ev):
        if ev["type"] == "status":
            print(f"   [status] {ev['data']['description']}", flush=True)
        elif ev["type"] == "replace":
            replaced.append(ev["data"]["content"])

    async def call(ev):
        """The browser: answers the ask-user card and the change dialog in order."""
        answer = answers.pop(0) if answers else {"status": "cancelled", "answers": {}}
        print(f"   [{ev['type']}] -> {answer}", flush=True)
        return answer

    async def turn(user: str) -> str:
        messages.append({"role": "user", "content": user})
        print(f"\nUSER: {user}")
        replaced.clear()
        out = await pipe.pipe(
            {"model": f"bebbi_deep_research.{preset}", "messages": messages},
            {"id": "test-user"},
            emit,
            "chat-test",
            f"msg-{len(messages)}",
            __event_call__=call,
        )
        out = out or (replaced[-1] if replaced else "")  # a queued job answers with a replace event and an empty return
        messages.append({"role": "assistant", "content": out})
        shown = mod.MARK.sub("", out).strip()
        print(f"ASSISTANT ({len(out)} chars):\n{shown[:1800]}{' ...' if len(shown) > 1800 else ''}")
        return out

    print("models:", pipe.pipes())
    out = await turn(task)
    if mod._state([*messages, {}]).get("state") == "clarify":
        out = await turn("weiter")
    deadline = time.time() + 60 * wait_minutes
    while mod._state([*messages, {}]).get("state") == "job" and time.time() < deadline:
        await asyncio.sleep(15)
        out = await turn("Status?")
    ok = out.lstrip().startswith("#")
    print("\nRESULT:", "report received" if ok else "NO REPORT (still running, queued or failed)")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("preset", nargs="?", default="rechtsrecherche")
    ap.add_argument("--url", default=os.environ.get("RA_SERVICE_URL", "http://127.0.0.1:8088"))
    ap.add_argument("--no-edit", action="store_true")
    ap.add_argument("--wait", type=float, default=30, help="minutes to wait for the report")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.task, a.preset, a.url, not a.no_edit, a.wait)))

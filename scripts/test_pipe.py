#!/usr/bin/env python3
"""Simulate an Open WebUI chat against the pipe + job service (no Open WebUI needed).

  RA_TOKEN=... uv run python scripts/test_pipe.py "<task>" [preset] [--url http://127.0.0.1:8088] [--no-edit]

Turns: task -> (clarifying questions -> "weiter") -> plan -> plan edit -> "ok jetzt" -> report.
Put "schnell" into the task for depth quick: only quick runs of a focus preset start outside the night window.
Exit code 0 if a report came back, 1 otherwise.
"""

import argparse
import asyncio
import importlib.util
import os
import re
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("pipe", Path(__file__).resolve().parent.parent / "pipe" / "deep_research.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


async def main(task: str, preset: str, url: str, edit: bool) -> int:
    pipe = mod.Pipe()
    pipe.valves.SERVICE_URL = url
    pipe.valves.SERVICE_TOKEN = os.environ["RA_TOKEN"]
    pipe.valves.POLL_SECONDS = 5
    messages: list[dict] = []

    async def emit(ev):
        print(f"   [status] {ev['data']['description']}", flush=True)

    async def turn(user: str) -> str:
        messages.append({"role": "user", "content": user})
        print(f"\nUSER: {user}")
        out = await pipe.pipe({"model": f"bebbi_deep_research.{preset}", "messages": messages}, {"id": "test-user"},
                              emit, "chat-test", f"msg-{len(messages)}")
        messages.append({"role": "assistant", "content": out})
        shown = re.sub(r"<!--.*?-->", "", out, flags=re.S).strip()
        print(f"ASSISTANT ({len(out)} chars):\n{shown[:1800]}{' ...' if len(shown) > 1800 else ''}")
        return out

    print("models:", pipe.pipes())
    out = await turn(task)
    if '"state": "clarify"' in out:
        out = await turn("weiter")
    if edit:
        out = await turn("Bitte zusätzlich die Praxis des Datenschutzbeauftragten berücksichtigen.")
    out = await turn("ok jetzt")
    ok = out.lstrip().startswith("#")
    print("\nRESULT:", "report received" if ok else "NO REPORT (still running, queued or failed)")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("preset", nargs="?", default="rechtsrecherche")
    ap.add_argument("--url", default=os.environ.get("RA_SERVICE_URL", "http://127.0.0.1:8088"))
    ap.add_argument("--no-edit", action="store_true")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.task, a.preset, a.url, not a.no_edit)))

"""OpenAI-compatible LLM access with two roles (think / fast), JSON repair and usage counters."""

import json
import re
from dataclasses import dataclass, field

import json_repair
from openai import AsyncOpenAI


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    by_step: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLM:
    def __init__(self, base_url: str, api_key: str, models: dict[str, str] | None = None,
                 max_tokens: dict[str, int] | None = None, timeout: float = 1800,
                 extra_body: dict[str, dict] | None = None):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=3)
        self.models = models or {"think": "gemma-think", "fast": "gemma-fast", "embed": "qwen-embed"}
        self.max_tokens = max_tokens or {"think": 16000, "fast": 4000}
        # Per-role extra request body, e.g. {"think": {"chat_template_kwargs": {"enable_thinking": True}}}.
        # Empty by default = behaviour of the evaluated version (thinking is selected by the model alias).
        self.extra_body = extra_body or {}
        self.usage = Usage()

    def _kw(self, role: str) -> dict:
        eb = self.extra_body.get(role)
        return {"extra_body": eb} if eb else {}

    def _count(self, step: str, r) -> None:
        u = r.usage
        self.usage.calls += 1
        if u:
            self.usage.prompt_tokens += u.prompt_tokens or 0
            self.usage.completion_tokens += u.completion_tokens or 0
            self.usage.by_step[step] = self.usage.by_step.get(step, 0) + (u.prompt_tokens or 0) + (u.completion_tokens or 0)

    async def text(self, role: str, system: str, user: str, step: str, max_tokens: int | None = None) -> str:
        r = await self.client.chat.completions.create(
            model=self.models[role], max_tokens=max_tokens or self.max_tokens[role],
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **self._kw(role))
        self._count(step, r)
        out = (r.choices[0].message.content or "").strip()
        if not out and r.choices[0].finish_reason == "length":
            # Thinking tokens count towards max_tokens; an empty answer means the reasoning used it all.
            return await self.text(role, system, user, step, max_tokens=2 * (max_tokens or self.max_tokens[role]))
        return out

    async def json(self, role: str, system: str, user: str, step: str, default=None):
        """JSON output with repair; one retry with a stricter reminder. Returns `default` if both fail."""
        for attempt in range(2):
            out = await self.text(role, system, user if attempt == 0 else
                                  user + "\n\nReturn ONLY the JSON object, no prose, no code fences.", step)
            parsed = parse_json(out)
            if parsed is not None:
                return parsed
        return default

    async def chat(self, role: str, messages: list[dict], tools: list[dict] | None, step: str):
        r = await self.client.chat.completions.create(
            model=self.models[role], max_tokens=self.max_tokens[role], messages=messages, tools=tools or None,
            **self._kw(role))
        self._count(step, r)
        return r.choices[0].message

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for i in range(0, len(texts), 32):
            r = await self.client.embeddings.create(model=self.models["embed"], input=[t[:4000] for t in texts[i:i + 32]])
            out += [d.embedding for d in r.data]
        return out


def parse_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        try:
            v = json_repair.loads(m.group(0))
            return v if v not in ("", None) else None
        except Exception:
            return None

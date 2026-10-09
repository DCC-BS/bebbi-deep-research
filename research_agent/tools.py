"""Search (Serper-compatible endpoint), fetch (Firecrawl), passage focus (embeddings) and MCP access."""

import asyncio
import json
import math
import time
from urllib.parse import urlparse

import httpx


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def domain_match(url: str, domains: list[str]) -> bool:
    d = domain(url)
    return any(d == x or d.endswith("." + x) for x in domains)


class Tools:
    def __init__(self, search_url: str, search_key: str, firecrawl_url: str, firecrawl_key: str = ""):
        self.search_url, self.search_key = search_url, search_key
        self.firecrawl_url, self.firecrawl_key = firecrawl_url, firecrawl_key
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=150, write=30, pool=60))

    async def search(self, q: str, lang: str = "de", num: int = 8) -> list[dict]:
        body = {"q": q, "num": num, "gl": "ch", "hl": lang if lang in ("de", "fr", "it", "en") else "de"}
        try:
            r = await self.http.post(self.search_url, json=body, headers={"X-API-KEY": self.search_key})
            return [{"title": o.get("title", ""), "url": o.get("link", ""), "snippet": o.get("snippet", ""),
                     "date": o.get("date", "")}
                    for o in (r.json().get("organic") or []) if o.get("link")]
        except (httpx.HTTPError, ValueError):
            return []

    async def fetch(self, url: str) -> tuple[str, str]:
        """Returns (title, markdown); empty markdown if the page could not be fetched."""
        title, text, _ = await self.fetch_meta(url)
        return title, text

    async def fetch_meta(self, url: str) -> tuple[str, str, str]:
        """Returns (title, markdown, date): the date is the page's published/modified date if it states one."""
        try:
            r = await self.http.post(f"{self.firecrawl_url}/v2/scrape",
                                     json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
                                     headers={"Authorization": f"Bearer {self.firecrawl_key}"})
            data = r.json().get("data") or {}
            meta = data.get("metadata") or {}
            date = next((str(meta[k]) for k in ("modifiedTime", "article:modified_time", "dcterms.modified",
                                                 "publishedTime", "article:published_time", "dcterms.date", "date")
                         if meta.get(k) and isinstance(meta.get(k), str)), "")
            return meta.get("title", "") if isinstance(meta.get("title", ""), str) else "", data.get("markdown") or "", date
        except (httpx.HTTPError, ValueError):
            return "", "", ""


async def focus(llm, text: str, queries: list[str], budget_chars: int = 8000, chunk: int = 1200) -> str:
    """Keep the passages most similar to the queries (document order), so long pages are not cut at the head."""
    if len(text) <= budget_chars:
        return text
    chunks = [text[i:i + chunk] for i in range(0, len(text), chunk - 150)]
    vecs = await llm.embed(queries + chunks)
    qv, cv = vecs[:len(queries)], vecs[len(queries):]

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a) * sum(y * y for y in b)) or 1)

    score = [max(cos(c, q) for q in qv) for c in cv]
    keep, used = [], 0
    for i in sorted(range(len(chunks)), key=lambda i: -score[i]):
        if used + len(chunks[i]) > budget_chars:
            break
        keep.append(i)
        used += len(chunks[i])
    return "\n[...]\n".join(chunks[i] for i in sorted(keep))


class MCPClient:
    """Minimal streamable-HTTP MCP client (initialize, tools/list, tools/call), sync httpx in a thread.
    Every request is retried on 5xx and connection errors (the Grossrat server answers 503 now and then),
    and the session is re-opened when the server forgets it (404)."""

    RETRIES = 6

    def __init__(self, url: str, allow: list[str] | None = None, timeout: float = 120):
        self.url, self.allow = url, set(allow or [])
        self.http = httpx.Client(timeout=timeout, headers={"Accept": "application/json, text/event-stream",
                                                           "Content-Type": "application/json"})
        self._id = 0
        self._tools = None
        self._open()

    def _open(self) -> None:
        self.http.headers.pop("mcp-session-id", None)
        r = self._post("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "research-agent", "version": "0.2"}}, reopen=False)
        if sid := r.headers.get("mcp-session-id"):
            self.http.headers["mcp-session-id"] = sid
        self.http.post(self.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _post(self, method: str, params: dict, reopen: bool = True):
        for attempt in range(self.RETRIES):
            self._id += 1
            try:
                r = self.http.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
            except httpx.TransportError:
                if attempt == self.RETRIES - 1:
                    raise
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 404 and reopen and "mcp-session-id" in self.http.headers:
                self._open()
                continue
            if r.status_code >= 500 and attempt < self.RETRIES - 1:
                time.sleep(3 * (attempt + 1))     # 3+6+...+15 s: rides out a container restart
                continue
            r.raise_for_status()
            return r
        raise RuntimeError(f"MCP {method}: no answer after {self.RETRIES} attempts")

    def _rpc(self, method: str, params: dict):
        r = self._post(method, params)
        text = r.text
        if "text/event-stream" in r.headers.get("content-type", ""):
            text = next(line[6:] for line in reversed(text.splitlines()) if line.startswith("data: "))
        data = json.loads(text)
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["result"]

    def tools(self) -> list[dict]:
        if self._tools is None:
            self._tools = [t for t in self._rpc("tools/list", {})["tools"] if not self.allow or t["name"] in self.allow]
        return self._tools

    def call(self, name: str, arguments: dict) -> str:
        """Tool result as text. A result flagged isError is raised (and retried once: the Grossrat server
        reports transient overload this way), so it never becomes a source."""
        for attempt in range(2):
            res = self._rpc("tools/call", {"name": name, "arguments": arguments})
            text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            if not res.get("isError"):
                return text
            if attempt == 0:
                time.sleep(3)
        raise RuntimeError(f"{name}: {text[:300]}")

    async def acall(self, name: str, arguments: dict) -> str:
        return await asyncio.to_thread(self.call, name, arguments)

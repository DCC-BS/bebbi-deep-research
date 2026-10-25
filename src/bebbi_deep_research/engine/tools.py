"""Search (Serper-compatible endpoint), fetch (Firecrawl), passage focus (embeddings) and MCP access."""

import asyncio
import ipaddress
import json
import re
import threading
import time
from collections import Counter
from urllib.parse import urlparse

import httpx
from dcc_backend_common.logger import get_logger

from . import metrics
from .vectors import scores

logger = get_logger(__name__)


def domain(url: str) -> str:
    """Return the host of a URL without a leading "www."."""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def domain_match(url: str, domains: list[str]) -> bool:
    """Return whether the URL's domain is one of `domains` or a subdomain of one."""
    d = domain(url)
    return any(d == x or d.endswith("." + x) for x in domains)


_BAD_SUFFIX = (".local", ".localhost", ".svc", ".cluster.local", ".internal", ".lan", ".home.arpa")
_NUMERIC_HOST = re.compile(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+))*", re.I)


def fetchable(url: str) -> bool:
    """Return whether a URL may be fetched: public http(s) pages only.

    Rejected: other schemes, localhost and cluster-internal names, and private, loopback, link-local or reserved
    IP literals (the scraper runs inside the cluster).
    """
    try:
        u = urlparse(url.strip())
        host = (u.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost" or "." not in host and ":" not in host or host.endswith(_BAD_SUFFIX):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return not _NUMERIC_HOST.fullmatch(host)  # 2130706433 / 0x7f.1: inet_aton forms of an IP
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return ip.is_global and not ip.is_multicast


class Tools:
    """Web search (Serper) and page fetching (Firecrawl) for one research.

    Attributes:
        errors: Failed calls of this research by ``"<tool>:<reason>"`` (reason: HTTP status, exception name or
            ``bad_json``), e.g. ``{"search:429": 2}``.
    """

    def __init__(self, search_url: str, search_key: str, firecrawl_url: str, firecrawl_key: str = ""):
        """Create the HTTP client for search and fetch."""
        self.search_url, self.search_key = search_url, search_key
        self.firecrawl_url, self.firecrawl_key = firecrawl_url, firecrawl_key
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=150, write=30, pool=60))
        self.errors: Counter[str] = Counter()

    def _failed(self, tool: str, reason: str, url: str) -> None:
        """Count and log a failed search or fetch; the log names the host only (queries and paths may be personal).

        401 and 403 mean a wrong key or a blocked account and are logged as errors; the rest as warnings.
        """
        self.errors[f"{tool}:{reason}"] += 1
        metrics.inc("research_tool_errors_total", tool=tool, reason=reason)
        log = logger.error if reason in ("401", "403") else logger.warning
        log(f"{tool} failed", tool=tool, reason=reason, host=urlparse(url).hostname or "")

    async def search(self, q: str, lang: str = "de", num: int = 8) -> list[dict]:
        """Search the web (Swiss results); return title, URL, snippet and date per hit.

        A failure (HTTP status other than 200, transport error, unreadable body) returns [] and is counted in
        `errors` and logged, so a wrong key or an exhausted quota shows up instead of looking like "no results".
        """
        body = {"q": q, "num": num, "gl": "ch", "hl": lang if lang in ("de", "fr", "it", "en") else "de"}
        try:
            r = await self.http.post(self.search_url, json=body, headers={"X-API-KEY": self.search_key})
        except httpx.HTTPError as e:
            self._failed("search", type(e).__name__, self.search_url)
            return []
        if r.status_code != 200:
            self._failed("search", str(r.status_code), self.search_url)
            return []
        try:
            organic = r.json().get("organic") or []
        except (ValueError, AttributeError):
            self._failed("search", "bad_json", self.search_url)
            return []
        return [
            {
                "title": o.get("title", ""),
                "url": o.get("link", ""),
                "snippet": o.get("snippet", ""),
                "date": o.get("date", ""),
            }
            for o in organic
            if isinstance(o, dict) and o.get("link")
        ]

    async def fetch(self, url: str) -> tuple[str, str]:
        """Returns (title, markdown); empty markdown if the page could not be fetched."""
        title, text, _ = await self.fetch_meta(url)
        return title, text

    async def fetch_meta(self, url: str) -> tuple[str, str, str]:
        """Returns (title, markdown, date): the date is the page's published/modified date if it states one.

        A failed scrape (HTTP status other than 200, transport error, unreadable body) returns empty strings and is
        counted in `errors` and logged with the page's host.
        """
        if not fetchable(url):
            return "", "", ""
        try:
            r = await self.http.post(
                f"{self.firecrawl_url}/v2/scrape",
                json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
                headers={"Authorization": f"Bearer {self.firecrawl_key}"},
            )
        except httpx.HTTPError as e:
            self._failed("fetch", type(e).__name__, url)
            return "", "", ""
        if r.status_code != 200:
            self._failed("fetch", str(r.status_code), url)
            return "", "", ""
        try:
            data = r.json().get("data") or {}
            meta = data.get("metadata") or {}
            date = next(
                (
                    str(meta[k])
                    for k in (
                        "modifiedTime",
                        "article:modified_time",
                        "dcterms.modified",
                        "publishedTime",
                        "article:published_time",
                        "dcterms.date",
                        "date",
                    )
                    if meta.get(k) and isinstance(meta.get(k), str)
                ),
                "",
            )
            return (
                meta.get("title", "") if isinstance(meta.get("title", ""), str) else "",
                data.get("markdown") or "",
                date,
            )
        except (ValueError, AttributeError):
            self._failed("fetch", "bad_json", url)
            return "", "", ""


FOCUS_MAX_CHARS = 200_000  # a fetched page is embedded up to here (about 190 chunks); the rest is dropped


async def focus(
    llm, text: str, queries: list[str], budget_chars: int = 8000, chunk: int = 1200, max_chars: int = FOCUS_MAX_CHARS
) -> str:
    """Keep the passages most similar to the queries (document order), so long pages are not cut at the head.

    Only the first ``max_chars`` characters are embedded: a page of a few million characters (a parsed law or PDF)
    would otherwise cost hundreds of MB of vectors and seconds of CPU on the event loop, on every read.
    """
    if len(text) <= budget_chars:
        return text
    text = text[:max_chars]
    chunks = [text[i : i + chunk] for i in range(0, len(text), chunk - 150)]
    vecs = await llm.embed(queries + chunks)
    score = (await asyncio.to_thread(scores, vecs, len(queries))).tolist()
    keep, used = [], 0
    for i in sorted(range(len(chunks)), key=lambda i: -score[i]):
        if used + len(chunks[i]) > budget_chars:
            break
        keep.append(i)
        used += len(chunks[i])
    return "\n[...]\n".join(chunks[i] for i in sorted(keep))


class MCPClient:
    """Minimal streamable-HTTP MCP client (initialize, tools/list, tools/call), sync httpx in a thread.

    Every request is retried on 5xx and connection errors (the Grossrat server answers 503 now and then), and
    the session is re-opened when the server forgets it (404).
    """

    RETRIES = 6

    def __init__(
        self,
        url: str,
        allow: list[str] | None = None,
        timeout: float = 120,
        retries: int | None = None,
        deadline: float | None = None,
    ):
        """Create the client (the session opens on the first request).

        Args:
            url: MCP endpoint.
            allow: Tool allowlist; empty means all tools.
            timeout: Timeout per request in seconds.
            retries: Attempts per request (default RETRIES).
            deadline: time.monotonic() value after which no further attempt is made (connecting at job start
                must not take minutes).
        """
        self.url, self.allow = url, set(allow or [])
        self.host = urlparse(url).hostname or ""
        self.retries, self.deadline = retries or self.RETRIES, deadline
        self._lock = threading.RLock()  # request ids and the session are shared by the threads of one job
        self.http = httpx.Client(
            timeout=timeout,
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
        self._id = 0
        self._tools = None
        self._open()
        self.retries, self.deadline = self.RETRIES, None  # connect limits apply to the connect only

    def _open(self) -> None:
        with self._lock:
            self.http.headers.pop("mcp-session-id", None)
            r = self._post(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "research-agent", "version": "0.2"},
                },
                reopen=False,
            )
            if sid := r.headers.get("mcp-session-id"):
                self.http.headers["mcp-session-id"] = sid
            self.http.post(self.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).raise_for_status()

    def _retrying(self, method: str, attempt: int, reason: str) -> None:
        metrics.inc("research_mcp_errors_total", host=self.host, kind="retry")
        logger.warning("mcp request retry", host=self.host, method=method, attempt=attempt + 1, reason=reason)

    def _post(self, method: str, params: dict, reopen: bool = True):
        for attempt in range(self.retries):
            with self._lock:
                self._id += 1
                rid, sid = self._id, self.http.headers.get("mcp-session-id")
            last = attempt == self.retries - 1 or (
                self.deadline is not None and time.monotonic() + 3 * (attempt + 1) > self.deadline
            )
            try:
                r = self.http.post(
                    self.url,
                    json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                    headers={"mcp-session-id": sid} if sid else None,
                )
            except httpx.TransportError as e:
                if last:
                    raise
                self._retrying(method, attempt, type(e).__name__)
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 404 and reopen and sid:
                logger.info("mcp session re-opened", host=self.host, method=method)
                with self._lock:
                    if self.http.headers.get("mcp-session-id") == sid:  # not re-opened by another thread meanwhile
                        self._open()
                continue
            if r.status_code >= 500 and not last:
                self._retrying(method, attempt, str(r.status_code))
                time.sleep(3 * (attempt + 1))  # 3+6+...: rides out a container restart
                continue
            r.raise_for_status()
            return r
        raise RuntimeError(f"MCP {method}: no answer after {self.retries} attempts")

    def _rpc(self, method: str, params: dict):
        r = self._post(method, params)
        text = r.text
        if "text/event-stream" in r.headers.get("content-type", ""):
            text = next((line[6:] for line in reversed(text.splitlines()) if line.startswith("data: ")), None)
            if text is None:
                raise RuntimeError(f"MCP {method}: event stream without a data line")
        data = json.loads(text)
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["result"]

    def tools(self) -> list[dict]:
        """Return the server's tools (allowlisted), cached after the first call."""
        if self._tools is None:
            self._tools = [t for t in self._rpc("tools/list", {})["tools"] if not self.allow or t["name"] in self.allow]
        return self._tools

    def call(self, name: str, arguments: dict) -> str:
        """Call a tool and return its result as text.

        A result flagged isError is raised (and retried once: the Grossrat server reports transient overload
        this way), so it never becomes a source.
        """
        for attempt in range(2):
            res = self._rpc("tools/call", {"name": name, "arguments": arguments})
            text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            if not res.get("isError"):
                return text
            metrics.inc("research_mcp_errors_total", host=self.host, kind="tool_error")
            logger.warning("mcp tool error result", host=self.host, tool=name, attempt=attempt + 1)
            if attempt == 0:
                time.sleep(3)
        raise RuntimeError(f"{name}: {text[:300]}")

    async def acall(self, name: str, arguments: dict) -> str:
        """Call a tool from async code (in a thread)."""
        return await asyncio.to_thread(self.call, name, arguments)

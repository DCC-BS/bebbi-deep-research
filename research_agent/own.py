"""The user's own sources (requirement A4): files uploaded in the chat and knowledge bases of Open WebUI.

The engine does not know Open WebUI. The caller hands over an `OwnSources` object:

* `files`: uploaded files with their extracted text (stored with the job, so night runs do not depend on the user's session);
* `collections`: knowledge bases (id and display name) plus `retrieve(query, collection)`, an async function that returns
  the passages of that one knowledge base: [{"text", "name", "file_id", "url", "date", "score"}]. The caller decides what
  it may query; the engine only passes the collections it was given and never ids that a model invented.

Two source kinds result: `upload` (title = file name) and `knowledge:<knowledge base name>` (title = document name).
URLs are the ones the caller supplied (a link the chat user can open); without one the source is listed without URL.

Focus engine: small uploads are read directly into the first message (no tool call); larger ones through the tool
`read_upload` (passages most similar to the question, by embeddings); knowledge bases through `knowledge_search`.
Pipeline engine: `evidence_items` yields per sub-question the passages that the extraction step turns into evidence cards.
"""

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .sources import Registry, Source

INLINE_FILE_CHARS = 6000        # uploads up to this size go into the first message as a whole
INLINE_TOTAL_CHARS = 18000      # ... as long as all inlined files together stay below this
PASSAGE_CHARS = 6000            # what one read_upload / pipeline extraction sees of a long file
OUT_CHARS = 9000                # cap of one knowledge_search result
CHUNK = 1200
OWN_TOOLS = ("read_upload", "knowledge_search")
MODES = ("prefer", "only", "off")

Retrieve = Callable[[str, "OwnCollection"], Awaitable[list[dict]]]


@dataclass
class OwnFile:
    id: str
    name: str
    text: str
    date: str = ""            # file date (upload), YYYY-MM-DD, empty if unknown
    url: str = ""             # link the chat user can open, empty if none


@dataclass
class OwnCollection:
    id: str
    name: str


@dataclass
class OwnSources:
    files: list[OwnFile] = field(default_factory=list)
    collections: list[OwnCollection] = field(default_factory=list)
    retrieve: Retrieve | None = None
    mode: str = "prefer"                                   # prefer | only (set by the engine from the preset)
    _index: dict = field(default_factory=dict, repr=False)

    def __bool__(self) -> bool:
        return bool(self.files or (self.collections and self.retrieve))

    def names(self) -> list[str]:
        return [f.name for f in self.files] + [c.name for c in self.collections]


# ------------------------------------------------------------------ passages of long texts
class PassageIndex:
    """Chunks of one text with embeddings (computed once per run); `top` returns the passages most similar to queries."""

    def __init__(self, text: str):
        self.text = text
        step = max(CHUNK - 150, math.ceil(len(text) / 1200))      # at most ~1200 chunks, however large the file is
        self.chunks = [text[i:i + step + 150] for i in range(0, len(text), step)] or [""]
        self._vecs: list[list[float]] | None = None
        self._lock = asyncio.Lock()

    async def top(self, llm, queries: list[str], budget_chars: int = PASSAGE_CHARS) -> tuple[str, float]:
        if len(self.text) <= budget_chars:
            return self.text, 1.0
        async with self._lock:
            if self._vecs is None:
                self._vecs = await llm.embed(self.chunks)
        qv = await llm.embed([q for q in queries if q.strip()] or [""])
        score = [max(_cos(c, q) for q in qv) for c in self._vecs]
        keep, used = [], 0
        for i in sorted(range(len(self.chunks)), key=lambda i: -score[i]):
            if used + len(self.chunks[i]) > budget_chars:
                break
            keep.append(i)
            used += len(self.chunks[i])
        keep.sort()
        return "\n[...]\n".join(self.chunks[i] for i in keep), max(score[i] for i in keep) if keep else 0.0


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a) * sum(y * y for y in b)) or 1)


def _index(own: OwnSources, f: OwnFile) -> PassageIndex:
    return own._index.setdefault(f.id, PassageIndex(f.text))


def kind_of(collection: OwnCollection | None) -> str:
    return f"knowledge:{collection.name}" if collection and collection.name else "knowledge"


# ------------------------------------------------------------------ focus engine
def tool_specs(own: OwnSources) -> list[dict]:
    specs = []
    if own.files:
        specs.append({"type": "function", "function": {
            "name": "read_upload",
            "description": "Read a file the user uploaded (the passages most relevant to your query). Use the file's "
                           "number (U1, U2, ...) or its name.",
            "parameters": {"type": "object", "properties": {
                "file": {"type": "string", "description": "U1, U2, ... or the file name"},
                "query": {"type": "string", "description": "what you look for in the file (3-10 words)"}},
                "required": ["file"]}}})
    if own.collections and own.retrieve:
        names = ", ".join(f'"{c.name}"' for c in own.collections)
        specs.append({"type": "function", "function": {
            "name": "knowledge_search",
            "description": f"Search the user's knowledge bases ({names}). Returns the best matching passages with "
                           "document names and source IDs.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "specific question or keywords, in the language of the documents"}},
                "required": ["query"]}}})
    return specs


def prompt_block(own: OwnSources, reg: Registry, de: bool = True) -> tuple[str, str]:
    """(system text, first-message text). Registers the inlined uploads as sources, so they carry IDs from the start."""
    listing, inline, used = [], [], 0
    for i, f in enumerate(own.files, 1):
        if len(f.text) <= INLINE_FILE_CHARS and used + len(f.text) <= INLINE_TOTAL_CHARS:
            used += len(f.text)
            src = reg.add(f.url, f.name, "upload", f.text, f.date, key=f"upload:{f.id}")
            listing.append(f"- U{i} \"{f.name}\": complete text below as [{src.id}]")
            inline.append(f"[{src.id}] {f.name}{' (' + f.date + ')' if f.date else ''}\n{f.text}")
        else:
            listing.append(f"- U{i} \"{f.name}\" ({len(f.text)} characters): read the relevant passages with read_upload")
    for c in own.collections:
        if own.retrieve:
            listing.append(f"- knowledge base \"{c.name}\": search it with knowledge_search")
    if own.mode == "only":
        rule = ("Use ONLY these sources. There is no web search and no other data tool in this run. What the documents "
                "do not answer goes into the section of open points; never fill gaps from your own knowledge.")
    else:
        rule = ("Use these documents FIRST for everything they cover, and use web search and the data tools only for "
                "what they do not cover. Say in the report which statements come from the user's documents.")
    system = ("\nTHE USER'S OWN SOURCES (uploaded files and knowledge bases of the user)\n" + "\n".join(listing)
              + f"\n{rule}\nThey are cited with their [S<n>] ID like every other source; the report names the document "
                "(file name) in the sentence where it matters.\n")
    first = ("\nTEXT OF THE USER'S SMALL DOCUMENTS (cite by ID):\n\n" + "\n\n".join(inline) + "\n") if inline else ""
    return system, first


def _find_file(own: OwnSources, ref: str) -> OwnFile | None:
    ref = str(ref or "").strip()
    if ref.upper().startswith("U") and ref[1:].isdigit():
        i = int(ref[1:])
        return own.files[i - 1] if 1 <= i <= len(own.files) else None
    low = ref.lower()
    return next((f for f in own.files if f.name.lower() == low), None) or \
        next((f for f in own.files if low and low in f.name.lower()), None)


async def read_upload(own: OwnSources, args: dict, reg: Registry, llm, question: str) -> tuple[str, list[Source]]:
    f = _find_file(own, args.get("file"))
    if f is None:
        avail = "; ".join(f"U{i} {x.name}" for i, x in enumerate(own.files, 1))
        return f"error: unknown file. Available: {avail}", []
    query = str(args.get("query") or "").strip()
    text, _ = await _index(own, f).top(llm, [query, question] if query else [question])
    src = reg.add(f.url, f.name, "upload", text, f.date, key=f"upload:{f.id}")
    return f"[{src.id}] {src.title}{' (' + src.date + ')' if src.date else ''}\n{text}", [src]


async def knowledge_search(own: OwnSources, args: dict, reg: Registry) -> tuple[str, list[Source]]:
    query = str(args.get("query") or "").strip()
    if not query:
        return "error: empty query", []
    if not (own.collections and own.retrieve):
        return "error: no knowledge base available", []
    results = await asyncio.gather(*(own.retrieve(query, c) for c in own.collections), return_exceptions=True)
    groups: dict[tuple, dict] = {}
    errors = []
    for c, res in zip(own.collections, results):
        if isinstance(res, Exception):
            errors.append(f'knowledge base "{c.name}" not reachable ({type(res).__name__})')
            continue
        for h in res:
            g = groups.setdefault((c.id, h.get("file_id") or h.get("name")), {"c": c, "hit": h, "texts": []})
            if h.get("text") and h["text"] not in g["texts"]:
                g["texts"].append(h["text"])
    if not groups:
        return "; ".join(errors) or "no results in the knowledge bases for this query", []
    out, added = [], []
    for (cid, fid), g in groups.items():
        h, c = g["hit"], g["c"]
        text = "\n[...]\n".join(g["texts"])
        src = reg.add(h.get("url") or "", h.get("name") or "Dokument", kind_of(c), text, h.get("date") or "",
                      key=f"knowledge:{cid}:{fid}")
        added.append(src)
        out.append(f"[{src.id}] {src.title} (Wissensbasis {c.name}{', ' + src.date if src.date else ''})\n{text}")
    body = "\n\n".join(out + errors)
    return (body[:OUT_CHARS] + "\n... [truncated]") if len(body) > OUT_CHARS else body, added


async def call(name: str, args: dict, own: OwnSources, reg: Registry, llm, question: str) -> tuple[str, list[Source]]:
    if name == "read_upload":
        return await read_upload(own, args, reg, llm, question)
    return await knowledge_search(own, args, reg)


# ------------------------------------------------------------------ pipeline engine
async def evidence_items(own: OwnSources, llm, queries: list[str], max_files: int = 3) -> list[dict]:
    """Per sub-question: the passages of the best matching uploads and of the knowledge bases, as
    [{title, url, kind, text, date}] for the extraction step. Uploads are ranked by their best passage."""
    items: list[dict] = []
    if own.files:
        ranked = await asyncio.gather(*(_index(own, f).top(llm, queries) for f in own.files))
        order = sorted(range(len(own.files)), key=lambda i: -ranked[i][1])[:max_files]
        items += [{"title": own.files[i].name, "url": own.files[i].url, "kind": "upload", "date": own.files[i].date,
                   "text": ranked[i][0]} for i in order if ranked[i][0].strip()]
    if own.collections and own.retrieve:
        results = await asyncio.gather(*(own.retrieve(queries[0], c) for c in own.collections), return_exceptions=True)
        for c, res in zip(own.collections, results):
            if isinstance(res, Exception):
                continue
            by_file: dict[str, dict] = {}
            for h in res:
                g = by_file.setdefault(h.get("file_id") or h.get("name") or "", {
                    "title": h.get("name") or "Dokument", "url": h.get("url") or "", "kind": kind_of(c),
                    "date": h.get("date") or "", "text": ""})
                g["text"] = (g["text"] + "\n[...]\n" + h["text"]) if g["text"] else h.get("text", "")
            items += [g for g in by_file.values() if g["text"].strip()]
    return items

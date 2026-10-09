"""Source records: every search hit, fetched page and MCP result item gets an ID, a public URL, a title
and a date (Stand). The model only sees and cites IDs; code resolves them to the source list.

MCP results: structured (JSON) results are walked for items that carry a URL (Grossrat `url`,
`citation_url`, `pdf_url`, statistik `portal_url`); text results (opencaselaw Markdown) are split into
items around their document links. A citation never points to the MCP endpoint or names a tool. A
result without any public URL becomes a source without URL; the engine never invents one, except for
the documented data.bs.ch dataset page."""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from .text import Evidence, cantonal_labels

URL_KEYS = ("citation_url", "pdf_url", "url_ges", "url", "portal_url", "link", "html_url")
TITLE_KEYS = ("titel_ges", "titel_dok", "title", "titel", "name", "bezeichnung")
DATE_KEYS = ("dokudatum", "datum", "date", "beginn_ges", "aktualisierungsdatum", "modified", "published",
             "publikationsdatum", "in_force", "stand")
NOISE_KEYS = {"score", "found_by", "match_expression", "snippet_matched", "mode", "ranks", "include_links", "links"}
LAW_TOOLS = {"search_laws", "get_law", "search_legislation", "get_legislation", "browse_legislation_changes"}
_MD_LINK = re.compile(r"\[([^\]]{1,200})\]\((https?://[^)\s]+)\)")
_BARE = re.compile(r"(?<![(\w])https?://[^\s)\]>\"']+")
_FOOTER = re.compile(r"\n-{3,}\s*\nℹ️.*\Z", re.S)          # opencaselaw advertising footer


@dataclass
class Source:
    id: str
    url: str
    title: str
    date: str = ""               # publication date or Stand from the source, empty if unknown
    kind: str = "web"            # web | mcp:<server> | upload | knowledge:<knowledge base>
    fetched_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"))
    text: str = ""               # what the model saw from this source (for the number check)
    evidence: Evidence | None = field(default=None, repr=False)

    def ev(self) -> Evidence:
        if self.evidence is None:
            self.evidence = Evidence(" ".join(re.split(r"[/=?&_]", self.url)) + "\n" + self.title + "\n" + self.date + "\n" + self.text)
        return self.evidence


class Registry:
    def __init__(self):
        self.sources: dict[str, Source] = {}
        self._by_url: dict[str, str] = {}
        self.cantonal_numbers: dict[str, str] = {}      # "153.260" -> "SG" (from law sources)

    def add(self, url: str, title: str, kind: str, text: str = "", date: str = "", key: str = "") -> Source:
        """`key` identifies a source that has no (unique) URL, e.g. an uploaded file; the URL is the key otherwise."""
        url = (url or "").strip()
        key = key or (url.split("#")[0] if url else "")
        if key and key in self._by_url:
            s = self.sources[self._by_url[key]]
            if text and text not in s.text:
                s.text = (s.text + "\n" + text)[-60000:]
                s.evidence = None
            if date and not s.date:
                s.date = date
            if title and (not s.title or s.title == s.url):
                s.title = title
            return s
        s = Source(id=f"S{len(self.sources) + 1}", url=url, title=_clean(title) or url or "?", kind=kind,
                   text=text, date=_date(date))
        self.sources[s.id] = s
        if key:
            self._by_url[key] = s.id
        for m in re.finditer(r"\b(SG|SGS|LS|BSG|SRL|SAR|BGS|sGS)[ -](?:Nummer:\*\*\s*)?(\d{1,3}(?:\.\d+)+)", text + " " + title):
            self.cantonal_numbers.setdefault(m.group(2), m.group(1))
        if m := re.search(r"gesetzessammlung\.bs\.ch/(?:data|app/de/texts_of_law)/(\d{1,3}(?:\.\d+)+)", url):
            self.cantonal_numbers.setdefault(m.group(1), "SG")
        return s

    def get(self, sid: str) -> Source | None:
        return self.sources.get(sid)

    def to_list(self) -> list[dict]:
        return [{k: v for k, v in asdict(s).items() if k not in ("evidence", "text")} | {"chars": len(s.text)}
                for s in self.sources.values()]


def _clean(s) -> str:
    s = re.sub(r"[*`#]+", "", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip(" —-|:")
    return s if len(s) <= 220 else s[:219].rsplit(" ", 1)[0] + " …"


def _date(s) -> str:
    s = str(s or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else s[:40]


def _is_endpoint(url: str, endpoint: str) -> bool:
    """The MCP server itself (root, /mcp) is never a source; document pages on the same host are."""
    u, e = urlparse(url), urlparse(endpoint)
    return u.netloc.lower().removeprefix("www.") == e.netloc.lower().removeprefix("www.") and u.path.rstrip("/") in ("", "/mcp", "/sse")


# ------------------------------------------------------------------ MCP results
def mcp_to_sources(server: str, tool: str, args: dict, out: str, reg: Registry, endpoint: str,
                   context: dict | None = None, max_chars: int = 8000) -> tuple[str, list[Source]]:
    """Register the items of one MCP tool result as sources. Returns the text for the model, with the
    source ID next to every item, and the sources added. `context` keeps per-run lookups (dataset titles)."""
    context = context if context is not None else {}
    kind = f"mcp:{server}"
    if tool in LAW_TOOLS:
        out = cantonal_labels(out, str(args.get("canton") or "").upper() or None)
    out = _FOOTER.sub("", out).strip()
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, (dict, list)):
        return _json_sources(server, tool, args, data, reg, endpoint, kind, context, max_chars)
    return _text_sources(server, tool, args, out, reg, endpoint, kind, max_chars)


def _item_url(d: dict, endpoint: str) -> str:
    for k in URL_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.startswith("http") and not _is_endpoint(v, endpoint):
            return v
    return ""


def _item_title(d: dict, parent_title: str = "") -> str:
    parts = [str(d[k]) for k in ("titel_ges", "titel_dok") if d.get(k)]
    if not parts:
        parts = [str(d[k]) for k in TITLE_KEYS if d.get(k)][:1]
    sig = d.get("signatur_dok") or d.get("signatur_ges")
    title = " – ".join(dict.fromkeys(parts))
    if parent_title and title and parent_title not in title:
        title = f"{parent_title} – {title}"
    elif not title:
        title = parent_title
    if sig and sig not in title:
        title = f"{title} ({sig})" if title else str(sig)
    return title


def _item_date(d: dict) -> str:
    return next((str(d[k]) for k in DATE_KEYS if d.get(k)), "")


def _json_sources(server, tool, args, data, reg, endpoint, kind, context, max_chars):
    added: list[Source] = []

    def walk(node, parent_title=""):
        if isinstance(node, list):
            return [walk(x, parent_title) for x in node]
        if not isinstance(node, dict):
            return node
        node = {k: v for k, v in node.items() if k not in NOISE_KEYS and v not in (None, "", [], {})}
        url = _item_url(node, endpoint)
        if server == "data-bs" and not url and node.get("dataset_id"):
            url = f"https://data.bs.ch/explore/dataset/{node['dataset_id']}/"
            if node.get("title"):
                context.setdefault("datasets", {})[str(node["dataset_id"])] = str(node["title"])
        title = _item_title(node, parent_title if url else "")
        children = {k: walk(v, title or parent_title) for k, v in node.items()}
        if url:
            flat = {k: v for k, v in node.items() if not isinstance(v, (list, dict))}
            src = reg.add(url, title, kind, json.dumps(node, ensure_ascii=False)[:6000], _item_date(flat))
            added.append(src)
            return {"source": src.id, **children}
        return children

    data = walk(data)
    if not added:      # a result without item URLs: one source for the whole result
        url, title = "", f"{server}: {tool}"
        if server == "data-bs" and args.get("dataset_id"):
            ds = str(args["dataset_id"])
            url = f"https://data.bs.ch/explore/dataset/{ds}/"
            title = context.get("datasets", {}).get(ds) or f"data.bs.ch Datensatz {ds}"
        text = json.dumps(data, ensure_ascii=False)
        src = reg.add(url, title, kind, text[:20000], _item_date(data) if isinstance(data, dict) else "")
        added.append(src)
        body = f"[{src.id}] {src.title}{' (' + url + ')' if url else ''}\n{_compact(data, max_chars)}"
        return body, added
    return _compact(data, max_chars), added


def _compact(data, max_chars: int) -> str:
    s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"... [truncated, {len(s)} chars]"


def _links(text: str, endpoint: str) -> list[tuple[str, str]]:
    found = _MD_LINK.findall(text) + [("", u.rstrip(".,;")) for u in _BARE.findall(_MD_LINK.sub(" ", text))]
    return [(lbl, u) for lbl, u in found if not _is_endpoint(u, endpoint)]


def _item_line_title(block: str) -> str:
    """Title of a list item: its first line with link labels kept, numbering and tags removed, plus a
    'Title:' line if the item has one (court decisions)."""
    lines = block.strip().split("\n")
    first = _MD_LINK.sub(lambda m: m.group(1) if m.group(1).lower() not in ("lexfind", "pdf", "link", "quelle") else "", lines[0])
    first = re.sub(r"^\W*\d+\.\s*", "", _clean(first))
    first = re.sub(r"\[[a-z_]+\]|\[[A-Z]{2}\]|§ None", "", first)
    extra = next((ln.split(":", 1)[1].strip() for ln in lines[1:4] if ln.strip().startswith("Title:")), "")
    return _clean(f"{first} – {extra}" if extra else first)


def _text_sources(server, tool, args, out, reg, endpoint, kind, max_chars):
    added: list[Source] = []
    links = _links(out, endpoint)
    doc_title = _doc_title(out)
    is_list = re.match(r"(search|find|list|browse)", tool) is not None
    if not links:
        src = reg.add("", doc_title or f"{server}: {tool}", kind, out[:20000])
        return f"[{src.id}] {src.title}\n{out[:max_chars]}", [src]
    if not is_list:      # one document: one source, best URL (official collection first)
        url = _best(links)
        src = reg.add(url, doc_title or links[0][0], kind, out[:60000], _text_date(out))
        return f"[{src.id}] {src.title} ({url})\n{out[:max_chars]}", [src]
    # a result list: one source per item block (the block between numbered items)
    blocks = re.split(r"\n(?=\*\*\d+\.|\d+\.\s|#{2,3}\s)", out)
    shown = []
    for block in blocks:
        blinks = _links(block, endpoint)
        if not blinks:
            shown.append(block)
            continue
        url = _best(blinks)
        title = _item_line_title(block) or blinks[0][0] or url
        src = reg.add(url, title, kind, block[:6000], _text_date(block))
        added.append(src)
        shown.append(f"[{src.id}] " + block.strip())
    text = "\n".join(shown)
    return (text[:max_chars] + (f"... [truncated, {len(text)} chars]" if len(text) > max_chars else "")), added


def _best(links: list[tuple[str, str]]) -> str:
    for pref in ("gesetzessammlung.", "fedlex.admin.ch", "grosserrat.bs.ch", "bger.ch", "entscheid"):
        for _, u in links:
            if pref in u:
                return u
    return links[0][1]


def _doc_title(out: str) -> str:
    lines = [ln.strip() for ln in out.split("\n")[:8] if ln.strip()]
    heads = [ln for ln in lines if ln.startswith("# ")]
    bold = [ln for ln in lines if ln.startswith("**") and ln.endswith("**") and len(ln) < 160]
    parts = [_clean(h) for h in heads[:1]] + [_clean(b) for b in bold[:1]]
    return " – ".join(p for p in parts if p)


def _text_date(text: str) -> str:
    m = re.search(r"(?:In force since|In Kraft seit|Stand|Datum|Date)[:*\s]+(\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    m = re.search(r"\((\d{4}-\d{2}-\d{2})\)", text)
    return m.group(1) if m else ""


# ------------------------------------------------------------------ web
def search_to_sources(hits: list[dict], reg: Registry) -> tuple[str, list[Source]]:
    lines, added = [], []
    for h in hits:
        src = reg.add(h["url"], h.get("title", ""), "web", h.get("snippet", ""), h.get("date", ""))
        added.append(src)
        date = f" ({h['date']})" if h.get("date") else ""
        lines.append(f"[{src.id}] {h.get('title', '')}{date}\n  {h['url']}\n  {h.get('snippet', '')}")
    return "\n".join(lines) or "no results", added

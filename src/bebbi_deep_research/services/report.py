"""Structured view of a finished report: the numbered source list as data, and the report text for the Open WebUI chat.

The engines write the sources as the last section of the Markdown report (`## Quellen`, one `[n] Title. URL (Stand ...;
abgerufen am ...)` line per source, numbered in order of first citation). The report text is the single source of truth:
`build_sources` reads that list back and enriches every entry with the kind of source and a short excerpt from the
evidence ledger (pipeline engine: the verbatim quote of the evidence card; focus engine: the sentence of the report that
cites the source). Nothing in the engines has to change for this.
"""

import re

# Readable names for the kind of a source; a citation never names "MCP" or a tool.
SOURCE_LABELS = {
    "web": ("Web", "Web"),
    "mcp:grossrat": (
        "Grosser Rat Basel-Stadt (Geschäfte und Dokumente)",
        "Grand Council of Basel-Stadt (business and documents)",
    ),
    "mcp:opencaselaw": (
        "Rechtsdatenbank (Erlasse und Entscheide, opencaselaw.ch)",
        "Legal database (laws and decisions, opencaselaw.ch)",
    ),
    "mcp:data-bs": ("Open-Data-Portal Basel-Stadt (data.bs.ch)", "Open data portal Basel-Stadt (data.bs.ch)"),
    "mcp:statistik": (
        "Statistisches Amt Basel-Stadt (statistik.bs.ch)",
        "Statistical Office Basel-Stadt (statistik.bs.ch)",
    ),
    "upload": ("Hochgeladene Datei", "Uploaded file"),
    "knowledge": ("Wissensbasis", "Knowledge base"),
}


def source_label(kind: str, de: bool = True) -> str:
    """Return the display label of a source kind.

    'mcp:grossrat' -> 'Grosser Rat Basel-Stadt (Geschäfte und Dokumente)'; unknown servers -> 'Fachdatenbank';
    'upload' -> 'Hochgeladene Datei'; 'knowledge:Name' -> 'Wissensbasis Name'.
    """
    if kind in SOURCE_LABELS:
        return SOURCE_LABELS[kind][0 if de else 1]
    if kind.startswith("knowledge:"):  # knowledge:<name of the knowledge base>
        return f"{SOURCE_LABELS['knowledge'][0 if de else 1]} {kind.split(':', 1)[1]}"
    return ("Fachdatenbank" if de else "specialist database") if kind.startswith("mcp:") else "Web"


SOURCES_HEADING = re.compile(r"^## (Quellen|Sources)[ \t]*$", re.M)
_LINE = re.compile(
    r"^\[(\d+)\][ \t]+(.+?)[.:]?[ \t]+(https?://\S+|keine öffentliche URL|no public URL)[ \t]+\((.*)\)[ \t]*$"
)
_CITE = re.compile(r"\[(\d+)\]")
EXCERPT_CHARS = 400


# characters that are invalid in XML 1.0 (python-docx raises on them) and useless in chat text
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff\ud800-\udfff]")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]\[[^\]]*\]")
_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_HTML = re.compile(r"</?[A-Za-z][^>]*>|<!--.*?-->|<![A-Za-z][^>]*>|<\?[^>]*\?>", re.S)
_MD_LINK = re.compile(r"\[(?P<t>[^\]]*)\]\(\s*<?(?P<u>[^)\s>]*)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_REF_DEF = re.compile(r"^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*\S.*$", re.M)


def clean_text(text: str) -> str:
    """Remove characters that are invalid in XML (docx) and control characters; newlines and tabs stay."""
    return _BAD_CHARS.sub("", text or "")


def sanitize_markdown(text: str, allowed_urls: set[str] | frozenset[str] = frozenset()) -> str:
    """Remove what untrusted report text could abuse in the chat.

    Report text from the web and from model output is untrusted: no images (they load without a click), no raw HTML,
    links only to registered source URLs (otherwise the link text stays). Bare URLs and `[n]` citations are untouched.
    """
    text = clean_text(text)
    text = _IMAGE.sub("", text)
    text = _AUTOLINK.sub(lambda m: m.group(0) if m.group(1) in allowed_urls else "", text)
    text = _HTML.sub("", text)
    text = _REF_DEF.sub("", text)
    return _MD_LINK.sub(lambda m: m.group(0) if m.group("u") in allowed_urls else m.group("t"), text)


def split_report(report: str) -> tuple[str, str, list[str]]:
    """Return (body, heading, source lines): text before the source section, its heading, and the raw source lines."""
    report = clean_text(report)  # str.splitlines() would also split at \x0b, \x0c, \x1c-\x1e
    found = list(SOURCES_HEADING.finditer(report))
    if not found:
        return report, "", []
    m = found[-1]
    lines = [ln.strip() for ln in report[m.end() :].strip().splitlines() if ln.strip()]
    return report[: m.start()].rstrip(), m.group(1), lines


def _clip(text: str, limit: int = EXCERPT_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + " …"


def _claim(body: str, n: int) -> str:
    """The first sentence or table row of the report that cites source n (markdown and citation markers removed)."""
    marker = f"[{n}]"
    for block in re.split(r"\n+", body):
        if marker not in block or block.lstrip().startswith("#"):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9\[])", block):
            if marker in sentence:
                s = re.sub(r"\s*\[\d+\]", "", sentence)
                s = re.sub(r"[*_`|]+", " ", re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", s))
                return _clip(s.strip())
    return ""


def _when(inner: str) -> tuple[str, str]:
    stand = fetched = ""
    for part in (p.strip() for p in inner.split(";")):
        for prefix in ("Stand ", "dated "):
            if part.startswith(prefix):
                stand = part[len(prefix) :].strip()
        for prefix in ("abgerufen am ", "accessed "):
            if part.startswith(prefix):
                fetched = part[len(prefix) :].strip()
    return stand, fetched


def _ledger_lookup(ledger: dict | None) -> tuple[dict, dict]:
    """(by key, by key) -> (kind, quote): key is the URL, or `title:<title>` for sources without a public URL."""
    kinds: dict[str, str] = {}
    quotes: dict[str, str] = {}
    for s in (ledger or {}).get("sources", []):  # focus engine
        kinds.setdefault(s.get("url") or f"title:{s.get('title', '')}", s.get("kind", "web"))
    for c in (ledger or {}).get("cards", []):  # pipeline engine
        key = c.get("url") or f"title:{c.get('title', '')}"
        kinds.setdefault(key, c.get("source_kind", "web"))
        text = (c.get("quote") or c.get("finding") or "").strip()
        if key not in quotes and len(text) >= 20:
            quotes[key] = text
    return kinds, quotes


def build_sources(report: str, ledger: dict | None = None) -> list[dict]:
    """The numbered sources of a report: number, title, url, stand, fetched, kind, excerpt, excerpt_kind.

    `excerpt_kind` is `quote` (verbatim passage of the source, pipeline engine) or `claim` (the sentence of the report
    that relies on the source, focus engine).
    """
    body, _, lines = split_report(report)
    kinds, quotes = _ledger_lookup(ledger)
    out = []
    for ln in lines:
        m = _LINE.match(ln)
        if not m:
            continue
        n, title, where, inner = int(m.group(1)), m.group(2).strip(), clean_text(m.group(3)), clean_text(m.group(4))
        url = where if where.startswith("http") else ""
        stand, fetched = _when(inner)
        key = url or f"title:{title}"
        quote = quotes.get(key) or next(
            (q for k, q in quotes.items() if k.startswith("title:") and k[6:].startswith(title[:60])), ""
        )
        claim = _claim(body, n)
        kind = kinds.get(key) or next(
            (v for k, v in kinds.items() if k.startswith("title:") and not url and k[6:].startswith(title[:60])), "web"
        )
        out.append(
            {
                "n": n,
                "title": sanitize_markdown(title).strip() or "Quelle",
                "url": url,
                "stand": stand,
                "fetched": fetched,
                "kind": kind,
                "excerpt": sanitize_markdown(_clip(quote) if quote else claim),
                "excerpt_kind": "quote" if quote else "claim",
            }
        )
    return out if [s["n"] for s in out] == list(range(1, len(out) + 1)) else []


def ui_markdown(report: str, sources: list[dict]) -> str:
    """Report text for the Open WebUI chat, where `[n]` becomes a citation badge for the n-th source event.

    * the source list becomes a real numbered list (a line starting with `[1]` would turn into a badge);
    * a `[n]` without a matching source (e.g. a year in brackets) is escaped, otherwise the badge would be empty;
    * without a parsable source list the report is returned unchanged.
    """
    if not sources:
        return sanitize_markdown(report or "")
    allowed = {s["url"] for s in sources if s["url"]}
    body, heading, _ = split_report(report)
    body = sanitize_markdown(body, allowed)
    count = len(sources)
    body = _CITE.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= count else f"\\[{m.group(1)}\\]", body)
    items = []
    for s in sources:
        when = "; ".join(
            x
            for x in (
                f"Stand {s['stand']}"
                if s["stand"] and heading == "Quellen"
                else f"dated {s['stand']}"
                if s["stand"]
                else "",
                f"abgerufen am {s['fetched']}" if heading == "Quellen" else f"accessed {s['fetched']}",
            )
            if x
        )
        where = s["url"] or ("keine öffentliche URL" if heading == "Quellen" else "no public URL")
        items.append(f"{s['n']}. {s['title']}. {where} ({when})")
    return f"{body}\n\n## {heading}\n\n" + "\n".join(items) + "\n"


def owui_source_events(sources: list[dict]) -> list[dict]:
    """One Open WebUI `source` event per source, in the order of the numbers (the n-th event is citation `[n]`).

    Open WebUI groups sources by `metadata.source` and shows the badge title from `metadata.name`; both must be unique
    per source, otherwise the numbering shifts. `metadata.source` is the URL (the source list shows it), or a stable
    `quelle-<n>` id for sources without a public URL.
    """
    events, used_ids, used_names = [], set(), set()
    for s in sources:
        sid = s["url"] or f"quelle-{s['n']}"
        if sid in used_ids:
            sid = f"{sid}#{s['n']}"
        name = s["title"] or sid
        if name in used_names:
            name = f"{name} ({s['n']})"
        used_ids.add(sid)
        used_names.add(name)
        lines = [f"**{s['title']}**", ""]
        facts = [
            f"Stand: {s['stand']}" if s["stand"] else "",
            f"abgerufen am {s['fetched']}" if s["fetched"] else "",
            f"Quelle: {source_label(s['kind'])}" if s["kind"] else "",
        ]
        lines += ["  \n".join(f for f in facts if f)]
        if s["excerpt"]:
            lines += [
                "",
                ("Auszug aus der Quelle:" if s["excerpt_kind"] == "quote" else "Aussage im Bericht:"),
                "",
                f"> {s['excerpt']}",
            ]
        events.append(
            {
                "type": "source",
                "data": {
                    "source": {"name": name, **({"url": s["url"]} if s["url"] else {})},
                    "document": ["\n".join(lines)],
                    "metadata": [
                        {
                            "source": sid,
                            "name": name,
                            "stand": s["stand"],
                            "fetched_at": s["fetched"],
                            "kind": s["kind"],
                        }
                    ],
                },
            }
        )
    return events

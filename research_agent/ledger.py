"""Evidence cards and the research ledger. Code owns IDs, URLs, coverage and the evidence trail."""

import re
import time
from dataclasses import asdict, dataclass, field

from rapidfuzz import fuzz


@dataclass
class SubQ:
    id: str
    part: str
    text: str
    intent: str = "secondary"   # primary | data | secondary
    round: int = 1


@dataclass
class Card:
    id: str
    subq: str
    url: str
    title: str
    quote: str
    finding: str
    source_kind: str            # web | mcp:<server>
    fetched_at: str
    date: str = ""              # publication / "Stand" date stated in the source


def _norm(s: str) -> str:
    s = re.sub(r"[*_`#>\[\]|]", " ", s.lower())
    s = s.replace("’", "'").replace(" ", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def quote_in_page(quote: str, page: str, threshold: float = 92.0) -> bool:
    """Verbatim check of an extracted quote against the text the model saw (whitespace/markup-insensitive,
    tolerant to small OCR/markdown differences)."""
    q, p = _norm(quote), _norm(page)
    if len(q) < 20:
        return False
    if q in p:
        return True
    return fuzz.partial_ratio(q, p) >= threshold


@dataclass
class Ledger:
    subqs: dict[str, SubQ] = field(default_factory=dict)
    cards: dict[str, Card] = field(default_factory=dict)
    fetched: set[str] = field(default_factory=set)
    queries: list[str] = field(default_factory=list)
    gaps: dict[str, str] = field(default_factory=dict)       # subq id -> missing info (from reflection)
    trail: list[dict] = field(default_factory=list)          # evidence trail (O3)
    mcp_seen: set[str] = field(default_factory=set)          # tool+args already called (dedupe)

    def log(self, kind: str, **data) -> None:
        self.trail.append({"t": round(time.time(), 1), "kind": kind, **data})

    def add_card(self, subq: str, url: str, title: str, quote: str, finding: str, source_kind: str,
                 date: str = "") -> Card | None:
        key = (url, _norm(quote)[:120])
        if any((c.url, _norm(c.quote)[:120]) == key for c in self.cards.values()):
            return None
        card = Card(id=f"E{len(self.cards) + 1}", subq=subq, url=url, title=title or url, quote=quote.strip(),
                    finding=finding.strip(), source_kind=source_kind, fetched_at=time.strftime("%Y-%m-%d"),
                    date=date.strip()[:40])
        self.cards[card.id] = card
        return card

    def cards_for(self, subq_ids: set[str]) -> list[Card]:
        return [c for c in self.cards.values() if c.subq in subq_ids]

    def coverage(self, subq_id: str) -> int:
        """Distinct sources supporting a sub-question."""
        return len({c.url for c in self.cards.values() if c.subq == subq_id})

    def asked(self, q: str) -> bool:
        n = _norm(q)
        return any(fuzz.ratio(n, _norm(x)) >= 90 for x in self.queries)

    def to_dict(self) -> dict:
        return {"subqs": [asdict(s) for s in self.subqs.values()], "cards": [asdict(c) for c in self.cards.values()],
                "queries": self.queries, "gaps": self.gaps, "trail": self.trail}

"""The user's own sources in the job service (requirement A4).

Validation and limits, the storage format, and the link to Open WebUI's retrieval for knowledge bases.

The pipe (running inside Open WebUI as the user) decides what the user may read and sends file texts and knowledge base
ids with the plan and the job. The service stores them with the job, so a night run does not depend on the user's
session, and passes them to the engine (`engine/own.py`); the file texts are removed when the job ends. Knowledge
bases are queried with the service's admin key, but only with the ids that were stored with the job, never with ids
the model or a request made up at run time.
"""

from datetime import UTC, datetime

import httpx
from dcc_backend_common.logger import get_logger

from bebbi_deep_research.engine.own import OwnCollection, OwnFile, OwnSources
from bebbi_deep_research.models.own_sources import PREVIEW_CHARS, OwnSourcesRequest
from bebbi_deep_research.utils.app_config import AppConfig

logger = get_logger(__name__)


def prepare(
    own: OwnSourcesRequest | None, settings: AppConfig, preview: bool = False
) -> tuple[dict | None, dict | None]:
    """Apply the limits to the user's own sources and return (stored, info).

    `stored` is what goes into the database (None if there is nothing); `info` is what the pipe shows the user
    (names, sizes, what was cut, remarks). Limits: RA_OWN_MAX_FILES, RA_OWN_MAX_FILE_CHARS, RA_OWN_MAX_COLLECTIONS.
    `preview` (plan step): files keep only their first PREVIEW_CHARS characters; the job gets the whole text.
    """
    if own is None or not (own.files or own.collections or own.notes):
        return None, None
    notes = list(own.notes)
    files, seen = [], set()
    for f in own.files:
        if f.id in seen:
            continue
        seen.add(f.id)
        if len(files) >= settings.own_max_files:
            notes.append(
                f"Datei „{f.name}“ wird nicht verwendet: höchstens {settings.own_max_files} Dateien pro Recherche."
            )
            continue
        text = f.text.strip()
        total = max(f.chars or 0, len(text))  # length of the whole file (the pipe may have sent less)
        cut = total > settings.own_max_file_chars
        if cut:
            text = text[: settings.own_max_file_chars]
            notes.append(
                f"Datei „{f.name}“ ist länger als {settings.own_max_file_chars:,} Zeichen und wird gekürzt "
                f"(die ersten {settings.own_max_file_chars:,} Zeichen werden verwendet).".replace(",", "'")
            )
        if not preview and not text:
            notes.append(f"Datei „{f.name}“ enthält keinen lesbaren Text und wird nicht verwendet.")
            continue
        files.append(
            {
                "id": f.id,
                "name": f.name,
                "text": text[:PREVIEW_CHARS] if preview else text,
                "chars": total,
                "date": f.date,
                "url": f.url,
                "cut": cut,
            }
        )
    cols, seen = [], set()
    for c in own.collections:
        if c.id in seen:
            continue
        seen.add(c.id)
        if len(cols) >= settings.own_max_collections:
            limit = settings.own_max_collections
            notes.append(f"Wissensbasis „{c.name or c.id}“ wird nicht verwendet: höchstens {limit} pro Recherche.")
            continue
        cols.append({"id": c.id, "name": c.name or "Wissensbasis"})
    base = own.base_url.rstrip("/") if own.base_url.startswith(("http://", "https://")) else ""
    stored = {"files": files, "collections": cols, "base_url": base, "notes": notes}
    return (stored if files or cols else None), describe(stored)


def describe(stored: dict | None) -> dict | None:
    """The `info` view (no texts) of a stored record: what the pipe shows the user and GET /jobs/{id} returns."""
    if not stored:
        return None
    return {
        "files": [
            {"name": f["name"], "chars": f.get("chars", len(f.get("text", ""))), "cut": f.get("cut", False)}
            for f in stored.get("files", [])
        ],
        "collections": [{"name": c["name"]} for c in stored.get("collections", [])],
        "notes": stored.get("notes", []),
    }


def without_texts(stored: dict | None) -> dict | None:
    """The stored record without the file texts (kept: names, sizes, ids, links, knowledge bases, notes).

    A finished job needs no texts: delivery and the job views use the report, the evidence and `describe`.
    """
    if not stored:
        return stored
    return {**stored, "files": [{k: v for k, v in f.items() if k != "text"} for f in stored.get("files", [])]}


class OwuiRetriever:
    """Passages of one Open WebUI knowledge base: POST /api/v1/retrieval/query/collection with the service's admin key.

    Only ids from `allowed` (those stored with the job, checked by the pipe against the user's rights at approval)
    can be queried. A hit becomes {text, name, file_id, url, date, score}; the link is Open WebUI's file route on the
    public base URL (readable for the chat user exactly when Open WebUI lets the user read the file).
    """

    def __init__(self, settings: AppConfig, allowed: list[str], base_url: str, http_factory=None, k: int = 6):
        """Create the retriever.

        Args:
            settings: Service configuration (Open WebUI URL and admin key).
            allowed: Knowledge base ids stored with the job.
            base_url: Public base URL of Open WebUI for links to files.
            http_factory: Returns the HTTP client (tests pass a fake).
            k: Passages per query.
        """
        self.settings, self.allowed, self.base_url, self.k = settings, set(allowed), base_url, k
        self.http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=60))
        self._dates: dict[str, str] = {}

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.owui_api_key}"}

    async def __call__(self, query: str, collection: OwnCollection) -> list[dict]:
        """Query one approved knowledge base and return its passages with document name, link and date.

        Raises:
            PermissionError: If the knowledge base was not stored with the job.
        """
        if collection.id not in self.allowed:
            raise PermissionError("knowledge base was not approved for this job")
        s = self.settings
        if not (s.owui_url and s.owui_api_key):
            raise RuntimeError("OWUI_URL / OWUI_API_KEY not configured")
        logger.info("knowledge query", collection=collection.id, name=collection.name)  # never the query text
        async with self.http_factory() as http:
            r = await http.post(
                f"{s.owui_url}/api/v1/retrieval/query/collection",
                headers=self._auth,
                json={"collection_names": [collection.id], "query": query, "k": self.k},
            )
            r.raise_for_status()
            data = r.json() or {}
            docs = (data.get("documents") or [[]])[0]
            metas = (data.get("metadatas") or [[]])[0]
            dist = (data.get("distances") or [[]])[0]
            hits = []
            for i, text in enumerate(docs):
                m = metas[i] if i < len(metas) and isinstance(metas[i], dict) else {}
                fid = m.get("file_id") or ""
                hits.append(
                    {
                        "text": text,
                        "name": m.get("name") or m.get("source") or "Dokument",
                        "file_id": fid,
                        "url": f"{self.base_url}/api/v1/files/{fid}/content" if fid and self.base_url else "",
                        "date": await self._date(http, fid) if fid else "",
                        "score": dist[i] if i < len(dist) else None,
                    }
                )
        return hits

    async def _date(self, http, file_id: str) -> str:
        if file_id not in self._dates:
            date = ""
            try:
                r = await http.get(f"{self.settings.owui_url}/api/v1/files/{file_id}", headers=self._auth)
                if r.status_code == 200 and (ts := r.json().get("created_at")):
                    date = datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d")
            except (httpx.HTTPError, ValueError, TypeError):
                pass
            self._dates[file_id] = date
        return self._dates[file_id]


def to_engine(stored: dict | None, settings: AppConfig, http_factory=None) -> OwnSources | None:
    """OwnSources for the engine from a stored job record (None if the job has no own sources)."""
    if not stored:
        return None
    cols = [OwnCollection(c["id"], c["name"]) for c in stored.get("collections", [])]
    retriever = (
        OwuiRetriever(settings, [c.id for c in cols], stored.get("base_url", ""), http_factory) if cols else None
    )
    return OwnSources(
        files=[
            OwnFile(f["id"], f["name"], f["text"], f.get("date", ""), f.get("url", "")) for f in stored.get("files", [])
        ],
        collections=cols,
        retrieve=retriever,
    )

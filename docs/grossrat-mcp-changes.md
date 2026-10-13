# Grossrat MCP server: requested changes

For the maintainers of `grossrat-bs-mcp` (`https://mcp-grosserrat.statistik.bs.ch/mcp`). The repository is not
reachable from the research-agent workspace, so the changes are described here instead of a pull request.
Each item says what we observed, what to change and how to test it. Evidence comes from the evaluation runs
of 2026-10-09 (evaluation repository, `docs/bebbi-bot-deep-research.md`, section "v4"; [mcp-servers.md](mcp-servers.md)).

The server already does the most important thing well: every Geschäft and document comes with a public URL
(`url`, `citation_url`, `pdf_url`) and a date, so the agent cites grosserrat.bs.ch documents directly.

## 1. Legislature periods (highest value)

**Observed.** Questions such as "Welche Geschäfte hat der Grosse Rat in der laufenden Legislatur behandelt?"
(test task DE-G3) need the start and end of the current legislature. No tool returns it. The agent guessed
"seit 2022" or "seit Oktober 2022" in 3 of 4 runs; the judge scored the task 30–52% in those runs and 78% in
the one run that found the period. A prompt rule did not help (tested, 3 repeats).

**Change.**
- `get_corpus_stats` returns `legislaturen: [{name: "2025–2029", start: "2025-02-01", end: "2029-01-31",
  current: true}, ...]` (dates from the Parlamentsdienst).
- `find_geschaefte` and `search_documents` accept `legislatur` ("2025–2029" or "current") as a filter, which
  the server turns into `since`/`until`.

**Test.** `find_geschaefte(queries=["Digitalisierung"], legislatur="current")` returns only items with
`beginn_ges` or a document inside the current period.

## 2. Default list size

**Observed.** `find_geschaefte` returns 100 items by default (over 8,000 characters). The agent now always
sends `limit: 30`; other clients do not.

**Change.** Default `limit` 25; keep `next_offset`. Mention in the description that more can be paged.

## 3. Transient error results

**Observed.** During the first test two `get_geschaeft` calls returned a short result flagged `isError`; the
same calls worked a minute later. The text gave no reason.

**Change.** Find the cause (index or database lock under concurrent calls? model warm-up?). Until then, return
a clear `ToolError` ("index busy, retry in a few seconds") so clients can retry deliberately. The research agent
retries `isError` once.

## 4. Cold start

**Observed.** The first `search_documents` call took 11.5 s, later calls under 1 s.

**Change.** Load the search model and open the index at startup; let `/health` report "not ready" until then
(the compose healthcheck already allows 60 s start time).

## 5. Missing author fields in search hits

**Observed.** `search_documents` hits have empty `urheber` and `partei`, while `find_geschaefte` fills them for
the same Geschäft.

**Change.** Fill both from the Geschäft master data.

## 6. Data plausibility

**Observed.** Geschäft `26.5406` has `beginn_ges` 2026-10-14, after its last document (2026-09-17) and after
the day of the query. The judge marks reports down for repeating such dates, and the agent cannot tell a data
error from a fact.

**Change.** Check `beginn_ges` against the document dates at import; flag implausible values
(`beginn_ges_plausible: false`) or correct them from the first document.

## 7. Shorter tool descriptions

**Observed.** `search_documents` (1,028 characters) and `find_geschaefte` (886) are sent with every model call.
The agent now replaces them with its own short texts (`config/presets.yaml`, `mcp_servers.grossrat.tools`),
which other clients (Open WebUI, ChatGPT connectors) would benefit from too:

| Tool | Description used by the agent |
|---|---|
| find_geschaefte | Complete list of Grosser Rat business items for a question. Give 3-6 German phrasings (council wording, whole compounds); filter with since/until (YYYY-MM-DD), geschaeftsart, status. Returns number, title, type, status, URL; no texts. |
| get_geschaeft | Dossier of one business item by number (e.g. "23.5544"). Status, all documents with date and URL, assignments with deadlines, council decisions. |
| search_documents | Full-text search for passages in all documents (motions, Regierungsrat answers, Ratschläge, minutes). Returns snippets with a citation link and dok_laufnr. |
| get_document | Text of one document by dok_laufnr (20,000 characters per window; continue with offset). Use it to read what a Regierungsrat answer or Ratschlag actually says. |
| list_facets | Allowed values of a filter (partei, geschaeftsart, status, dokumentart) in exact spelling. |

## 8. Availability (front proxy, not this server)

All bs.ch MCP hosts returned 503 in bursts at the same time (Apache/Airlock front), 37% of health samples during
a two-hour run on 2026-10-09. Reported as reduced since (2026-10-13).

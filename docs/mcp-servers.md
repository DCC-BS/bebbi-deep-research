# MCP servers: state and improvements

Status 2026-10-09. What the research agent needs from an MCP server, what each server delivers today, and
what to change. The code changes for our own servers are merged (2026-10-09): `mcp-data-bs` #7 and #5,
`mcp-statistikportal` #3. They take effect once the servers are redeployed.

## What the agent needs

1. **A public URL, a title and a date per result item.** The agent cites that URL, never the MCP endpoint
   or a tool name ([research_agent/sources.py](../research_agent/sources.py) reads `citation_url`,
   `pdf_url`, `url_ges`, `url`, `portal_url` from JSON results, and Markdown links from text results).
2. **Errors the model can act on.** With the MCP Python SDK only a `ToolError` reaches the model with its
   message. Any other exception arrives as "Error executing tool <name>", and the model cannot fix its call.
3. **Small results.** Lists of 10–30 items, short descriptions, full text only on request.
4. **Stable endpoints and short, precise tool descriptions.**

## Servers

| Server | Who | In presets | State |
|---|---|---|---|
| `mcp-grosserrat.statistik.bs.ch/mcp` | us (code not in this workspace) | grossrat, rechtsrecherche, statistik, verwaltung_web | Works. The best source for Grossrat tasks |
| `mcp.data.bs.ch/mcp` | us | statistik, verwaltung_web | Works; fixes merged (#5, #7), not yet deployed |
| `mcp.statistik.bs.ch/mcp` | us | statistik, verwaltung_web | Configured, **switched off** (`enabled: false` in `config/presets.yaml`) until it works |
| `mcp.opencaselaw.ch` | external | rechtsrecherche, verwaltung_web | Works; labels need fixing (done in the engine) |

### Shared front proxy (all *.bs.ch hosts)

- Bursts of `503 Service Unavailable` from the Apache/Airlock front (`Server: Apache`, cookie `AL_SESS`),
  on all three hosts at once. On 2026-10-09 at 10:31: data.bs 10 of 10 requests, Grossrat 14 of 30; a
  minute later all 200. During the first Grossrat test the server alternated 200/503 for minutes.
- The engine now retries every request up to 6 times over about 45 s, and re-opens the session after a 404.
- **Suggestion:** check the backend pool and health checks of the front proxy; monitor `/healthz` of every
  host. `scripts/mcp_health.sh` logs the status every 30 s during evaluation runs (`logs/mcp-health.log`).

### Grosser Rat (`grossrat`)

What works well:
- Results carry `url` (Geschäft page), `citation_url` (PDF page with search phrase), `pdf_url` and dates
  (`dokudatum`, `beginn_ges`). Every Geschäft and document becomes its own citable source.
- `find_geschaefte` with several phrasings plus `get_geschaeft` give complete lists with status, deadlines
  and Regierungsrat documents. DE-G3 went from 0 evidence cards (v2/v3, web only) to 13 cited Geschäfte
  in 2.5 minutes and 27k tokens.

Suggestions:
- **Transient error results.** In the first test, two `get_geschaeft` calls returned a short error flagged
  `isError`; the same calls worked a minute later. Return a clear message ("index busy, retry") or remove
  the cause. The engine now retries `isError` results once and never turns them into sources.
- **Default `limit` of `find_geschaefte` is 100**, which is over 8,000 characters. The engine now sends
  `limit: 30`. A server default of 20–30 would help every client.
- **`search_documents` hits have empty `urheber` and `partei`**, while `find_geschaefte` has them.
- The first `search_documents` call took 11.5 s (cold start?); later calls take under 1 s.
- Data: `26.5406` has `beginn_ges` 2026-10-14, after its last document (2026-09-17). The judge marks
  reports down for repeating such dates.
- **Legislature periods.** Questions like "in der laufenden Legislatur" need the start and end dates of the
  current legislature. No tool returns them; the model guessed "seit 2022" in 3 of 4 runs (DE-G3). Add them
  to `get_corpus_stats` or offer `legislatur` as a filter of `find_geschaefte`.
- The `search`/`fetch` pair duplicates `search_documents`/`get_document` (ChatGPT convention). The presets do
  not allow it, which is fine.

### data.bs.ch (`data-bs`), merged as #7 and #5

Observed in the focus engine (DE-ST1): both `get_records` calls failed because `jahr` is a text field and the
model wrote `jahr >= '2016'`. The portal answers with a clear message
(`IncompatibleTypesInComparisonFilter`), but the server swallowed it.

Merged in #7 (tests pass, ruff clean); #5 adds a round-trip test for the encoded `where` clause of export URLs:
- API errors, timeouts and network errors are raised as `ToolError` with the portal's message.
- `url` (dataset page) in `get_datasets`, `get_dataset` and `get_records`; `dataset_id` in `get_records`.
- List descriptions cut to 400 characters (`get_dataset` returns the full text): 8 hits took 7,500 characters.
- `get_records` docstring: year fields are often text, use `group_by`/`order_by` or `in (...)`.

Further suggestions: units and field descriptions in `get_dataset` (`fields[].description` from the
portal); `modified` labelled as Stand; keep `/mcp` stable.

### Statistics portal (`statistik`), merged as #3

Not called in this round, as agreed. Code only: `fetch` raises `ToolError` with a reason (unknown id, portal
not reachable, no JSON) instead of a bare crash. 17 tests pass. Results already carry `portal_url`,
`aktualisierungsdatum` and `quellenangabe`, which the source layer uses as URL, Stand and title. Switch the
server on with `enabled: true` in `config/presets.yaml` once it works.

### opencaselaw (external)

- **Labels every number "SR"**, also for cantonal law ("Gesetz über die Information und den Datenschutz
  (SR 153.260)" for Basel-Stadt). This is where the SG/SR mix-up in earlier reports came from. The engine
  rewrites the labels in law-tool results (BS → SG, BL → SGS, ZH → LS, BE → BSG, …) and fixes "SR <nr>" in the
  report when the evidence shows the number is cantonal.
- `get_law` links only LexFind; `get_legislation` also lists `gesetzessammlung.bs.ch`, which the engine
  prefers as the citation URL.
- An advertising footer on every result (stripped by the engine); "§ None" in `search_laws` titles.
- Decision links (`mcp.opencaselaw.ch/entscheid/...`) are public pages and are kept as sources.
- If we self-host it (task list "Later"), fix the labels at the source.

# Presets, engines, budgets

`config/presets.yaml` is the only steering surface: no code per topic. In Kubernetes it is a ConfigMap
(chart: `presets.inline` or `presets.existingConfigMap`; the chart ships a copy in `files/presets.yaml`, kept identical to
`config/presets.yaml` by a test). Restart the pod after a change (the chart rolls the pod via a checksum annotation).

The file is validated when the service starts (`engine/config.py`, pydantic): an unknown or misspelled key at any level,
an `engine`, `language` or `own_sources` value outside the lists below, a route other than `web` or `mcp:<server of
mcp_servers>`, an `mcp` allowlist for a server that is not configured or with a tool missing from the server's `tools`,
a `budget` override of a field that does not exist, and a missing `quick`/`standard`/`deep` budget stop the start with
a message that lists every problem (`presets file ... is invalid: - presets.dokumente.own_source: unknown key (typo?)`).
Check a file (for example the ConfigMap copy in kdkp-kiw-k8s-config) before deploying it:
`uv run python -m bebbi_deep_research.engine.config path/to/presets.yaml`.

## Engines

| Engine | Presets | How it works |
|---|---|---|
| `focus` | rechtsrecherche, grossrat, statistik, verwaltung_web, dokumente | Plan (checklist), then a bounded tool-calling loop over web search, page fetch and the preset's MCP tools. Every source becomes a record with URL, title, Stand and a verbatim quote; the report cites `[S<n>]` and code resolves them to a numbered source list. Numbers and identifiers are checked against the evidence. About 60k tokens and 3 minutes per report. |
| `pipeline` | web_survey | Plan, research rounds per sub-question (queries, fetch, extraction into evidence cards), section-wise writing, verification (quote and entailment checks), summary. About 190k tokens and 5 minutes per report. |

Daytime rule: only `quick` runs of a `focus` preset may start outside the night window.

## Budgets (depth)

`quick`, `standard`, `deep` in `budgets:`. Fields: `rounds`, `max_subq`, `queries_per_subq`, `fetch_per_subq`,
`max_follow_up`, `max_tokens` (hard stop), `words_per_part`, `parts` (min, max parts), `focus_calls` (tool calls of the
focus engine), `focus_words` (typical report length). A preset may override single fields with `budget:`.

## Preset fields

| Field | Meaning |
|---|---|
| `engine` | `focus` or `pipeline` |
| `description` | shown in the model description and by `GET /presets` |
| `language` | `de`, `en`, `auto` (language of the task) |
| `routes` | per intent (`primary`, `data`, `secondary`) the ordered sources: `web`, `mcp:<server>` |
| `mcp` | per MCP server the tool allowlist |
| `prefer_domains` / `avoid_domains` | domain lists per intent / global |
| `query_rules` | text added to query generation and to the focus engine's instructions |
| `report` | `parts_hint`, `words_per_part`, `analysis_part`, `closing_part`, `writer_rules`, `length_rule`, `format_rule` |
| `verify_entailment` | check that cited quotes support the sentence |
| `plan_first` | focus engine: plan before the tool loop (the plan is the checklist); default `true` |
| `house_style` | `true` (default): German reports follow the Hausstil Kanton Basel-Stadt (`engine/house_style.py`, copied from text-mate-backend `utils/house_style.py`: tone, short sentences, Swiss spelling and typography, numbers, dates, `Fr. 20.–`, gender-neutral wording, anglicisms). Letter rules (Sie/wir) are switched off for reports and citations `[S12]` stay untouched. Not applied to English reports. `false` turns it off |
| `own_sources` | what happens with files uploaded in the chat and knowledge bases (own sources, [integration.md](integration.md#5b-own-sources-uploaded-files-and-knowledge-bases-requirement-a4)): `prefer` (default) the model uses them first and web search and data tools for the rest; `only` removes web search, page fetch and all MCP tools (and the planner's web discovery), the report rests on the user's documents alone and the service refuses a question without any readable own source; `off` ignores attachments and says so in the plan |

Own sources work in both engines. Focus engine: tools `read_upload` and `knowledge_search`, small uploads are read directly. Pipeline
engine: in every sub-question the best matching uploads and the knowledge hits are extracted into evidence cards before the web and MCP routes
(in `only` mode instead of them). Source kinds in the ledger: `upload`, `knowledge:<knowledge base>`; labels "Hochgeladene Datei" and
"Wissensbasis <Name>". The shipped preset `dokumente` ("Eigene Dokumente") is the `only` example.

## Add a preset

1. Add an entry under `presets:` (copy `grossrat` for a focus preset; set `routes`, `mcp`, `prefer_domains`, `query_rules`, `report`).
2. Add `name:Label` to the pipe valve `PRESETS` (the preset appears in the card of "Deep Research"; the service
   suggests it from its `description`) and to `LABELS` and `SUGGESTIONS` in `scripts/build_openwebui_assets.py`; run
   `uv run python scripts/build_openwebui_assets.py`.
3. Add the label for the Word file to `PRESET_LABELS` in `services/docx_export.py` (without it the Word header shows
   the preset name) and the preset name to the list in `tests/unit/test_packaging.py`.
4. Copy the file into the chart: `cp config/presets.yaml deploy/helm/bebbi-deep-research/files/presets.yaml`, and the
   deployed copy in kdkp-kiw-k8s-config.
5. Run `mise run test:unit` (it loads and validates both copies) and test with `scripts/test_pipe.py "<question>" <preset>`.

## Add an MCP server

1. Under `mcp_servers:` add `name: {url: https://.../mcp, hint: "how to use its tools"}`. The hint is shown to the focus
   engine next to the tools. `enabled: false` keeps it configured but unused.
2. In presets add `mcp: {name: [tool, ...]}` (allowlist; servers like opencaselaw expose many tools) and, if the server is a
   primary source, `routes: {primary: [mcp:name, web]}`.
3. Server requirement for good citations: each result item should carry a public URL, a title and a date
   ([mcp-servers.md](mcp-servers.md)); the engine turns every item with a URL into its own citable source.
4. The MCP host must be reachable from the pod, with or without proxy ([operations.md](operations.md#proxy)).

# Presets, engines, budgets

`config/presets.yaml` is the only steering surface: no code per topic. In Kubernetes it is a ConfigMap
(chart: `presets.inline` or `presets.existingConfigMap`; the chart ships a copy in `files/presets.yaml`, kept identical to
`config/presets.yaml` by a test). Restart the pod after a change (the chart rolls the pod via a checksum annotation).

## Engines

| Engine | Presets | How it works |
|---|---|---|
| `focus` | rechtsrecherche, grossrat, statistik, verwaltung_web | Plan (checklist), then a bounded tool-calling loop over web search, page fetch and the preset's MCP tools. Every source becomes a record with URL, title, Stand and a verbatim quote; the report cites `[S<n>]` and code resolves them to a numbered source list. Numbers and identifiers are checked against the evidence. About 60k tokens and 3 minutes per report. |
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

## Add a preset

1. Add an entry under `presets:` (copy `grossrat` for a focus preset; set `routes`, `mcp`, `prefer_domains`, `query_rules`, `report`).
2. Add `name:Label` to the pipe valve `PRESETS` (the model "Deep Research · Label" appears) and to `LABELS` in
   `scripts/build_openwebui_assets.py` if you want a seeded model entry; run `uv run python scripts/build_openwebui_assets.py`.
3. Copy the file into the chart: `cp config/presets.yaml deploy/helm/bebbi-deep-research/files/presets.yaml`.
4. Test with `scripts/test_pipe.py "<question>" <preset>`.

## Add an MCP server

1. Under `mcp_servers:` add `name: {url: https://.../mcp, hint: "how to use its tools"}`. The hint is shown to the focus
   engine next to the tools. `enabled: false` keeps it configured but unused.
2. In presets add `mcp: {name: [tool, ...]}` (allowlist; servers like opencaselaw expose many tools) and, if the server is a
   primary source, `routes: {primary: [mcp:name, web]}`.
3. Server requirement for good citations: each result item should carry a public URL, a title and a date
   ([mcp-servers.md](mcp-servers.md)); the engine turns every item with a URL into its own citable source.
4. The MCP host must be reachable from the pod, with or without proxy ([operations.md](operations.md#proxy)).

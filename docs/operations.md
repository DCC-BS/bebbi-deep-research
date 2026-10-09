# Operations

## Configuration reference

All settings are environment variables (Helm: `env:` plain values, `secrets.keys:` Secret references).
Defaults are those of the evaluated setup. Variables marked **S** are secrets.

### Service

| Variable | Default | Meaning |
|---|---|---|
| `RA_TOKEN` **S** | required | bearer token of the API; same value as the pipe valve `SERVICE_TOKEN` |
| `RA_DATABASE_URL` **S** | `sqlite+aiosqlite:////tmp/research-agent.db` | `postgresql://user:pw@host:5432/db` (mapped to asyncpg). `RA_DB=/path/file.db` is accepted as SQLite shortcut |
| `RA_PRESETS` | `config/presets.yaml` (`/app/config/presets.yaml` in the image, `/config/presets.yaml` in the chart) | presets, MCP servers, budgets |
| `RA_HOST`, `RA_PORT` | `0.0.0.0`, `8088` | listen address |
| `RA_LOG_LEVEL` | `INFO` | JSON logs on stdout |

### LLM roles

The engine uses three roles: `think` (planning, research, writing), `fast` (decisions, routing, short texts) and `embed`
(passage selection). One OpenAI-compatible endpoint serves all three.

| Variable | Default | Meaning |
|---|---|---|
| `RA_LLM_BASE_URL` | required | e.g. `http://vllm.llm.svc:8000/v1` |
| `RA_LLM_API_KEY` **S** | empty | `{job}` is replaced by the job id (per-job metering at the evaluation gateway) |
| `RA_LLM_MODEL_THINK` / `_FAST` / `_EMBED` | `gemma-think` / `gemma-fast` / `qwen-embed` | real model names. Production: `RedHatAI/gemma-4-31B-it-NVFP4` for think and fast, `Qwen/Qwen3-Embedding-4B` for embed |
| `RA_LLM_EXTRA_BODY_THINK` / `_FAST` | empty | JSON merged into every chat request of that role. vLLM thinking switch: `{"chat_template_kwargs": {"enable_thinking": true}}` for think, `false` for fast. Empty = nothing added (the evaluation selected thinking by model alias). Verify the key name against the model's chat template |

The embed role needs `/v1/embeddings` on the same endpoint.

### Search, fetch, MCP

| Variable | Default | Meaning |
|---|---|---|
| `RA_SEARCH_URL`, `RA_SEARCH_KEY` **S** | required | Serper-compatible search endpoint (`https://google.serper.dev/search`); key in the `X-API-KEY` header |
| `RA_FIRECRAWL_URL`, `RA_FIRECRAWL_KEY` **S** | required / empty | Firecrawl v2 `/v2/scrape` |
| MCP servers | in `presets.yaml` | URL per server, `enabled: false` keeps one configured but unused (statistik is off) |

### Own sources (uploaded files, knowledge bases)

| Variable | Default | Meaning |
|---|---|---|
| `RA_OWN_MAX_FILES` | `10` | uploaded files used per research; more are dropped with a remark in the chat |
| `RA_OWN_MAX_FILE_CHARS` | `2000000` | extracted text per file; longer texts are cut (remark in the chat) |
| `RA_OWN_MAX_COLLECTIONS` | `5` | knowledge bases per research |

File texts are stored with the job (`jobs.own_sources`; drafts keep a 1,500-character preview) and stay there with the finished job like the
report; plan a retention job if documents must not stay in the database. Knowledge bases are read at run time through
`OWUI_URL`/`OWUI_API_KEY` (`POST /api/v1/retrieval/query/collection`, one stored id at a time, logged as `knowledge query`); without both
variables a knowledge base cannot be used (the tool reports "not reachable" to the model and the report says so under open points).
Access rules and limits: [integration.md](integration.md#5b-own-sources-uploaded-files-and-knowledge-bases-requirement-a4).

### Queue and window

| Variable | Default | Meaning |
|---|---|---|
| `RA_NIGHT_WINDOW` | `20:00-05:00` | `HH:MM-HH:MM`, may cross midnight |
| `RA_TIMEZONE` | `Europe/Zurich` | time zone of the window (the container runs UTC) |
| `RA_WORKERS` | `1` | concurrent jobs inside the window (per replica) |
| `RA_DAY_WORKERS` | `1` | concurrent jobs outside the window |
| `RA_QUOTA_PER_DAY` | `0` | jobs per user and 24 h, `0` = off (decision 2026-10-09). Exceeding returns HTTP 429 |
| `RA_POLL_SECONDS` | `5` | worker loop interval |
| `RA_STALE_SECONDS` | `120` | a running job without heartbeat for this long is re-queued (dead replica) |

### Delivery

| Variable | Meaning |
|---|---|
| `OWUI_URL` | Open WebUI base URL (cluster-internal) |
| `OWUI_API_KEY` **S** | API key of the admin service account. Without both, delivery is `skipped` and the report is only available via the pipe fallback and the API |

### Proxy

All outgoing HTTP (httpx, OpenAI client) honours `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`
(chart: `proxy.http`, `proxy.https`, `proxy.noProxy`). Production egress is `http://proxy.bs.ch:3128`.
`NO_PROXY` takes host names, domain suffixes (`.svc`), IPs and CIDR ranges, no wildcards.

* Must bypass the proxy: vLLM, Firecrawl, Open WebUI, Postgres (all cluster-internal).
* Depends on where they are reachable from: Serper (`google.serper.dev`) and the MCP servers (`mcp.opencaselaw.ch`
  external; `*.bs.ch` hosts may be internal). If a `*.bs.ch` MCP host is reachable only directly, add it to `NO_PROXY`;
  if only via proxy, leave it out. A wrong choice shows up as connection errors in the MCP log lines of a run and the
  engine silently falls back to web search.

## Night window and daytime rule

* `when=night` jobs (the default of the API; the pipe sends it for "heute Nacht" and for depth `deep`) start only inside the window.
* `when=now` jobs start at once inside the window. Outside it, **only `quick` runs of a focus-engine preset** start
  immediately; every other combination is changed to `night` by the service (the response says `"when": "night"`).
  Focus presets: all except `web_survey`.
* Concurrency: `RA_WORKERS` inside the window, `RA_DAY_WORKERS` outside (default 1 each). Jobs run in order of submission.
* A quick focus run costs about 20-60k tokens and 2-3 minutes. A night of 9 hours fits roughly 100 survey reports
  (4.5 min) or 200 focus reports, sequentially, on the evaluation GPUs.

## Scaling and GPU load

* Load on vLLM = `workers x` the calls of one run. Each run issues LLM calls in parallel (the engine uses a
  semaphore of 3 per job). Raise `RA_WORKERS` only after watching vLLM queue time (`vllm:num_requests_waiting`)
  during the night.
* More replicas multiply the worker counts. Jobs are claimed with an atomic `update ... where status='queued'`, so replicas
  do not run the same job twice. One replica is the recommended setup.
* Token budgets per run are set by depth in `presets.yaml` (`quick` 200k, `standard` 450k, `deep` 900k tokens, hard stop).
* Measure before allowing more daytime runs: `RA_DAY_WORKERS` is the only knob that moves load into office hours.

## Job lifecycle and restarts

`queued` to `starting` to `running` to `done` or `error`. Events (status lines) are stored with the job (last 50).

* **Graceful shutdown (SIGTERM):** running jobs are cancelled and put back to `queued`; they restart from the beginning on
  the next start (no partial resume). Set `terminationGracePeriodSeconds` >= 30.
* **Crash / OOM kill:** the heartbeat (every worker tick) stops; after `RA_STALE_SECONDS` any replica re-queues the job.
* **Failure:** the job becomes `error` with the message; the message is also written into the chat.
* Drafts older than 14 days are deleted; jobs and reports are kept (add a retention job if needed).

## API

Bearer token on all except probes. Compatible with the pipe and usable from n8n.

| Method, path | Purpose |
|---|---|
| `GET /healthz` | liveness (process up) |
| `GET /readyz` | readiness: 200 if the database answers and the configuration is complete, else 503 with `problems` |
| `GET /metrics` | Prometheus text: `research_jobs{status}`, `research_in_night_window` (unauthenticated, no content) |
| `GET /presets` | preset names, descriptions, engine |
| `POST /clarify` `{task, preset?, own_sources?}` | clarifying questions and assumptions (knows the attached sources) |
| `POST /plans` `{task, preset, depth, own_sources?}` | plan draft: `{draft_id, plan, own_sources}` (HTTP 422 with a user message if the preset is `own_sources: only` and nothing readable is attached) |
| `POST /plans/{draft}/edit` `{instruction}` | edited plan (`own_sources` info of the draft) |
| `POST /jobs` `{draft_id, user_id, chat_id?, message_id?, when, own_sources?}` | submit: `{job_id, when, ahead, window, own_sources}`; `own_sources` = `{files: [{id, name, text, chars?, date?, url?}], collections: [{id, name}], base_url, notes}`, sent by the pipe after it checked what the user may read. Send it with the job again: the job takes only what this request carries |
| `GET /jobs/{id}` | status, last 10 events, report, stats (token usage), delivery state, `sources` (number, title, url, stand, fetched, kind, excerpt), `own_sources` (names and sizes, no texts), `ui_report`, `owui_events`, `docx` |
| `GET /jobs/{id}/report.docx` | the report as Word file (409 while the job runs) |
| `GET /jobs/lookup?chat_id&message_id&user_id` | job that delivers into a chat message (used by the download action) |
| `GET /jobs/{id}/evidence` | evidence trail: sources, queries, pages, quotes |

## Logs and metrics

One JSON object per line on stdout: `ts`, `level`, `logger`, `msg` plus fields. Job events: `job queued`, `job started`,
`job done` (with `runtime_s`, `calls`, `prompt_tokens`, `completion_tokens`), `job failed`, `job re-queued on shutdown`,
`delivery ok` / `delivery failed...`, `re-queued stale jobs`. Token usage per run is also in `GET /jobs/{id}` under
`stats.usage`. OTel is not built in; use the `/metrics` endpoint and log shipping.

## Troubleshooting

| Symptom | Check |
|---|---|
| Pod not ready | `curl /readyz`: `problems` lists missing variables, wrong window syntax, unknown time zone or an unreachable database |
| Pipe says "Deep Research ist nicht verfügbar: invalid token" | `SERVICE_TOKEN` valve differs from `RA_TOKEN` |
| Job stays `queued` | outside the window and not a quick focus run: it waits for 20:00. `GET /metrics` shows `research_in_night_window`. Check `RA_TIMEZONE` |
| Job `queued` forever inside the window | all workers busy (`RA_WORKERS`), or no replica running |
| Report not in the chat | `delivery` field of the job: `failed: HTTP 401` = API key / `ENABLE_API_KEYS` / endpoint restrictions; `HTTP 404/401` on chat = wrong chat; `skipped` = no `OWUI_URL`/`OWUI_API_KEY`. The user gets the report by writing again in that chat |
| Many `503` from the MCP servers | the shared front proxy of the `*.bs.ch` hosts returns bursts of 503 (all hosts at once, minutes long). The MCP client retries each request up to 6 times over about 45 s, re-opens the session after a 404 and falls back to web search for 2 minutes after three failed connects. Nothing to do on our side except monitoring `/healthz` of the hosts (see [mcp-servers.md](mcp-servers.md)); runs complete anyway, with fewer MCP sources |
| Reports without MCP sources | the MCP host is unreachable from the pod (proxy / NO_PROXY, NetworkPolicy), or `enabled: false` |
| Empty answers / "length" finishes from the LLM | thinking tokens count against `max_tokens`; the engine retries once with double the budget. Check `RA_LLM_EXTRA_BODY_*` switches thinking off for the fast role |
| Timeouts | LLM client timeout is 1800 s with 3 retries; Firecrawl read timeout 150 s |
| Own sources: plan says "nicht lesbar" for a file or knowledge base the user can read in Open WebUI | the pipe checks with the chat user's rights inside Open WebUI; check the share settings of the knowledge base (read grant for the user or group) or whether the file belongs to the user or to a knowledge base they can read |
| Own sources: "wird noch verarbeitet" / "keinen lesbaren Text" | Open WebUI has not finished extracting the file, or it is a scan without a text layer (enable OCR/content extraction in Open WebUI's document settings) |
| Own sources: no upload button, or Open WebUI rewrites the question | the model entries need `capabilities.file_upload: true` and `file_context: false` (`deploy/openwebui/models.json`, re-import after upgrading the repo) |
| Own sources: knowledge base "not reachable" in the report | `OWUI_URL` / `OWUI_API_KEY` of the service, `POST /api/v1/retrieval/query/collection` needs an admin key; log lines `knowledge query` show the ids queried |
| Database | tables `drafts` and `jobs` are created on start; the column `own_sources` of both is added automatically to older databases; other schema changes still need a manual `alter table` |

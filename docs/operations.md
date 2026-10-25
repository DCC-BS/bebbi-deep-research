# Operations

## Configuration reference

All settings are environment variables (Helm: `env:` plain values, `secrets.keys:` Secret references). The schema is
`.env.schema` (varlock): the image validates the environment with it before the service starts (`APP_MODE=prod`), and
`mise run dev` loads local secrets from Proton Pass (`.env.dev.schema`, placeholder vault paths to be filled in).
Defaults are those of the evaluated setup. Variables marked **S** are secrets. A missing required variable or an invalid
value (window syntax, time zone, worker counts, presets file) stops the start with a clear error.

### Service

| Variable | Default | Meaning |
|---|---|---|
| `HMAC_SECRET` **S** | required | pseudonymises user ids in usage events (`openssl rand 32 \| base64`) |
| `RA_TOKEN` **S** | required | bearer token of the API; same value as the pipe valve `SERVICE_TOKEN`. Full trust: the holder can read all jobs and reports and submit for any user. Separate tokens per caller are not supported yet |
| `RA_DATABASE_URL` **S** | `sqlite+aiosqlite:////tmp/research-agent.db` | `postgresql://user:pw@host:5432/db` (mapped to asyncpg). Special characters in the password must be URL-encoded (`@` -> `%40`, `/` -> `%2F`, `:` -> `%3A`, `#` -> `%23`); the same goes for `POSTGRES_PASSWORD` in compose, which is placed into this URL. `RA_DB=/path/file.db` is accepted as SQLite shortcut |
| `RA_PRESETS` | `config/presets.yaml` (`/app/config/presets.yaml` in the image, `/config/presets.yaml` in the chart) | presets, MCP servers, budgets |
| `PORT` | `8088` | listen port of the image (`RA_HOST`/`RA_PORT` for `uv run bebbi-deep-research`) |
| `LOG_LEVEL` | `INFO` | backend-common logger level (usage events are always logged) |
| `IS_PROD` | `true` in the image | JSON log lines; `false` = coloured console output |
| `PYDANTIC_AI_NO_BANNER` | `true` | suppresses pydantic-ai's start banner |

### LLM roles

The engine uses three roles: `think` (planning, research, writing), `fast` (decisions, routing, short texts) and `embed`
(passage selection). One OpenAI-compatible endpoint serves all three.

| Variable | Default | Meaning |
|---|---|---|
| `RA_LLM_BASE_URL` | required | e.g. `http://vllm.llm.svc:8000/v1` |
| `RA_LLM_API_KEY` **S** | empty | `{job}` is replaced by the job id (per-job metering at the evaluation gateway) |
| `RA_LLM_EMBED_BASE_URL` | empty | embeddings endpoint when a gateway serves them under another path than the chat models (KDKP: `.../kis/embeddings/v1` next to `.../kis/gemma4/v1`); empty: `RA_LLM_BASE_URL` |
| `RA_LLM_EMBED_API_KEY` **S** | empty | key for `RA_LLM_EMBED_BASE_URL` (`{job}` as above); used only when that is set |
| `RA_LLM_MODEL_THINK` / `_FAST` / `_EMBED` | `gemma-think` / `gemma-fast` / `qwen-embed` | real model names. Production: `RedHatAI/gemma-4-31B-it-NVFP4` for think and fast, `Qwen/Qwen3-Embedding-4B` for embed |
| `RA_LLM_EXTRA_BODY_THINK` / `_FAST` | empty | JSON merged into every chat request of that role, over backend-common's thinking switch (`chat_template_kwargs.enable_thinking`: `true` for think, `false` for fast, always sent). Verify the key name against the model's chat template |
| `RA_LLM_TIMEOUT_SECONDS` | `1800` | timeout of one chat or embedding request (httpx read timeout: a slow but steady answer does not trip it). The run as a whole is capped by `RA_JOB_TIMEOUT_SECONDS` |
| `RA_LLM_MAX_RETRIES` | `3` | retries of a request after a timeout, a dropped connection, 5xx or 429 (backoff, `Retry-After` honoured); a refused connection is not retried |
| `RA_LLM_CONCURRENCY` | `6` | requests the process sends to the LLM endpoint at the same time (chat and embeddings, all jobs and chat turns together); further requests wait in the process |

The LLM layer is backend-common's pydantic-ai `BaseAgent` (one per role): retries of transient errors with backoff
(`RA_LLM_MAX_RETRIES`, not of refused connections), `max_completion_tokens` per role (think 16000, fast 4000; an empty answer is retried once
with twice the budget), one `llm_call` usage event per call, `ß` replaced by `ss` in every answer.

The embed role needs `/v1/embeddings` on the same endpoint, or its own endpoint in `RA_LLM_EMBED_BASE_URL`.

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

Long files are split into at most about 1,200 passages and embedded once per run; the vectors are kept as float32
(about 12 MB per file of 2 million characters with Qwen3-Embedding-4B, so 10 such files take about 120 MB of the pod's
memory for the duration of the run). Similarity scoring runs in a worker thread.

File texts are stored with the job (`jobs.own_sources`; drafts keep a 1,500-character preview) and stay there with the finished job like the
report; plan a retention job if documents must not stay in the database. Knowledge bases are read at run time through
`OWUI_URL`/`OWUI_API_KEY` (`POST /api/v1/retrieval/query/collection`, one stored id at a time, logged as `knowledge query` at DEBUG level); without both
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
| `RA_JOB_TIMEOUT_SECONDS` | `3600` | a run that takes longer is stopped and reported as failed in the chat; a hung LLM or MCP call cannot block the queue |

### Delivery

| Variable | Meaning |
|---|---|
| `OWUI_URL` | Open WebUI base URL (cluster-internal) |
| `OWUI_API_KEY` **S** | API key of the admin service account: knowledge bases and file dates of own sources (no chat access). Without it a knowledge base cannot be used |
| `OWUI_DELIVERY_SECRET` **S** | shared secret of the delivery route in the Open WebUI image (`DCC_DEEP_RESEARCH_DELIVERY_SECRET` there). Without it delivery is `skipped` and the report is only available via the pipe fallback and the API |
| `OWUI_DELIVERY_URL` | full URL of the route; default `OWUI_URL` + `/api/v1/dcc/deep-research/deliver` |

### Proxy

All outgoing HTTP (httpx, OpenAI client) honours `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`
(chart: `proxy.http`, `proxy.https`, `proxy.noProxy`). Production egress is `http://proxy.bs.ch:3128`.
`NO_PROXY` takes host names, domain suffixes (`.svc`), IPs and CIDR ranges, no wildcards.

* Must bypass the proxy: vLLM, Firecrawl, Open WebUI, Postgres (all cluster-internal).
* Depends on where they are reachable from: Serper (`google.serper.dev`) and the MCP servers (`mcp.opencaselaw.ch`
  external; `*.bs.ch` hosts may be internal). If a `*.bs.ch` MCP host is reachable only directly, add it to `NO_PROXY`;
  if only via proxy, leave it out. A wrong choice shows up as `mcp request retry` and `mcp connect failed, web search
  instead for 2 minutes` log lines (WARNING, with the server or host), and the run falls back to web search.

## Night window and daytime rule

* `when=night` jobs (the default of the API; for now the pipe sends it for every approved plan) start only inside the window.
* `when=now` jobs start at once inside the window. Outside it, **only `quick` runs of a focus-engine preset** start
  immediately; every other combination is changed to `night` by the service (the response says `"when": "night"`).
  Focus presets: all except `web_survey`.
* Concurrency: `RA_WORKERS` inside the window, `RA_DAY_WORKERS` outside (default 1 each). Jobs run in order of submission.
* A quick focus run costs about 20-60k tokens and 2-3 minutes. A night of 9 hours fits roughly 100 survey reports
  (4.5 min) or 200 focus reports, sequentially, on the evaluation GPUs.

## Scaling and GPU load

* Load on vLLM: at most `RA_LLM_CONCURRENCY` (default 6) requests per replica at a time, chat and embeddings
  together, however many jobs run. Inside a job, 3 sub-questions are researched at once and each fans out into
  page reads and extractions; without the cap one pipeline run reaches 15-20 concurrent calls. Requests above the
  cap wait in the service, not in vLLM. Raise `RA_WORKERS` or `RA_LLM_CONCURRENCY` only after watching vLLM queue
  time (`vllm:num_requests_waiting`) during the night.
* More replicas multiply the worker counts. Jobs are claimed with an atomic `update ... where status='queued'`, so replicas
  do not run the same job twice. One replica is the recommended setup; run more only after job claims carry an owner token (until then a stale re-queue can let two replicas work on the same job). The chart uses the `Recreate` strategy so that a rollout never runs two pods at once.
* Token budgets per run are set by depth in `presets.yaml` (`quick` 200k, `standard` 450k, `deep` 900k tokens, hard stop).
* Measure before allowing more daytime runs: `RA_DAY_WORKERS` is the only knob that moves load into office hours.

## Job lifecycle and restarts

`queued` to `starting` to `running` to `done` or `error`. Events (status lines) are stored with the job (last 50).

* **Graceful shutdown (SIGTERM):** running jobs are cancelled and put back to `queued`; they restart from the beginning on
  the next start (no partial resume). uvicorn waits up to 20 s (`--timeout-graceful-shutdown` in the Dockerfile entrypoint); the chart sets `terminationGracePeriodSeconds: 45`, keep it above that plus the re-queue.
* **Crash / OOM kill:** the heartbeat (every worker tick) stops; after `RA_STALE_SECONDS` any replica re-queues the job.
* **Failure:** the job becomes `error` with the message; the message is also written into the chat.
* Drafts older than 14 days are deleted; jobs and reports are kept (add a retention job if needed).

## API

Bearer token on all except probes and metrics. Errors use backend-common's format
`{"errorId", "status", "debugMessage"}`; `debugMessage` is German text the pipe shows as it is (request validation errors
keep FastAPI's `detail`).

| Method, path | Purpose |
|---|---|
| `GET /health/liveness`, `/health/startup` | process up / started (backend-common probes) |
| `GET /health/readiness` | 200 if the LLM endpoint (`/models`) and Open WebUI (`/health`, if `OWUI_URL` is set) answer, else 503 with `checks` |
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

## NetworkPolicy

`networkPolicy.enabled: true` creates an ingress rule for port 8088 from the Open WebUI pods plus `extraIngressFrom`, and the egress list from
`values.yaml`. Caveats:

* Open WebUI normally runs in another namespace: set `networkPolicy.openWebUINamespaceLabels` (e.g. `{kubernetes.io/metadata.name: open-webui}`);
  without it only pods of the service's own namespace match.
* Prometheus scraping `/metrics` and n8n need an entry in `extraIngressFrom` (see the comments in `values.yaml`).
* Egress rules match ports only. Adapt them to the cluster (DNS, proxy port, Postgres, vLLM, Firecrawl, Open WebUI, 443 for MCP servers/Serper
  without proxy); a port that is missing shows up as timeouts. The 8080 rule opens that port to all destinations, tighten it with `to:` selectors.
* Check the result: `helm template . --set networkPolicy.enabled=true`.

## Tests and packaging

CI (`.github/workflows/main.yml`, DCC-BS ci-workflows) runs `mise run ci-check` (lockfile, ruff format and lint, ty,
varlock scan) and `mise run test:unit`; a second job runs `tests/integration` and the unit suite against a Postgres service container. `tests/unit/test_packaging.py` fails if
`deploy/helm/bebbi-deep-research/files/presets.yaml` differs from `config/presets.yaml`. The image is built and pushed
to GHCR by `.github/workflows/docker-publish.yml` (manual trigger), tagged with the version in `pyproject.toml` and
`latest`. A version is published once: the workflow stops if the tag already exists, so bump the version (and
`Chart.yaml`) for every build. kdkp-kiw-k8s-config pins `<version>@sha256:<digest>`, and Renovate opens the PR for the
next version. The image carries `org.opencontainers.image.revision` (the commit).

Postgres: `mise run integration` with `RA_TEST_DATABASE_URL=postgresql://user:pw@localhost:5432/test` runs
`tests/integration` (concurrent schema init, upgrade of a first-release database). The same variable runs the unit suite
on Postgres instead of SQLite (`tests/unit/conftest.py` drops `jobs` and `drafts` before each test: use a throwaway
database).

## Logs and metrics

backend-common logger (structlog): one JSON object per line on stdout with `timestamp`, `level`, `logger`, `app`,
`event` (closed vocabulary: `request_finished`, `request_failed`, `llm_call`, `app_event`, `health check failed` /
`recovered`) or `message` (free text) plus fields. Every request carries a `request_id`. Job messages: `job queued`,
`job started`, `job done` (with `runtime_s`, `calls`, `prompt_tokens`, `completion_tokens`), `job failed`,
`job re-queued on shutdown`, `delivery ok` / `delivery failed`, `re-queued stale jobs`. Usage events (`app_event`)
`research.plan` and `research.submit` carry a pseudonymised user id (HMAC), never the raw id. Token usage per run is
also in `GET /jobs/{id}` under `stats.usage`. Use the `/metrics` endpoint and log shipping.

Engine lines (written during a run; they carry the `job_id` the job runner binds to the log context). They name
hosts, never search queries, page paths or document text:

| Line | Level | Fields | Meaning |
|---|---|---|---|
| `llm request retry` | WARNING | `model`, `attempt`, `error`, `wait_s` | a chat request is retried (timeout, dropped connection, 5xx, 429) |
| `empty LLM answer, retrying with a larger budget` | WARNING | `step`, `role` | thinking used up `max_completion_tokens` |
| `search failed` / `fetch failed` | ERROR for 401/403, else WARNING | `tool`, `reason`, `host` | Serper or Firecrawl answered with another status than 200, failed, or sent unreadable JSON |
| `tool call` / `tool failed` | INFO / WARNING | `tool`, `host` (fetch_page), `sources`, `chars`, `seconds` / `error` | one tool call of the focus engine |
| `focus loop done` | INFO | `tool_calls`, `sources`, `tokens` | the focus engine's tool loop ended |
| `research round done` | INFO | `round`, `subqs`, `new_cards`, `tokens` | one research round of the pipeline engine |
| `research route failed` | WARNING | `route`, `subq`, `error` | a source route (web, MCP server, own sources) of a sub-question failed; the run goes on |
| `mcp request retry` | WARNING | `host`, `method`, `attempt`, `reason` | an MCP request got a 5xx or a transport error and is repeated |
| `mcp session re-opened` | INFO | `host`, `method` | the server forgot the session (404) |
| `mcp tool error result` | WARNING | `host`, `tool`, `attempt` | the server answered with `isError` (retried once) |
| `mcp tool failed` | WARNING | `server`, `tool`, `error` | a pipeline MCP call failed after its retries; the model gets the error |
| `mcp connect failed, web search instead for 2 minutes` | WARNING | `server`, `error` | connecting failed; the server is skipped for 2 minutes |
| `mcp tools/list failed, server dropped` | WARNING | `server`, `error` | a connected server stopped answering; it reconnects after 2 minutes |
| `knowledge base not reachable` | WARNING | `collection`, `error` | the Open WebUI retrieval call for a knowledge base failed |

## Troubleshooting

| Symptom | Check |
|---|---|
| Pod does not start | the log names the missing or invalid variable (varlock before the start, then the service's own check) |
| Pod not ready | `curl /health/readiness`: `checks` shows whether the LLM endpoint or Open WebUI is unreachable |
| Pipe says "Deep Research ist nicht verfügbar: invalid token" | `SERVICE_TOKEN` valve differs from `RA_TOKEN` |
| Job stays `queued` | outside the window and not a quick focus run: it waits for 20:00. `GET /metrics` shows `research_in_night_window`. Check `RA_TIMEZONE` |
| Job `queued` forever inside the window | all workers busy (`RA_WORKERS`), or no replica running |
| Report not in the chat | `delivery` field of the job: `failed: HTTP 401` = wrong `OWUI_DELIVERY_SECRET`; `HTTP 404` = route missing (image without the `dr_delivery` hook or `DCC_DEEP_RESEARCH_DELIVERY_SECRET` unset) or chat/message not found; `HTTP 403` = the message is not the one the pipe queued for this job (marker gone, other user, other model prefix); `skipped` = no `OWUI_URL`/`OWUI_DELIVERY_SECRET`. The user gets the report by writing again in that chat |
| Many `503` from the MCP servers | the shared front proxy of the `*.bs.ch` hosts returns bursts of 503 (all hosts at once, minutes long). The MCP client retries each request up to 6 times over about 45 s, re-opens the session after a 404 and falls back to web search for 2 minutes after three failed connects. Nothing to do on our side except monitoring `/healthz` of the hosts (see [mcp-servers.md](mcp-servers.md)); runs complete anyway, with fewer MCP sources |
| Reports with few or no web sources | log lines `search failed` / `fetch failed` with `reason` (HTTP status or error type) and `host`; `401`/`403` (logged as errors) mean a wrong `RA_SEARCH_KEY` / `RA_FIRECRAWL_KEY`, `429` an exhausted quota, `5xx` an outage. `GET /jobs/{id}` shows the counts of one run under `stats.tool_errors` (e.g. `{"search:429": 4}`) |
| Reports without MCP sources | the MCP host is unreachable from the pod (proxy / NO_PROXY, NetworkPolicy), or `enabled: false` |
| Empty answers from the LLM | thinking tokens count against `max_completion_tokens`; the engine retries once with double the budget. Check that `RA_LLM_EXTRA_BODY_FAST` keeps thinking off for the fast role |
| Timeouts | LLM request timeout `RA_LLM_TIMEOUT_SECONDS` (1800 s) with `RA_LLM_MAX_RETRIES` (3) retries; Firecrawl read timeout 150 s; the whole run is stopped after `RA_JOB_TIMEOUT_SECONDS` |
| Own sources: plan says "nicht lesbar" for a file or knowledge base the user can read in Open WebUI | the pipe checks with the chat user's rights inside Open WebUI; check the share settings of the knowledge base (read grant for the user or group) or whether the file belongs to the user or to a knowledge base they can read |
| Own sources: "wird noch verarbeitet" / "keinen lesbaren Text" | Open WebUI (Docling) has not finished extracting the file, or extraction failed for it: check the Docling service and the file's processing status in Open WebUI |
| Own sources: no upload button, or Open WebUI rewrites the question | the model entries need `capabilities.file_upload: true` and `file_context: false` (`deploy/openwebui/models.json`, re-import after upgrading the repo) |
| Own sources: knowledge base "not reachable" in the report | `OWUI_URL` / `OWUI_API_KEY` of the service, `POST /api/v1/retrieval/query/collection` needs an admin key; log lines `knowledge query` show the ids queried |
| Database | tables `drafts` and `jobs` are created on start; the column `own_sources` of both is added automatically to older databases; other schema changes still need a manual `alter table` |

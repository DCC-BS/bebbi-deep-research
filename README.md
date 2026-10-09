# bebbi-deep-research

Deep-research agent for **Bebbi Bot** (Open WebUI of the canton of Basel-Stadt). The user picks a model such as
"Deep Research · Recht" in the chat, answers at most two clarifying questions, approves or edits a research plan, and
gets a cited German report back in the same chat. Heavy runs happen at night; quick runs of the focus engine may start
during office hours.

This repository is the self-contained production deliverable. The evaluation that led to it (benchmarks, alternatives,
tuning history) lives in the evaluation repository, see [docs/evaluation.md](docs/evaluation.md).

## Architecture

```
 User ── Open WebUI v0.11.4 (Kubernetes)
           │  model "Deep Research · <preset>"
           ▼
   ┌──────────────────┐  HTTP + bearer token (SERVICE_TOKEN = RA_TOKEN)
   │ pipe/deep_research.py │──────────────────────────────┐
   │ (OWUI function, manifold) │  clarify · plan · edit · approve · poll   │
   └──────────────────┘                                    ▼
          ▲                              ┌───────────────────────────────┐
          │ POST /api/v1/chats/{chat}/   │ service/  (FastAPI, this image)    │
          │ messages/{msg} (admin key)   │  /clarify /plans /jobs /healthz    │
          │ report delivery              │  job queue · night window ·        │
          └──────────────────────────────│  daytime rule · workers            │
                                         │  research_agent/ (engine)          │
                                         └───┬───────────┬───────────┬───────┘
                                             │           │           │
                                 Postgres ◄──┘     vLLM (OpenAI API)  Serper · Firecrawl · MCP servers
                              (drafts, jobs,       think / fast / embed   (opencaselaw, data.bs.ch,
                               events, reports)    roles                   Grosser Rat, statistik)
```

* **pipe** (`pipe/deep_research.py`): Open WebUI manifold function, one model per preset. Depends only on `httpx` and
  `pydantic`. Keeps the conversation state in a hidden marker of the assistant message.
* **service** (`service/`): FastAPI job service. Chat-turn endpoints, a job queue in Postgres (SQLite for development),
  the night window, report delivery into the chat. Stateless apart from the database, so restarts re-queue running jobs.
* **engine** (`research_agent/`): the evaluated version v5. Focus engine (tool-calling loop with code-resolved citations)
  for administrative presets, pipeline engine (plan, research rounds, section-wise writing) for broad web surveys.
* **config** (`config/presets.yaml`): presets, MCP servers, budgets. Mounted as ConfigMap in Kubernetes.

## Quickstart (local)

```bash
uv sync                                  # Python 3.12+, uv
uv run pytest && uv run ruff check .

cp .env.example .env                     # fill in LLM, search, Firecrawl endpoints and secrets
docker compose up -d --build             # service + Postgres + Open WebUI v0.11.4
curl localhost:8088/readyz               # {"status":"ok","problems":[]}
uv run python scripts/install_pipe.py --models    # installs the pipe and sets the valves (docs/integration.md)
```

Open <http://localhost:3000>, pick "Deep Research · Grosser Rat" and ask a question. Without Open WebUI:

```bash
RA_TOKEN=... uv run python scripts/test_pipe.py "Welche Vorstösse zum Stadtklima gab es? Bitte schnell." grossrat
scripts/smoke_local.sh                   # starts the service on SQLite against the local evaluation stack
```

## Layout

| Path | Content |
|---|---|
| `research_agent/` | engine (copied unchanged from the evaluation, v5; only `llm.py` got the per-role extra body) |
| `service/` | job service: settings, DB layer, job runner, FastAPI app, JSON logging |
| `pipe/deep_research.py` | Open WebUI function |
| `config/presets.yaml` | presets, MCP servers, budgets |
| `Dockerfile`, `compose.yaml` | image (multi-stage, uv, uid 10001, read-only root) and local stack |
| `deploy/helm/bebbi-deep-research/` | Helm chart |
| `deploy/openwebui/` | `function.json` and `models.json` for the seeder |
| `docs/` | integration, operations, presets, evaluation, MCP servers |
| `tests/`, `scripts/` | unit and service tests; pipe simulator, installer, smoke test |

## Documentation

* [docs/integration.md](docs/integration.md): installing the pipe, valves, admin service account, seeder, fallback
* [docs/operations.md](docs/operations.md): all environment variables, night window, scaling, logs, troubleshooting
* [docs/presets.md](docs/presets.md): presets, engines, budgets, adding presets and MCP servers
* [docs/evaluation.md](docs/evaluation.md): results and decisions of the evaluation
* [docs/mcp-servers.md](docs/mcp-servers.md): state and needs of the MCP servers

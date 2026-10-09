# Integration into Open WebUI

Target: `ghcr.io/fd-itbs/open-webui` v0.11.4 (fork of Open WebUI v0.11.4), Kubernetes, GitOps. The integration uses
only extension points: one function (pipe), model entries, one API key. No fork patch.

```
Open WebUI ──(pipe, valves)──► bebbi-deep-research service ──► Postgres, vLLM, Serper, Firecrawl, MCP
     ▲                                     │
     └──── POST /api/v1/chats/{chat}/messages/{message}  (admin API key, report delivery)
```

## 1. Deploy the service

Helm chart `deploy/helm/bebbi-deep-research` (see [operations.md](operations.md) for every setting). Create the Secret
`bebbi-deep-research` first (SealedSecret / External Secrets), keys:

| Key | Content |
|---|---|
| `RA_TOKEN` | random string, shared with the pipe valve `SERVICE_TOKEN` |
| `RA_DATABASE_URL` | `postgresql://user:password@host:5432/db` (tables are created on start, `create table if not exists`) |
| `RA_SEARCH_KEY` | Serper API key |
| `RA_FIRECRAWL_KEY`, `RA_LLM_API_KEY` | only if the endpoints need keys |
| `OWUI_API_KEY` | API key of the Open WebUI service account (step 3) |

Check: `kubectl port-forward svc/bebbi-deep-research 8088` then `curl localhost:8088/readyz` returns `{"status":"ok"}`.
`/readyz` lists missing configuration under `problems`.

## 2. Install the pipe and set the valves

Open WebUI imports a function as Python source. The source is `pipe/deep_research.py` (needs only `httpx` and
`pydantic`, both in the Open WebUI image). The function id is `bebbi_deep_research`; the models appear as
`bebbi_deep_research.<preset>` (the pipe takes the preset from the part after the last dot).

**Seeder (GitOps).** `deploy/openwebui/function.json` is one entry in the format of `POST /api/v1/functions/sync`
(id, name, type `pipe`, content, valves, `is_active`, `is_global`). Let the seeder post it with an admin key, with
`SERVICE_TOKEN` filled in from the seeder's secret store (the file contains an empty token on purpose).
Regenerate after changing the pipe or presets: `uv run python scripts/build_openwebui_assets.py`
(`--check` fails if the files are stale; a unit test does the same).

**By hand / local.** `scripts/install_pipe.py` calls `functions/create` (or `update` if it exists), activates the
function globally and sets the valves:

```bash
OWUI_URL=http://localhost:3000 OWUI_API_KEY=sk-... RA_TOKEN=... uv run python scripts/install_pipe.py --models
```

Or in the UI: Admin Panel, Functions, "+", paste `pipe/deep_research.py`, save, enable, set "global". The gear icon opens the valves.

### Valves

| Valve | Default | Meaning |
|---|---|---|
| `SERVICE_URL` | `http://bebbi-deep-research:8088` | base URL of the service (cluster-internal service name, include the namespace if needed) |
| `SERVICE_TOKEN` | empty | must equal `RA_TOKEN` |
| `DEFAULT_DEPTH` | `standard` | `quick`, `standard`, `deep`; the user can say "schnell" or "gründlich" in the question |
| `NIGHT_FOR_DEPTH` | `deep` | depths that always go to the night window |
| `POLL_SECONDS` | `10` | polling interval while a "jetzt" job is streamed |
| `MAX_WAIT_MINUTES` | `25` | how long the pipe streams a running job before handing over to the delivery |
| `PRESETS` | `rechtsrecherche:Recht,grossrat:Grosser Rat,statistik:Statistik,verwaltung_web:Verwaltung,web_survey:Web` | `preset:label` pairs, one model per entry; preset names must exist in `presets.yaml` |

## 3. Admin service account for report delivery

The service writes the finished report into the assistant message with
`POST /api/v1/chats/{chat_id}/messages/{message_id}`. In v0.11.4 this endpoint accepts the chat owner or an admin
(decision: admin service key).

1. Create a user "deep-research-service" with role admin (Entra group sync can map it, or create it locally).
2. Enable API keys (`ENABLE_API_KEYS=true`); if `ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS` is on, allow `/api/v1/chats`.
3. Settings, Account, API keys: create a key, store it as `OWUI_API_KEY` in the service Secret.
4. Set `OWUI_URL` (chart value `env.OWUI_URL`) to the in-cluster Open WebUI service.

The key can read and write every chat. Keep it in the Secret only, and restrict ingress to the service with the
NetworkPolicy (`networkPolicy.enabled`). Delivery is retried 3 times; the result is stored per job (`delivery`
in `GET /jobs/{id}`: `ok`, `skipped` when the job has no chat, or `failed: ...`).

## 4. Models (optional entries)

The manifold creates the models automatically. `deploy/openwebui/models.json` adds names, descriptions and
suggestion prompts per preset ("Deep Research · Recht", ...) in the format of `POST /api/v1/models/import`; set
`access_grants` there. In v0.11.4 a model without grants is visible to admins only (the old `access_control: null` no longer means public); the file grants read to everyone (`principal_type: user, principal_id: "*"`). Replace it with `principal_type: group` entries for Entra groups (G1 is optional).

## 5. Chat flow and the fallback

1. First message: the pipe asks up to two clarifying questions (answer, or "weiter").
2. Plan shown: "ok" starts now, "heute Nacht" queues for the night window, free text edits the plan (max 3 times).
3. A "jetzt" job streams status events (`status` emitter) for up to `MAX_WAIT_MINUTES`; otherwise the message says the
   job is queued/running. When the job finishes, the service replaces the assistant message content with the report.
4. **Fallback:** the assistant message carries a hidden state marker (`<!-- research-agent:{...} -->`). If the user writes
   again in that chat before the delivery, the pipe asks the service for the job and returns the report (or status) in the
   new reply. If the delivery failed, the report is retrievable this way and via `GET /jobs/{id}`.

After a delivery the marker is gone (the report replaces the message), so the next message starts a new research.

## 6. Network

* Pipe to service: cluster-internal HTTP (valve `SERVICE_URL`).
* Service to Open WebUI: `OWUI_URL`, cluster-internal, must be in `NO_PROXY`.
* Service to vLLM, Firecrawl, Postgres: cluster-internal, in `NO_PROXY`. Serper and the MCP servers go through the
  proxy unless they are reachable directly. See [operations.md](operations.md#proxy).

## 7. Local end-to-end test (compose)

```bash
cp .env.example .env     # set RA_*, POSTGRES_PASSWORD, WEBUI_SECRET_KEY, WEBUI_ADMIN_EMAIL/PASSWORD
docker compose up -d --build
OWUI_ADMIN_EMAIL=... OWUI_ADMIN_PASSWORD=... RA_TOKEN=... uv run python scripts/install_pipe.py --create-api-key --models
# put the printed OWUI_API_KEY into .env, then: docker compose up -d service
```

Set `RA_NIGHT_WINDOW=00:00-23:59` in `.env` to let every job run at once while testing. With the evaluation stack on the
same host add `-f compose.eval-net.yaml` (see the comment in that file).

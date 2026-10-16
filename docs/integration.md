# Integration into Open WebUI

Target: `ghcr.io/fd-itbs/open-webui` v0.12.3 (fork of Open WebUI v0.12.3; verified on v0.11.4 before), Kubernetes, GitOps. The integration uses
only extension points: two functions (the pipe and the Word download action), model entries, one API key, and one delivery route that the image adds (section 3).

```mermaid
flowchart LR
    owui["Open WebUI"] -- "pipe, valves" --> svc["bebbi-deep-research service"]
    svc --> deps["Postgres · vLLM · Serper · Firecrawl · MCP"]
    svc -- "POST /api/v1/dcc/deep-research/deliver<br/>shared secret: report and sources delivery" --> owui
```

## 1. Deploy the service

Helm chart `deploy/helm/bebbi-deep-research` (see [operations.md](operations.md) for every setting). Create the Secret
`bebbi-deep-research` first (SealedSecret / External Secrets), keys:

| Key | Content |
|---|---|
| `RA_TOKEN` | random string, shared with the pipe valve `SERVICE_TOKEN` |
| `HMAC_SECRET` | random string (`openssl rand 32 \| base64`), pseudonymises user ids in usage events |
| `RA_DATABASE_URL` | `postgresql://user:password@host:5432/db` (tables are created on start, `create table if not exists`) |
| `RA_SEARCH_KEY` | Serper API key |
| `RA_FIRECRAWL_KEY`, `RA_LLM_API_KEY` | only if the endpoints need keys |
| `OWUI_API_KEY` | API key of the Open WebUI service account (step 3) |

Check: `kubectl port-forward svc/bebbi-deep-research 8088` then `curl localhost:8088/health/readiness` returns
`{"status":"ready",...}`; `checks` names an unreachable LLM endpoint or Open WebUI. Missing or invalid configuration stops
the pod at start with the variable named in the log (varlock and the service's own check).

## 2. Install the pipe and set the valves

Open WebUI imports a function as Python source. The source is `pipe/deep_research.py` (needs only `httpx` and
`pydantic`, both in the Open WebUI image). The function id is `bebbi_deep_research`; the models appear as
`bebbi_deep_research.<preset>` (the pipe takes the preset from the part after the last dot).

**Seeder (GitOps).** `deploy/openwebui/function.json` holds two entries in the format of `POST /api/v1/functions/sync`
(id, name, type `pipe` and `action`, content, valves, `is_active`, `is_global`): `bebbi_deep_research` and
`bebbi_deep_research_download` (section 5a). Both have the valves `SERVICE_URL` and `SERVICE_TOKEN`. Let the seeder post it with an admin key, with
`SERVICE_TOKEN` filled in from the seeder's secret store (the file contains an empty token on purpose).
Regenerate after changing the pipe or presets: `uv run python scripts/build_openwebui_assets.py`
(`--check` fails if the files are stale; a unit test does the same).

**By hand / local.** `scripts/install_pipe.py` calls `functions/create` (or `update` if it exists) for both functions,
activates them globally and sets the valves:

```bash
OWUI_URL=http://localhost:3000 OWUI_API_KEY=sk-... RA_TOKEN=... uv run python scripts/install_pipe.py --models \
  --service-url http://service:8088
```

Pass `--service-url` explicitly: the default `http://service:8088` is the compose service name and is right only for the local compose
stack. In a cluster use the Kubernetes service URL (the default of the `SERVICE_URL` valve, `http://bebbi-deep-research:8088`, add the
namespace if Open WebUI runs in another one: `http://bebbi-deep-research.<ns>.svc:8088`). The URL is what Open WebUI (the pipe) reaches, not what your shell reaches.

Or in the UI: Admin Panel, Functions, "+", paste `pipe/deep_research.py` (and `pipe/deep_research_download.py`), save, enable, set "global". The gear icon opens the valves.

### Valves

| Valve | Default | Meaning |
|---|---|---|
| `SERVICE_URL` | `http://bebbi-deep-research:8088` | base URL of the service (cluster-internal service name, include the namespace if needed) |
| `SERVICE_TOKEN` | empty | must equal `RA_TOKEN` |
| `DEFAULT_DEPTH` | `standard` | `quick`, `standard`, `deep`; the user can say "schnell" or "gründlich" in the question |
| `DECISION_MINUTES` | `30` | how long the approve/change/discard card waits; no answer counts as no |
| `POLL_SECONDS` | `10` | wait between checks whether the service has delivered a finished report (max 2 s per check) |
| `PRESETS` | `rechtsrecherche:Recht,grossrat:Grosser Rat,statistik:Statistik,verwaltung_web:Verwaltung,web_survey:Web,dokumente:Eigene Dokumente` | `preset:label` pairs, one model per entry; preset names must exist in `presets.yaml` |
| `OWN_SOURCES` | `true` | use the chat's uploaded files and knowledge bases as sources (section 5b); `false` ignores them |
| `PUBLIC_URL` | empty | public base URL of Open WebUI for the links to uploaded files; empty = Open WebUI's `WEBUI_URL` setting, else the request URL. Set `WEBUI_URL` in production |
| `OWN_MAX_FILES`, `OWN_MAX_FILE_CHARS` | `10`, `2000000` | files per research and extracted characters per file the pipe sends (the service applies its own limits too, [operations.md](operations.md)) |

## 3. Report delivery (night run) and the admin service account

The night run finishes without any user session, so the service must write the report into the user's chat by itself. Since Open
WebUI v0.12.0 that is no longer possible with an admin API key: with `ENABLE_ADMIN_CHAT_ACCESS=False` (our setting)
`POST /api/v1/chats/{id}/messages/{message_id}` and `.../event` answer 401 for the chat of another user (`routers/chats.py`),
and so does `GET /api/v1/chats/{id}`. The service therefore uses one narrow route that the **Open WebUI image** adds
(post-import hook `dr_delivery` in the kdkp-open-web-ui repository):

```
POST {OWUI_URL}/api/v1/dcc/deep-research/deliver
Authorization: Bearer <OWUI_DELIVERY_SECRET>
{"chat_id", "message_id", "job_id", "user_id", "content", "sources": [<data of one `source` event>, ...]}
```

What the route does and refuses (the checks are on the Open WebUI side, not trusted from the service):

* No route at all unless `DCC_DEEP_RESEARCH_DELIVERY_SECRET` is set in Open WebUI; the secret is compared in constant time.
  It is its own secret (not an API key), so the service's admin key has no write access to chats.
* The chat must belong to `user_id`; the target must be an assistant message of a `bebbi_deep_research.*` model (prefix
  configurable with `DCC_DEEP_RESEARCH_MODEL_PREFIX`) whose text still carries the pipe's state marker `{"state": "job",
  "job": <job_id>}`. The pipe writes that marker when it queues the job, so only a message the pipe queued for exactly this
  job can be written, once: after the delivery the marker is gone and a different text is refused (403); the same text
  again is accepted without writing (a retry after a lost answer).
* It stores one `source` event per source (not if the message has sources already: pipe was faster, or a retry after a
  failed text write), then the text, and emits `chat:message`, through Open WebUI's own `Chats` model and event emitter
  (so an open chat updates live). It cannot send other event types or change other fields of a message.

Setup:

1. Open WebUI (image): set `DCC_DEEP_RESEARCH_DELIVERY_SECRET` (random string).
2. Service Secret: the same value as `OWUI_DELIVERY_SECRET`. `OWUI_URL` points to the in-cluster Open WebUI service; the route is
   `OWUI_URL` + `/api/v1/dcc/deep-research/deliver` unless `OWUI_DELIVERY_URL` says otherwise.
3. Without the secret in the service the job ends with `delivery: skipped`; without the route in Open WebUI with
   `failed: HTTP 404`. Either way the user gets the report by writing again in that chat.

The **admin API key** (`OWUI_API_KEY`) is still used, read-only, for own sources: `POST /api/v1/retrieval/query/collection`
(knowledge bases of the user, only ids the pipe approved for the job) and `GET /api/v1/files/{id}` (date of a document).
Both still accept an admin key at v0.12.3 (`BYPASS_ADMIN_ACCESS_CONTROL` default true, section 7). Create a user
"deep-research-service" with role admin, enable API keys (`ENABLE_API_KEYS=true`; with `ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS` allow
`/api/v1/retrieval` and `/api/v1/files`), create the key in Settings, Account, API keys, store it as `OWUI_API_KEY`. The key no longer
needs `/api/v1/chats`.

The pipe sends the chat user's id (`user_id`) on `POST /plans`, `POST /plans/{id}/edit`, `POST /jobs` and as `?user_id=` on `GET /jobs/{id}`
(and the other job reads). When both the stored draft/job and the request carry a user id and they differ, the service answers 404. Calls
without `user_id` (n8n, scripts) are not checked.

`RA_TOKEN` is full trust: whoever holds it can read every job and report and submit for any user id; the admin key paths run through the
service. There are no separate tokens per caller yet; keep the token in the Secret and restrict ingress with the NetworkPolicy.

Delivery is retried 3 times (not for 400, 401, 403, 404, which do not change by waiting); the result is stored per job
(`delivery` in `GET /jobs/{id}`: `ok`, `skipped` when the job has no chat or no secret, or `failed: ...`). The admin key can read every
knowledge base and file; keep it in the Secret only, and restrict ingress to the service with the NetworkPolicy
(`networkPolicy.enabled`).

## 4. Models (optional entries)

The manifold creates the models automatically. `deploy/openwebui/models.json` adds names, descriptions and
suggestion prompts per preset ("Deep Research · Recht", ...) in the format of `POST /api/v1/models/import`; set
`access_grants` there. The entries set `capabilities.file_upload: true` (the chat offers file upload and knowledge bases, section 5b) and
`file_context: false` (Open WebUI must not run its own retrieval on the message that the pipe interprets). In v0.11.4 a model without grants is visible to admins only (the old `access_control: null` no longer means public); the file grants read to everyone (`principal_type: user, principal_id: "*"`). Replace it with `principal_type: group` entries for Entra groups (G1 is optional).

## 5. Chat flow and the fallback

1. First message: the pipe asks up to two clarifying questions (answer, or "weiter").
2. Plan shown, then Open WebUI's ask-user card above the input field (`__event_call__` with `request:user_input`, the
   dialog Open WebUI v0.11 uses for its `ask_user` tool), in the same turn: **Freigeben** queues the job for the night
   window, **Ändern** opens an input dialog for the change, the plan is edited and the card shown again (max 3 changes,
   then only Freigeben/Verwerfen), **Verwerfen** ends without a job. The card has no free-text option, so nothing is
   interpreted. No answer (card cancelled, closed after `DECISION_MINUTES`, browser gone) counts as no: no job runs and
   the plan is not kept; the user starts over by asking again. Clients without a browser session (n8n, plain API calls
   through Open WebUI) are out of scope: they get no card and therefore never start a job.
3. For now every job runs in the night window; starting right away is not offered (the service still accepts
   `when=now` for its own API). The message says the job is queued. When the job finishes, the service replaces the
   assistant message content with the report.
4. **Fallback:** the queued message carries a hidden state marker (`[//]: # (research-agent:...)`). If the user writes
   again in that chat before the delivery, the pipe asks the service for the job and returns the report (or status) in the
   new reply. If the delivery failed, the report is retrievable this way and via `GET /jobs/{id}`.

After a delivery the marker is gone (the report replaces the message), so the next message starts a new research. This
is acceptable: the report is the end of a research, and a follow-up question becomes a new task (the pipe asks
clarifying questions or shows a plan). A follow-up that asks about the report content is not answered from the report;
users should use a normal model for that. Making the delivered message carry a "finished" marker would only make the
pipe answer the next message with the same report again, so we left it out. The same holds for a report that the pipe
returns itself (later message, fallback above): it carries no marker either.

### How the pipe and the delivery fit Open WebUI v0.11.4 and v0.12.3 (found in the end-to-end test)

* **State marker**: a markdown link reference definition `[//]: # (research-agent:<base64>)`. It is valid markdown and
  never rendered. The first version used an HTML comment, which Open WebUI shows as text.
* **`output` wins over `content`**: v0.11 stores the pipe answer as `output` items and the UI renders `output` in
  preference to `content`. the delivery route (like the `POST /chats/{id}/messages/{mid}` it replaced, and the `chat:message` event) changes only `content`, so
  a delivery into a normal pipe answer is stored but never shown. The pipe therefore sends the text of every message
  that the service will replace later ("queued for the night", "runs in the background") as a `replace` event and
  returns an empty string; the stored message then has `content` and an empty `output`, and the delivery is
  displayed, live in an open chat and after reload.
* **Background tasks** (title, tags, follow-ups) are sent by Open WebUI to the chat model, i.e. to the pipe. The pipe
  answers them locally (`__task__`) and never calls the service for them; the chat title comes from the question/plan.
* **Citations**: see section 5a. The zero-width-space workaround of the first version is gone.
* **Models** need `access_grants` (read for everyone, or groups); `access_control: null` of older versions means
  "admin only" in v0.11.4. `deploy/openwebui/models.json` has the grant. The models must not set
  `capabilities.citations: false`: Open WebUI then removes every `[n]` from the text (models.json sets it to `true`).
* **Model list cache**: after installing a function or model, open Open WebUI once (`GET /api/models`) before sending
  completions with the new model id; the first completion otherwise answers "Model not found".

## 5a. Citations and Word download (how Open WebUI v0.11.4 and v0.12.3 do it)

**What Open WebUI expects** (read in the image: backend `/app/backend/open_webui`, frontend from the source maps of
`/app/build/_app/immutable`):

* A message has a list `sources`. The pipe adds to it with `__event_emitter__({"type": "source", "data": {...}})`
  (`socket/main.py`, `get_event_emitter`: for `source`/`citation` it appends `data` to the message's `sources`, stored
  in the chat). Item format: `{"source": {"name", "url"}, "document": [text], "metadata": [{"source": id, "name": title}]}`.
* `[n]` in the text becomes a badge (`utils/marked/citation-extension.ts`, `Markdown/SourceToken.svelte`). The badge
  label is the n-th entry of the list of `metadata.name` (`ContentRenderer.svelte`, duplicates removed!), the list under the
  message and the click target are the n-th entry grouped by `metadata.source` (`Messages/Citations.svelte`; a
  `source` starting with `http` is shown as URL). So `metadata.source` and `metadata.name` must be unique per source or
  the numbering shifts. Without `sources` the badge is dropped; a `[2024]` in the text with fewer sources is an empty badge.
* Sources can be written after the fact: `POST /api/v1/chats/{chat}/messages/{message}/event` with `{"type": "source",
  "data": {...}}` (owner or admin; `routers/chats.py`) stores it like the emitter and pushes it to an open chat. The plain
  `POST .../messages/{message}` accepts only `content`.
* Message files: list `files` of the message (event `files`), item `{"type": "file", "id", "url": id, "name", "size"}`
  (`ResponseMessage.svelte`, `common/FileItem.svelte`); a click opens the file preview, which has a Word preview and a
  download. The file must be readable by the user: `/api/v1/files/{id}/content` allows the owner, admins, or users who
  reach the file through a knowledge base, channel, shared chat or shared model (`utils/access_control/files.py`).
  **A file uploaded with the service's admin key belongs to the admin, the chat user gets 404.**

**What we do**

* The service returns the structured sources with the job (`GET /jobs/{id}`): `sources` (`n`, `title`, `url`, `stand`,
  `fetched`, `kind` = `web` or `mcp:<server>`, `excerpt`, `excerpt_kind` = `quote` for the verbatim card quote of the
  pipeline engine, `claim` for the sentence of the report that cites the source in the focus engine), `ui_report`,
  `owui_events` (ready-made source events) and `docx`/`docx_name`. They are derived from the report text and the evidence
  ledger (`services/report.py`); the engines are unchanged and the stored report text is unchanged. Old clients ignore the new fields.
* `ui_report` is the report for the chat: the source list becomes a numbered list (a line starting with `[1]` would turn into a
  badge), stray `[n]` without a source are escaped. The list stays in the text because Open WebUI's own list shows only the
  URL (collapsed under "n Sources") and neither Stand nor the retrieval date; the preview of a source (click on a badge) shows
  title, Stand, retrieval date, kind and the excerpt.
* **Pipe path** (report returned in the chat): the pipe emits the `source` events in the order of the numbers, returns
  `ui_report`, and stores the Word file as a file of the user *inside Open WebUI* (`store_user_file`, the upload handler
  of Open WebUI called in-process with the chat user) and emits a `files` event.
* **Delivery path** (night run): the service posts text and sources to the delivery route (section 3), which stores the `source` events, then the text; after a reload the
  message carries `sources`, badges and list work. The Word file cannot be attached by the service (ownership, see above).
  The action function `bebbi_deep_research_download` adds a button ("Word-Datei") under every Deep Research answer; it runs as
  the user inside Open WebUI, looks up the job by chat and message id (`GET /jobs/lookup`, checks the user), fetches the
  `.docx` and attaches it. A second click does not attach it again.
* The pipe (fallback reply) and the service may both write a report. The service skips the source events if the message
  already has sources; the pipe waits for the delivery and skips its source events if the delivery succeeded.
* `GET /jobs/{id}/report.docx` (bearer token) returns the Word file for n8n and other apps (409 while the job runs).
  python-docx renders title, header block (question, date, preset, depth), headings, lists, Markdown tables as Word tables,
  citations as links to the source list, the source list with hyperlinks, Stand and retrieval date, and a footer.
  Rendering is on demand from the stored report; old jobs work too.


## 5b. Own sources: uploaded files and knowledge bases (requirement A4)

The user can attach files in the chat (paperclip) and knowledge bases (`#` or the attach menu) to a Deep Research model, or the
admin attaches a knowledge base to the model itself (Workspace, Models, Knowledge). The report then cites them like any other source.
Verified against Open WebUI v0.11.4, see section 7, scenarios 9 to 13.

**What Open WebUI v0.11.4 provides** (read in the image, `/app/backend/open_webui`):

* The browser sends the attachments as `files` of the completion request: `{"type": "file", "id", "name", "file": {record}, ...}`
  for an upload, `{"type": "collection", "id" (= knowledge id), "name"}` for a knowledge base (other types: `url`, `note`, `chat`,
  `folder`, `image`). `utils/middleware.py` pops `files` from the body, so **a pipe does not find them in `body["files"]`**: they are in
  `metadata["files"]`, which functions receive as `__files__` (`functions.py`, `extra_params`) and inside `__metadata__`.
* The files of the chat accumulate: the browser sends the same items with every later turn.
* An upload is processed in the background (`POST /api/v1/files/?process=true`, `data.status` `pending` then `completed`; the browser
  polls `/files/{id}/process/status`). The extracted text is `file.data["content"]` in the file record (`models/files.py`).
* Model-attached knowledge is `model.meta.knowledge` (`Models.get_model_by_id`). Open WebUI adds it to `files` only for
  `function_calling: legacy`; in the default native mode a pipe has to read it itself.
* **Message rewriting**: unless the model sets `capabilities.file_context: false`, `chat_completion_files_handler` runs its own retrieval
  (it even asks the task model for search queries) and wraps the user's message in the RAG template; native mode additionally prepends an
  `<attached_files>` block. Both would change what the pipe takes for the user's words ("ok" becomes a template). `models.json` therefore sets
  `file_context: false` and `file_upload: true` (it was `false`, which hid the upload button); the pipe in addition reads the original text
  from `__metadata__["user_message"]` and strips an `<attached_files>` block.
* Access rules (what Open WebUI applies itself): a file is readable for its owner, admins, and users who reach it through a knowledge base
  grant, a channel, a shared chat or a shared model (`utils/access_control/files.py: has_access_to_file`). A knowledge base is readable for
  its owner, admins and holders of a read grant (`retrieval/utils.py: filter_accessible_collections`, the rule behind
  `/api/v1/retrieval/query/collection` too).
* `POST /api/v1/retrieval/query/collection` `{collection_names, query, k}` answers `{documents: [[...]], metadatas: [[{file_id, name, source,
  ...}]], distances: [[...]]}` (distances are similarities, higher is better). With an admin key it can read any collection.
* `GET /api/v1/files/{id}/content` serves a file to anyone who may read it (cookie or token); `GET /api/v1/files/{id}` returns the record
  (`created_at`). Users need the permission `workspace.knowledge` to create knowledge bases (default: off).

**What we do**

1. **Pipe, as the chat user, inside Open WebUI** (`collect_own_sources` in `pipe/deep_research.py`). At every turn that creates a plan or a
   job (clarify, plan, approval; the approval collects again, so the latest attachments count) it gathers the file items and collection items
   of `__files__` plus the model's knowledge, and checks each id with Open WebUI's own functions in-process: files with `Files.get_file_by_id`
   + `has_access_to_file`, knowledge bases with `filter_accessible_collections` (plus: the knowledge base must exist). What is not readable is
   dropped and named in a remark ("Wissensbasis ... ist für Sie nicht lesbar"), never with content. If the check itself fails, nothing is used
   (fail closed). The extracted text always comes from the file record, never from the browser's payload. The plan step sends only the first
   1,500 characters of each file; the approval sends the whole text (cut at 2,000,000 characters). Links: `<public base>/api/v1/files/<id>/content`,
   public base = valve `PUBLIC_URL`, else the `webui.url` setting (`WEBUI_URL`), else the request URL.
2. **Service** (`services/own_sources.py`). Applies the limits (`RA_OWN_MAX_FILES` 10, `RA_OWN_MAX_FILE_CHARS` 2,000,000, `RA_OWN_MAX_COLLECTIONS` 5) and
   reports what it cut, in German, back to the pipe (shown in the plan and in the "queued" message). The plan step stores previews with the
   draft; the job stores the full texts and the knowledge base ids (`jobs.own_sources`), so a night run does not depend on the user's session.
   Old databases get the column added on start. `GET /jobs/{id}` returns names and sizes (`own_sources`), never texts.
3. **Knowledge bases at run time**: the engine tool `knowledge_search(query)` asks `OwuiRetriever`, which POSTs to
   `/api/v1/retrieval/query/collection` with the service's admin key for **one stored id at a time**. The tool has no collection argument, the
   retriever refuses ids that were not stored with the job, and every query is logged (`knowledge query`, collection id). The file date (Stand)
   comes from `GET /api/v1/files/{file_id}` (`created_at`).
4. **Engine** (`engine/own.py`, additive). Source kinds `upload` (title = file name, Stand = upload date) and `knowledge:<name of the
   knowledge base>` (title = document name). Focus engine: uploads up to 6,000 characters (18,000 together) are put into the first message with
   their source IDs (no tool call); larger ones are read with `read_upload(file, query)` (the passages most similar to the query, embeddings,
   computed once per file and run); knowledge bases with `knowledge_search`. Pipeline engine (web_survey): in every sub-question the best three
   uploads (by best passage) and the knowledge hits go through the normal extraction into evidence cards (kind `upload` / `knowledge:...`),
   before the web and MCP routes; the quote check applies as for web pages. Citation checking (`verify`) is untouched and applies to the
   texts of own sources like to any other source.
5. **Presets**: `own_sources: prefer | only | off` (default `prefer`: own documents first, web and data tools for the rest). `only` removes the
   web and MCP tools and the web discovery of the planner (preset `dokumente`, "Deep Research · Eigene Dokumente"); without any readable
   own source the service answers HTTP 422 with a message for the user. `off` ignores attachments (and says so in the plan).
6. **Labels and links**: the source list says "Hochgeladene Datei" or "Wissensbasis <name>" (previews, Word file); never "MCP". The link is the
   file route above (the user can open it exactly when Open WebUI lets them read the file); without a base URL the source is listed with
   "keine öffentliche URL".

**Limits and behaviour to know**

* The pipe uses the text Open WebUI extracted from the file (`file.data.content`). In the Bebbi Bot deployment every
  upload goes through Docling (with OCR), so scans and Office files arrive as text. Only if extraction fails or is still
  running does the pipe say so ("enthält keinen lesbaren Text" / "wird noch verarbeitet") and leave the file out.
* Attachments of the type `url`, `note`, `chat`, `folder` are reported and not used. Images are ignored.
* A knowledge base is approved when the plan is approved. If the user loses access before a night run starts, the run still reads it with
  the service key (the content was approved by the system at approval time). Delete or re-share in Open WebUI and cancel the job if that matters.
* The service trusts the pipe (shared bearer token). Anyone who can call the service with that token can name any knowledge base id; keep
  the NetworkPolicy and the token as described in section 3.
* Retrieval quality for knowledge bases is Open WebUI's (its embedding model, hybrid search and `k` settings, here `k` = 6 chunks per query).

## 6. Network

* Pipe to service: cluster-internal HTTP (valve `SERVICE_URL`).
* Service to Open WebUI: `OWUI_URL`, cluster-internal, must be in `NO_PROXY`.
* Service to vLLM, Firecrawl, Postgres: cluster-internal, in `NO_PROXY`. Serper and the MCP servers go through the
  proxy unless they are reachable directly. See [operations.md](operations.md#proxy).

## 7. End-to-end test with a real Open WebUI (verified 2026-10-09)

Setup (2026-10-09, then `compose.yaml` + `compose.eval-net.yaml`; today `docker-compose.dev.yml` +
`docker/eval-net.compose.yml`): Open WebUI v0.11.4, service, Postgres; LLM
gateway, search and Firecrawl of the evaluation stack), admin from `WEBUI_ADMIN_EMAIL/PASSWORD`, API key and the
pipe/models via `scripts/install_pipe.py --create-api-key --models`, a normal user "tester" for the chats. Keys only in
the git-ignored `.env`. `scripts/e2e_owui.py` drives Open WebUI like the browser (`POST /api/v1/chats/new`, then
`POST /api/chat/completions` with `chat_id`, `id` = assistant message, `parent_id`, `user_message`; v0.11 runs this
as a background task and stores both messages itself, the script polls the stored chat). One run through the real UI
was done in the browser pane (typing into the chat). Screenshots: [plan](img/plan-turn.jpg),
[report](img/report-top.jpg), [sources](img/report-sources.jpg).

| # | Scenario | Result |
|---|---|---|
| 1 | Daytime quick run (preset grossrat, 16:2x local, outside the window): question, clarifying question, answer, plan, plan edit, "ok jetzt" | passed. Job `928532b4e965`, 182 s, 23k prompt tokens, `when=now`. Report with 14 numbered sources with URLs, state marker invisible (see screenshots). Delivery was `skipped` there (the key was not yet in the service); the pipe returned the report itself |
| 2 | Night run with delivery by admin key | passed. "heute Nacht" queued the job (`research_in_night_window 0`, stayed `queued`); restart with `RA_NIGHT_WINDOW=00:00-23:59` ran it: job `472eb33c7e5a`, 157 s, `delivery ok`; the stored assistant message of the tester's chat is the 6186-character report. Live update in an open chat verified with the delivery endpoint on a message of an open page (content replaced without reload), after the `output` fix above. Job `8382516f4090` (247 s, `delivery ok`) delivered into the chat as well |
| 3 | Fallback (service without `OWUI_API_KEY`) | passed. Job `02ae007b9453` (282 s) finished, `delivery skipped`; the chat still showed the queued text. Typing "Ist der Bericht schon fertig?" in the real UI returned the report in the new reply, sources visible, no marker |
| 4 | Error path (wrong model name `RA_LLM_MODEL_*`) | passed for the chat-turn endpoints: the user sees "Deep Research ist nicht verfügbar: Die Anfrage konnte nicht verarbeitet werden. Das Sprachmodell lehnt die Anfrage ab (Modellname oder Zugang falsch konfiguriert) ..." (was a bare "Internal Server Error"). A failing *job* was not run live (job budget); unit test `test_llm_errors_are_reported_in_german` covers the job error and its delivery text |
| 5 | Service restart while a job runs | passed. `docker compose restart service` 41 s into job `8382516f4090`: log "job re-queued on shutdown", restarted 2 s after the start, finished (247 s total). The crash path (no heartbeat, `RA_STALE_SECONDS`) is covered by the unit tests only |

Citations and Word download (verified 2026-10-09 against the same stack, API level, see limitations for the UI):

| # | Scenario | Result |
|---|---|---|
| 6 | Daytime run, pipe path (chat `b0cee6d1…`, job `6f3219d8b44e`, grossrat, quick, 107 s) | passed with one finding: the stored message had 26 sources, the 13 sources twice, because the pipe and the service delivery (key now set) both wrote them. Fixed (see section 5a); re-run below. The file was attached (41 kB), `GET /api/v1/files/{id}/content` as the chat user returned 200 with `attachment` |
| 7 | Night run, delivery by admin key (chat `ed9fc2f9…`, job `c9cb672c08ce`, rechtsrecherche, 101 s) | passed. Queued with "heute Nacht", window opened by restart, `delivery ok`. Re-read through the chat API like after a reload: content 5046 characters, `sources` 5 in the order of the list, unique ids, no file. The button action (`POST /api/chat/actions/bebbi_deep_research_download` as the chat user, as the UI does) attached one 40 kB file; the second call attached nothing; download as the user 200. The file opens with python-docx (3 pages, 2 tables, hyperlinks) and renders in LibreOffice ([page 1](img/word-report-p1.png), [sources](img/word-report-sources.png)) |
| 8 | Daytime run again after the fix (chat `c987918b…`, grossrat, quick) | passed: 19 sources, 19 unique, one Word file, download as the user 200 |

Own sources (verified 2026-10-09 on the same stack, API level exactly as the browser sends the chat: file upload with background
processing, `files` in the completion request, test user "tester" without admin rights; section 5b). Quick runs, 4 research jobs in total:

| # | Scenario | Result |
|---|---|---|
| 9 | File in the chat, preset Recht (job `24888517d1ec`, 78 s, 13k prompt tokens): a self-made 1-page PDF (734 characters, no personal data) with a deadline and fee for stalls on public ground, question needs the file plus the public law | passed. The clarifying step no longer asks for the file; the plan lists "Eigene Quellen: Datei merkblatt.pdf (734 Zeichen)"; the file (small: read directly) is source [1] in the report with the right figures (28/42 days, CHF 135/67.50), the law sources come from opencaselaw. Stored message: 4 `sources` events (kind `upload` first), badge/list as for any source, link `http://localhost:3000/api/v1/files/<id>/content` returns 200 `application/pdf` for the tester; the Word file lists "Art der Quelle: Hochgeladene Datei" |
| 10 | Knowledge base of the tester (3 text files) attached to the chat, preset Verwaltung, "heute Nacht" (job `79b3b69ee797`) | passed. Queued with the sources named in the message (window 03:00-04:00, job stayed `queued`); restart with `RA_NIGHT_WINDOW=00:00-23:59` ran it without any user session: 16 s, two `knowledge_search` calls, no web call, `delivery ok`; the stored message cites the two relevant documents (kind `knowledge:Beschaffung und Personal (Test)`) with file links and Stand. Knowledge base attached to the *model* (admin, Workspace): the plan step lists it as well (checked on `bebbi_deep_research.verwaltung_web`, no job) |
| 11 | Access control: a knowledge base of the admin, not shared, attached by id by the tester next to the tester's own (job `a76b17a12fd0`) | passed. The plan says "Wissensbasis 8eb67fe4-... ist für Sie nicht lesbar und wird nicht verwendet"; the job stores one collection; the service log shows `knowledge query` only for the tester's base (2 queries); the report says that "Projekt Zeta" (only in the admin's document) was not found. The same for a foreign file id is a unit test (the real Open WebUI check ran for the knowledge base) |
| 12 | `own_sources: only` (preset Eigene Dokumente), file + knowledge base (job `3fddd112aef9`, 15 s) | passed. Tool trail: one `knowledge_search`, nothing else (no `web_search`, no MCP tool; `tools_used` and the trail of `GET /jobs/{id}/evidence`); sources `upload` + `knowledge`. Without attachments the model answers "Dieses Modell arbeitet nur mit eigenen Quellen. Bitte laden Sie eine Datei hoch ..." |
| 13 | Large file, many files, scans, pipeline engine | not run live (job budget of the test round). Covered by unit tests with fakes: limits and cut notes, `read_upload` on a long text (passage selection, embeddings once), the pipeline's evidence cards and the `only` mode of the pipeline engine. A first live run of web_survey with attachments is still due |

**Re-run on Open WebUI v0.12.3** (2026-10-16, `ENABLE_ADMIN_CHAT_ACCESS=false`, image `open-webui:v0.12.3-kdkp-local` plus the `dr_delivery` hook, compose project `owui-dr`, Gemma 4 31B through the evaluation gateway; the UI part in the browser pane as the test user):

| # | Scenario | Result |
|---|---|---|
| 14 | The old delivery with the admin key | fails as announced: `POST /chats/{id}/messages/{mid}`, `.../event` and `GET /chats/{id}` of the tester's chat answer 401 |
| 15 | Night run through the delivery route (grossrat, chat `be82c220…`, job `4d31e9d5eb6e`): plan card answered by a websocket client (`e2e_owui.py say --approve`), job queued, window opened by restart, chat page open in the browser | passed. `delivery ok`; the open page showed the report and badges **without reload**; stored message 6624 characters, 14 `sources` (14 unique), `output` empty, no marker in the text, 14 "Sources" and clickable badges after reload (popup with title, Stand, excerpt) |
| 16 | Word file (action `bebbi_deep_research_download` as the tester, twice) | passed. One file attached, the second call attached nothing; `GET /files/{id}/content` as the tester 200 (42 kB, python-docx: 51 paragraphs, 1 table) |
| 17 | Scope of the route | passed. Wrong secret 401; delivered message with other text 403; other `user_id` 403; user message or non-Deep-Research message 403; same text again 200 `already`. Nothing was written in the refused cases |
| 18 | Own sources: file `.txt` and a knowledge base of the tester, preset dokumente (job `4aa5e24ccb6b`) | passed. Both sources cited with the right figures (28 days, CHF 135; CHF 15 000), kinds `upload` and `knowledge:Test KB 5`, links 200 for the tester. The admin key still reads the foreign knowledge base (`/retrieval/query/collection`, same response shape) and `GET /files/{id}` (`created_at`) |
| 19 | Job queued, user writes again before it ran; then the job runs and the user writes again (chat `b260441c…`, job `b40ade3da0a1`) | passed. The state marker came back in the status answer (so the second message is a new message, marker intact); the delivery went into the **first** message (19 sources, no marker) and left the status message alone; the next user message returned the report with the Word file attached by the pipe. (That message carries no `source` events, because the service delivered them already: its `[n]` are plain text; unchanged from v0.11.4) |

Known limitations:

* The rendered chat (badges, source popup, file chip, action button) was **not** looked at in the browser pane in this
  round: the pane had no logged-in session and the credentials could not be passed to it. The behaviour above is verified by
  the stored chat data and the Open WebUI source (section 5a), not by screenshots of the new elements. Check once in the UI:
  badges clickable, "n Sources" under the message, file chip, the download icon under the message.
* No Playwright/Chrome was available earlier; the browser run used the desktop app's browser pane. The status events
  ("Erstelle Rechercheplan", ...) are stored with the message (`Plan erstellt` shows in the UI); live status of a
  running "jetzt" job was not observed because the UI session of that run was a script.
* After a delivery the next message starts a new research (see above).
* In the delivery path the Word file is attached when the user clicks the action button (the service has no way to create a
  file owned by the user); the pipe path attaches it automatically. If Open WebUI restarts or the action function is not
  installed, the file is still available from the service (`/jobs/{id}/report.docx`).
* The native source list shows URLs only; the Markdown list under the report keeps title, Stand and retrieval date, so the
  list is visible twice (one collapsed).
* The in-process upload imports `open_webui.routers.files` and `open_webui.models.users` (checked on v0.12.3); re-test it on upgrades (checklist in section 9).
* Own sources were exercised through the API the way the browser sends them; the attach menu and the `#` picker were not clicked in the browser pane.
* The delivery secret can write the text of any message that the pipe queued and that is still waiting (and nothing else); keep it in the Secret and restrict ingress with the NetworkPolicy. The admin key can read every knowledge base and file.
* A message that carries a status text (job still running when the user writes again) is a new assistant message; the
  report is delivered into the original one.

## 8. Local end-to-end test (compose)

```bash
mise run docker:up       # varlock loads the variables (.env.schema; secrets from Proton Pass), builds and starts
OWUI_ADMIN_EMAIL=... OWUI_ADMIN_PASSWORD=... RA_TOKEN=... \
  uv run python scripts/install_pipe.py --service-url http://service:8088 --create-api-key --models
# store the printed OWUI_API_KEY in Proton Pass (or export it), then recreate the service: mise run docker:up
# (without OWUI_API_KEY a knowledge base cannot be used)
```

Every approved research is queued for the night window: set `RA_NIGHT_WINDOW=00:00-23:59` to let jobs run at once
while testing. The plan card needs a browser session: `scripts/e2e_owui.py say CHAT "..." --approve` holds a websocket session and
clicks "Freigeben".

Delivery needs the route of the kdkp image (the upstream `ghcr.io/open-webui/open-webui` image has none; the jobs then end with
`delivery: failed: HTTP 404`, the pipe still returns the report on the next message). With the image: set the same random
`OWUI_DELIVERY_SECRET` in `.env` (the compose files pass it to the service and to Open WebUI as `DCC_DEEP_RESEARCH_DELIVERY_SECRET`) and
point `open-webui` at the image, e.g. with an override file that sets `image:` and the `HTTP(S)_PROXY` variables that the image bakes in (empty
for a local stack). `scripts/check_owui_internals.py` checks the Open WebUI internals the pipe imports. With the evaluation stack on the same host add `-f docker/eval-net.compose.yml` (see the comment in that
file).

## 9. Upgrade checklist: Open WebUI internals we depend on

Re-test these when the Open WebUI version changes (checked: v0.12.3). Unit tests fake them (`tests/unit/test_pipe_own.py:
test_real_helpers_use_open_webui_functions` fails if our wiring changes, but not if Open WebUI changes); the end-to-end check is
`scripts/e2e_owui.py` (section 7); `scripts/check_owui_internals.py` runs the helpers of the pipe against the real modules inside the container.

| Dependency | Used for | Look at |
|---|---|---|
| `ENABLE_ADMIN_CHAT_ACCESS` and the checks of `POST /api/v1/chats/{id}/messages/{mid}`, `.../event`, `GET /api/v1/chats/{id}` (401 for an admin since 0.12.0) | why the service cannot write with an admin key | section 3, `routers/chats.py` |
| `open_webui.models.chats.Chats`: `get_chat_by_id`, `get_message_by_id_and_message_id`, `upsert_message_to_chat_by_id_and_message_id`, `get_message_metadata`; `open_webui.socket.main.get_event_emitter(info, update_db)` and what it stores for `source`, `files`, `replace` | the delivery route of the image (`dr_delivery` hook), the Word action | section 3; kdkp-open-web-ui README |
| `output` vs `content` of a message (UI renders `output` first), `replace` event stores `content` | queued-message `replace` event, delivery shown live | sections 3 and 5 |
| `__task__` values of background requests (`title_generation`, `tags_generation`, `follow_up_generation`, and since 0.12 `memory_review`, `context_compaction`) | the pipe answers every task locally | `functions.py`, `utils/memory.py`, `utils/context_compaction.py` |
| `source` events, `metadata.source` / `metadata.name` unique per source | citation badges | section 5a |
| `open_webui.routers.files.upload_file_handler`, `open_webui.models.users.Users` | Word file as the user's file | section 5a |
| `__files__` / `__metadata__["files"]` and `["user_message"]` of a pipe; item types `file`, `collection` | which attachments the chat has (own sources) | `functions.py`, `utils/middleware.py` |
| `capabilities.file_context` and `file_upload` of the model | no RAG rewrite of the user's message; upload button | `utils/middleware.py: chat_completion_files_handler` |
| `open_webui.models.files.Files.get_file_by_id`, `file.data["content"]`, `.created_at`, `.meta["name"]` | text, name and date of uploads | `models/files.py` |
| `open_webui.utils.access_control.files.has_access_to_file(file_id, "read", user)` | may the user read this file | `utils/access_control/files.py` |
| `open_webui.retrieval.utils.filter_accessible_collections({id}, user)`, `open_webui.models.knowledge.Knowledges.get_knowledge_by_id` | may the user read this knowledge base | `retrieval/utils.py`, `models/knowledge.py` |
| `open_webui.models.models.Models.get_model_by_id(...).meta.knowledge` | knowledge attached to the model | `models/models.py` |
| `open_webui.config.Config.get("webui.url")` | public base URL for file links | `config.py` |
| `POST /api/v1/retrieval/query/collection` (admin key): response `documents`, `metadatas` (`file_id`, `name`), `distances` | `knowledge_search` | `routers/retrieval.py` |
| `GET /api/v1/files/{id}` (`created_at`), `GET /api/v1/files/{id}/content` | Stand of knowledge documents, links in the source list | `routers/files.py` |
| permission `workspace.knowledge` | users can create knowledge bases | Admin Panel, Users, Groups |

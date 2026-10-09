# Integration into Open WebUI

Target: `ghcr.io/fd-itbs/open-webui` v0.11.4 (fork of Open WebUI v0.11.4), Kubernetes, GitOps. The integration uses
only extension points: two functions (the pipe and the Word download action), model entries, one API key. No fork patch.

```
Open WebUI ──(pipe, valves)──► bebbi-deep-research service ──► Postgres, vLLM, Serper, Firecrawl, MCP
     ▲                                     │
     └──── POST /api/v1/chats/{chat}/messages/{message}[/event]  (admin API key, report and sources delivery)
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

**Seeder (GitOps).** `deploy/openwebui/function.json` holds two entries in the format of `POST /api/v1/functions/sync`
(id, name, type `pipe` and `action`, content, valves, `is_active`, `is_global`): `bebbi_deep_research` and
`bebbi_deep_research_download` (section 5a). Both have the valves `SERVICE_URL` and `SERVICE_TOKEN`. Let the seeder post it with an admin key, with
`SERVICE_TOKEN` filled in from the seeder's secret store (the file contains an empty token on purpose).
Regenerate after changing the pipe or presets: `uv run python scripts/build_openwebui_assets.py`
(`--check` fails if the files are stale; a unit test does the same).

**By hand / local.** `scripts/install_pipe.py` calls `functions/create` (or `update` if it exists) for both functions,
activates them globally and sets the valves:

```bash
OWUI_URL=http://localhost:3000 OWUI_API_KEY=sk-... RA_TOKEN=... uv run python scripts/install_pipe.py --models
```

Or in the UI: Admin Panel, Functions, "+", paste `pipe/deep_research.py` (and `pipe/deep_research_download.py`), save, enable, set "global". The gear icon opens the valves.

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
`POST /api/v1/chats/{chat_id}/messages/{message_id}` (text) and `POST .../messages/{message_id}/event` (one `source`
event per source, section 5a). In v0.11.4 both endpoints accept the chat owner or an admin (decision: admin service key).

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

After a delivery the marker is gone (the report replaces the message), so the next message starts a new research. This
is acceptable: the report is the end of a research, and a follow-up question becomes a new task (the pipe asks
clarifying questions or shows a plan). A follow-up that asks about the report content is not answered from the report;
users should use a normal model for that. Making the delivered message carry a "finished" marker would only make the
pipe answer the next message with the same report again, so we left it out. The same holds for a report that the pipe
returns itself (`jetzt` run that finishes while streaming): it carries no marker either.

### How the pipe and the delivery fit Open WebUI v0.11.4 (found in the end-to-end test)

* **State marker**: a markdown link reference definition `[//]: # (research-agent:<base64>)`. It is valid markdown and
  never rendered. The first version used an HTML comment, which Open WebUI shows as text.
* **`output` wins over `content`**: v0.11 stores the pipe answer as `output` items and the UI renders `output` in
  preference to `content`. `POST /chats/{id}/messages/{mid}` (and the `chat:message` event) change only `content`, so
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

## 5a. Citations and Word download (how Open WebUI v0.11.4 does it)

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
  ledger (`service/report.py`); the engines are unchanged and the stored report text is unchanged. Old clients ignore the new fields.
* `ui_report` is the report for the chat: the source list becomes a numbered list (a line starting with `[1]` would turn into a
  badge), stray `[n]` without a source are escaped. The list stays in the text because Open WebUI's own list shows only the
  URL (collapsed under "n Sources") and neither Stand nor the retrieval date; the preview of a source (click on a badge) shows
  title, Stand, retrieval date, kind and the excerpt.
* **Pipe path** (report returned in the chat): the pipe emits the `source` events in the order of the numbers, returns
  `ui_report`, and stores the Word file as a file of the user *inside Open WebUI* (`store_user_file`, the upload handler
  of Open WebUI called in-process with the chat user) and emits a `files` event.
* **Delivery path** (night run): the service posts the `source` events, then the text, with the admin key; after a reload the
  message carries `sources`, badges and list work. The Word file cannot be attached by the service (ownership, see above).
  The action function `bebbi_deep_research_download` adds a button ("Word-Datei") under every Deep Research answer; it runs as
  the user inside Open WebUI, looks up the job by chat and message id (`GET /jobs/lookup`, checks the user), fetches the
  `.docx` and attaches it. A second click does not attach it again.
* The pipe and the service both write into the same message for "jetzt" jobs. The service skips the source events if the
  message already has sources; the pipe waits for the delivery and skips its source events if the delivery succeeded.
* `GET /jobs/{id}/report.docx` (bearer token) returns the Word file for n8n and other apps (409 while the job runs).
  python-docx renders title, header block (question, date, preset, depth), headings, lists, Markdown tables as Word tables,
  citations as links to the source list, the source list with hyperlinks, Stand and retrieval date, and a footer.
  Rendering is on demand from the stored report; old jobs work too.


## 6. Network

* Pipe to service: cluster-internal HTTP (valve `SERVICE_URL`).
* Service to Open WebUI: `OWUI_URL`, cluster-internal, must be in `NO_PROXY`.
* Service to vLLM, Firecrawl, Postgres: cluster-internal, in `NO_PROXY`. Serper and the MCP servers go through the
  proxy unless they are reachable directly. See [operations.md](operations.md#proxy).

## 7. End-to-end test with a real Open WebUI (verified 2026-10-09)

Setup: `docker compose -f compose.yaml -f compose.eval-net.yaml up -d` (Open WebUI v0.11.4, service, Postgres; LLM
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
* The in-process upload imports `open_webui.routers.files` and `open_webui.models.users` (pinned to v0.11.4); re-test it on upgrades.
* The delivery key can write every chat (admin); keep NetworkPolicy and secret handling as described.
* A message that carries a status text (job still running when the user writes again) is a new assistant message; the
  report is delivered into the original one.

## 8. Local end-to-end test (compose)

```bash
cp .env.example .env     # set RA_*, POSTGRES_PASSWORD, WEBUI_SECRET_KEY, WEBUI_ADMIN_EMAIL/PASSWORD
docker compose up -d --build
OWUI_ADMIN_EMAIL=... OWUI_ADMIN_PASSWORD=... RA_TOKEN=... uv run python scripts/install_pipe.py --create-api-key --models
# put the printed OWUI_API_KEY into .env, then recreate the service so that it reads it: docker compose up -d service
# (without the key the delivery is `skipped`; after code changes build with `docker compose build service`)
```

Set `RA_NIGHT_WINDOW=00:00-23:59` in `.env` to let every job run at once while testing. With the evaluation stack on the
same host add `-f compose.eval-net.yaml` (see the comment in that file).

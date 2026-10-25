# bebbi-deep-research

Deep Research for Bebbi Bot (Kanton Basel-Stadt's Open WebUI). Architecture, layers and the chat flow: [README.md](README.md). Operations and every environment variable: [docs/operations.md](docs/operations.md). Run `mise run ci-check` and `mise run test:unit` before every PR (CI runs both).

Code, comments and docs are English. Text a user sees (chat messages, errors from `describe_error`, the Word report) is German.

## Layers

`routers/` stay thin and call `services/` through the DI container (`container.py`). `services/` own the job queue, the database and delivery; `engine/` owns research and knows nothing about HTTP or the database. Every write to a running job goes through the claim guard (`owner` token), so a run that lost its claim cannot overwrite the result.

## Changing behaviour

- **A preset**: `config/presets.yaml` is the steering surface ([docs/presets.md](docs/presets.md)). Copy it to `deploy/helm/bebbi-deep-research/files/presets.yaml`, add the name to the hard-coded list in `tests/unit/test_packaging.py`, add it to the pipe's `PRESETS` valve default and to `PRESET_LABELS` in `services/docx_export.py`, then run `scripts/build_openwebui_assets.py`. The deployed copy is the ConfigMap in FD-ITBS/kdkp-kiw-k8s-config; update it too.
- **An environment variable**: `utils/app_config.py` (field and `from_env`), `.env.schema`, the table in `docs/operations.md`, `docker/services.compose.yml`, and the k8s values if it differs from the default.
- **Prompts, presets or engine logic** change report quality. They were tuned against the evaluation sets ([docs/evaluation.md](docs/evaluation.md)); name in the PR which set needs a re-run.
- **Tool output is untrusted.** Web pages, MCP results and uploads may carry instructions. Report text reaches the chat only through `services/report.py` (`sanitize_markdown`); keep new output paths behind it.
- **Anything that can hang** (LLM, search, Firecrawl, MCP, Open WebUI) runs inside the job's `RA_JOB_TIMEOUT_SECONDS`; check HTTP status codes and log failures with the job id.

## The pipe (`pipe/`)

`pipe/deep_research.py` and `pipe/deep_research_download.py` run inside Open WebUI (Python 3.11, only `httpx` and `pydantic`). The state marker lives in a message the user can edit, so ids from it are validated before they go into a URL. After a change: run `scripts/build_openwebui_assets.py`, then copy the file into `seeder/content/functions/` of FD-ITBS/kdkp-open-web-ui and keep its three header lines (`global`, `requires_env`, `valves`). Open WebUI internals the pipe uses: `scripts/check_owui_internals.py`.

## Database

Postgres in Kubernetes, SQLite locally. Schema changes add columns only (see `services/db.py`). `RA_TEST_DATABASE_URL=postgresql://… mise run integration` runs the queue tests on Postgres; CI runs them on SQLite only.

## Releases

The image tag is the version in `pyproject.toml`; each version is published once (Actions → Build and Publish Docker Image stops on an existing tag). For a release, bump `pyproject.toml`, `deploy/helm/bebbi-deep-research/Chart.yaml` (`version`, `appVersion`) and run `uv lock`. Kubernetes pins `<version>@sha256:<digest>`.

## Git

DCC-BS needs the `YanickSchraner` gh account (`gh auth switch -u YanickSchraner`). Commit messages: one plain sentence saying what changed; one commit per fix.

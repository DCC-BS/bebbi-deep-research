# syntax=docker/dockerfile:1
# Multi-stage build with uv. The runtime image runs as non-root (uid 10001) and works with a read-only root
# filesystem: it writes only to /tmp (HOME, SQLite dev database), no .pyc files are written at runtime.

FROM ghcr.io/astral-sh/uv:0.9 AS uv

FROM python:3.12-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY research_agent ./research_agent
COPY service ./service
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable --reinstall-package bebbi-deep-research

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="bebbi-deep-research" \
      org.opencontainers.image.description="Deep-research job service for Bebbi Bot (Open WebUI)"
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY config ./config
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    RA_PRESETS=/app/config/presets.yaml \
    RA_PORT=8088
USER 10001:10001
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=3s CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8088/healthz', timeout=2)"]
ENTRYPOINT ["bebbi-deep-research"]

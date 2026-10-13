# Built from the shared FastAPI template (DCC-BS/dcc-docker-images, fastapi/Dockerfile): the mise base image installs
# the toolchain pinned in mise.toml (uv, varlock); assemble-runtime strips it to python + varlock.
#
#   docker build -f Dockerfile .
#
# The python version lives in exactly one place: pyproject.toml (requires-python).

# Stage 1: Build the application
FROM ghcr.io/dcc-bs/dcc-docker-images/mise:13-slim AS build

# Optional GitHub token to raise the API rate limit when mise resolves the `github:dmno-dev/varlock` release:
#   docker build --build-arg GITHUB_TOKEN=<token> .
ARG GITHUB_TOKEN=""
ENV GITHUB_TOKEN="${GITHUB_TOKEN}"

ENV APP_MODE=build
ENV DOCKER_BUILD=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_HTTP_TIMEOUT=120
ENV UV_PYTHON_INSTALL_DIR="/uv-python"

WORKDIR /app

# Source must be present before `mise install`: its postinstall hook runs `uv sync --locked --no-dev`
COPY . .

RUN mise trust -a && mise install

# Minimal runtime: only python + varlock
RUN assemble-runtime python

# Stage 2: Run the application
FROM debian:13-slim

LABEL org.opencontainers.image.title="bebbi-deep-research" \
      org.opencontainers.image.description="Deep-research job service for Bebbi Bot (Open WebUI)"

WORKDIR /app

RUN useradd --create-home --uid 1000 app

ENV APP_MODE=prod
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYDANTIC_AI_NO_BANNER=1
# Read-only root filesystem in Kubernetes: everything that writes (varlock, the SQLite dev database) uses /tmp
ENV HOME=/tmp
ENV PATH="/runtime/varlock:/app/.venv/bin:$PATH"

COPY --from=build --chown=app:app /app /app
COPY --from=build --chown=app:app /runtime /runtime
COPY --chown=app:app .env*.schema /app/

USER app

EXPOSE 8088

# Load and validate the environment via varlock, then run uvicorn; SIGTERM re-queues running jobs (20 s grace)
ENTRYPOINT ["/bin/sh", "-c", "varlock load && exec varlock run -- uvicorn bebbi_deep_research.app:create_app --factory --host 0.0.0.0 --port \"${PORT:-8088}\" --no-access-log --timeout-graceful-shutdown 20"]

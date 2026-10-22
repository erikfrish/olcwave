# syntax=docker/dockerfile:1
# OLCWave API image. Python 3.13, dependency install via uv from the frozen
# uv.lock (uv sync --frozen). Credentials are injected at runtime through
# env_file (deploy/backend.env, gitignored) - nothing secret is baked in and
# the image ships no .env or venv.
#
# NOTE on ENV scoping: every FROM resets the environment. UV_PROJECT_ENVIRONMENT
# and PATH are therefore re-declared in the FINAL stage (below) - the builder
# stage values do NOT carry over.
FROM python:3.13-slim@sha256:08b493e9c2f92a0f84e45062338a1503d0f585d63ba8783baad5a24cdf200894 AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# uv is installed into its own stage, not on top of the runtime layers.
FROM base AS uvsource
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir uv==0.9.11

FROM base
COPY --from=uvsource /usr/local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first: rebuilds only when pyproject/uv.lock change.
# UV_PROJECT_ENVIRONMENT pins the venv OUTSIDE /app so no .venv is baked
# into the image layers; PATH points at it for the runtime CMD.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src

EXPOSE 8000
# venv is already on PATH; run the interpreter directly (no uv wrapper at
# runtime, so no uv semantics can surprise the process).
CMD ["python", "/app/src/main.py"]

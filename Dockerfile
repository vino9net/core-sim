# syntax=docker/dockerfile:1

FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer: bind-mount only the lockfile and manifest so this stays cached
# across source edits. --no-install-project keeps core_sim itself out of it.
# The postgres extra is baked in because config.py swaps engines from env at startup
# (ARCH_DESIGN.md D3/D7) — pg_naive must not need a different image.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project --extra postgres

COPY . /app

# --no-editable copies core_sim into the venv rather than linking back to /app/src,
# which is what lets the final stage take .venv alone.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --extra postgres


FROM python:3.14-slim

# uv is build-time only; the runtime just needs the venv on PATH.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

USER app
WORKDIR /app

EXPOSE 8000

# core-sim-relay is the other entrypoint (pyproject [project.scripts]); override with
# `docker run ... core-sim-relay`.
CMD ["core-sim"]

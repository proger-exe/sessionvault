FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY alembic ./alembic
COPY alembic.ini ./
COPY src ./src

RUN uv sync --frozen --no-dev

RUN apt-get purge -y --auto-remove gcc libc6-dev

ENV PATH="/app/.venv/bin:${PATH}"

USER 65532:65532

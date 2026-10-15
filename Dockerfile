# syntax=docker/dockerfile:1
FROM python:3.12.10-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first for layer caching; versions come from the committed uv.lock.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12.10-slim-bookworm

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin mlsentry \
    && mkdir -p /var/lib/ml-sentry/artifacts \
    && chown -R mlsentry /var/lib/ml-sentry

COPY --from=build /app/.venv /app/.venv
COPY configs /app/configs

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER mlsentry
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4)"]

# Migrations are applied before the API starts; both fail fast with readable errors.
CMD ["sh", "-c", "ml-sentry migrate && exec ml-sentry serve"]

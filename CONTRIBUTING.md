# Contributing

## Development setup

You need [uv](https://docs.astral.sh/uv/), and Docker for the integration tiers.

```bash
uv sync                       # creates .venv with Python 3.12 and dev tools
uv run ruff format src tests examples
uv run ruff check src tests examples
uv run mypy
uv run pytest                 # unit, graph, API, safety, client-over-HTTP (no network, no Docker)
```

## Test tiers

| Tier | How | Covers |
|---|---|---|
| Default | `uv run pytest` | Everything that runs without external services |
| PostgreSQL | `ML_SENTRY_TEST_DATABASE_URL=... uv run pytest -m postgres` | Migrations, append-only trigger, durable checkpoints, the two-gate loop across process restarts |
| Live stack | `ML_SENTRY_E2E=1 ... uv run pytest -m stack` | The real Compose stack over HTTP, including the worker, both approvals and MLflow |

**PostgreSQL tier.** Point it at a **disposable** database; the tests drop its
`public` schema:

```bash
docker compose up -d postgres
docker compose exec postgres createdb -U mlsentry mlsentry_test
ML_SENTRY_TEST_DATABASE_URL=postgresql+psycopg://mlsentry:<POSTGRES_PASSWORD>@127.0.0.1:5433/mlsentry_test \
  uv run pytest -m postgres
```

**Live-stack tier.** Start the stack with two reviewer entries, a runner token and
MLflow, then run:

```bash
docker compose --profile mlflow up --build -d --wait
ML_SENTRY_E2E=1 ML_SENTRY_E2E_MLFLOW=1 ML_SENTRY_API_TOKEN=... \
ML_SENTRY_E2E_REVIEWER_TOKEN=<alice> ML_SENTRY_E2E_REVIEWER2_TOKEN=<bob> \
  uv run pytest -m stack tests/e2e
```

The CI `stack` job shows the exact setup.

## Rules

- **Migrations.** Schema changes need an Alembic migration.
  `tests/integration/test_database.py` fails if models and migrations diverge.
- **Determinism.** Policy decisions and gates stay deterministic and LLM-free.
- **Side effects.** Every side effect is idempotent and re-validated before it
  happens. Add a routing or safety test for any new path toward training or release.
- **Notebook.** After editing `examples/colab/colab_walkthrough.py` or
  `ml_sentry_client.py`, run `python examples/colab/build_notebook.py`. CI checks
  that they're in sync.
- **Tests and secrets.** No external services in default tests; live tests are
  opt-in. Never commit secrets.
- **Docs and lockfile.** Update docs together with code, and commit `uv.lock`.

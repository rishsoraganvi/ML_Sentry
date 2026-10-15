# Quickstart

## 1. Configure

```bash
cp .env.example .env          # Windows cmd: copy .env.example .env
```

Set `POSTGRES_PASSWORD` and `ML_SENTRY_API_TOKEN` in `.env`. Use random values of at
least 32 characters; `.env.example` shows generator commands for bash and
PowerShell. If a required value is missing, Compose stops with a message that names
it.

## 2. Start

```bash
docker compose up --build -d --wait     # postgres, api, worker
docker compose ps
docker compose logs -f api worker
```

- **Migrations.** The api container applies database migrations and LangGraph
  checkpoint setup, then starts.
- **Worker.** It runs mock jobs, expires approvals, enforces job deadlines and
  resumes workflows.
- **Ports.** API on `127.0.0.1:8000`, PostgreSQL on `127.0.0.1:5433`.
- **Optional MLflow.** `docker compose --profile mlflow up -d` (`127.0.0.1:5000`).

## 3. Check

```bash
docker compose exec api ml-sentry doctor
```

## 4. Demo: detection only

```bash
docker compose exec api ml-sentry demo
```

The demo sends five synthetic evaluations of `house_price_regressor`, whose policy
doesn't allow retraining:

| Event | Severity | Proposed action |
|---|---|---|
| baseline | none | none |
| healthy run | none | none |
| +14 % RMSE | warning | notify |
| +36 % RMSE | critical | investigate |
| 12 samples | invalid_evidence | notify |

Each incident's workflow runs, writes a console notification, and leaves the
incident `awaiting_review`.

## 5. Demo: the full two-approval loop (safe, mock only)

**Create a reviewer.**
```bash
docker compose exec api ml-sentry add-reviewer alice
```
Copy the printed `alice:<hash>` entry into `.env` as `ML_SENTRY_REVIEWERS=...` and
keep the token. Then reload:
```bash
docker compose up -d
```

**Start the demo.**
```bash
docker compose exec api ml-sentry demo --full
```

**Approve, as a human.**
1. Open <http://127.0.0.1:8000/ui/> and enter Alice's token.
2. **Approvals → retraining → Approve exactly this.**
3. The worker runs the mock job within seconds. The candidate passes its gates and a
   **deployment** approval appears. Approve it.
4. Open **Incidents** to see the incident `resolved` and the audit trail
   (`approval.decided` → `job.completed` → `release.verified`).

Nothing is trained or deployed outside ML Sentry. Re-running the demo is idempotent.

## 6. Next

- Connect your model: [your-model.md](your-model.md), or [colab.md](colab.md) for Colab.
- Interactive API docs: <http://127.0.0.1:8000/docs>.

## Stop, keep or delete data

```bash
docker compose down      # stops containers; data in volumes is KEPT
docker compose down -v   # DESTRUCTIVE: also deletes the database and artifact volumes
```

See [upgrade-uninstall.md](upgrade-uninstall.md).

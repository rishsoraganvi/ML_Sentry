# Backup and restore

Everything ML Sentry knows is in PostgreSQL:
- events, decisions, incidents and the audit log
- approvals, jobs and releases
- LangGraph workflow checkpoints

The `artifacts` volume is currently only checked for writability. MLflow data, if you
use the bundled service, lives in the `mlflowdata` volume.

## Back up

```bash
docker compose exec -T postgres pg_dump -U mlsentry -d mlsentry -Fc > ml-sentry-$(date +%F).dump
```

On Windows, use cmd:

```
docker compose exec -T postgres pg_dump -U mlsentry -d mlsentry -Fc > ml-sentry-backup.dump
```

## Restore

```bash
docker compose stop api worker
docker compose exec -T postgres dropdb -U mlsentry mlsentry
docker compose exec -T postgres createdb -U mlsentry mlsentry
docker compose exec -T postgres pg_restore -U mlsentry -d mlsentry --no-owner < ml-sentry-backup.dump
docker compose start api worker
docker compose exec api ml-sentry doctor
```

**Verified.** We restored a dump into a scratch database and compared row counts of
events, incidents, audit events, approvals and checkpoints. They were identical, and
the audit log's append-only trigger was restored too. Workflows that were waiting
continue after the restore, because their checkpoints are in the same dump.

## MLflow (bundled)

Stop the `mlflow` service, then back up the `mlflowdata` volume with a volume backup
tool or `docker run --rm -v ml-sentry_mlflowdata:/data -v "$PWD":/backup alpine tar czf /backup/mlflow.tgz -C /data .`.

## Retention

- **Audit events** are append-only. Delete old ones only by deliberate database
  administration (the trigger must be dropped first). Document that process for
  your organization.
- **Checkpoints** of completed workflows can be removed with LangGraph's checkpointer
  tools. The audit log remains the record of what happened.

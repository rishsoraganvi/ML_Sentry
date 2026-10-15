# Upgrade and uninstall

## Upgrade

```bash
docker compose exec -T postgres pg_dump -U mlsentry -d mlsentry -Fc > before-upgrade.dump   # always first
git pull                                  # or change the pinned image tag in docker-compose.release.yml
docker compose up --build -d --wait       # the api applies migrations and LangGraph checkpoint setup on start
docker compose exec api ml-sentry doctor
```

- **Data is preserved.** Upgrades never delete governance records or artifacts.
  Migrations only add tables and columns. Upgrading a Milestone 1 database to the
  current schema in place has been tested (0001 → 0002 → 0003).
- **Running workflows continue.** Workflows waiting for an approval or job resume
  from their checkpoints after the upgrade.
- **Version tags.** Use pinned version tags, never `latest`, for reproducible
  installs.

## Stop without deleting anything

```bash
docker compose down        # containers removed; volumes (database, artifacts, MLflow) kept
docker compose up -d       # everything comes back as it was
```

## Uninstall

| Command | Effect |
|---|---|
| `docker compose down` | Removes containers and the network. **Keeps** all data volumes |
| `docker compose --profile mlflow down -v` | **Deletes all data**: database, audit log, checkpoints, artifacts and bundled MLflow. Irreversible; back up first |
| `docker image rm ml-sentry:dev` | Removes the locally built image |

The volumes are named `ml-sentry_pgdata`, `ml-sentry_artifacts` and
`ml-sentry_mlflowdata`. List them with `docker volume ls`.

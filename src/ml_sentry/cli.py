"""``ml-sentry`` command-line interface."""

from __future__ import annotations

import os
import secrets
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx
import typer
from sqlalchemy import Engine, text

from ml_sentry.policies.loader import PolicyError, load_policy
from ml_sentry.settings import ConfigurationError, Settings, load_settings
from ml_sentry.storage.database import make_engine, migration_status, ping, upgrade_to_head

app = typer.Typer(no_args_is_help=True, add_completion=False, help="ML Sentry governance service")


def _settings_or_exit() -> Settings:
    try:
        return load_settings()
    except ConfigurationError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None


@app.command("generate-token")
def generate_token() -> None:
    """Print a random API token for ML_SENTRY_API_TOKEN."""
    typer.echo(secrets.token_urlsafe(32))


@app.command("validate-policy")
def validate_policy(path: Path) -> None:
    """Validate a policy file without starting the service."""
    try:
        loaded = load_policy(path)
    except PolicyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    typer.echo(f"OK: {path} (version {loaded.version}, models: {', '.join(loaded.policy.models)})")


@app.command()
def migrate() -> None:
    """Apply database migrations up to the latest revision."""
    from ml_sentry.graph.runtime import open_checkpointer

    settings = _settings_or_exit()
    upgrade_to_head(make_engine(settings.database_url))
    # LangGraph manages its own checkpoint tables; setup() is idempotent.
    with open_checkpointer(settings.database_url):
        pass
    typer.echo("Database migrations and workflow checkpoint tables are up to date.")


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind address inside the container"),  # noqa: S104
    port: int = 8000,
) -> None:
    """Validate configuration and policy, then run the API server."""
    import uvicorn

    from ml_sentry.main import create_app

    settings = _settings_or_exit()
    try:
        application = create_app(settings)
    except PolicyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None
    uvicorn.run(application, host=host, port=port, log_level=settings.log_level.lower())


_SYMBOL = {"PASS": typer.colors.GREEN, "FAIL": typer.colors.RED, "SKIP": None, "INFO": None}


def _report(status: str, name: str, detail: str) -> None:
    label = typer.style(f"[{status}]", fg=_SYMBOL[status])
    typer.echo(f"{label} {name}: {detail}")


def _check_workflows(engine: Engine, check: Callable[[str, str, str], None]) -> None:
    if engine.dialect.name == "postgresql":
        with engine.connect() as conn:
            exists = conn.scalar(text("SELECT to_regclass('public.checkpoints') IS NOT NULL"))
        if exists:
            check("PASS", "workflow checkpoints", "LangGraph checkpoint tables present")
        else:
            check("FAIL", "workflow checkpoints", "tables missing; run: ml-sentry migrate")
    else:
        check("INFO", "workflow checkpoints", "in-memory (non-PostgreSQL database; not durable)")
    with engine.connect() as conn:
        counts: dict[str, int] = dict(
            conn.execute(
                text(
                    "SELECT workflow_status, count(*) FROM incidents "
                    "WHERE workflow_status IS NOT NULL GROUP BY workflow_status"
                )
            ).all()
        )
    failed = counts.get("failed", 0)
    detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no workflows yet"
    check("FAIL" if failed else "PASS", "workflows", detail)


@app.command()
def doctor() -> None:
    """Check configuration, dependencies and health; exit 1 if anything fails."""
    failures = 0

    def check(status: str, name: str, detail: str) -> None:
        nonlocal failures
        failures += status == "FAIL"
        _report(status, name, detail)

    try:
        settings = load_settings()
    except ConfigurationError as exc:
        check("FAIL", "configuration", str(exc))
        raise typer.Exit(1) from None
    check("PASS", "configuration", "required environment variables are set")

    engine = make_engine(settings.database_url)
    try:
        ping(engine)
        check("PASS", "database", "connected")
        current, head = migration_status(engine)
        if current == head:
            check("PASS", "migrations", f"at head ({head})")
            _check_workflows(engine, check)
        else:
            check("FAIL", "migrations", f"at {current}, expected {head}; run: ml-sentry migrate")
    except Exception as exc:
        check(
            "FAIL",
            "database",
            f"cannot connect ({type(exc).__name__}); check ML_SENTRY_DATABASE_URL "
            "and that the postgres service is healthy",
        )
    finally:
        engine.dispose()

    try:
        loaded = load_policy(settings.policy_path)
        check(
            "PASS",
            "policy",
            f"{settings.policy_path} version {loaded.version}, "
            f"models: {', '.join(loaded.policy.models)}",
        )
        defaults = loaded.policy.defaults
        check(
            "INFO",
            "execution (policy)",
            f"retraining_enabled={defaults.retraining_enabled}, "
            f"deployment_enabled={defaults.deployment_enabled}",
        )
    except PolicyError as exc:
        check("FAIL", "policy", str(exc))

    check(
        "INFO",
        "execution (environment)",
        f"ML_SENTRY_RETRAINING_ENABLED={settings.retraining_enabled}, "
        f"ML_SENTRY_DEPLOYMENT_ENABLED={settings.deployment_enabled} "
        "(required only for external runners and non-mock release targets)",
    )

    try:
        settings.artifact_dir.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(dir=settings.artifact_dir)
        os.close(fd)
        Path(probe).unlink()
        check("PASS", "artifact storage", f"{settings.artifact_dir} is writable")
    except OSError as exc:
        check("FAIL", "artifact storage", f"{settings.artifact_dir}: {exc.strerror}")

    provider = settings.notification_provider
    check(
        "PASS",
        "notifications",
        f"provider '{provider}'"
        + (
            " (local log only)"
            if provider in ("console", "none")
            else " configured; send a test with POST /api/v1/admin/test-notification"
        ),
    )
    reviewers = sorted(settings.reviewer_hashes)
    check(
        "PASS" if reviewers else "INFO",
        "reviewers",
        f"{len(reviewers)} configured ({', '.join(reviewers)})"
        if reviewers
        else "none configured: approvals cannot be decided (ml-sentry add-reviewer NAME)",
    )
    check(
        "INFO",
        "external runner token",
        "configured" if settings.runner_token else "not set (external training jobs disabled)",
    )

    if settings.mlflow_tracking_uri:
        try:
            resp = httpx.get(f"{settings.mlflow_tracking_uri.rstrip('/')}/health", timeout=5)
            resp.raise_for_status()
            experiments = settings.mlflow_experiments
            check(
                "PASS" if experiments else "INFO",
                "mlflow",
                f"{settings.mlflow_tracking_uri} reachable; allowed experiments: "
                + (", ".join(experiments) or "none (set ML_SENTRY_MLFLOW_ALLOWED_EXPERIMENTS)"),
            )
        except httpx.HTTPError as exc:
            check(
                "FAIL",
                "mlflow",
                f"{settings.mlflow_tracking_uri} unreachable ({type(exc).__name__}); "
                "check ML_SENTRY_MLFLOW_TRACKING_URI",
            )
    else:
        check("SKIP", "mlflow", "ML_SENTRY_MLFLOW_TRACKING_URI not set")

    try:
        resp = httpx.get(f"{settings.api_url.rstrip('/')}/health/ready", timeout=5)
        if resp.status_code == 200:
            check("PASS", "api", f"{settings.api_url} ready")
        else:
            check("FAIL", "api", f"{settings.api_url} not ready: {resp.text[:300]}")
    except httpx.HTTPError as exc:
        check("FAIL", "api", f"{settings.api_url} unreachable ({type(exc).__name__})")

    check(
        "INFO",
        "worker",
        "runs as the 'worker' Compose service (check: docker compose ps worker)",
    )

    if failures:
        typer.secho(f"{failures} check(s) failed.", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho("All checks passed.", fg=typer.colors.GREEN)


@app.command("add-reviewer")
def add_reviewer(name: str) -> None:
    """Create a reviewer token. Only its SHA-256 hash goes into the configuration."""
    from ml_sentry.api.security import token_hash
    from ml_sentry.settings import parse_reviewers

    parse_reviewers(f"{name}:{'0' * 64}")  # validates the name
    token = secrets.token_urlsafe(32)
    typer.echo(f"Reviewer token for '{name}' (give it to them privately; it is shown once):")
    typer.echo(f"  {token}")
    typer.echo("Add this entry to ML_SENTRY_REVIEWERS (comma-separated) and restart api/worker:")
    typer.echo(f"  {name}:{token_hash(token)}")


@app.command()
def worker() -> None:
    """Run the background worker (mock jobs, approval expiry, job deadlines, recovery)."""
    from ml_sentry.graph.runtime import WorkflowRunner, build_deps, open_checkpointer
    from ml_sentry.jobs.worker import Worker
    from ml_sentry.storage.database import make_session_factory

    settings = _settings_or_exit()
    import logging

    logging.basicConfig(level=settings.log_level)
    try:
        loaded = load_policy(settings.policy_path)
    except PolicyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None
    factory = make_session_factory(make_engine(settings.database_url))
    with open_checkpointer(settings.database_url) as checkpointer:
        runner = WorkflowRunner(build_deps(settings, factory, loaded), checkpointer)
        Worker(runner).run_forever(settings.worker_poll_seconds)


@app.command("mlflow-import")
def mlflow_import(
    run_id: str,
    role: str = typer.Option("", help="Override the run's role: monitoring or baseline"),
) -> None:
    """Ingest one finished MLflow run (from an allow-listed experiment)."""
    from ml_sentry.evaluation.mlflow_adapter import MlflowClient, MlflowError
    from ml_sentry.services.ingestion import EventConflictError, ingest_event
    from ml_sentry.storage.database import make_session_factory

    settings = _settings_or_exit()
    loaded = load_policy(settings.policy_path)
    try:
        event = MlflowClient.from_settings(settings).run_to_event(run_id, role=role or None)
        outcome = ingest_event(
            make_session_factory(make_engine(settings.database_url)),
            event,
            loaded,
            actor="mlflow-import",
        )
    except (MlflowError, EventConflictError) as exc:
        typer.secho(f"{type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    d = outcome.decision
    typer.echo(f"{event.event_id}: severity={d.severity} action={d.proposed_action}")
    if outcome.incident:
        typer.echo(f"incident {outcome.incident.id} (the worker starts its workflow)")


@app.command()
def demo(
    full: bool = typer.Option(
        False, "--full", help="Also run the remediation loop for 'demo_regressor'"
    ),
) -> None:
    """Submit synthetic, offline evaluation events and show the policy decisions."""
    from ml_sentry.demo import DEMO_MODEL, REMEDIATION_MODEL, demo_events, remediation_events
    from ml_sentry.graph.runtime import WorkflowRunner, build_deps, open_checkpointer
    from ml_sentry.notifications.console import ConsoleNotifier
    from ml_sentry.services.ingestion import EventConflictError, ingest_event
    from ml_sentry.storage.database import make_session_factory

    settings = _settings_or_exit()
    try:
        loaded = load_policy(settings.policy_path)
    except PolicyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None
    needed = [DEMO_MODEL, REMEDIATION_MODEL] if full else [DEMO_MODEL]
    if any(m not in loaded.policy.models for m in needed):
        typer.secho(
            f"The demo needs policies for {', '.join(needed)}; use "
            "configs/governance.example.yaml.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    factory = make_session_factory(make_engine(settings.database_url))
    # The demo always uses the console sink, whatever provider is configured.
    deps = build_deps(settings, factory, loaded, notifier=ConsoleNotifier())
    typer.echo(f"Policy {loaded.version}. Nothing is trained, deployed or sent externally.\n")
    events = demo_events() + (remediation_events() if full else [])
    pending: list[str] = []
    with open_checkpointer(settings.database_url) as checkpointer:
        runner = WorkflowRunner(deps, checkpointer)
        for description, event in events:
            try:
                outcome = ingest_event(factory, event, loaded, actor="demo-cli")
            except EventConflictError:
                typer.secho(f"{event.event_id}: conflicts with an existing event", fg="red")
                continue
            d = outcome.decision
            rmse = event.metrics.get("rmse")
            typer.echo(
                f"{description:<36} rmse={rmse:>10.1f}  severity={d.severity:<17} "
                f"action={d.proposed_action}" + ("" if outcome.created else "  (already recorded)")
            )
            if outcome.incident:
                workflow = runner.run_incident(outcome.incident.id)
                typer.echo(f"{'':<36} incident {outcome.incident.id}  workflow={workflow}")
                if workflow == "waiting":
                    pending.append(str(outcome.incident.id))
    typer.echo("\nInspect incidents with: GET /api/v1/incidents (see docs/quickstart.md)")
    if pending:
        typer.echo(
            "\nThe remediation demo is now WAITING FOR A HUMAN. Nothing is approved for you:\n"
            f"  open {settings.link_base}/ui/ , enter a reviewer token "
            "(ml-sentry add-reviewer NAME),\n"
            "  review and approve the retraining request. The worker then runs the mock job;\n"
            "  approve the deployment request that follows. See docs/approvals.md."
        )


if __name__ == "__main__":
    app()

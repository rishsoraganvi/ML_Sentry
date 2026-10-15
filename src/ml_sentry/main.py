"""FastAPI application factory.

Configuration and policy are validated while the app is built, so the service
refuses to start (fails closed) instead of processing events with a bad setup.
Workflow recovery (pending/stale workflows, expired approvals, job deadlines) is
done by the separate ``ml-sentry worker`` process.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ml_sentry import __version__
from ml_sentry.api import governance, ui
from ml_sentry.api.errors import install_error_handlers
from ml_sentry.api.middleware import BodySizeLimitMiddleware
from ml_sentry.api.routes import health, v1
from ml_sentry.graph.runtime import WorkflowRunner, build_deps, open_checkpointer
from ml_sentry.notifications.base import Notifier
from ml_sentry.policies.loader import load_policy
from ml_sentry.settings import Settings, get_settings
from ml_sentry.storage.database import make_engine, make_session_factory

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, notifier: Notifier | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)
    policy = load_policy(settings.policy_path)
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    deps = build_deps(settings, session_factory, policy, notifier=notifier)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with open_checkpointer(settings.database_url) as checkpointer:
            app.state.workflow = WorkflowRunner(deps, checkpointer)
            logger.info(
                "ML Sentry %s started with policy %s (%d models, %d reviewers)",
                __version__,
                policy.version,
                len(policy.policy.models),
                len(settings.reviewer_hashes),
            )
            yield

    app = FastAPI(
        title="ML Sentry",
        version=__version__,
        description="Governance for machine-learning regression models.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.policy = policy
    app.state.engine = engine
    app.state.session_factory = session_factory

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    install_error_handlers(app)
    app.include_router(health)
    app.include_router(v1)
    app.include_router(governance.router)
    app.include_router(ui.router)
    return app

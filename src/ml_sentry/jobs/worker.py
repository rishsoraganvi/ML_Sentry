"""Background worker: runs mock jobs, expires approvals, times out jobs, recovers workflows.

The worker never executes user code. ``external`` jobs are claimed and run by the
user's own pipeline over the API; the worker only enforces their deadlines.
"""

from __future__ import annotations

import logging
import threading
import uuid

from ml_sentry.graph.runtime import WorkflowRunner
from ml_sentry.jobs.mock_runner import MOCK_ACTOR, simulate
from ml_sentry.jobs.service import (
    JobError,
    claim_job,
    complete_job,
    queued_jobs,
    timeout_overdue,
)
from ml_sentry.services.approvals import expire_overdue

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, runner: WorkflowRunner) -> None:
        self.runner = runner
        self._sessions = runner.deps.session_factory

    def _wake(self, incident_ids: list[uuid.UUID]) -> None:
        for incident_id in dict.fromkeys(incident_ids):
            self.runner.run_incident(incident_id)

    def run_mock_jobs(self) -> list[uuid.UUID]:
        done: list[uuid.UUID] = []
        for job in queued_jobs(self._sessions, runner="mock"):
            try:
                claimed = claim_job(self._sessions, job.id, MOCK_ACTOR, runner="mock")
            except JobError:
                continue  # claimed elsewhere or past its deadline
            result = simulate(claimed.spec, str(claimed.id))
            complete_job(self._sessions, claimed.id, MOCK_ACTOR, result)
            done.append(claimed.incident_id)
        return done

    def run_once(self) -> dict[str, int]:
        expired = expire_overdue(self._sessions)
        timed_out = timeout_overdue(self._sessions)
        mock_done = self.run_mock_jobs()
        self._wake([*expired, *timed_out, *mock_done])
        resumed = self.runner.resume_pending()
        woken = self.runner.wake_stale_waiting()
        return {
            "approvals_expired": len(expired),
            "jobs_timed_out": len(timed_out),
            "mock_jobs_run": len(mock_done),
            "workflows_resumed": len(resumed),
            "workflows_woken": len(woken),
        }

    def run_forever(self, poll_seconds: float, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        logger.info("worker %s started (poll every %.1fs)", self.runner.owner, poll_seconds)
        while not stop.is_set():
            try:
                stats = self.run_once()
                if any(stats.values()):
                    logger.info("worker cycle: %s", stats)
            except Exception:
                logger.exception("worker cycle failed")
            stop.wait(poll_seconds)

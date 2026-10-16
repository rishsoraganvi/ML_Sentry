"""Worker heartbeat used by the container healthcheck."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from ml_sentry.cli import app as cli
from ml_sentry.graph.runtime import WorkflowRunner
from ml_sentry.jobs import worker as worker_module
from ml_sentry.jobs.worker import Worker, heartbeat_age
from ml_sentry.policies.loader import LoadedPolicy
from tests.graph.conftest import RecordingNotifier, make_deps


def test_completed_cycle_writes_heartbeat(
    session_factory: sessionmaker[Session], policy: LoadedPolicy, tmp_path: Path
) -> None:
    beat = tmp_path / "hb"
    stop = threading.Event()
    runner = WorkflowRunner(
        make_deps(session_factory, policy, RecordingNotifier()), InMemorySaver()
    )
    thread = threading.Thread(
        target=Worker(runner).run_forever, args=(0.05, stop, beat), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not beat.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=5)
    age = heartbeat_age(beat)
    assert age is not None and age < 5


def test_failed_cycle_does_not_beat(tmp_path: Path) -> None:
    class Broken(Worker):
        def __init__(self) -> None:
            self.runner = type("R", (), {"owner": "test"})()  # type: ignore[assignment]

        def run_once(self) -> dict[str, int]:
            raise RuntimeError("database down")

    beat, stop = tmp_path / "hb", threading.Event()
    stop_later = threading.Timer(0.2, stop.set)
    stop_later.start()
    Broken().run_forever(0.02, stop, beat)
    assert heartbeat_age(beat) is None


@pytest.mark.parametrize(
    ("age", "exit_code", "text"),
    [(None, 1, "no heartbeat"), (5.0, 0, "healthy"), (600.0, 1, "unhealthy")],
)
def test_worker_health_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, age: float | None, exit_code: int, text: str
) -> None:
    beat = tmp_path / "hb"
    if age is not None:
        beat.write_text("x")
        os.utime(beat, (time.time() - age, time.time() - age))
    monkeypatch.setattr(worker_module, "HEARTBEAT_PATH", beat)
    monkeypatch.setattr(worker_module, "heartbeat_age", lambda path=beat: heartbeat_age(path))
    result = CliRunner().invoke(cli, ["worker-health", "--max-age", "60"])
    assert result.exit_code == exit_code
    assert text in result.output

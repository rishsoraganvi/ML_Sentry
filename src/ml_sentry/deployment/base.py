"""Release adapter contract.

``release`` must be idempotent and return what was there before (for rollback);
``verify`` reports whether the target now serves the candidate; ``rollback``
restores the previous reference. Adapters never decide anything: the workflow only
calls them after a valid, re-checked deployment approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ReleasePlan:
    model_name: str
    target: str
    adapter: str
    candidate_ref: str
    candidate_version: str
    registered_model: str | None
    alias: str
    simulate_verification_failure: bool = False


@dataclass(frozen=True)
class Previous:
    ref: str | None
    version: str | None


class ReleaseError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code[:64]


class ReleaseAdapter(Protocol):
    name: str

    def current(self, plan: ReleasePlan) -> Previous: ...

    def release(self, plan: ReleasePlan) -> None: ...

    def verify(self, plan: ReleasePlan) -> bool: ...

    def rollback(self, plan: ReleasePlan, previous: Previous) -> None: ...

"""Bearer-token authentication with three separated roles.

* ``service``  - ML_SENTRY_API_TOKEN: submit evaluations, read, admin checks.
                 It can NOT decide approvals.
* ``reviewer`` - per-person tokens configured as SHA-256 hashes in
                 ML_SENTRY_REVIEWERS: read and decide approvals.
* ``runner``   - ML_SENTRY_RUNNER_TOKEN: claim and report external training jobs only.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ml_sentry.api.errors import ApiError
from ml_sentry.settings import Settings

Role = Literal["service", "reviewer", "runner"]

_bearer = HTTPBearer(auto_error=False, description="Service, reviewer or runner token")

API_ACTOR = "api-token"
RUNNER_ACTOR = "runner-token"


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def identify(token: str, settings: Settings) -> Principal | None:
    encoded = token.encode()
    if hmac.compare_digest(encoded, settings.api_token.get_secret_value().encode()):
        return Principal(API_ACTOR, "service")
    if settings.runner_token is not None and hmac.compare_digest(
        encoded, settings.runner_token.get_secret_value().encode()
    ):
        return Principal(RUNNER_ACTOR, "runner")
    digest = token_hash(token)
    match = None
    for name, expected in settings.reviewer_hashes.items():
        if hmac.compare_digest(digest, expected):
            match = Principal(f"reviewer:{name}", "reviewer")
    return match


def authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(401, "UNAUTHENTICATED", "Missing bearer token")
    principal = identify(credentials.credentials, request.app.state.settings)
    if principal is None:
        raise ApiError(401, "UNAUTHENTICATED", "Invalid bearer token")
    return principal


def require_roles(*roles: Role) -> Callable[..., Principal]:
    def dependency(principal: Principal = Depends(authenticate)) -> Principal:
        if principal.role not in roles:
            raise ApiError(
                403, "FORBIDDEN", f"This action needs one of the roles: {', '.join(roles)}"
            )
        return principal

    return dependency


# Kept for the evaluation endpoint: only the service token may submit evidence.
def require_token(principal: Principal = Depends(require_roles("service"))) -> str:
    return principal.name

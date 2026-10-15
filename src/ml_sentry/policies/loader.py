"""Load and validate the governance policy file; fail closed on any problem."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from ml_sentry.policies.schemas import PolicyFile


class PolicyError(Exception):
    """The policy file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class LoadedPolicy:
    policy: PolicyFile
    version: str  # "<schema_version>-<sha256 prefix of the file bytes>"
    path: Path


def parse_policy(raw: bytes, path: Path) -> LoadedPolicy:
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path}: not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise PolicyError(f"{path}: expected a mapping at the top level")
    try:
        policy = PolicyFile.model_validate(data)
    except ValidationError as exc:
        problems = "\n".join(
            f"  - {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise PolicyError(f"{path}: invalid policy:\n{problems}") from None
    digest = hashlib.sha256(raw).hexdigest()[:12]
    return LoadedPolicy(policy=policy, version=f"{policy.schema_version}-{digest}", path=path)


def load_policy(path: Path) -> LoadedPolicy:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PolicyError(
            f"cannot read policy file {path}: {exc.strerror} (check ML_SENTRY_POLICY_PATH)"
        ) from None
    return parse_policy(raw, path)

# Changelog

## 0.1.0 — unreleased (all plan milestones implemented; image not yet published)

### Project
- **Repository:** <https://github.com/rishsoraganvi/ML_Sentry>, with docs, the
  release Compose file and the Colab badge pointing at it.
- **License:** MIT, confirmed.
- **Docs:** guide to choosing an alert channel (console, Slack, Telegram or webhook)
  and how alerts differ from the audit trail.

### Milestone 7 — release readiness
- **Docs:** your-model guide, Google Colab guide, approvals, MLflow, training runners,
  release targets, troubleshooting, backup and restore (tested), upgrade and uninstall.
- **Release Compose:** `docker-compose.release.yml`, pinned image tag. Prepared but
  not yet tested against a published image.
- **CI:** lint, types, unit, graph and PostgreSQL tests; notebook sync check;
  dependency audit; gitleaks secret scan; a live Compose stack (with MLflow)
  end-to-end job.
- **Publishing:** `build-publish-image.yml` publishes immutable GHCR tags only after CI
  passes, and only when the tag matches the package version.

### Milestone 6 — release adapters and verification
- **Adapter contract:** `current`, `release`, `verify`, `rollback`.
- **Adapters:** `mock` (ML Sentry alias table) and `mlflow_alias` (Model Registry
  alias, tested against MLflow 3.10).
- **Release:**
  - One release per Gate B approval, re-validated immediately before release.
  - Automatic rollback when verification fails.
  - The released candidate becomes the new baseline.

### Milestone 5 — training runners and candidate gates
- **Runners:**
  - Allow-listed pipelines with fixed config, training dataset and timeout.
  - One job per Gate A approval.
  - `mock` runner (worker) and `external` pull-based runner (runner token, claim
    and report API).
- **Candidate gates:** valid evidence, same holdout, no regression against the
  champion, and primary-metric improvement. Failed gates preserve the champion.
- **Limits:** cooldowns, max attempts per incident and job deadlines.
- **Worker:** new `ml-sentry worker` Compose service.

### Milestone 4 — MLflow
- **REST client:** no `mlflow` dependency.
- **Ingestion:** allow-listed experiments; tag-based run → event mapping, including
  `mlflow.evaluate()` metric names.
- **Interfaces:** `POST /api/v1/evaluations/mlflow`, `ml-sentry mlflow-import`, and
  MLflow-backed candidate results.
- **Compose:** optional `mlflow` profile with a pinned image.
- **Tested:** the same decision as an equivalent generic event.

### Milestone 3 — human approval and notifications
- **Roles:** service, reviewer (hashed personal tokens; `ml-sentry add-reviewer`)
  and runner.
- **Gates:**
  - Two gates with exact-spec digests, expiry, distinct-approver option, and
    concurrency-safe single decisions.
  - Re-validation (policy version, spec, reviewer authorization) before side effects.
- **Approval API and UI:** minimal reviewer UI at `/ui/` (strict CSP).
- **Notification providers:** Slack, Telegram and signed webhook, alongside
  console/none. Approval-request notifications; `POST /api/v1/admin/test-notification`.

### Graph and data
- **Workflow:** real `interrupt()`-based approval and job waits; deterministic
  `investigate` node; cross-process workflow leases; recovery of missed wake-ups.
  State version 2.
- **Migration `0003`:** approvals, jobs, releases, model aliases and workflow leases.
- **Example policies:** the example policy adds a `demo_regressor` (mock-only full
  loop). New `configs/my-model.example.yaml` template.
- **Client and Colab:** a single-file stdlib client and a Colab walkthrough
  notebook (built from a tested script).

### Milestone 2 — LangGraph orchestration
- **Workflow:** typed `GovernanceState` and a governance graph with one thread per
  incident, PostgreSQL checkpoints and restart recovery.
- **Notifications:** console notifications with idempotent delivery records.
- **Migration `0002`.**

### Milestones 0–1 — skeleton, regression events and deterministic policies
- **Skeleton:** packaging, configuration, Docker and Compose, migrations, health
  checks, `doctor` and CI.
- **Evaluation:** canonical event schema, metric metadata and evidence validation,
  YAML policies, the deterministic engine, and the idempotent event API with
  incidents and audit.

# Release targets

A release happens only after **Gate B** is approved for the exact candidate, and
after that approval is re-validated. There is one release per approval. The release
is then verified, and rolled back to the previous reference if verification fails.

```yaml
release_targets:
  local_alias:
    adapter: mock
  mlflow_champion:
    adapter: mlflow_alias
    registered_model: churn_value_regressor
    alias: champion
models:
  churn_value_regressor:
    deployment: {allowed: true, target: mlflow_champion, min_primary_improvement: 0.01}
```

## `mock`

The mock adapter sets ML Sentry's own `model_aliases` row
(`model_name`, `champion`) to the candidate's reference.
- **Verify:** reads the alias back.
- **Rollback:** restores the previous reference.
- `simulate_verification_failure: true` exercises the rollback path.

No environment switch is needed.

## `mlflow_alias`

This adapter is tested against MLflow 3.10.
- **Release:** sets the registered-model alias (default `champion`) to the
  candidate's `candidate_model_version`, which must be a version of
  `registered_model`.
- **Verify:** reads the alias back.
- **Rollback:** restores the previous version, or deletes the alias if there was
  none.

> **A registry alias change is not a deployment.** Your serving system must load
> `models:/<name>@champion` (or poll the alias) for the change to reach traffic.
> ML Sentry verifies the alias, not your endpoint.

Requirements:
- `ML_SENTRY_DEPLOYMENT_ENABLED=true`.
- MLflow configured as described in [mlflow-integration.md](mlflow-integration.md).
- The external runner reports `candidate_model_version` as the registered version
  it created.

## After a verified release

ML Sentry then:
1. Records `release.performed` and `release.verified` in the audit log.
2. Registers the candidate's holdout evaluation as the model's **new baseline**
   (event `release-<id>`), so later monitoring is compared with the new champion.
3. Marks the incident `resolved`.

If verification fails, the release is rolled back (`release.rollback`), the incident
goes to `awaiting_review`, and the owner is notified.

## Adding another target

Implement `current`, `release`, `verify` and `rollback` from
`ml_sentry.deployment.base.ReleaseAdapter`, and register the adapter name in the
policy schema and `make_adapter`. Test it the same way the MLflow adapter is tested,
including rollback, before calling it supported.

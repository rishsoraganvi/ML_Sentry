# Regression policies

A policy is a YAML file (schema version 1) that is validated at startup. Missing files,
malformed YAML, unknown keys, unknown metrics and inconsistent thresholds all stop
the service (fail closed). Check a file with `ml-sentry validate-policy <path>`.

The policy version recorded with every decision is `<schema_version>-<sha256 prefix
of the file bytes>`, so any edit to the file produces a new version.

## Metrics

Direction and valid range come from built-in metric metadata. They are never
inferred from the metric's name, and the policy must declare the matching direction.

| Metric | Direction | Valid range | Notes |
|---|---|---|---|
| `mae` | minimize | ≥ 0 | |
| `mse` | minimize | ≥ 0 | |
| `rmse` | minimize | ≥ 0 | must satisfy `rmse² ≈ mse` when both are sent |
| `r2` | maximize | ≤ 1 | may be negative; undefined for a constant target (send it omitted or NaN) |
| `mape` | minimize | ≥ 0 | a fraction (0.05 = 5 %); undefined when any target is 0 |
| `smape` | minimize | 0 … 2 | a fraction; `2|y−ŷ|/(|y|+|ŷ|)`, pairs where both are 0 count as 0 |

You may send extra metrics. They must be finite, and they are stored but not
evaluated.

## Thresholds

```yaml
models:
  my_model:
    task_type: regression
    primary_metric: rmse
    minimum_sample_count: 200        # optional, overrides defaults
    require_baseline: true           # optional, overrides defaults
    metrics:
      rmse:
        direction: minimize
        warning_relative_increase: 0.10
        critical_relative_increase: 0.20
        maximum_acceptable: 50000
      r2:
        direction: maximize
        warning_relative_decrease: 0.05
        minimum_acceptable: 0.70
```

- **Absolute limits** (`maximum_acceptable`, `minimum_acceptable`): a breach is
  always **critical**.
- **Relative limits** measure degradation against the baseline:
  `(current − baseline) / |baseline|` for minimize metrics and the mirror image
  for maximize metrics. Use `*_relative_increase` for minimize metrics and
  `*_relative_decrease` for maximize metrics. Using the wrong one is a load error.
  A breach means strictly greater than the threshold.
- **Zero baseline**: if `|baseline| ≤ 1e-12`, relative change is undefined. Only
  absolute limits are applied, and the result records a finding that says so.
- Overall severity is the worst metric severity. The primary metric is labelled
  in the result. Other configured metrics are guardrails and count the same way.

## Evidence validation

These checks run before any threshold. If any fails, the result is
`invalid_evidence`. No performance result is produced, an *evidence* incident is
opened, and the proposed action is `notify`. Invalid evidence never leads to
retraining.

| Code | Meaning |
|---|---|
| `UNKNOWN_MODEL` | no policy for this `model_name` |
| `EMPTY_EVALUATION` / `INSUFFICIENT_SAMPLES` | `sample_count` is 0 or below the minimum |
| `TIMESTAMP_IN_FUTURE` | more than 5 minutes ahead of the server clock |
| `METRIC_NOT_FINITE` | NaN or ±Infinity |
| `METRIC_OUT_OF_RANGE` | outside the metric's mathematical range, e.g. MAE < 0 or R² > 1 |
| `METRIC_MISSING` | a metric configured in the policy was not sent |
| `METRIC_INCONSISTENT` | `rmse² ≠ mse`, or `mae > rmse` |
| `BASELINE_MISSING` | `require_baseline` is on and no comparable baseline exists |

## Decisions

| Severity | Proposed action |
|---|---|
| none | `none` (decision recorded, no incident) |
| warning | `notify` |
| critical | `request_retraining_approval` only if `defaults.retraining_enabled` **and** the model's `retraining.allowed` are true. Otherwise `investigate` |
| invalid_evidence | `notify` |

These are *proposals*. A `request_retraining_approval` starts the two-gate workflow
([workflow.md](workflow.md)). Nothing is executed without separate human approval
for retraining and for deployment: `require_human_*_approval` and
`require_candidate_evaluation` accept only `true`.

## Pipelines, release targets and remediation settings

```yaml
defaults:
  approval_ttl_hours: 24            # pending approvals expire after this
  require_distinct_approvers: false # true: Gate B approver must differ from Gate A's
pipelines:                          # allow-listed training jobs (docs/training-adapters.md)
  my_retrain: {runner: external, training_dataset: {dataset_id: d, dataset_version: v},
               config: {max_iter: 300}, timeout_minutes: 120}
release_targets:                    # allow-listed release targets (docs/deployment.md)
  champion_alias: {adapter: mock}
models:
  my_model:
    retraining: {allowed: true, pipeline_id: my_retrain, cooldown_hours: 24,
                 max_attempts_per_incident: 1}
    deployment: {allowed: true, target: champion_alias, min_primary_improvement: 0.0}
```

The loader rejects all of the following:
- an `allowed: true` without a pipeline or target
- a reference to an undefined pipeline or target
- `deployment.allowed` without `retraining.allowed`
- unknown runner or adapter names
- any unknown key, such as a `command`

Start from `configs/my-model.example.yaml`.

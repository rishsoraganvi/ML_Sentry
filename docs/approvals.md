# Human approvals

Nothing that changes a model happens without two independent human decisions:

| Gate | When | What the reviewer sees | What approval authorizes |
|---|---|---|---|
| **A: retraining** | Critical, valid evidence, and the policy allows retraining | Incident evidence (severity, findings, metrics, baseline, dataset, sample count); pipeline id, runner, training dataset, config, timeout; the champion's holdout; policy version; expiry | Exactly one training job with exactly that spec |
| **B: deployment** | The candidate passed every gate | Candidate and champion refs and metrics; holdout and protocol; all gate results; release target, adapter and alias; verification and rollback plan; policy version; expiry | Exactly one release of exactly that candidate to that target |

## How to decide

**UI.** Open `http://127.0.0.1:8000/ui/` and enter your personal reviewer token. The
token is kept only in that browser tab. Open the request, read the full spec and
press **Approve exactly this** or **Reject**.

**API.**

```bash
curl -H "Authorization: Bearer $REVIEWER_TOKEN" http://127.0.0.1:8000/api/v1/approvals/pending
curl -X POST -H "Authorization: Bearer $REVIEWER_TOKEN" -H "Content-Type: application/json" \
  -d '{"decision":"approve","spec_digest":"<spec_digest from the request>","note":"looks right"}' \
  http://127.0.0.1:8000/api/v1/approvals/<approval_id>/decision
```

You must send back the `spec_digest` you reviewed. If the pending spec is not what
you saw, the decision is refused with `SPEC_DIGEST_MISMATCH`.

## Reviewers

```bash
docker compose exec api ml-sentry add-reviewer alice
```

This prints a one-time token for Alice and an entry `alice:<sha256>`. Put the entry
into `ML_SENTRY_REVIEWERS` (comma-separated) and restart with `docker compose up -d`.
Only hashes are configured. To revoke a reviewer, remove their entry and restart.
Approvals they gave that haven't been acted on yet then **stop working**
(`APPROVER_NOT_AUTHORIZED`).

## Rules enforced

- **Only reviewers decide.** The service and runner tokens get `403`. Notifications
  and LLMs can't approve anything.
- **One decision per request.** A second decision gets `409 APPROVAL_ALREADY_DECIDED`,
  even under concurrency.
- **Expiry.** Requests expire after `defaults.approval_ttl_hours` (default 24). The
  worker marks them `expired` and the workflow ends without acting. Late decisions
  get `409 APPROVAL_EXPIRED`.
- **Re-validation before acting.** Immediately before creating the job or releasing,
  the approval is checked again: status `approved`, decided before expiry, reviewer
  still configured, same policy version, and the **same spec digest** recomputed
  from current state. Any difference blocks the action (`POLICY_CHANGED`,
  `SPEC_CHANGED`, …).
- **Distinct approvers.** With `defaults.require_distinct_approvers: true`, the
  deployment approver must differ from the retraining approver.
- **Kill switches.** External runners and non-mock release targets additionally need
  `ML_SENTRY_RETRAINING_ENABLED=true` / `ML_SENTRY_DEPLOYMENT_ENABLED=true`.
- **Audit.** Every decision is in the incident's audit timeline: `approval.requested`,
  `approval.decided` or `approval.expired`.

Notifications link to the UI on `ML_SENTRY_PUBLIC_URL`. That address is local by
default, so remote reviewers need a secure way in (VPN, SSH tunnel or an HTTPS
proxy with authentication).

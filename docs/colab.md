# Using ML Sentry when your model runs in Google Colab

Colab notebooks run on Google's machines. ML Sentry runs on yours (or on a server
you control). The governance layer stays outside Colab:

```
 Google Colab (your notebook)                    Your machine (Docker Compose)
 ───────────────────────────                     ──────────────────────────────
 train / evaluate model                          ML Sentry api + worker + Postgres
 ml_sentry_client.submit_evaluation()  ──HTTPS──▶  validate → policy → incident
                                                 → approval request (UI / Slack…)
                                                          ▲
                                       you approve in the UI on your machine
 ml_sentry_client.run_jobs(train_fn)  ◀──HTTPS──  approved job (exact spec)
 retrain with approved config,
 evaluate on the SAME holdout        ──HTTPS──▶  candidate gates → 2nd approval
                                                 → release → verify → audit
```

Colab only ever holds the **service** token (to send evaluations) and the **runner**
token (to run approved jobs). **Approvals happen only in the ML Sentry UI on your
side.** Never put a reviewer token in a notebook.

Ready-made notebook:
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/rishsoraganvi/ML_Sentry/blob/main/examples/colab/ml_sentry_colab_walkthrough.ipynb)
([`examples/colab/ml_sentry_colab_walkthrough.ipynb`](../examples/colab/ml_sentry_colab_walkthrough.ipynb)).
The badge opens the notebook directly from GitHub. You can also upload the file with
*File → Upload notebook*.

The notebook trains a `HistGradientBoostingRegressor` on California Housing,
registers a baseline, reports a degraded version, and runs the approved retraining
job. The whole flow has been tested end to end against a live stack.

**Using the client in your own notebook.** Download it in a cell:

```python
!wget -q https://raw.githubusercontent.com/rishsoraganvi/ML_Sentry/main/examples/colab/ml_sentry_client.py
from ml_sentry_client import MLSentryClient, regression_metrics
```

## Step 1: Start ML Sentry with a policy for your Colab model

On your machine:

```bash
cp configs/my-model.example.yaml configs/governance.yaml   # edit model name/thresholds later
```

Add to `.env`:

```bash
ML_SENTRY_POLICY_PATH=/app/configs/governance.yaml
ML_SENTRY_RETRAINING_ENABLED=true          # allows the external (Colab) runner
ML_SENTRY_RUNNER_TOKEN=<openssl rand -hex 32>
ML_SENTRY_REVIEWERS=<entry printed by: docker compose exec api ml-sentry add-reviewer YOURNAME>
```

```bash
docker compose up --build -d --wait
docker compose exec api ml-sentry doctor     # policy, reviewers, runner token all PASS/INFO
```

## Step 2: Let Colab reach the API over HTTPS

The API listens on `127.0.0.1:8000`. Colab can't reach that directly, so pick one option.

**A. Temporary tunnel (simplest).** Use [Cloudflare quick tunnels](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/):

```bash
cloudflared tunnel --url http://127.0.0.1:8000
# prints https://<random-words>.trycloudflare.com  → this is ML_SENTRY_URL in Colab
```

`ngrok http 8000` works the same way. Stop the tunnel (Ctrl+C) when you're done.

**B. A server you control.** Run ML Sentry on a VM behind an HTTPS reverse proxy
(Caddy, nginx) with a firewall. Don't publish port 8000 without TLS.

**C. No inbound access (offline).** Have the notebook write evaluation events as
JSON (`json.dump(event, open('event.json','w'))`), download them, and submit
locally:

```bash
python examples/send_evaluation_event.py event.json
```

The external runner needs option A or B. Without them, run retraining on your own
machine with the same client.

### Optional: get alerts away from your desk

Colab jobs and approval requests can arrive while you're away from the terminal. In
that case, send alerts to your phone with Telegram, or to your team with Slack,
instead of the default log output:

```bash
# .env on your machine (not in Colab)
ML_SENTRY_NOTIFICATION_PROVIDER=telegram
ML_SENTRY_TELEGRAM_BOT_TOKEN=123456:ABC...
ML_SENTRY_TELEGRAM_CHAT_ID=987654321
```

Then run `docker compose up -d` to apply it.

Alerts link to your ML Sentry UI, so set `ML_SENTRY_PUBLIC_URL` to an address you
can open. Opening the link still requires your reviewer token, and replying to an
alert never approves anything.

All platforms and setup steps:
[notifications.md → Choosing your alert channel](notifications.md#choosing-your-alert-channel).

## Step 3: Store the secrets in Colab

In Colab, open the 🔑 **Secrets** panel (left sidebar) and add:

| Name | Value |
|---|---|
| `ML_SENTRY_URL` | the tunnel or server URL, e.g. `https://xyz.trycloudflare.com` |
| `ML_SENTRY_TOKEN` | your `ML_SENTRY_API_TOKEN` (service token) |
| `ML_SENTRY_RUNNER_TOKEN` | your `ML_SENTRY_RUNNER_TOKEN` |

Enable *Notebook access* for each one. The notebook reads them with
`google.colab.userdata.get(...)`, so they never appear in the notebook itself.

## Step 4: Run the notebook

1. **Load the client.** Run the `%%writefile ml_sentry_client.py` cell. It contains
   the standard-library client, so nothing needs installing.
2. **Register the champion.** The training and baseline cells register the
   champion's evaluation on a fixed holdout (`california-housing @ v1-seed42`,
   protocol `holdout-20pct-v1`).
3. **Report a regression.** The monitoring cell reports a weak model. ML Sentry
   answers `critical -> request_retraining_approval`.
4. **Approve retraining, on your machine.** Open `http://127.0.0.1:8000/ui/`, enter
   your **reviewer** token, inspect the retraining request and approve it.
5. **Let the notebook retrain.** The `run_jobs` cell, which was waiting, claims the
   job, retrains with the **approved** config, evaluates on the same holdout and
   reports the candidate.
6. **Approve the deployment.** If the candidate passes every gate, a deployment
   approval appears. Approve it, and ML Sentry releases to the configured target
   and verifies it. The incident becomes `resolved`.

## Adapting the notebook to your own model

Change the following, keeping the event fields the same:
- **`MODEL_NAME`.** Match the model name in your policy.
- **The data cell.** Load your data and **fix the holdout**: same rows, same
  preprocessing. Version it in `HOLDOUT["dataset_version"]`.
- **`evaluate(model)`.** Return `regression_metrics(y_true, y_pred)`, or your own
  metrics using the same names.
- **`train_and_evaluate(spec)`.** Read hyperparameters only from `spec["config"]`
  (the approved, allow-listed config in your policy). Save the model somewhere
  durable, such as Google Drive, GCS or the MLflow Model Registry, and return that
  reference as `candidate_model_ref`.

If you log to MLflow from Colab, you can call `sentry.submit_mlflow_run(run_id)`
instead of `submit_evaluation`. The ML Sentry server must be able to reach your
MLflow server, and the experiment must be in `ML_SENTRY_MLFLOW_ALLOWED_EXPERIMENTS`.
See [mlflow-integration.md](mlflow-integration.md).

## Colab specifics

- **Sessions end.** Colab disconnects idle sessions. If the runtime restarts, re-run
  the setup cells and then `run_jobs`. A claimed job has a deadline
  (`timeout_minutes` in the pipeline). If Colab dies mid-job, the job times out,
  the incident ends as `training_failed`, and nothing is released.
- **Waiting for approval.** `run_jobs(wait_seconds=600)` waits 10 minutes for an
  approved job. Increase it, or re-run the cell after approving.
- **Re-running cells.** Event IDs include a run ID. Re-sending the same event is
  harmless (idempotent). A new run ID creates new records.
- **Clock.** Colab's clock is accurate. Events more than 5 minutes in the future are
  rejected as invalid evidence.

## Security checklist

- [ ] Tunnel or HTTPS only. Never plain HTTP over the internet.
- [ ] Colab holds only the service and runner tokens, in **Secrets**, never in cells.
- [ ] Reviewer tokens stay with the people who approve.
- [ ] Stop the tunnel when you're not using it. Anyone with the URL can reach the
      login-free UI page, although every action needs a token.
- [ ] Rotate tokens if a notebook with outputs was shared. Clear outputs before sharing.
- [ ] Keep `ML_SENTRY_DEPLOYMENT_ENABLED=false` unless a real release target is configured.

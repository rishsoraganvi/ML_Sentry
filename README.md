# ML Sentry

Self-hosted governance for supervised **regression** models.

You send ML Sentry your evaluation results, and it governs what happens next:
- It **validates** the evidence and applies an explicit, version-controlled **policy**.
- It **opens an incident** when quality regresses and **notifies** the owner.
- It asks a human to **approve retraining** before any job starts.
- It **compares the candidate with the champion** on the same fixed holdout.
- It asks for a **second, separate approval** before releasing, then **verifies** the
  release and rolls it back if verification fails.
- It **audits** every step.

```
Install → connect evaluation source → validate metrics → detect incident → notify owner
→ approve retraining → train/evaluate candidate → approve release → verify → audit
```

**Status: v0.1.0.** All plan milestones (0–7) are implemented and tested. Image
publishing is prepared but not yet done (see [Release status](#release-status)).

## What it does and does not do

| Does | Does not |
|---|---|
| Accept evaluations over an authenticated HTTP API, or read them from MLflow runs | Discover, import or execute your models or training code |
| Validate metrics (finite, in range, consistent), sample counts, timestamps, baselines | Use an LLM for any decision |
| Apply absolute and relative thresholds from YAML; fail closed on any problem | Retrain or deploy without two explicit human approvals |
| Run a durable LangGraph workflow per incident (PostgreSQL checkpoints, resumable) | Treat a notification or an LLM output as an approval |
| Hand approved training jobs to **your** pipeline (e.g. Colab), or simulate them (mock) | Accept commands, scripts or artifact paths from events or API calls |
| Gate candidates against the champion on the same holdout | Replace your experiment tracker or serving stack |
| Release via a mock alias or an MLflow Model Registry alias, then verify or roll back | Claim that a registry alias change is a deployment (your serving must follow it) |

## Quickstart

You need Docker with Compose v2. No local Python is required.

```bash
git clone https://github.com/rishsoraganvi/ML_Sentry.git && cd ML_Sentry
cp .env.example .env        # set POSTGRES_PASSWORD and ML_SENTRY_API_TOKEN (see the file)
docker compose up --build -d --wait
docker compose exec api ml-sentry doctor
docker compose exec api ml-sentry demo
```

On Windows cmd, use `copy .env.example .env` instead of `cp`.

To try the **full two-approval loop** safely, use the mock runner and mock target:

```bash
docker compose exec api ml-sentry add-reviewer alice   # put the printed entry in ML_SENTRY_REVIEWERS
docker compose up -d                                   # reload the configuration
docker compose exec api ml-sentry demo --full
# open http://127.0.0.1:8000/ui/ , enter alice's token, approve twice
```

See [docs/quickstart.md](docs/quickstart.md).

## Use it with your own model

Start with **[docs/your-model.md](docs/your-model.md)**. It covers three integration paths:

| Your setup | Path |
|---|---|
| Any Python/HTTP evaluation job | POST evaluation events ([generic API](docs/generic-api-integration.md)) |
| You log evaluations to MLflow | Point ML Sentry at the run ([MLflow](docs/mlflow-integration.md)) |
| Your model lives in **Google Colab** | [docs/colab.md](docs/colab.md) and the ready-made notebook: [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/rishsoraganvi/ML_Sentry/blob/main/examples/colab/ml_sentry_colab_walkthrough.ipynb) |

## Choosing where alerts go (Slack, Telegram, webhook…)

ML Sentry keeps two separate records of what happens:

- **The audit trail** is always stored in ML Sentry's database, whichever alert
  platform you choose. You view it in the UI (incident → *Audit timeline*) or with
  `GET /api/v1/incidents/{id}/audit`. It cannot be turned off.
- **Alerts** are push messages telling people that an approval is needed or that
  an incident has an outcome. You choose **one** platform for them with
  `ML_SENTRY_NOTIFICATION_PROVIDER` in `.env`.

| If you… | Set `ML_SENTRY_NOTIFICATION_PROVIDER=` | You also set | Setup |
|---|---|---|---|
| are trying ML Sentry out, or work alone at the terminal | `console` (default) | nothing | Messages appear in `docker compose logs -f api worker` |
| work in a **Slack** workspace | `slack` | `ML_SENTRY_SLACK_WEBHOOK_URL` | ~2 min: create an Incoming Webhook ([steps](docs/notifications.md#slack)) |
| want alerts **on your phone**, or don't use Slack | `telegram` | `ML_SENTRY_TELEGRAM_BOT_TOKEN`, `ML_SENTRY_TELEGRAM_CHAT_ID` | ~3 min: make a bot with @BotFather ([steps](docs/notifications.md#telegram)) |
| use **Teams, Discord, email, PagerDuty, Jira, n8n/Zapier**, or want **several** platforms | `webhook` | `ML_SENTRY_WEBHOOK_URL`, `ML_SENTRY_WEBHOOK_SECRET` | Point it at your relay or automation and verify the signature ([steps](docs/notifications.md#webhook-anything-else)) |
| want **no push messages** (audit trail only) | `none` | nothing | — |

For example, to send alerts to Slack:

```bash
# .env
ML_SENTRY_NOTIFICATION_PROVIDER=slack
ML_SENTRY_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
```

Apply the change and send a test alert:

```bash
docker compose up -d
docker compose exec api ml-sentry doctor
curl -X POST -H "Authorization: Bearer $ML_SENTRY_API_TOKEN" http://127.0.0.1:8000/api/v1/admin/test-notification
```

`doctor` shows which provider is active, and the test alert confirms that it works.

**Good to know:**
- **No approval by message.** An alert only links to the approval UI. Nobody can
  approve anything by replying to it.
- **Failures don't change decisions.** A failed delivery is recorded with an error
  code and never changes a decision.
- **No secrets in alerts.** Messages never contain secrets or data rows.
- **Links.** Set `ML_SENTRY_PUBLIC_URL` to an address your reviewers can open, so the
  links in alerts work.

Full guide: **[docs/notifications.md](docs/notifications.md)**. It covers choosing a
platform, setup steps for each one, the delivery guarantees, and how to verify webhook
signatures.

## Documentation

- **Getting started:** [Quickstart](docs/quickstart.md) · [Your model](docs/your-model.md) · [Google Colab](docs/colab.md) · [Choosing an alert platform](docs/notifications.md#choosing-your-alert-channel) · [Configuration](docs/configuration.md)
- **Concepts:** [Regression policies](docs/regression-policies.md) · [Governance workflow](docs/workflow.md) · [Approvals](docs/approvals.md) · [Architecture](docs/architecture.md)
- **Integrations:** [Generic API](docs/generic-api-integration.md) · [MLflow](docs/mlflow-integration.md) · [Training runners](docs/training-adapters.md) · [Release targets](docs/deployment.md) · [Notifications](docs/notifications.md)
- **Operations:** [Security](SECURITY.md) · [Troubleshooting](docs/troubleshooting.md) · [Backup & restore](docs/backup-restore.md) · [Upgrade & uninstall](docs/upgrade-uninstall.md)
- **Project:** [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

## Security and limitations

- **Network:** ports bind to `127.0.0.1`. Exposing the API (for example to Colab)
  needs TLS and care; see [docs/colab.md](docs/colab.md#security-checklist).
- **Tokens:**
  - The **service** token can't approve anything.
  - **Reviewers** use personal tokens, stored only as hashes.
  - The **runner** token can only claim and report jobs.
- **Deployment:** Docker Compose is a single-host deployment, not a hardened
  multi-tenant platform.
- **Scope:** single-target regression only. Example thresholds are placeholders.

## Release status

The source install above is complete and tested. The image-based install
(`docker-compose.release.yml`) and the publish workflow are ready, but no image has
been published yet.

Pushing a `v0.1.0` tag to GitHub runs CI and then publishes
`ghcr.io/rishsoraganvi/ml-sentry:0.1.0`. Until that image exists and has been
tested, use the source install.

When the image is available:

```bash
mkdir ml-sentry && cd ml-sentry
curl -fsSLO https://raw.githubusercontent.com/rishsoraganvi/ML_Sentry/main/docker-compose.release.yml
curl -fsSL https://raw.githubusercontent.com/rishsoraganvi/ML_Sentry/main/.env.example -o .env
# edit .env, then:
docker compose -f docker-compose.release.yml up -d --wait
```

PowerShell:

```powershell
iwr https://raw.githubusercontent.com/rishsoraganvi/ML_Sentry/main/docker-compose.release.yml -OutFile docker-compose.release.yml
```

## Support

- **Questions and bugs:** [GitHub Issues](https://github.com/rishsoraganvi/ML_Sentry/issues).
- **Security reports:** see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © 2026 Rishabh Soraganvi and ML Sentry contributors.

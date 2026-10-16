# Notifications

Notifications inform. They never approve anything: they link to the approval UI.

## Alerts and audits are two different things

| | Alerts (notifications) | Audit trail |
|---|---|---|
| **What** | Push messages: "approval needed", "incident outcome" | The complete, append-only record of every step: evidence, decisions, approvals, jobs, releases |
| **Where** | **One** channel you choose: console, Slack, Telegram or webhook | Always in ML Sentry's PostgreSQL database, whichever channel you pick |
| **How you view it** | In that channel | Reviewer UI (`/ui/` → incident → *Audit timeline*), `GET /api/v1/incidents/{id}/audit`, or database backups |
| **Can it be turned off?** | Yes (`none`) | No |

Choosing Slack or Telegram only changes **where alerts are pushed**. The audit trail
is identical in every case. To copy alerts into another system (SIEM, ticketing,
email), use the `webhook` provider.

## Choosing your alert channel

Pick the channel your team already watches:

| If you… | Choose | Setup effort |
|---|---|---|
| are trying it out, or work alone at the terminal | `console` (default) | none; messages appear in `docker compose logs -f api worker` |
| work in a Slack workspace | `slack` | ~2 min: create an Incoming Webhook for a channel and paste its URL |
| want alerts on your phone, or don't use Slack | `telegram` | ~3 min: create a bot with @BotFather, get your chat ID |
| use Teams, Discord, email, PagerDuty, Jira, n8n/Zapier, or need **several** channels | `webhook` | point it at your own relay or automation; verify the signature |
| want no push messages at all (audit only) | `none` | none |

Only **one** provider is active at a time. To reach several destinations, use
`webhook` and fan out from your automation tool.

### Comparing the platforms

| | `console` | `slack` | `telegram` | `webhook` | `none` |
|---|---|---|---|---|---|
| Where messages appear | api/worker logs | One Slack channel | One Telegram chat or group | Any HTTPS endpoint you run or subscribe to | Nowhere |
| Reaches your phone | No | Yes (Slack app) | Yes (Telegram app) | Depends on the receiver | No |
| Several people | Whoever reads the logs | Everyone in the channel | Everyone in the group | Anything your relay supports | – |
| Several platforms at once | No | No | No | **Yes**, by fanning out from the receiver | – |
| Needs an account or app | No | Slack workspace + app | Telegram bot | Your own endpoint or automation service | No |
| Where the data goes | Stays on your machine | Slack's servers | Telegram's servers | Wherever you send it | Stays on your machine |
| Message is signed | – | No (secret URL) | No (secret bot token) | **Yes** (HMAC-SHA256) | – |
| Settings | none | 1 | 2 | 2 | none |

**How to decide:**
1. **Already using Slack?** Use `slack`.
2. **Want phone alerts without Slack?** Use `telegram`.
3. **Need any other platform, more than one, or a ticket or page?** Use `webhook`.
4. **Just trying ML Sentry out?** Keep `console`.

Data policy matters too: Slack and Telegram store message contents on their servers.
ML Sentry messages contain only model names, metrics summaries and links, never data
rows or secrets. Still, use `webhook` to an internal endpoint if no third party may
receive them.

### Other platforms through `webhook`

These platforms expect their own message formats, so put a small relay between ML
Sentry and the platform. The relay receives ML Sentry's signed JSON, verifies it,
and posts in the platform's format. A no-code automation tool (n8n, Zapier, Make,
Power Automate) or a few lines of your own code work as relays.

| Destination | Relay does |
|---|---|
| **Microsoft Teams** | Post `summary` and `link` to a Teams channel, e.g. a Power Automate "post message in a chat or channel" flow triggered by HTTP |
| **Discord** | POST `{"content": "<summary>\n<link>"}` to a Discord channel webhook URL |
| **Email** | Send an email with subject `"[ML Sentry] " + kind + " " + model_name` and body `summary` + `link` |
| **PagerDuty / Opsgenie** | Create an incident for `kind == "approval_request"`, or for `severity == "critical"` |
| **Jira / GitHub Issues** | Open a ticket per `incident_id`, deduplicated on that ID |
| **Several at once** | Do any combination of the above from one relay |

ML Sentry's JSON fields are listed under [Webhook signature](#webhook-signature).
Always verify the signature before acting on a message.

### Slack

1. In Slack, go to <https://api.slack.com/apps> → *Create New App* → *From scratch*.
2. Open *Incoming Webhooks* → turn it on → *Add New Webhook to Workspace* → choose a
   channel (e.g. `#ml-alerts`).
3. Copy the `https://hooks.slack.com/services/...` URL into `.env`:
   ```bash
   ML_SENTRY_NOTIFICATION_PROVIDER=slack
   ML_SENTRY_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
   ```

### Telegram

1. In Telegram, message **@BotFather** → `/newbot` → copy the bot token.
2. Send any message to your new bot, or add it to a group.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   `chat.id`. Group IDs are negative numbers.
4. Add to `.env`:
   ```bash
   ML_SENTRY_NOTIFICATION_PROVIDER=telegram
   ML_SENTRY_TELEGRAM_BOT_TOKEN=123456:ABC...
   ML_SENTRY_TELEGRAM_CHAT_ID=987654321
   ```

### Webhook (anything else)

1. Create an HTTPS endpoint that accepts JSON: your own service, an n8n/Zapier/Make
   workflow, or a Teams/Discord relay.
2. Generate a shared secret: `openssl rand -hex 32`.
3. Add to `.env`:
   ```bash
   ML_SENTRY_NOTIFICATION_PROVIDER=webhook
   ML_SENTRY_WEBHOOK_URL=https://automation.example.com/ml-sentry
   ML_SENTRY_WEBHOOK_SECRET=<the secret>
   ```
4. Verify the signature in your receiver (see [Webhook signature](#webhook-signature)).

### Apply and test

```bash
docker compose up -d                       # restart api + worker with the new settings
docker compose exec api ml-sentry doctor   # shows the active provider
curl -X POST -H "Authorization: Bearer $ML_SENTRY_API_TOKEN" http://127.0.0.1:8000/api/v1/admin/test-notification
```

The test returns `{"delivered": true}` or a short error code, for example `HTTP_404`
for a wrong webhook URL, or `TELEGRAM_NOT_OK` for a wrong chat ID.

Make sure `ML_SENTRY_PUBLIC_URL` is an address your reviewers can open, so the links
in messages work.

## Reference

| `ML_SENTRY_NOTIFICATION_PROVIDER` | Needs | Behaviour |
|---|---|---|
| `console` (default) | – | Writes to the api/worker logs at WARNING level |
| `none` | – | Records deliveries as `skipped` |
| `slack` | `ML_SENTRY_SLACK_WEBHOOK_URL` (https) | [Incoming webhook](https://api.slack.com/messaging/webhooks) `{"text": ...}` |
| `telegram` | `ML_SENTRY_TELEGRAM_BOT_TOKEN`, `ML_SENTRY_TELEGRAM_CHAT_ID` | [Bot API `sendMessage`](https://core.telegram.org/bots/api#sendmessage) |
| `webhook` | `ML_SENTRY_WEBHOOK_URL` (https), `ML_SENTRY_WEBHOOK_SECRET` | Signed JSON POST (below) |

A missing setting for the chosen provider stops startup with a message naming it.
To send an explicit test message:

```bash
curl -X POST -H "Authorization: Bearer $ML_SENTRY_API_TOKEN" http://127.0.0.1:8000/api/v1/admin/test-notification
```

## Messages

| Kind | When |
|---|---|
| `approval_request` | Gate A or Gate B was requested (one per gate) |
| `incident` | The workflow reached its outcome (one per incident) |

Each message carries the incident ID, severity, model and version, status, proposed
action, a short summary or outcome, and a link (`ML_SENTRY_PUBLIC_URL/ui/#...`).
Messages never include secrets, datasets, prediction rows or stack traces.

## Delivery guarantees

- **At most once per kind.** Each (incident, kind) is sent at most once (key
  `incident_id:kind`), even when workflow steps re-run.
- **Retries.** Up to 3 attempts with exponential backoff.
- **Recorded.** Every delivery is stored with status, attempt count and a short
  error code, and shown under `notifications` in `GET /api/v1/incidents/{id}`.
- **Secrets stay out.** Error codes come from HTTP status codes or exception types
  only, so the Telegram bot token, which is part of its URL, never reaches logs.
- **Decisions are unaffected.** A failed delivery never changes a decision.

## Webhook signature

Each request carries two headers:
- `X-ML-Sentry-Timestamp: <unix seconds>`
- `X-ML-Sentry-Signature: sha256=<hex HMAC-SHA256(secret, timestamp + "." + body)>`

Verify it and reject old timestamps:

```python
import hashlib, hmac, time
def verify(secret: bytes, timestamp: str, body: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature) and abs(time.time() - int(timestamp)) < 300
```

The body is JSON:
`{"type": "ml_sentry.<kind>", "incident_id", "severity", "model_name", "model_version", "status", "proposed_action", "summary", "link", "kind"}`.

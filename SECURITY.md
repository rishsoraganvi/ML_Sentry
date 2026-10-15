# Security

## Reporting

Please report vulnerabilities **privately** through GitHub's private vulnerability
reporting:
<https://github.com/rishsoraganvi/ML_Sentry/security/advisories/new>.
Don't open a public issue for security problems.

The repository owner needs to enable this under
*Settings → Code security → Private vulnerability reporting*.

**Supported versions:** the latest release, currently 0.1.x.

## Safeguards

**Authentication and authorization**
- Three roles with separate tokens (constant-time comparison):
  - **service:** submit and read.
  - **reviewer:** decide approvals. Tokens are personal and stored only as SHA-256 hashes.
  - **runner:** claim and report jobs.
- The service token can't approve. Notifications and LLMs can't approve.
- Weak and placeholder tokens are rejected at startup.

**Approvals**
- Two separate human gates.
- Each approval is bound to an exact spec digest, expires, and is re-validated
  (status, expiry, reviewer still authorized, policy version, digest) immediately
  before a job is created or a model is released.

**Execution**
- No endpoint, event or job accepts commands, code or artifact paths.
- Training runs either as the built-in mock or in the **user's own** pipeline, which
  pulls approved jobs.
- Pipelines and release targets are allow-listed in the policy.
- Non-mock execution additionally requires an environment kill switch.

**Input handling**
- Strict schemas (unknown fields rejected) and a request-body size limit.
- Validation errors don't echo submitted values.
- `report_uri` and model references are stored, never fetched.

**Secrets**
- Secrets live only in environment variables or the env file.
- Notification and MLflow error codes are derived from status codes or exception
  types only, so secrets embedded in URLs (e.g. Telegram) are never logged.

**Containers**
- Non-root user, read-only root filesystem, all capabilities dropped,
  `no-new-privileges`.
- No Docker socket mounted.
- Ports bound to `127.0.0.1`.

**Reviewer UI**
- Strict CSP (`script-src 'self'`, no inline scripts).
- All values rendered as text.
- The token is held only in the tab's `sessionStorage`.

**Audit and supply chain**
- Audit records are append-only: the application layer plus a PostgreSQL trigger.
  This is not cryptographic.
- CI runs a dependency audit, a secret scan and the live-stack tests before images
  are published.

## Known limitations

- **No TLS of its own.** Expose the API only through HTTPS (a tunnel or reverse
  proxy); see [docs/colab.md](docs/colab.md#security-checklist).
- **No SSO or OIDC.** Reviewer identity is a personal bearer token.
- **Single-host deployment.** Docker Compose is not a hardened multi-tenant platform.
- **Registry alias releases** change MLflow aliases only. Serving must follow the alias.

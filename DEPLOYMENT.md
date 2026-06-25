# Deployment

A focused checklist for moving from local dev to a real server. Not a
tutorial — assumes you've deployed a FastAPI + Postgres stack before and
just need the AffixAI-specific bits.

## 1. Pre-flight checklist

Before pointing real users at the app, confirm:

- [ ] **All secrets rotated.** Any Stripe / SMTP / JWT / encryption key that
      ever appeared in dev or a chat transcript needs new values. See `.env`
      template below.
- [ ] **`ENVIRONMENT=production`** set on the server. This triggers the
      startup validation in `app/core/config.py` which refuses to boot with
      placeholder secrets, `DEBUG=true`, localhost SMTP, or
      `EMAIL_FROM=@example.com`.
- [ ] **DNS records** for your domain include `MX`, `SPF`, `DKIM`, and
      `DMARC` records for the addresses listed in
      `LEADS_CONTACT_TO` / `LEADS_CAREERS_TO` / `EMAIL_FROM` (the lead
      forwarding addresses default to `affixai.com` — override these).
- [ ] **Stripe / Paystack / Flutterwave webhook URLs** registered with the
      providers, pointing at `https://yourdomain.com/api/v1/webhooks/...`.
- [ ] **`ALLOWED_ORIGINS`** has no `localhost` entries.
- [ ] **Legal pages** (`/privacy`, `/terms`, `/dpa`) reviewed by counsel.
      Each currently shows a yellow "draft — not legal advice" banner.

## 2. Database migrations

Production must use `aerich`, not `generate_schemas`.

The baseline migration was created with:

```bash
aerich init-db                # already done; do not run again
```

To generate a new migration after editing a model:

```bash
aerich migrate --name describe_the_change
aerich upgrade                # applies pending migrations
```

`register_db(app, generate_schemas=settings.DEBUG)` in `main.py` only
auto-creates tables when `DEBUG=true`. In production it's a no-op — you
must run `aerich upgrade` as part of the deploy pipeline.

## 3. The `.env` file

Required secrets that have no safe defaults — startup fails without them
in production:

```dotenv
# Application
ENVIRONMENT=production
DEBUG=false
APP_VERSION=1.0.0
PORT=8000

# Secrets — generate fresh values with `openssl rand -hex 32`
JWT_SECRET=<32+ char random>
JWT_REFRESH_SECRET=<different 32+ char random>
API_SECRET_KEY=<32+ char random>
INTERNAL_API_KEY=<24+ char random>
ENCRYPTION_KEY=<32+ char random>  # AES-256-GCM key — rotating this breaks
                                  # every vault row that was encrypted with
                                  # the old key

# Database
DATABASE_URL=postgres://user:pass@db.host:5432/affixai

# CORS — your frontend origin(s), NO localhost in prod
ALLOWED_ORIGINS=https://app.affixai.com,https://www.affixai.com

# SMTP — must not be localhost
SMTP_HOST=smtp.sendgrid.net
SMTP_PORT=587
SMTP_USERNAME=apikey
SMTP_PASSWORD=<provider api key>
SMTP_USE_TLS=true
EMAIL_FROM=no-reply@affixai.com   # must NOT end with @example.com
EMAIL_FROM_NAME=AffixAI
FRONTEND_URL=https://app.affixai.com

# Lead forwarding (optional — falls back to hardcoded affixai.com)
LEADS_CONTACT_TO=hello@affixai.com
LEADS_CAREERS_TO=careers@affixai.com

# Super-admin allowlist (emails auto-promoted on register/login)
SUPER_ADMIN_EMAILS=you@yourdomain.com

# AWS — required for document storage
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=<...>
AWS_SECRET_ACCESS_KEY=<...>
AWS_S3_BUCKET=affixai-docs-prod

# Redis (used for caching + workflow scheduler)
REDIS_HOST=redis.host
REDIS_PORT=6379
REDIS_PASSWORD=<...>

# Billing — pick ONE provider, fill its block
PAYMENT_PROVIDER=stripe
BILLING_CURRENCY=USD
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_PRO=price_...
STRIPE_PRICE_ENTERPRISE=price_...

# Error reporting (optional — empty disables Sentry)
SENTRY_DSN=https://<key>@<project>.ingest.sentry.io/<id>
SENTRY_TRACES_SAMPLE_RATE=0.1
```

The frontend reads its config from `import.meta.env.VITE_*` at build time:

```dotenv
VITE_API_URL=https://api.affixai.com
VITE_SENTRY_DSN=https://<key>@<project>.ingest.sentry.io/<id>
VITE_SENTRY_ENV=production
VITE_APP_VERSION=1.0.0
VITE_SENTRY_TRACES_SAMPLE_RATE=0.1
```

## 4. Process management

```
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

`--workers > 1` runs the APScheduler in each worker. For the **workflow
scheduler** (hourly reminders + expiration sweeps) you want exactly one
copy running, so either:

- Use `--workers 1` and scale horizontally by adding instances, OR
- Disable the in-process scheduler on N-1 workers (set
  `WORKFLOW_SCHEDULER_ENABLED=false`) and run it on one dedicated instance.

The lead-form rate limiter is **per-process and in-memory**. A
load-balanced multi-instance setup multiplies the effective rate by the
worker count. For higher-traffic deployments, swap the in-memory deque
in `app/api/routes/leads.py` for a Redis counter.

## 5. Webhook delivery durability

Outgoing webhooks (`webhook_dispatcher.py`) retry with backoff
(5s, 30s, 120s — max 4 attempts) inside an asyncio background task. If the
process restarts mid-retry, that delivery is lost.

For higher durability (financial events, etc.), graduate to a persistent
queue:

1. Persist `WebhookEvent` rows with `payload`, `event_type`, `status`,
   `next_attempt_at`.
2. Replace the `asyncio.create_task` call with an enqueue.
3. Run a worker that polls the queue and calls `_deliver` per row.
4. Add an APScheduler job to re-check stuck rows.

## 6. Backups

- **Postgres**: managed Postgres providers usually do PITR by default.
  Verify retention is ≥7 days. Without managed: nightly `pg_dump` to S3.
- **S3 documents**: enable versioning on the bucket. Lifecycle policy to
  Glacier for objects older than 90 days if cost is a concern.
- **Encryption key**: store `ENCRYPTION_KEY` in a secret manager
  (AWS Secrets Manager / 1Password Vault). If you lose this, every vault
  row is unreadable.

## 7. Health checks

- `GET /health/` — returns 200 with service status (OCR, Redis, S3).
- For uptime monitoring, hit this every 30s. The endpoint is fast and
  doesn't touch the database — fine for a load-balancer probe.

## 8. First-launch smoke test

After the first deploy, click through:

1. Register a new user. Confirm verification email lands.
2. Save a vault entry. Reload — confirm it persists.
3. Upload a PDF, auto-affix, download — confirm S3 read/write.
4. Test webhook delivery via Settings → Webhooks → "Send test event".
5. Trigger a Sentry test event:
   `curl https://api.affixai.com/api/v1/admin/users -H "Authorization: Bearer <bad>"` —
   should 401 (not 500). Then break something on purpose and confirm it
   shows up in Sentry.
6. Check `/admin/leads` after submitting a `/contact` form.

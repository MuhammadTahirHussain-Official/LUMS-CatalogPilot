# CatalogPilot

One home for a seller's product catalog: upload it from a spreadsheet, clean it up with
reviewable AI suggestions, and publish it to stores and marketplaces with a preview, an
approval step, an audit trail and rollback.

This branch is a working first version of the platform described in the eight
specification documents (SRS, architecture, database, API, security, AI, testing, roadmap).
It runs on one machine with nothing to install beyond Python.

**Moving a store:** the "Move a store" wizard reads products from one store (practice
store, Shopify or WooCommerce), suggests improvements based on a few questions, and
publishes to another store, with a checkpoint after every page so a crash never restarts
or duplicates anything. Full documentation is in [`documentation/`](documentation/README.md);
start with [known gaps](documentation/08-known-gaps.md) before using it on a real store.

## Run it locally

Requirements: Python 3.12 or newer. No Docker, database server or Node.js needed.

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -r backend/requirements-dev.txt
```

```bash
.venv/Scripts/python -m uvicorn catalogpilot.api.app:app --app-dir backend --port 8000 --no-server-header
```

Open http://localhost:8000 and create an account. To fill it with sample products and a
sandbox store in one go (while the server is running):

```bash
.venv/Scripts/python scripts/seed_demo.py
```

The seeded account's login is written to `var/dev-login.txt` (never committed).
On macOS/Linux use `.venv/bin/python` instead of `.venv/Scripts/python`.

### Try the whole flow

1. **Uploads** → choose `samples/products.csv` → check the column mapping → **Import products**.
2. **Catalog** → **Suggest improvements** → **Review** the suggestions (approve, edit or reject).
3. **Channels** → **Add channel** → *Sandbox store* → connect it with any token of 12+ characters.
4. **Publishing** → **Publish products** → read the preview → **Approve and publish** → watch progress.
5. On the finished job choose **Roll back…** to undo it. **Activity** shows the audit trail.

### Tests

```bash
cd backend
```

```bash
../.venv/Scripts/python -m pytest
```

61 tests cover tenant isolation, store-to-store migration at 1,500 products, the crash/takeover guarantees (no duplicates, no lost
items, no zombie writes), credential handling, the upload attack corpus, AI guardrails and
the HTTP API end to end.

Re-fit the AI confidence calibration (writes `backend/catalogpilot/ai/calibration.json`):

```bash
../.venv/Scripts/python -m catalogpilot.ai.train
```

## What is built

| Area | State |
| --- | --- |
| Accounts, sessions (CSRF), TOTP two-step login, recovery codes, step-up, roles, invitations, API keys | Working, tested |
| Tenant isolation in the ORM (automatic filter + write guard) | Working, tested |
| Upload: resumable chunks, checksums, type detection by content, scanning, quarantine | Working, tested against an attack corpus |
| Parsers: CSV/TSV (encoding + delimiter detection), XLSX, JSON, NDJSON, XML (no DTD/XXE) | Working, tested |
| Column mapping suggestions, templates, per-row errors, error file, re-import without duplicates | Working, tested |
| Hosted catalog with versions, per-product diffs, restore, CSV/XLSX export with formula neutralization | Working, tested |
| Publish engine: preview/diff, approval, leases, fencing tokens, idempotent writes, retries with jitter, dead-letter, poison quarantine, circuit breaker, rate limiting, pause/resume/cancel, rollback | Working, tested with injected crashes |
| Credential vault: write-only intake, envelope encryption, task-bound leases, usage log, crypto-shred | Working, tested |
| AI: title / description / attribute / category suggestions, validators, calibrated confidence, policy auto-approval, review | Working (rules provider), tested |
| Tamper-evident audit log with verification | Working, tested |
| Web app (no build step) | Working, checked by hand in a browser |
| Connectors: Sandbox store, CSV feed | Working |
| Connectors: Shopify, WooCommerce | **Written from the API documentation, not yet run against a live store** |
| PostgreSQL row-level security (`python -m catalogpilot.rls`) | **Generated, not yet run against a real PostgreSQL server** |

## Deliberate differences from the specification

- **Lease table instead of Temporal.** The spec's own fallback (ADR-03) is used: handlers
  claim work through leases with fencing tokens stored in the database. Same guarantees,
  no workflow cluster to operate. Temporal can replace the claim loop later.
- **SQLite for development, PostgreSQL for production.** Production refuses to start on
  SQLite, without a master key, or with non-secure cookies.
- **Web app is plain HTML/CSS/JS** served by the API on the same origin instead of
  Next.js: nothing to install or build, a strict Content-Security-Policy with no inline
  script, and no cross-origin cookie handling.
- **Variants and media are stored on the product row** (JSON) rather than in their own
  tables. Simpler now; worth splitting out before catalogs reach millions of variants.
- **AI is rules-first.** The default provider is deterministic and sends nothing off the
  machine. A hosted model (Claude, via the official SDK) can be enabled with
  `CP_AI_PROVIDER=anthropic` and `CP_ANTHROPIC_API_KEY`, and only for organizations that
  opt in. Its confidence is capped and never auto-approved until it has its own calibration.
- **Titles in all capitals are never auto-approved.** The benchmark showed re-casing is
  only ~90% right ("IPHONE" could be "iPhone"), below the 98% bar, so those go to review.

## Not built yet

Continuous sync and drift detection, importing from a connected store, marketplace
connectors (Amazon, eBay, Etsy, Google, Meta), SSO/SCIM, passkeys, billing and plans,
real email delivery (messages go to `var/outbox/` in development), a commercial malware
engine (ClamAV is supported through `CP_CLAMD_HOST`; the built-in scanner is a baseline),
S3 object storage, Kubernetes deployment, load tests at 100,000 products, an external
penetration test and SOC 2. The benchmark that calibrates AI confidence is synthetic; a
real labeled set is needed before trusting it on real catalogs.

## Configuration

Every setting is an environment variable with the `CP_` prefix (see
`backend/catalogpilot/config.py`). The important ones for production:

| Variable | Purpose |
| --- | --- |
| `CP_ENV=production` | Turns on the production safety checks |
| `CP_DATABASE_URL` | PostgreSQL URL, e.g. `postgresql+psycopg://user:pass@host/db` |
| `CP_MASTER_KEY_B64` | 32-byte base64 master key for the credential vault (keep it in a KMS/secret manager) |
| `CP_COOKIE_SECURE=true` | Required behind HTTPS |
| `CP_CLAMD_HOST` | ClamAV daemon for malware scanning |
| `CP_WORKER=0` | Run the API without the in-process publish handler (run handlers separately) |

## Layout

```text
backend/catalogpilot/
  api/          HTTP layer: routes, auth, CSRF, rate limits, error format
  identity/     accounts, sessions, MFA, API keys, members
  security/     password hashing, envelope encryption, audit chain, roles, SSRF guard
  datahub/      upload, scan, parse, map, import, export
  catalog/      product model, cleaning rules, versions
  engine/       jobs, transform/validate/diff, publish handler
  vault/        credential broker
  connectors/   sandbox, CSV feed, Shopify, WooCommerce
  ai/           tasks, validators, gateway, confidence, calibration
backend/tests/  test suite
frontend/       web app (static files)
samples/        example product file
scripts/        demo seeding
```

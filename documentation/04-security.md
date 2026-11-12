# 04 · Security

## What is protected and how

| Asset | Protection | Where |
| --- | --- | --- |
| Store keys | Encrypted with a key of their own, which is wrapped by a per-organization key. Bound to the organization and credential, so a copied ciphertext cannot be decrypted elsewhere. Never returned by any endpoint. Destroyed on disconnect. | `security/crypto.py`, `vault/broker.py` |
| Use of store keys | Released only to a work item that is currently leased, for one store and one kind of operation. A handler with a stale lease is refused. Every release and refusal is logged. | `vault/broker.py` |
| Passwords | Argon2id with a server-side pepper; 12+ characters; common passwords and ones containing the email or product name are refused | `security/passwords.py` |
| Sessions | Random 256-bit id stored only as a keyed hash; HttpOnly cookie; idle and absolute timeouts; every state change needs a CSRF token and a trusted origin | `identity/service.py`, `api/deps.py` |
| Sign-in | Same response whether or not an account exists; temporary lock after repeated failures; per-address and per-account rate limits | same |
| Two-step sign-in | Authenticator codes (each works once) and ten single-use recovery codes | same |
| Sensitive actions | Changing keys, roles, policies, approving high-impact publishes require re-confirming identity within 5 minutes | `security/authz.py` |
| Organization data | Every query is filtered by organization inside the data layer and every write is checked; another organization's objects answer "not found" | `db.py` |
| Cost prices | Hidden from, and unwritable by, roles without the cost permission | `api/routes.py` |
| Uploaded files | Held in quarantine until type (by content), threat signatures, archive safety and image limits pass | `datahub/scan.py` |
| Data from other stores | Treated as untrusted: cleaned, sanitized, bounded per product | `catalog/normalize.py` |
| AI | Receives only the fields a task needs (never prices, cost, stock, barcodes, emails or phone numbers); its output is validated before it can become a suggestion | `ai/tasks.py` |
| Audit log | Each record is chained to the previous one; edits and deletions are detectable | `security/audit.py` |
| Outbound requests | HTTPS only, private and internal addresses refused, no redirects followed | `security/netguard.py`, `connectors/http.py` |
| Browser | Content-security policy with no inline script or style and no third-party origins; all text is inserted as text, never as markup | `api/app.py`, `frontend/js/ui.js` |

## What is deliberately not shown

- **To anonymous callers:** nothing except the sign-in endpoints and a health check. The
  list of platforms, AI status and category list all require sign-in.
- **To sellers:** no stack traces, file paths, SQL, internal ids of handlers, paging
  cursors, model names or benchmark figures. Errors are a plain sentence plus a suggested
  action. Validation errors name the field in words, never the internal path or the
  submitted value.
- **In responses:** the server software header is switched off; error responses for
  "missing" and "not yours" are identical.
- **In the interface:** unverified connectors are labelled "Beta", not described in
  engineering terms. Activity entries hide internal identifiers.
- **In the audit log:** any detail whose name suggests a secret is replaced with
  `[redacted]` before it is stored.

## Development conveniences that must not reach production

| Convenience | Production behaviour |
| --- | --- |
| Email verification code returned by the register call so sign-up works without email | Not returned when `CP_ENV=production` |
| Emails written to `var/outbox/` | Sending raises until a real email provider is configured |
| Master key generated into `var/dev-master.key` | Startup refuses without `CP_MASTER_KEY_B64` |
| SQLite database, non-secure cookie | Startup refuses |
| Interactive API docs at `/docs` | Disabled |

## Not done

No external penetration test. No commercial malware engine (ClamAV can be attached; the
built-in checks are a baseline). PostgreSQL row-level security is generated but has never
been executed. Rate limiting is in-process, so it is per server, not global. See
[08](08-known-gaps.md).

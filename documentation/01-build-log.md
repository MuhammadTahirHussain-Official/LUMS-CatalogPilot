# 01 · Build log

Each step, in the order it was done, with why. Commits are on branch `feature/platform-v3`.

## Round 1: the platform

| # | Step | Why this way |
| --- | --- | --- |
| 1 | Checked the laptop: Python 3.13 and Node present; no Docker, PostgreSQL, Redis, GitHub CLI or GPU; slow network | Decides what can actually be run and tested here |
| 2 | Found the repository `LUMS-CatalogPilot`, created branch `feature/platform-v3` from `main` | Work was asked to stay off `main` |
| 3 | Database schema for tenants, users, stores, credentials, uploads, catalog, jobs, proposals, audit | One schema that runs on SQLite (development) and PostgreSQL (production) |
| 4 | Tenant isolation inside the data layer: every query is filtered by organization automatically, every write is checked | A forgotten filter in one screen can then never leak another organization's data |
| 5 | Credential encryption (a fresh key per credential, wrapped by a per-organization key), password hashing, tamper-evident audit log, roles | Security is built first, not added at the end |
| 6 | Upload pipeline: resumable chunks, checksums, type detection from content, threat checks, quarantine | Uploaded files are the largest attack surface |
| 7 | Parsers for CSV, TSV, Excel, JSON, XML; column mapping with suggestions; per-row error reports | Real seller data lives in messy spreadsheets |
| 8 | Catalog versions with per-product differences and restore | Every change must be reversible |
| 9 | Publish engine: preview, approval, leases, fencing tokens, idempotent writes, retries, circuit breaker, rollback | "A crash never means a half-finished, duplicated catalog" |
| 10 | Credential broker: a key is released only to the one task that needs it, for as long as that task holds its lease | A compromised component elsewhere cannot read store keys |
| 11 | Connectors: practice store, CSV feed, Shopify, WooCommerce | One interface per platform; the engine contains no platform-specific code |
| 12 | AI suggestions with guardrails and a confidence score calibrated on a benchmark | Suggestions are proposals; nothing is applied without policy or a person |
| 13 | Accounts, sessions, two-step sign-in, API keys, HTTP API | |
| 14 | Web app | |

### Decisions that differ from the original specification

- **Database lease table instead of Temporal** (the specification's own fallback). Temporal needs infrastructure this machine cannot run; the guarantees are the same.
- **Plain HTML/CSS/JavaScript web app instead of Next.js.** Nothing to download or build on a slow connection, and a strict content-security policy with no inline script.
- **AI is rule-based by default.** No GPU is available, and a deterministic default sends nothing off the machine. A hosted model can be switched on per organization.

### Defects the tests found during round 1 (and the fix)

| Found by | Defect | Fix |
| --- | --- | --- |
| Chaos test | Items merely waiting in a crashed handler's batch were counted as crashes and wrongly set aside | Only an item whose external call had started counts |
| Money test | A price like `1e400` crashed the parser | Magnitude is checked before scaling |
| AI benchmark | Extraction rewrote the seller's spelling (`Gray` → `Grey`) | The seller's own spelling is kept |
| AI benchmark | Lower-case titles with shouted promo text were not re-cased; short words stayed shouted | Casing is judged after promo text is removed |
| AI benchmark | Tied scores were calibrated wrongly | Equal scores are pooled before fitting |
| Tooling | Shell quoting corrupted five regular expressions | Rewritten; source files are now only edited through the editor tools |

## Round 2: store-to-store migration, guided interface, worst cases

| # | Step | Why this way |
| --- | --- | --- |
| 15 | Looked at existing migration tools and their user complaints ([06](06-market-research.md)) | Build against the real pain points: duplicates on re-run, missing images, lost attributes |
| 16 | Store import: read a connected store page by page with a saved checkpoint after every page ([02](02-migration-flow.md)) | A 1,000+ product import must survive a crash without starting over or duplicating |
| 17 | Normalization of source data as untrusted input: cleaned, sanitized, range-checked per product | A hostile or broken store must not be able to inject anything, and one bad product must not stop the rest |
| 18 | Import report: found / new / updated / unreadable, plus missing images, brand, category, price | Tells the seller what will not carry over before they publish |
| 19 | Improvement preferences: the seller answers a few questions (title style, promo removal, brand first, length, which fields) | The product should ask, not assume |
| 20 | Search-text (SEO) suggestions, always reviewed | No benchmark exists for them yet, so they are never applied automatically |
| 21 | Worst-case tests at 1,500 products ([03](03-failure-scenarios.md)) | Every scenario is proven, not claimed |
| 22 | Guided "Move a store" wizard, new navigation with icons, friendlier wording ([05](05-interface-and-pip.md)) | A new user should never have to hunt for the next step |
| 23 | Pip, the helper character with moods | Guides new users, reacts to mistakes, explains what not to do |
| 24 | Removed wording meant for developers; tightened what the API reveals ([04](04-security.md)) | Nothing internal should reach the seller's screen or an anonymous caller |
| 25 | This documentation folder | |

### Defects the tests found during round 2

| Found by | Defect | Fix |
| --- | --- | --- |
| Injection test | Removing a pasted link from a title left fragments ("Visit or mail") | The whole sentence carrying the link is dropped |
| Injection test | Search-text suggestions could carry leftover contact placeholders | Sentences with links or contacts are dropped; a validator now forbids any link or contact in search text |
| API test | A store connected only for publishing could still be imported from | Import now requires the store to be connected as a source |
| Browser check | "Next" button missing after running suggestions; helper replies scrolled out of view | Fixed |

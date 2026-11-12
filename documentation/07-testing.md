# 07 · Testing

Run from `backend/`:

```bash
../.venv/Scripts/python -m pytest
```

61 automated tests, all passing at the time of writing.

| File | Tests | What it proves |
| --- | --- | --- |
| `test_foundations.py` | 18 | Organization isolation (reads, writes, bulk updates, no organization at all), catalog versions and restore, concurrent-edit detection, audit tampering detection, credential encryption bound to its owner, password policy, money and barcode parsing |
| `test_engine.py` | 13 | Publishing: no duplicates under crashes and lost responses (400 items), stale handlers fenced out, one failing store does not block another, retries then set-aside then manual retry, store rejection isolated to one item, revoked key pauses, poison item quarantined, rollback, step-up for high impact, key never stored in plain text, passwords refused |
| `test_ai.py` | 8 | Hostile model output rejected, categories limited to candidates, prices and contacts never sent to AI, calibration maths, safe changes applied and uncertain ones queued, approve / edit / reject / stale, policy and kill switch, instructions hidden in product text change nothing |
| `test_api.py` | 7 | Whole journeys over HTTP: upload → map → import → improve → publish → rollback; store-to-store migration; CSRF; cross-organization access answers "not found" on every endpoint tried; roles and cost visibility; two-step sign-in; the upload attack set |
| `test_migration.py` | 15 | The worst cases in [03](03-failure-scenarios.md) at 1,500 products, plus hostile source data |

## The upload attack set

Antivirus test file, an executable renamed `.csv`, HTML and a shell script renamed `.csv`,
an image renamed `.csv`, archive entries that escape their folder, nested archives, a
120 MB zero-filled archive entry, a macro workbook, an image claiming 60,000 × 60,000
pixels, an image with a script appended, XML with an external entity, a legacy Office
file. All must be quarantined or refused, and never parsed.

## AI benchmark

```bash
../.venv/Scripts/python -m catalogpilot.ai.train
```

1,500 **synthetic** products, deliberately damaged the way real catalogs are.

| Task | Accuracy | Applied automatically when |
| --- | --- | --- |
| Titles | 93.9% overall | Only titles whose capitalization did not have to be guessed (39% of them); those were 100% correct on the benchmark |
| Descriptions | 100% | Small edits; large deletions go to review |
| Attributes | 100% | Yes |
| Categories | 74.4% (top choice) | Never: categories are high impact |
| Search text | not benchmarked | Never |
| Hidden instructions obeyed | 0 of 16 | |

These figures describe the built-in rules on generated data. They are not a claim about
real catalogs, and they do not transfer to any other model.

## Checked by hand in a browser

Sign-up, sign-in, home, the full wizard (connect a practice source, import 1,200 products,
answer the improvement questions, approve low-impact suggestions, choose a destination,
create a trial preview), the publish preview and live progress, products, review, stores,
activity, settings, and Pip's panel and replies. Reloading mid-wizard returned to the same
step.

## Not tested

Shopify and WooCommerce against live stores; PostgreSQL; several handler processes truly
in parallel; catalogs above 2,500 products; the hosted AI provider; the browser file picker
(uploads are tested through the API); screen readers; mobile devices; rollback from the
interface (tested through the API).

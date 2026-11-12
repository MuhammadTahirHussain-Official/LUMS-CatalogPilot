# 03 · Failure scenarios

What happens when things go wrong, and the automated test that proves it. Tests are in
`backend/tests/`. "Sandbox" faults are injected by the practice store, which can be told to
fail, throttle, lose responses, expire page markers or reject keys.

## While importing from a store (1,000+ products)

| Scenario | What the system does | What the seller sees | Test |
| --- | --- | --- | --- |
| The server crashes or is restarted mid-import | The work item's lease expires; any handler resumes from the saved page | A short pause, then progress continues | `test_server_goes_offline_mid_import_and_resumes_from_checkpoint` (asserts only the remaining pages are fetched) |
| Handlers die repeatedly | Same, each time | Same | `test_import_1500_products_survives_crashes_outages_and_expired_cursors` |
| The source store returns errors or times out | Back-off with random delay, retry the same page | "The store is slow to answer. Retrying automatically…" | same |
| The source store throttles | Request rate is halved and the store's wait time is honoured; attempts are not used up | Slower progress | same |
| The store rejects the saved page marker | Restart from page one, at most three times; matching by product identity makes the second pass a no-op | Nothing | same (two forced expiries) |
| The store stays down | After 8 attempts the import stops; **everything read so far is kept** | "The store stopped answering after N products. Nothing imported so far was lost." + a Continue button | `test_source_store_down_for_good_keeps_partial_import_and_retry_continues` |
| The seller presses Continue later | Resumes at the saved page | Progress continues | same (asserts 3 remaining pages, not 6) |
| The store's key is revoked mid-import | The store is marked "reconnect needed"; the import **waits** instead of failing | Prompt to reconnect | `test_revoked_key_pauses_import_and_reconnect_resumes` |
| One product is unreadable | Listed in the report with a reason; the page continues | "N products could not be read. See why" | 1,500-product test (3 unreadable) |
| The same product appears twice | Upsert by identity | One product | 1,500-product test (key uniqueness asserted) |
| The import is run a second time | Everything reported as already up to date; no new catalog version | "Already up to date: N" | same |
| A product is deleted in the store during the import | It is simply absent from later pages | Nothing | by construction |
| The seller pauses, resumes or stops | Checked between pages; what was imported stays | Status changes | `test_pause_resume_cancel_and_single_import_per_store` |
| Two imports of the same store are started | The second is refused and the interface shows the running one | The existing import | same |
| The source sends hostile or broken data (scripts, negative or non-numeric prices, absurd sizes, `javascript:` image links, path-like handles) | Rejected or sanitized per product | Reason in the report | `test_hostile_or_broken_source_data_is_rejected_per_product`, `test_source_data_is_sanitized_and_bounded` |

## While publishing to a store

| Scenario | What the system does | Test |
| --- | --- | --- |
| A handler dies **after** the store accepted the write but **before** it was recorded | The next owner repeats the write with the same idempotency key; the store returns the same listing | `test_chaos_crashes_and_lost_responses_never_duplicate`, `test_full_migration_import_enhance_publish_1500_with_crashes` |
| The store has no idempotency support | The engine first looks the product up by a marker it wrote | `test_no_duplicates_without_platform_idempotency` |
| A stalled handler wakes up after another took over | Its fencing token is stale: it gets no credential and cannot change state | `test_zombie_handler_is_fenced_out` |
| One destination is down | Its circuit breaker opens; its items wait; other destinations finish | `test_one_failing_channel_does_not_block_the_other` |
| The store rejects one product | That item fails with the store's reason and a suggested fix; the rest continue | `test_platform_rejection_fails_one_item_only` |
| Retries are exhausted | Items are set aside as "needs attention" and can be retried | `test_retries_exhausted_goes_to_dead_letter_then_manual_retry` |
| The key is revoked | The store asks to be reconnected; items wait | `test_revoked_credential_pauses_channel_without_failing_items` |
| One product keeps crashing the publisher | Set aside after three crashes; others unaffected | `test_poison_item_is_quarantined` |
| A bad publish needs undoing | Rollback removes created listings and restores changed ones | `test_rollback_deletes_created_and_restores_modified` |

## In the browser

| Scenario | What happens |
| --- | --- |
| The seller's internet drops | A banner appears, the helper says so, and nothing is re-rendered (typed input is not lost). Imports and publishing continue on the server. Status polling retries quietly. |
| The tab is closed mid-migration | The wizard's position is stored per organization in the browser; reopening returns to the same step with the live status. |
| An upload is interrupted | The browser asks the server how many bytes it has and continues from there (`test_upload_attack_corpus`, resumable section). |
| The session expires | The next action shows the sign-in screen with a notice. |
| A sensitive action is attempted without recent sign-in | A "confirm it's you" dialog; the action is then repeated automatically once. |
| Double-clicking a button | Buttons disable while their action runs; creating jobs and uploads is idempotent on the server. |

## Not covered

- **Loss of the database itself.** State lives in one SQLite file in development. Production
  needs PostgreSQL with replication and backups, which could not be set up or tested here.
- **True multi-process concurrency.** The tests run handlers one after another and simulate
  crashes and lease expiry. The claim logic uses conditional updates designed for
  concurrency, but it has not been run under real parallel load.
- **Real Shopify / WooCommerce behaviour** (their actual rate limits, error formats, cursor
  lifetimes).
- **Load at 100,000+ products.** The largest test is 2,500.

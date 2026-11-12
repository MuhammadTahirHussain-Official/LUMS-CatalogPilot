# 02 · Migration flow

Moving a catalog from store A (for example Shopify) to store B (for example WooCommerce)
is three independent stages. Each one can be repeated safely.

```text
 Store A ──read──▶ [1 Import] ──▶ Catalog (versioned) ──▶ [2 Improve] ──▶ [3 Publish] ──write──▶ Store B
                    checkpoint         every change           proposals        preview, approval,
                    per page           reversible             + review         rollback
```

## Stage 1: import (`backend/catalogpilot/engine/importer.py`)

- The source store is connected with role **source**. It is only ever read.
- An import is one job with one work item. The item's payload is the **checkpoint**: the
  page cursor and the running totals.
- For each page: take a credential lease → fetch the page → normalize every product →
  upsert into the catalog → advance the checkpoint. The catalog change and the checkpoint
  commit in **one database transaction**.
- A handler processes at most ten pages, then yields so other work is not starved.
- Product identity is `platform : store host : product id`. It comes from the store, not
  from our connection record, so reconnecting the same store never duplicates products.
- Every product passes through `catalog/normalize.py`: text cleaned and length-limited,
  HTML sanitized, numbers range-checked, image URLs restricted to http(s). A product that
  fails is listed in the report with a reason; the rest of the page still imports.

The report shown to the seller: products found, new, updated, already up to date, could
not be read (with reasons), and how many have no image, brand, category or price.

## Stage 2: improve (`backend/catalogpilot/ai/`)

The seller answers a few questions:

| Question | Options |
| --- | --- |
| What to help with | titles, descriptions, attributes, category, search text |
| Title style | Title Case, Sentence case, keep as is |
| Remove sales phrases | yes / no |
| Brand at the start of the title | yes / no |
| Title length | unlimited, 70, 120, 150 |

Each suggestion is stored as a **proposal** with the original value, the proposed value, a
reason, a confidence and a risk class. Then:

- Guardrails reject any suggestion that adds a link, contact detail, number, claim, word or
  markup that is not in the product's own data.
- A suggestion is applied automatically only if it is low risk, on a field the organization
  allows, **and** its score is above the threshold where measured accuracy was at least 98%.
- Category changes are high impact and always wait for a person. Restyled titles and search
  text always wait too, because no benchmark covers them yet.
- Prices, stock and cost are never sent to or changed by this stage.

## Stage 3: publish (`backend/catalogpilot/engine/`)

- Creating a publish job only builds a **preview**: per product, what would be created or
  changed, validation problems, and a risk class. Nothing is sent.
- After approval, each product is written with a deterministic idempotency key. A write
  repeated after a failure updates the existing listing instead of creating a second one.
- A finished job can be **rolled back**: listings it created are removed, listings it
  changed are restored to their previous content.

## Platforms

| Platform | Read (source) | Write (destination) | Status |
| --- | --- | --- | --- |
| Practice store | yes, 1,200 sample products | yes | Fully tested |
| CSV / product feed | no | yes | Fully tested |
| Shopify | yes | yes | Written from the API documentation; **not yet run against a live store** |
| WooCommerce | yes | yes | Written from the API documentation; **not yet run against a live store** |

Shopify and WooCommerce are labelled "Beta" in the interface for that reason.

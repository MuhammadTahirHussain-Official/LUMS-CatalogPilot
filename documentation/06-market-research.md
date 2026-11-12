# 06 · Market research

Two web searches were run on 11–12 November 2026. This is a small sample of public listings
and reviews, not a market study; treat the numbers as indicative.

## What existing store-migration tools do

- **LitExtension** offers a three-step flow (set up source and target, choose what to move,
  run), a free demo limited to about 20 items, and moves products, customers and orders.
  Its Shopify app listing showed about 290 reviews averaging 4.8.
- **Cart2Cart** showed lower averages in the listings found: 3.7 from 15 reviews on G2 and
  3.7 from about 250 reviews on an app-review aggregator.

## What their users complain about (from the reviews found)

| Complaint | Reported for |
| --- | --- |
| Running a migration again created **duplicate** records | Cart2Cart |
| Products arrived **without images** | Cart2Cart |
| **Variations broken**, model numbers missing, hundreds of products needing manual edits | Cart2Cart |
| **Tags, categories and brand names missing** after the move | Cart2Cart, LitExtension |
| Net prices imported where gross prices were expected | LitExtension |
| Slow support responses | Cart2Cart |

No review was found describing a migration that failed midway, so that risk is not
confirmed by this sample; it is handled here regardless.

## How this product answers each

| Pain point | Answer here | Evidence |
| --- | --- | --- |
| Duplicates on re-run | Products are matched by their identity in the source store; a repeat is a no-op | 1,500-product test re-runs the import and asserts zero new products and no new version |
| Missing images, brand, category | The import report counts each before anything is published | Wizard step 2 |
| Broken variations | Variants are validated before publishing (distinct options, SKUs, prices); problems block the item with a suggested fix | Preview screen |
| Data arrives as-is, mess included | Optional improvement step with the seller's own preferences, reviewed before applying | Wizard step 3 |
| No way back | Field-level preview before publishing; rollback after | Publish screen |
| Tiny free trial | A practice store with 1,200 products, and a 10-product trial publish to a real store | Wizard steps 1 and 5 |
| Waiting on support to understand a failure | Each failure has a plain reason and a next action in the interface | [03](03-failure-scenarios.md) |

## What competitors have that this does not (yet)

Customers, orders, reviews, reward points and multi-language content; dozens of platform
pairs; a track record with real stores. This product moves **products only**, between two
real platforms that are still unverified against live stores.

## Sources

- [LitExtension WooCommerce migration plugin (WordPress.org)](https://wordpress.org/plugins/litextension-data-migration-to-woocommerce/)
- [LitExtension: Shopify to WooCommerce](https://litextension.com/ja/woocommerce-migration/shopify-to-woocommerce.html)
- [LitExtension Store Migration reviews (Shopify App Store)](https://apps.shopify.com/litextension-shopify-migration-app/reviews?locale=sv)
- [Compare Cart2Cart and LitExtension (G2)](https://www.g2.com/compare/cart2cart-vs-litextension)
- [Cart2Cart reviews (G2)](https://www.g2.com/products/cart2cart/reviews)
- [Cart2Cart Store Migration App reviews (AppNavigator)](https://appnavigator.io/app/cart2cart-shopify-migration-module/)
- [Universal Store Migration App reviews (Shopify App Store)](https://apps.shopify.com/cart2cart-shopify-migration-module/reviews?page=21)

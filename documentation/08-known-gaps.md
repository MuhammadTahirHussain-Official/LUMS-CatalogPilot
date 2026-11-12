# 08 · Known gaps

Read this before relying on the product for a real store.

## Not proven

| Item | State | What is needed |
| --- | --- | --- |
| Shopify connector (read and write) | Written from the API documentation, never run against a store | A development store and a custom-app token; then run the connector against it and fix what differs |
| WooCommerce connector (read and write) | Same | A test WordPress site with WooCommerce and an API key |
| PostgreSQL, including row-level security | Schema is portable and the security rules are generated, but nothing has run on PostgreSQL | A PostgreSQL server (Docker Desktop is the easiest way) |
| Parallel handlers | Logic is designed for it; tests simulate it sequentially | Run two or more handler processes against PostgreSQL under load |
| Large catalogs | Largest test is 2,500 products. Import and publish work page by page, but AI suggestions and catalog versions are held in memory per request | Load tests at 100,000 products; move enhancement to a background job |
| AI accuracy on real data | Benchmark is synthetic | A labelled set of real products (1,000+) |

## Not built

- Customers, orders, reviews (out of scope by the specification).
- Continuous sync after the first publish; detecting changes made directly on a store.
- Marketplaces: Amazon, eBay, Etsy, Google Merchant, Meta.
- Images that exist only as uploaded files are not published (stores need a public URL).
- Single sign-on, passkeys, billing and plans, real email delivery.
- A commercial malware engine, S3 storage, Kubernetes deployment.
- External penetration test, SOC 2.
- Importing more than 100 variants per product from Shopify or WooCommerce (such a product
  is reported as unreadable rather than imported incomplete).

## Known rough edges

- Asking for suggestions on a large catalog runs inside the web request and can take tens
  of seconds; the page shows a busy state meanwhile.
- The rate limiter and the practice store keep their state in memory, so both reset when
  the server restarts (the practice source store refills itself).
- Pip answers from a fixed script.
- The interface is English only.

## Things that cannot be promised

- Working while the developer's laptop is switched off: the server runs on that laptop.
- Commercial outcome. The software can be made correct and safe; its value depends on
  customers.

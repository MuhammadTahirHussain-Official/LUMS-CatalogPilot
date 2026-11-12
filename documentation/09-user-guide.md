# 09 · User guide: moving a store

## Before you start

- You need an **API key** from each store, made for apps. Never your login password.
  - *Shopify:* Settings → Apps → Develop apps → create an app with product read and write
    access → copy the Admin API access token.
  - *WooCommerce:* WooCommerce → Settings → Advanced → REST API → Add key → Read/Write.
- Not ready to connect a real store? Use the **Practice store**. It comes with 1,200 sample
  products and nothing real is touched.

## The five steps

Open **Move a store** in the menu.

1. **Your current store.** Choose the kind of store, give it a name, paste its key, press
   Connect. We only read from this store.
2. **Bring products in.** Press *Bring my products in*. You can close the page; it carries
   on. When it finishes you see how many products were found, how many could not be read
   and why, and how many lack an image, brand or category.
3. **Improve them.** Tick what you want help with and how titles should look, then press
   *Suggest improvements*. Safe changes are applied; the rest wait for you. *Look through
   them* opens the review screen, or *Approve all low-impact suggestions* accepts the easy
   ones in one go. You can skip this step.
4. **Your new store.** Connect the store your products are going to.
5. **Preview & publish.** Keep *trial of 10 products* ticked the first time. You get a list
   of every product and what will happen to it. Press *Approve and publish* when it looks
   right, then watch the progress.

Only products marked **Active** are published. Change this under **Products**: select
products and choose *Mark active*.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| "The store is slow to answer. Retrying automatically…" | Nothing. It retries by itself. |
| "The import stopped before the end" | Press *Continue from where it stopped*. Nothing already imported is lost. |
| "Reconnect needed" on a store | Open **Stores**, press *Replace key* and paste a new key. Waiting work resumes. |
| A product is "blocked" in the preview | Read the reason beside it (for example "title too long for this store") and fix that product under **Products**. |
| You published something you did not want | Open the job under **Publishing** and press *Roll back…*. |
| You approved a suggestion you regret | **Products** → *History* → *Restore* an earlier version. |
| Something looks seriously wrong | **Settings** → *Freeze all publishing*. Nothing is sent to any store until you unfreeze. |
| You lost your internet connection | Reopen the page later. Work continued on the server. |

## What not to do

- Do not paste a store **password**, a verification code or card details anywhere. The
  product refuses them, and Pip will tell you off.
- Do not approve **High impact** suggestions without reading them.
- Do not share your account; invite people under **Settings** so each has their own access.

## Pip

The small character in the corner explains each screen, tells you what to do next and
answers common questions. Click it any time.

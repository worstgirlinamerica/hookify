# Hookify

Watches one or more Shopify stores for new listings, restocks, sellouts, and
price changes, and posts alerts to Discord as embeds. Runs on a schedule via
GitHub Actions, so it works even when your own computer is off.

No paid hosting, no server to maintain. Only dependency is Python's standard
library (no `pip install` needed).

## How it works

- `tracker.py` reads a list of stores from the `STORES_CONFIG` secret, pulls
  every published product from each store's Storefront GraphQL API (not just
  what's linked in nav — this catches unlisted/unlinked products too), and
  diffs the result against a saved snapshot in `state/`.
- The first run for a store just saves the baseline — no alerts, since there's
  nothing to compare against yet.
- Every run after that posts a Discord embed for anything that changed.
- `.github/workflows/watch.yml` runs the script every 10 minutes and commits
  the updated `state/` files back to the repo so the next run has something
  to diff against.

## One-time setup

### 1. Create the repo

1. Go to [github.com/new](https://github.com/new).
2. Name it whatever you want (e.g. `hookify`). Private is fine — this repo
   isn't going to contain your tokens directly, but private keeps it out of
   search anyway.
3. Create it, then either:
   - Upload these files through the GitHub web UI ("Add file" → "Upload
     files", drag in everything including the `.github` folder), or
   - Clone it locally and push:
     ```bash
     git clone https://github.com/YOUR-USERNAME/hookify.git
     cd hookify
     # copy in tracker.py, README.md, .gitignore, and .github/workflows/watch.yml
     git add .
     git commit -m "Initial setup"
     git push
     ```

**Important:** GitHub's web upload UI hides folders that start with a dot
(`.github`) in some views — if you're uploading manually, drag the whole
`.github` folder in, or create the file at path
`.github/workflows/watch.yml` directly using "Add file" → "Create new file"
and typing that full path in the filename box.

### 2. Get each store's domain + token

For each store you want to track:

1. Open the store's site in Chrome, open DevTools (F12) → Network tab, filter
   to `graphql`.
2. Browse the site (open a product, add to cart) until a request to
   `something.myshopify.com/api/.../graphql` shows up.
3. Click it → Headers → under Request Headers, copy the value of
   `x-shopify-storefront-access-token`. That's the token. The domain is in
   the request URL.

### 3. Add the STORES_CONFIG secret

1. In your repo: Settings → Secrets and variables → Actions → New repository
   secret.
2. Name: `STORES_CONFIG`
3. Value: a JSON array, one object per store:

```json
[
  {
    "label": "Store One",
    "domain": "store-one.myshopify.com",
    "token": "PASTE_TOKEN_HERE",
    "webhook": "https://discord.com/api/webhooks/xxxx/yyyy",
    "alert_types": ["new", "restock", "sellout", "price"]
  },
  {
    "label": "Store Two",
    "domain": "store-two.myshopify.com",
    "token": "PASTE_TOKEN_HERE",
    "webhook": "https://discord.com/api/webhooks/aaaa/bbbb",
    "alert_types": ["new", "restock"]
  }
]
```

`alert_types` controls which alert kinds fire for that store — drop any you
don't want (e.g. remove `"price"` if you don't care about price changes).

Get a webhook URL from: Discord → the channel you want alerts in → Edit
Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL.

### 4. Enable Actions and test it

1. Go to the **Actions** tab in your repo. If prompted, click "I understand
   my workflows, go ahead and enable them."
2. Click the **hookify** workflow on the left → **Run workflow** (this uses
   the `workflow_dispatch` trigger, so you don't have to wait for the cron).
3. Watch it run. First run should say "first run, baseline saved, no alerts
   sent" in the logs for each store — that's expected.
4. Run it again manually (or wait ~10 min for the schedule) after a real
   change happens, or temporarily edit `state/<store>.json` to remove one
   product so the next run treats it as "new" and you can confirm the Discord
   message looks right.

After that, it just runs every 10 minutes on its own. No tab, no computer, no
server required.

## Notes

- GitHub's cron scheduler is best-effort — it can run a few minutes late
  during periods of high load across GitHub. Fine for restock alerts, not
  suitable if you need second-level precision.
- Private repos get 2,000 free Actions minutes/month; each run here takes a
  few seconds, so checking every 10 minutes across a handful of stores stays
  well within the free tier.
- Product images from Shopify's CDN are forced to `?format=png` so Discord
  always gets a real PNG instead of whatever format content negotiation would
  otherwise pick.
- To add a store later, just edit the `STORES_CONFIG` secret's JSON — no code
  changes needed.
- To stop tracking a store, remove it from `STORES_CONFIG` and optionally
  delete its file under `state/`.

#!/usr/bin/env python3
"""
hookify - multi-store Shopify merch tracker with Discord alerts.

Reads store configs from the STORES_CONFIG environment variable (a JSON array),
polls each store's Storefront GraphQL API, diffs against a saved state file per
store, and posts Discord embeds for new listings, restocks, sellouts, and price
changes. Embed color is pulled from the product's own image, and each alert
includes link buttons to the product page and a one-click add-to-cart.

STORES_CONFIG format (set as a GitHub Actions repo secret):
[
  {
    "label": "ADÉLA",
    "domain": "shop.adelaxo.com",
    "token": "public_storefront_token",
    "webhook": "https://discord.com/api/webhooks/xxxx/yyyy",
    "alert_types": ["new", "restock", "sellout", "price"],
    "role_pings": {"new": "1234567890123456", "restock": "1234567890123456"}
  }
]

role_pings is optional. If set, the given role ID gets pinged (as
<@&roleid>) whenever that alert type fires for that store. Omit it, or
leave a kind out, for no ping on that kind.
"""

import io
import json
import os
import re
import sys
import time
import datetime
import urllib.request
import urllib.error

from PIL import Image

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
API_VERSION = "2025-01"
USER_AGENT = "Mozilla/5.0 (compatible; hookify/1.0; +https://github.com)"

PRODUCTS_QUERY = """
query Products($cursor: String) {
  products(first: 100, after: $cursor, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        handle
        productType
        onlineStoreUrl
        createdAt
        featuredImage { url }
        variants(first: 50) {
          edges {
            node {
              id
              title
              availableForSale
              price { amount currencyCode }
            }
          }
        }
      }
    }
  }
}
"""

FALLBACK_COLOR = 0xD4537E  # used only if we can't read the product image at all

ALERT_LABELS = {
    "new": "New Listing",
    "restock": "Restocked",
    "sellout": "Sold Out",
    "price": "Price Change",
}


def log(msg):
    print(f"[hookify] {msg}", flush=True)


def force_png(url):
    """Shopify CDN negotiates format by Accept header; force a stable PNG."""
    if not url:
        return url
    if "cdn.shopify.com" not in url:
        return url
    sep = "&" if "?" in url else "?"
    if "format=" in url:
        return url
    return f"{url}{sep}format=png"


def shopify_request(domain, token, query, variables=None):
    endpoint = f"https://{domain}/api/{API_VERSION}/graphql.json"
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Storefront-Access-Token": token,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_products(domain, token):
    products = {}
    cursor = None
    while True:
        data = shopify_request(domain, token, PRODUCTS_QUERY, {"cursor": cursor})
        if "errors" in data:
            raise RuntimeError(f"Shopify API error: {data['errors']}")
        block = data["data"]["products"]
        for edge in block["edges"]:
            node = edge["node"]
            variants = []
            for v in node["variants"]["edges"]:
                vn = v["node"]
                variants.append({
                    "id": vn["id"],
                    "title": vn["title"],
                    "available": vn["availableForSale"],
                    "price": vn["price"]["amount"],
                    "currency": vn["price"]["currencyCode"],
                })
            products[node["id"]] = {
                "title": node["title"],
                "handle": node["handle"],
                "type": (node.get("productType") or "").strip(),
                "url": node.get("onlineStoreUrl"),
                "created_at": node.get("createdAt"),
                "image": force_png((node.get("featuredImage") or {}).get("url")),
                "variants": variants,
            }
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
    return products


def any_available(product):
    return any(v["available"] for v in product["variants"])


def format_listed_date(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%b %-d, %Y")
    except Exception:
        return None


def get_dominant_color(image_url):
    """Downloads the product image and returns its average color as a
    Discord-compatible int. Falls back to a neutral pink if the image
    can't be fetched or decoded."""
    if not image_url:
        return FALLBACK_COLOR
    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img = img.resize((1, 1), Image.LANCZOS)
        r, g, b = img.getpixel((0, 0))
        return (r << 16) + (g << 8) + b
    except Exception as e:
        log(f"Could not read color from product image: {e}")
        return FALLBACK_COLOR


def state_path(label):
    safe = re.sub(r"[^a-z0-9_-]+", "-", label.lower()).strip("-") or "store"
    return os.path.join(STATE_DIR, f"{safe}.json")


def load_state(label):
    path = state_path(label)
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def save_state(label, products):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path(label), "w") as f:
        json.dump(products, f)


def build_embed(kind, store, product, extra=None):
    fields = [{"name": "Alert", "value": ALERT_LABELS[kind], "inline": True}]

    if kind == "price":
        fields.append({"name": "Old price", "value": extra["old"], "inline": True})
        fields.append({"name": "New price", "value": extra["new"], "inline": True})
    else:
        status = "In stock" if any_available(product) else "Sold out"
        fields.append({"name": "Status", "value": status, "inline": True})
        if product["variants"]:
            price = product["variants"][0]["price"]
            currency = product["variants"][0]["currency"]
            fields.append({"name": "Price", "value": f"{price} {currency}", "inline": True})

    if product["type"]:
        fields.append({"name": "Type", "value": product["type"], "inline": True})

    listed = format_listed_date(product.get("created_at"))
    if listed:
        fields.append({"name": "Listed", "value": listed, "inline": True})

    available_variants = [v["title"] for v in product["variants"] if v["available"]]
    if available_variants and available_variants != ["Default Title"]:
        fields.append({
            "name": "Available in",
            "value": ", ".join(available_variants),
            "inline": False,
        })

    total = len(product["variants"])
    in_stock = len(available_variants)
    if total > 1:
        fields.append({
            "name": "Stock",
            "value": f"{in_stock} of {total} variants available",
            "inline": True,
        })

    embed = {
        "title": product["title"],
        "url": product["url"],
        "color": get_dominant_color(product["image"]),
        "fields": fields,
        "footer": {"text": store["label"]},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if product["image"]:
        embed["image"] = {"url": product["image"]}
    return embed


def build_components(product, domain):
    """Link-style buttons only: these don't need an application behind the
    webhook since clicking them just opens a URL, no interaction required."""
    buttons = []
    if product["url"]:
        buttons.append({"type": 2, "style": 5, "label": "View product", "url": product["url"]})

    available = [v for v in product["variants"] if v["available"]]
    if available:
        numeric_id = available[0]["id"].rsplit("/", 1)[-1]
        cart_url = f"https://{domain}/cart/{numeric_id}:1"
        buttons.append({"type": 2, "style": 5, "label": "Quick add to cart", "url": cart_url})

    if not buttons:
        return None
    return [{"type": 1, "components": buttons}]


def role_ping_content(store, kind):
    role_id = (store.get("role_pings") or {}).get(kind)
    if not role_id:
        return None
    return f"<@&{role_id}>"


def post_to_discord(webhook, store_label, embed, components=None, content=None):
    payload = {"username": store_label, "embeds": [embed]}
    if content:
        payload["content"] = content
        payload["allowed_mentions"] = {"parse": [], "roles": [re.sub(r"\D", "", content)]}
    if components:
        payload["components"] = components

    url = webhook
    if components:
        sep = "&" if "?" in webhook else "?"
        url = f"{webhook}{sep}with_components=true"

    def send(use_components):
        body_payload = dict(payload)
        send_url = webhook
        if not use_components:
            body_payload.pop("components", None)
        else:
            sep = "&" if "?" in webhook else "?"
            send_url = f"{webhook}{sep}with_components=true"
        body = json.dumps(body_payload).encode("utf-8")
        req = urllib.request.Request(
            send_url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()

    try:
        send(use_components=bool(components))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "ignore")
        if components:
            log(f"Webhook rejected buttons ({e.code}: {err_body[:200]}), retrying without them.")
            try:
                send(use_components=False)
            except urllib.error.HTTPError as e2:
                log(f"Discord webhook error {e2.code}: {e2.read().decode('utf-8', 'ignore')[:200]}")
        else:
            log(f"Discord webhook error {e.code}: {err_body[:200]}")


def send_alert(store, kind, product, extra=None):
    embed = build_embed(kind, store, product, extra=extra)
    components = build_components(product, store["domain"])
    content = role_ping_content(store, kind)
    post_to_discord(store["webhook"], store["label"], embed, components=components, content=content)


def diff_and_alert(store, old, new):
    alert_types = set(store.get("alert_types", ["new", "restock", "sellout", "price"]))
    sent = 0

    old = old or {}
    for pid, product in new.items():
        if pid not in old:
            if "new" in alert_types:
                send_alert(store, "new", product)
                sent += 1
            continue

        old_product = old[pid]
        was_available = any(v["available"] for v in old_product["variants"])
        now_available = any_available(product)

        if not was_available and now_available and "restock" in alert_types:
            send_alert(store, "restock", product)
            sent += 1
        elif was_available and not now_available and "sellout" in alert_types:
            send_alert(store, "sellout", product)
            sent += 1

        if "price" in alert_types and product["variants"] and old_product["variants"]:
            old_price = old_product["variants"][0]["price"]
            new_price = product["variants"][0]["price"]
            if old_price != new_price:
                send_alert(store, "price", product, extra={"old": old_price, "new": new_price})
                sent += 1

    if not old:
        log(f"{store['label']}: first run, baseline saved, no alerts sent.")
        return

    log(f"{store['label']}: {sent} alert(s) sent.")


def run_store(store):
    label = store["label"]
    log(f"Checking {label} ({store['domain']})...")
    try:
        new_products = fetch_all_products(store["domain"], store["token"])
    except Exception as e:
        log(f"{label}: FAILED to fetch products: {e}")
        return

    old_products = load_state(label)
    diff_and_alert(store, old_products, new_products)
    save_state(label, new_products)


def preview_store(store, count):
    """Pull the real, current catalog and send real embeds for the most
    recently created products - one of each alert type, using actual data
    (title, image, price) so you can see exactly what a real alert looks
    like. Does not touch state/, so it won't affect the next normal run."""
    label = store["label"]
    log(f"Preview: fetching real catalog for {label} ({store['domain']})...")
    try:
        products = fetch_all_products(store["domain"], store["token"])
    except Exception as e:
        log(f"{label}: FAILED to fetch products: {e}")
        return

    if not products:
        log(f"{label}: store returned zero products, nothing to preview.")
        return

    most_recent = list(products.values())[:count]
    log(f"{label}: sending real preview embeds for {len(most_recent)} product(s).")

    for product in most_recent:
        send_alert(store, "new", product)

    if most_recent:
        sample = most_recent[0]
        send_alert(store, "restock", sample)
        send_alert(store, "sellout", sample)
        variants = sample["variants"]
        if variants:
            old_price = variants[0]["price"]
            try:
                new_price_val = str(round(float(old_price) + 5, 2))
            except ValueError:
                new_price_val = old_price
            send_alert(store, "price", sample, extra={"old": old_price, "new": new_price_val})

    log(f"{label}: preview sent - check Discord.")


def main():
    raw = os.environ.get("STORES_CONFIG")
    if not raw:
        log("STORES_CONFIG env var not set. Nothing to do.")
        sys.exit(1)

    try:
        stores = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"STORES_CONFIG is not valid JSON: {e}")
        sys.exit(1)

    mode = os.environ.get("HOOKIFY_MODE", "run")
    preview_count = int(os.environ.get("HOOKIFY_PREVIEW_COUNT", "3"))

    if mode == "preview":
        for store in stores:
            preview_store(store, preview_count)
        return

    for store in stores:
        run_store(store)


if __name__ == "__main__":
    main()

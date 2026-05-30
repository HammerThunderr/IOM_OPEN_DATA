"""
Isle of Man property price scraper.

Source: https://propertyprices.im/  (data sourced from the IoM Land Registry,
refreshed monthly, records since November 2000).

The site is a JavaScript-rendered app: the property records are NOT in the
initial HTML. They are loaded by a background request after the page boots.
Rather than guessing CSS selectors, this scraper uses Playwright to *intercept*
the network responses the page makes and captures the JSON payload directly.

Output: data/property_prices.json
On the first run it also writes data/_debug_property_prices_captures.json
containing every JSON response seen, so the exact data endpoint can be locked in
if the auto-detection needs a nudge.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

SITE_URL = "https://propertyprices.im/"
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_FILE = OUT_DIR / "property_prices.json"
DEBUG_FILE = OUT_DIR / "_debug_property_prices_captures.json"

# Heuristic: a JSON response is "records" if it's a list of dicts (or wraps one)
# whose objects mention a price / address-like field.
PRICE_HINTS = ("price", "amount", "value", "sale", "consideration")
ADDR_HINTS = ("address", "property", "location", "parish", "town", "street")


def looks_like_records(obj):
    """Return the list of record dicts if `obj` looks like property records."""
    candidates = []
    if isinstance(obj, list):
        candidates.append(obj)
    elif isinstance(obj, dict):
        # Common wrappers: {"data": [...]}, {"records": [...]}, {"results": [...]}
        for key in ("data", "records", "results", "items", "rows", "properties"):
            if isinstance(obj.get(key), list):
                candidates.append(obj[key])
        # Laravel paginator: {"data": [...], "current_page": 1, ...}
        if isinstance(obj.get("data"), list):
            candidates.append(obj["data"])

    for lst in candidates:
        if not lst or not isinstance(lst[0], dict):
            continue
        keys = " ".join(str(k).lower() for k in lst[0].keys())
        has_price = any(h in keys for h in PRICE_HINTS)
        has_addr = any(h in keys for h in ADDR_HINTS)
        if has_price or has_addr:
            return lst
    return None


def scrape():
    captured = []          # all JSON responses, for debugging
    record_batches = []    # responses that look like records

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def handle_response(response):
            ctype = (response.headers.get("content-type") or "").lower()
            if "json" not in ctype:
                return
            try:
                body = response.json()
            except Exception:
                return
            captured.append({"url": response.url, "status": response.status})
            recs = looks_like_records(body)
            if recs:
                record_batches.append({"url": response.url, "records": recs})

        page.on("response", handle_response)

        print(f"Loading {SITE_URL} ...", flush=True)
        page.goto(SITE_URL, wait_until="networkidle", timeout=60_000)

        # The records sit behind a "Filter" interaction. Try to trigger it so the
        # data request fires. Best-effort: ignore if the control isn't found.
        for selector in (
            "text=Filter",
            "button:has-text('Filter')",
            "button[type=submit]",
            "input[type=submit]",
        ):
            try:
                el = page.locator(selector).first
                if el.count() > 0:
                    el.click(timeout=5_000)
                    page.wait_for_load_state("networkidle", timeout=30_000)
                    break
            except Exception:
                continue

        # Give any late XHRs a moment.
        page.wait_for_timeout(3_000)
        browser.close()

    # Always dump what we saw — invaluable for locking the endpoint on run #1.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_FILE.write_text(json.dumps(captured, indent=2))
    print(f"Saw {len(captured)} JSON response(s); "
          f"{len(record_batches)} looked like records.", flush=True)

    if not record_batches:
        print("No record-shaped JSON captured. Inspect "
              f"{DEBUG_FILE.name} to find the real endpoint.", file=sys.stderr)
        sys.exit(1)

    # Pick the biggest batch (most complete dataset).
    best = max(record_batches, key=lambda b: len(b["records"]))
    records = best["records"]

    payload = {
        "source": SITE_URL,
        "source_note": "Data from the Isle of Man Land Registry, refreshed monthly.",
        "data_endpoint": best["url"],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "records": records,
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Wrote {len(records)} records -> {OUT_FILE}", flush=True)


if __name__ == "__main__":
    scrape()

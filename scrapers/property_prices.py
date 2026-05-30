"""
Isle of Man property price scraper.

Source: https://propertyprices.im/  (data sourced from the IoM Land Registry,
refreshed monthly, records since November 2000).

The site is a **Livewire** app. The property data is not served as a plain JSON
array. It lives in two places:

  1. On first load, inside a `wire:snapshot` attribute embedded in the HTML.
  2. On filter changes, inside the JSON envelope returned by POST /livewire/update
     (records sit in an escaped `snapshot` string and/or rendered `effects.html`).

This scraper tries three strategies, in order, and keeps the first that yields
records:

  A. Parse the initial `wire:snapshot` attribute(s) from the DOM.
  B. Parse any POST /livewire/update responses captured during load.
  C. Generic scrape of the largest <table> rendered on the page.

It always writes data/_debug_property_prices_captures.json with what each
strategy saw, so the source can be re-pointed quickly if the site changes.

Output: data/property_prices.json
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

SITE_URL = "https://propertyprices.im/"
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_FILE = OUT_DIR / "property_prices.json"
DEBUG_FILE = OUT_DIR / "_debug_property_prices_captures.json"

PRICE_HINTS = ("price", "amount", "value", "sale", "consideration", "sold")
ADDR_HINTS = ("address", "property", "location", "parish", "town", "street", "type")


def looks_like_records(obj):
    """Return obj as a list of record dicts if it looks like property records."""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        keys = " ".join(str(k).lower() for k in obj[0].keys())
        if any(h in keys for h in PRICE_HINTS) or any(h in keys for h in ADDR_HINTS):
            return obj
    return None


def unwrap_livewire(v):
    """Livewire v3 dehydrates arrays/objects as [value, {"s": ...}] tuples.
    Recursively strip that wrapping so we get plain Python structures back."""
    if (isinstance(v, list) and len(v) == 2
            and isinstance(v[1], dict) and "s" in v[1]):
        return unwrap_livewire(v[0])
    if isinstance(v, list):
        return [unwrap_livewire(x) for x in v]
    if isinstance(v, dict):
        return {k: unwrap_livewire(val) for k, val in v.items()}
    return v


def find_record_lists(node, found):
    """Walk a nested structure collecting every list that looks like records."""
    recs = looks_like_records(node)
    if recs:
        found.append(recs)
    if isinstance(node, dict):
        for val in node.values():
            find_record_lists(val, found)
    elif isinstance(node, list):
        for item in node:
            find_record_lists(item, found)


def records_from_snapshot(snapshot_json_str, debug_label, debug):
    """Parse a Livewire snapshot JSON string and pull out record lists."""
    try:
        snap = json.loads(snapshot_json_str)
    except (TypeError, json.JSONDecodeError):
        return []
    data = unwrap_livewire(snap.get("data", snap))
    found = []
    find_record_lists(data, found)
    debug.append({
        "source": debug_label,
        "snapshot_data_keys": list(data.keys()) if isinstance(data, dict) else None,
        "record_lists_found": [len(f) for f in found],
    })
    return found


def scrape():
    debug = {"livewire_updates": [], "dom_snapshots": [], "tables": []}
    all_found = []  # list of (strategy, records)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        livewire_responses = []

        def handle_response(response):
            if "/livewire/update" in response.url:
                try:
                    livewire_responses.append(response.json())
                except Exception:
                    pass

        page.on("response", handle_response)

        print(f"Loading {SITE_URL} ...", flush=True)
        page.goto(SITE_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(3_000)

        # --- Strategy A: initial wire:snapshot attributes in the DOM ---
        dom_snapshots = page.evaluate("""
        () => {
          const out = [];
          document.querySelectorAll('*').forEach(e => {
            const s = e.getAttribute('wire:snapshot');
            if (s) out.push(s);
          });
          return out;
        }
        """)
        for i, snap_str in enumerate(dom_snapshots):
            for recs in records_from_snapshot(snap_str, f"dom_snapshot[{i}]",
                                              debug["dom_snapshots"]):
                all_found.append(("dom_snapshot", recs))

        # --- Strategy C (collected now): generic table scrape ---
        tables = page.evaluate("""
        () => Array.from(document.querySelectorAll('table')).map(t => ({
          headers: Array.from(t.querySelectorAll('thead th, thead td'))
                        .map(h => h.innerText.trim()),
          rows: Array.from(t.querySelectorAll('tbody tr')).map(tr =>
                  Array.from(tr.querySelectorAll('td,th')).map(c => c.innerText.trim()))
        }))
        """)
        browser.close()

    # --- Strategy B: parse any /livewire/update responses we captured ---
    for i, resp in enumerate(livewire_responses):
        for comp in resp.get("components", []):
            for recs in records_from_snapshot(comp.get("snapshot"),
                                              f"livewire_update[{i}]",
                                              debug["livewire_updates"]):
                all_found.append(("livewire_update", recs))

    # --- Strategy C: turn the biggest table into records ---
    for i, t in enumerate(tables):
        debug["tables"].append({"headers": t["headers"], "row_count": len(t["rows"])})
        if not t["rows"]:
            continue
        headers = t["headers"] or [f"col_{j}" for j in range(len(t["rows"][0]))]
        table_recs = []
        for row in t["rows"]:
            table_recs.append({headers[j] if j < len(headers) else f"col_{j}": val
                               for j, val in enumerate(row)})
        all_found.append(("table", table_recs))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_FILE.write_text(json.dumps(debug, indent=2, ensure_ascii=False))

    if not all_found:
        print("No records found by any strategy. Inspect "
              f"{DEBUG_FILE.name} for the page structure.", file=sys.stderr)
        sys.exit(1)

    # Prefer the strategy with the most records (snapshot usually beats a
    # paginated table; falls back gracefully).
    strategy, records = max(all_found, key=lambda x: len(x[1]))
    print(f"Strategies that found records: "
          f"{[(s, len(r)) for s, r in all_found]}", flush=True)
    print(f"Using '{strategy}' with {len(records)} records.", flush=True)

    payload = {
        "source": SITE_URL,
        "source_note": "Data from the Isle of Man Land Registry, refreshed monthly.",
        "extraction_strategy": strategy,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "records": records,
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Wrote {len(records)} records -> {OUT_FILE}", flush=True)


if __name__ == "__main__":
    scrape()

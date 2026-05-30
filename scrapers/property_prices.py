"""
Isle of Man property price scraper.

Source: https://propertyprices.im/  (data from the IoM Land Registry,
refreshed monthly, records since November 2000).

The site is a Filament v4 app (Laravel + Livewire). The records live in a
paginated, lazy-loaded Filament table widget (`property-search-table`). To get
the data we must:

  1. Load the page and scroll so the lazy-loaded table widget actually fetches.
  2. Increase the table page size to the largest option offered.
  3. Read the reported total ("Showing x to y of N results").
  4. Page through with the "Next" control, scraping each page, until done.

Pagination etiquette / safety:
  * A short delay is inserted between page turns.
  * MAX_PAGES (env var) caps the run. 0 = unlimited. Default 0.
  * Rows are de-duplicated, so overlap between pages is harmless.

Output: data/property_prices.json
Debug:  data/_debug_property_prices_captures.json
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

SITE_URL = "https://propertyprices.im/"
OUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_FILE = OUT_DIR / "property_prices.json"
DEBUG_FILE = OUT_DIR / "_debug_property_prices_captures.json"

MAX_PAGES = int(os.getenv("MAX_PAGES", "0"))   # 0 == unlimited
PAGE_DELAY = float(os.getenv("PAGE_DELAY", "0.5"))
NAV_TIMEOUT = 60_000


def trigger_lazy_load(page):
    """Scroll the page so the x-intersect lazy-loaded widgets fetch, then wait
    for the records table to render."""
    for _ in range(6):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(800)
    page.wait_for_selector("table tbody tr", timeout=NAV_TIMEOUT)


def set_max_page_size(page, debug):
    """Find the table's 'per page' <select> and choose its largest option."""
    info = page.evaluate("""
    () => {
      const selects = Array.from(document.querySelectorAll('select'));
      for (let i = 0; i < selects.length; i++) {
        const opts = Array.from(selects[i].options).map(o => o.value);
        const numericish = opts.filter(v => /^\\d+$/.test(v) || v.toLowerCase() === 'all');
        if (numericish.length) {
          let best = null, bestVal = -1;
          for (const o of selects[i].options) {
            const v = o.value.toLowerCase() === 'all' ? Infinity : parseInt(o.value);
            if (!isNaN(v) && v > bestVal) { bestVal = v; best = o.value; }
          }
          return { index: i, options: opts, chosen: best };
        }
      }
      return null;
    }
    """)
    debug["per_page_select"] = info
    if info and info["chosen"] is not None:
        try:
            page.locator("select").nth(info["index"]).select_option(info["chosen"])
            page.wait_for_timeout(1500)
            page.wait_for_selector("table tbody tr", timeout=NAV_TIMEOUT)
            print(f"Set page size to '{info['chosen']}' "
                  f"(options: {info['options']}).", flush=True)
        except Exception as e:
            print(f"Could not set page size: {e}", flush=True)


def read_total(page):
    """Parse the 'Showing x to y of N results' summary, if present."""
    txt = page.evaluate("() => document.body.innerText")
    m = re.search(r"of\s+([\d,]+)\s+result", txt, re.I)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def scrape_current_page(page):
    """Return the current table's rows as a list of dicts keyed by header."""
    table = page.evaluate("""
    () => {
      const tables = Array.from(document.querySelectorAll('table'));
      if (!tables.length) return null;
      // pick the table with the most body rows
      const t = tables.sort((a, b) =>
        b.querySelectorAll('tbody tr').length - a.querySelectorAll('tbody tr').length)[0];
      const headers = Array.from(t.querySelectorAll('thead th, thead td'))
                           .map(h => h.innerText.trim());
      const rows = Array.from(t.querySelectorAll('tbody tr')).map(tr =>
        Array.from(tr.querySelectorAll('td,th')).map(c => c.innerText.trim()));
      return { headers, rows };
    }
    """)
    if not table or not table["rows"]:
        return []
    headers = table["headers"] or [f"col_{j}" for j in range(len(table["rows"][0]))]
    out = []
    for row in table["rows"]:
        out.append({headers[j] if j < len(headers) else f"col_{j}": v
                    for j, v in enumerate(row)})
    return out


def first_row_signature(page):
    sig = page.evaluate("""
    () => {
      const tr = document.querySelector('table tbody tr');
      return tr ? tr.innerText.trim() : null;
    }
    """)
    return sig


def go_next(page):
    """Click the pagination 'Next' control. Returns True if a click happened."""
    clicked = page.evaluate("""
    () => {
      const cands = Array.from(document.querySelectorAll('button, a')).filter(el => {
        const t  = (el.innerText || '').trim().toLowerCase();
        const al = (el.getAttribute('aria-label') || '').toLowerCase();
        const cn = (el.className || '').toString().toLowerCase();
        return t === 'next' || al === 'next' || el.rel === 'next' || cn.includes('next');
      });
      const btn = cands.find(el =>
        !el.disabled && el.getAttribute('aria-disabled') !== 'true');
      if (btn) { btn.scrollIntoView({block: 'center'}); btn.click(); return true; }
      return false;
    }
    """)
    return clicked


def scrape():
    debug = {"per_page_select": None, "total_reported": None, "pages_scraped": 0}
    seen = set()
    records = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})

        print(f"Loading {SITE_URL} ...", flush=True)
        page.goto(SITE_URL, wait_until="networkidle", timeout=NAV_TIMEOUT)
        trigger_lazy_load(page)

        set_max_page_size(page, debug)

        total = read_total(page)
        debug["total_reported"] = total
        if total is not None:
            print(f"Site reports {total} total records.", flush=True)

        page_num = 0
        while True:
            page_num += 1
            page_rows = scrape_current_page(page)
            new = 0
            for r in page_rows:
                key = tuple(sorted(r.items()))
                if key not in seen:
                    seen.add(key)
                    records.append(r)
                    new += 1
            print(f"Page {page_num}: {len(page_rows)} rows ({new} new), "
                  f"total collected {len(records)}.", flush=True)

            if MAX_PAGES and page_num >= MAX_PAGES:
                print(f"Hit MAX_PAGES={MAX_PAGES}; stopping.", flush=True)
                break
            if new == 0:
                # No new rows -> either last page or pagination didn't advance.
                break

            sig_before = first_row_signature(page)
            if not go_next(page):
                break  # no Next control -> last page
            # wait for the first row to change (table reloaded)
            changed = False
            for _ in range(40):
                page.wait_for_timeout(250)
                if first_row_signature(page) != sig_before:
                    changed = True
                    break
            if not changed:
                break
            time.sleep(PAGE_DELAY)

        debug["pages_scraped"] = page_num
        browser.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_FILE.write_text(json.dumps(debug, indent=2, ensure_ascii=False))

    if not records:
        print("No records scraped. See debug file.", file=sys.stderr)
        sys.exit(1)

    payload = {
        "source": SITE_URL,
        "source_note": "Data from the Isle of Man Land Registry, refreshed monthly.",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "total_reported_by_site": total,
        "record_count": len(records),
        "records": records,
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Wrote {len(records)} records -> {OUT_FILE}", flush=True)
    if total and len(records) < total:
        print(f"NOTE: collected {len(records)} of {total} reported. "
              f"Raise MAX_PAGES or check pagination.", flush=True)


if __name__ == "__main__":
    scrape()

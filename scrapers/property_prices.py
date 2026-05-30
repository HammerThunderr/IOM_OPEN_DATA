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
# FULL_SCRAPE=1 ignores any existing data file and re-scrapes everything.
# Use it for the first/baseline run, or to rebuild from scratch.
FULL_SCRAPE = os.getenv("FULL_SCRAPE", "0") == "1"
NAV_TIMEOUT = 60_000


def rowkey(record):
    """Stable identity for a record (used to detect already-seen rows)."""
    return tuple(sorted((str(k), str(v)) for k, v in record.items()))


def load_existing():
    """Return (records, keyset, baseline_complete) from the existing output
    file. A file is only trusted for incremental updates if it was written by a
    full scrape that reached the end (baseline_complete == true)."""
    if FULL_SCRAPE or not OUT_FILE.exists():
        return [], set(), False
    try:
        data = json.loads(OUT_FILE.read_text())
        recs = data.get("records", [])
        complete = bool(data.get("baseline_complete", False))
        return recs, {rowkey(r) for r in recs}, complete
    except (json.JSONDecodeError, OSError):
        return [], set(), False


def trigger_lazy_load(page):
    """Scroll the page so the x-intersect lazy-loaded widgets fetch, then wait
    for the records table to render."""
    for _ in range(6):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(800)
    page.wait_for_selector("table tbody tr", timeout=NAV_TIMEOUT)


def set_max_page_size(page, debug):
    """Choose the largest 'per page' option. Filament hides the native <select>
    behind a styled control, so we set the value and dispatch input/change
    events directly (Livewire's wire:model.live listens for these)."""
    info = page.evaluate(r"""
    () => {
      const selects = Array.from(document.querySelectorAll('select'));
      for (const s of selects) {
        const opts = Array.from(s.options).map(o => o.value);
        const numericish = opts.filter(v => /^\d+$/.test(v) || v.toLowerCase() === 'all');
        if (numericish.length) {
          let best = null, bestVal = -1;
          for (const o of s.options) {
            const v = o.value.toLowerCase() === 'all' ? Infinity : parseInt(o.value);
            if (!isNaN(v) && v > bestVal) { bestVal = v; best = o.value; }
          }
          s.value = best;
          s.dispatchEvent(new Event('input',  { bubbles: true }));
          s.dispatchEvent(new Event('change', { bubbles: true }));
          return { options: opts, chosen: best };
        }
      }
      return null;
    }
    """)
    debug["per_page_select"] = info
    if info:
        try:
            page.wait_for_timeout(1500)
            page.wait_for_selector("table tbody tr", timeout=NAV_TIMEOUT)
            print(f"Set page size to '{info['chosen']}' "
                  f"(options: {info['options']}).", flush=True)
        except Exception as e:
            print(f"Page size change may not have applied: {e}", flush=True)
    else:
        print("No page-size <select> found; staying at default page size.",
              flush=True)


def read_total(page):
    """Parse the pagination summary total, if the site shows one. Tolerates a
    few phrasings ('of N results/records/entries'). Returns None if absent."""
    txt = page.evaluate("() => document.body.innerText")
    txt = txt.replace("\xa0", " ")
    for pat in (r"of\s+([\d,]+)\s+result",
                r"of\s+([\d,]+)\s+record",
                r"of\s+([\d,]+)\s+entr",
                r"of\s+([\d,]+)\s+row"):
        m = re.search(pat, txt, re.I)
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
    """Advance to the next page of the table.

    Strategy, in order:
      1. An explicit "Next" control (text/aria-label/rel/class contains 'next',
         or a Livewire wire:click of nextPage(...)).
      2. Numbered pagination: find the current page and click current+1
         (matches Livewire setPage(N,...) buttons).

    Returns a dict: {clicked: bool, reason: str, diag: {...}} so the caller can
    log exactly why pagination stopped.
    """
    result = page.evaluate("""
    () => {
      const isUsable = el =>
        !el.disabled && el.getAttribute('aria-disabled') !== 'true'
        && el.offsetParent !== null;

      const all = Array.from(document.querySelectorAll('button, a'));

      // --- diagnostics: every control that smells like pagination ---
      const diag = all
        .filter(el => {
          const wc = (el.getAttribute('wire:click') || '').toLowerCase();
          const al = (el.getAttribute('aria-label') || '').toLowerCase();
          const t  = (el.innerText || '').trim().toLowerCase();
          return wc.includes('page') || al.includes('next') || al.includes('previous')
                 || /^\\d+$/.test(t) || t === 'next' || t === 'previous';
        })
        .map(el => ({
          text: (el.innerText || '').trim().slice(0, 20),
          aria: el.getAttribute('aria-label'),
          wire: el.getAttribute('wire:click'),
          disabled: !isUsable(el),
        }));

      // --- 1. explicit Next ---
      const nextBtn = all.find(el => {
        const t  = (el.innerText || '').trim().toLowerCase();
        const al = (el.getAttribute('aria-label') || '').toLowerCase();
        const cn = (el.className || '').toString().toLowerCase();
        const wc = (el.getAttribute('wire:click') || '').toLowerCase();
        const isNext = t === 'next' || al.includes('next') || el.rel === 'next'
                       || cn.includes('next') || wc.includes('nextpage');
        return isNext && isUsable(el);
      });
      if (nextBtn) {
        nextBtn.scrollIntoView({block: 'center'});
        nextBtn.click();
        return { clicked: true, reason: 'next-button', diag };
      }

      // --- 2. numbered pagination fallback ---
      const current = all.find(el =>
        el.getAttribute('aria-current') === 'page'
        || (el.className || '').toString().toLowerCase().includes('current'));
      if (current) {
        const cur = parseInt((current.innerText || '').trim());
        if (!isNaN(cur)) {
          const target = all.find(el =>
            parseInt((el.innerText || '').trim()) === cur + 1 && isUsable(el));
          if (target) {
            target.scrollIntoView({block: 'center'});
            target.click();
            return { clicked: true, reason: 'numbered-' + (cur + 1), diag };
          }
        }
      }

      return { clicked: false, reason: 'no-usable-next-control', diag };
    }
    """)
    return result


def scrape():
    debug = {"per_page_select": None, "total_reported": None, "pages_scraped": 0}

    existing_records, existing_keys, baseline_complete_flag = load_existing()
    print(f"Existing records on file: {len(existing_records)} "
          f"(baseline_complete={baseline_complete_flag}).", flush=True)

    new_records = []
    new_keys = set()

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

        # --- Decide mode (now that we know the site total) ---
        # Incremental ONLY when the file carries a trusted complete baseline
        # (and isn't clearly behind the site total). Otherwise full scrape and
        # rebuild. This is what makes it self-healing with no flags: a stale or
        # unstamped file (like an old partial scrape) is rebuilt automatically.
        behind_total = bool(total and len(existing_records) < total * 0.9)
        baseline_trustworthy = (existing_keys and baseline_complete_flag
                                and not behind_total)
        incremental = baseline_trustworthy and not FULL_SCRAPE

        if incremental:
            mode = "INCREMENTAL (new records only)"
            scan_keys = existing_keys      # stop when we reach saved records
        else:
            if FULL_SCRAPE:
                mode = "FULL (forced by FULL_SCRAPE)"
            elif existing_keys and not baseline_complete_flag:
                mode = (f"FULL (file not marked complete - rebuilding from "
                        f"{len(existing_records)} records)")
            elif behind_total:
                mode = (f"FULL (baseline behind: have {len(existing_records)}, "
                        f"site has {total} - rebuilding)")
            else:
                mode = "FULL (first run, no baseline)"
            scan_keys = set()              # treat everything as fresh; no early stop
        print(f"Mode: {mode}.", flush=True)

        page_num = 0
        while True:
            page_num += 1
            page_rows = scrape_current_page(page)
            new_this_page = 0
            hit_known = False
            for r in page_rows:
                key = rowkey(r)
                if key in scan_keys:
                    hit_known = True            # reached our baseline boundary
                    continue
                if key in new_keys:
                    continue                    # page overlap within this run
                new_keys.add(key)
                new_records.append(r)
                new_this_page += 1
            print(f"Page {page_num}: {len(page_rows)} rows, "
                  f"{new_this_page} collected this page "
                  f"(run total {len(new_records)}).", flush=True)

            # Incremental: once we touch records already saved, everything older
            # is already on file -> stop.
            if incremental and hit_known:
                debug["stop_reason"] = "incremental: reached previously-saved records"
                print(f"STOP: {debug['stop_reason']} on page {page_num}.", flush=True)
                break
            if MAX_PAGES and page_num >= MAX_PAGES:
                debug["stop_reason"] = f"hit MAX_PAGES={MAX_PAGES}"
                print(f"STOP: {debug['stop_reason']}.", flush=True)
                break
            if new_this_page == 0:
                debug["stop_reason"] = "page produced no new rows"
                print(f"STOP: {debug['stop_reason']} (page {page_num}).", flush=True)
                break

            sig_before = first_row_signature(page)
            nav = go_next(page)
            debug["last_pagination_diag"] = nav.get("diag")
            if not nav["clicked"]:
                debug["stop_reason"] = f"no next control ({nav['reason']})"
                print(f"STOP: {debug['stop_reason']} after page {page_num}. "
                      f"Pagination controls seen: {nav.get('diag')}", flush=True)
                break

            changed = False
            for _ in range(60):
                page.wait_for_timeout(250)
                if first_row_signature(page) != sig_before:
                    changed = True
                    break
            if not changed:
                debug["stop_reason"] = (f"clicked next ({nav['reason']}) but table "
                                        f"did not change within timeout")
                print(f"STOP: {debug['stop_reason']} after page {page_num}.",
                      flush=True)
                break
            time.sleep(PAGE_DELAY)

        debug["pages_scraped"] = page_num
        browser.close()

    # Merge: newest (this run) first, then everything we already had.
    merged = []
    merged_keys = set()
    for r in new_records + existing_records:
        key = rowkey(r)
        if key not in merged_keys:
            merged_keys.add(key)
            merged.append(r)

    # Records we collected that were NOT already on file (the real additions).
    added = sum(1 for r in new_records if rowkey(r) not in existing_keys)

    # Decide whether the result is a trustworthy complete baseline:
    #  - incremental run: the baseline was already complete; we only prepended
    #    newer records, so it stays complete.
    #  - full run: complete if pagination reached its natural end ("no next
    #    control"), or we collected >=90% of the site's reported total.
    stop_reason = debug.get("stop_reason", "")
    if incremental:
        baseline_complete = True
    else:
        clean_end = stop_reason.startswith("no next control")
        enough = bool(total and len(merged) >= total * 0.9)
        baseline_complete = bool(clean_end or enough)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_FILE.write_text(json.dumps(debug, indent=2, ensure_ascii=False))

    if not merged:
        print("No records collected and none on file. See debug file.",
              file=sys.stderr)
        sys.exit(1)

    payload = {
        "source": SITE_URL,
        "source_note": "Data from the Isle of Man Land Registry, refreshed monthly.",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "scrape_mode": mode,
        "baseline_complete": baseline_complete,
        "total_reported_by_site": total,
        "added_this_run": added,
        "record_count": len(merged),
        "records": merged,
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Done. {added} new record(s) added this run; "
          f"{len(merged)} records total -> {OUT_FILE}", flush=True)
    if not incremental and total and len(merged) < total:
        print(f"NOTE: collected {len(merged)} of {total} reported. "
              f"Pagination stopped early - check the STOP reason above.",
              flush=True)


if __name__ == "__main__":
    scrape()

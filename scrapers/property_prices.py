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


def prepare_table(page, debug):
    """Configure the table to show the FULL dataset (target ~40k records):
      1. Open the Filter panel so all filter controls are in the DOM.
      2. Clear every filter: dropdowns -> 'All'/empty; 'minimum'-type bounds ->
         lowest option; 'maximum'-type bounds -> highest option; filter text/
         number inputs -> emptied. (We avoid touching ambiguous controls so we
         never accidentally narrow the results.)
      3. Apply, then maximise the records-per-page control.
    Filament hides native inputs, so we set value + dispatch input/change, which
    is what Livewire's wire:model.live listens for."""

    # 1. Open the filter panel (funnel button -> mountAction('filters')).
    opened = page.evaluate(r"""
    () => {
      const els = Array.from(document.querySelectorAll('button, a'));
      const b = els.find(el =>
        (el.getAttribute('wire:click') || '').includes("mountAction('filters')")
        || (el.innerText || '').trim().toLowerCase() === 'filter');
      if (b) { b.click(); return true; }
      return false;
    }
    """)
    debug["filter_panel_opened"] = bool(opened)
    if opened:
        page.wait_for_timeout(1500)

    # 2. Clear all filter controls.
    cleared = page.evaluate(r"""
    () => {
      const log = [];
      const fire = (el, val, why) => {
        el.value = val;
        el.dispatchEvent(new Event('input',  { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        log.push({ tag: el.tagName,
                   model: el.getAttribute('wire:model.live') || el.getAttribute('wire:model'),
                   set: val, why });
      };
      const idOf = el => [
        el.getAttribute('wire:model.live'), el.getAttribute('wire:model'),
        el.getAttribute('name'), el.getAttribute('aria-label'),
        el.getAttribute('placeholder'), el.id
      ].filter(Boolean).join(' ').toLowerCase();

      // SELECT filters
      document.querySelectorAll('select').forEach(s => {
        const id = idOf(s);
        if (id.includes('recordsperpage') || id.includes('perpage')) return; // later
        const clearOpt = Array.from(s.options).find(o =>
          o.value === '' || /\b(all|any)\b/i.test(o.textContent || ''));
        if (clearOpt) { fire(s, clearOpt.value, 'clear->all'); return; }
        const nums = Array.from(s.options)
          .map(o => ({ v: parseInt(o.value), raw: o.value }))
          .filter(x => !isNaN(x.v));
        if (!nums.length) return;
        if (/min|from|lower|start|gte|greater|low/.test(id)) {
          const lo = nums.reduce((a, b) => b.v < a.v ? b : a);
          fire(s, lo.raw, 'min->lowest');
        } else if (/max|to\b|upper|end|lte|less|high/.test(id)) {
          const hi = nums.reduce((a, b) => b.v > a.v ? b : a);
          fire(s, hi.raw, 'max->highest');
        }
        // ambiguous -> leave alone (don't risk narrowing)
      });

      // INPUT filters (number/text) -> empty = no bound
      document.querySelectorAll('input').forEach(inp => {
        const t = (inp.type || '').toLowerCase();
        if (['checkbox', 'radio', 'hidden', 'submit', 'button'].includes(t)) return;
        const id = idOf(inp);
        const looksFilter = /filter|price|min|max|from|to|value|amount/.test(id);
        if (looksFilter && inp.value) fire(inp, '', 'clear-input');
      });

      return log;
    }
    """)
    debug["filters_cleared"] = cleared

    # Some filter forms need an explicit Apply; harmless if they're live.
    page.evaluate(r"""
    () => {
      const b = Array.from(document.querySelectorAll('button')).find(el => {
        const t = (el.innerText || '').trim().toLowerCase();
        return ['apply', 'apply filters', 'filter', 'save', 'done'].includes(t)
               && el.offsetParent !== null;
      });
      if (b) b.click();
    }
    """)
    page.wait_for_timeout(1500)
    # Close the panel if it's still open, so it doesn't cover the table.
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    page.wait_for_timeout(500)

    # 3. Maximise records-per-page.
    pp = page.evaluate(r"""
    () => {
      const s = Array.from(document.querySelectorAll('select')).find(el => {
        const m = (el.getAttribute('wire:model.live')
                   || el.getAttribute('wire:model') || '').toLowerCase();
        return m.includes('recordsperpage') || m.includes('perpage');
      });
      if (!s) return null;
      let best = null, bestVal = -1;
      for (const o of s.options) {
        const v = o.value.toLowerCase() === 'all' ? Infinity : parseInt(o.value);
        if (!isNaN(v) && v > bestVal) { bestVal = v; best = o.value; }
      }
      if (best === null) return null;
      s.value = best;
      s.dispatchEvent(new Event('input',  { bubbles: true }));
      s.dispatchEvent(new Event('change', { bubbles: true }));
      return best;
    }
    """)
    debug["page_size_set"] = pp

    try:
        page.wait_for_timeout(2000)
        page.wait_for_selector("table tbody tr", timeout=NAV_TIMEOUT)
    except Exception as e:
        print(f"Table prepare may not have fully applied: {e}", flush=True)

    print(f"Prepared table | panel_opened={bool(opened)} | "
          f"filters_cleared={cleared} | page_size={pp}", flush=True)


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


def pagination_state(page):
    """Read the current page number, the max page number, and whether a usable
    Next control exists. Lets us stop only when provably on the last page."""
    return page.evaluate(r"""
    () => {
      const items = Array.from(document.querySelectorAll('button, a'));
      const nums = [];
      let current = null;
      for (const el of items) {
        const t = (el.innerText || '').trim();
        const wc = (el.getAttribute('wire:click') || '');
        if (/^\d+$/.test(t)) {
          const n = parseInt(t);
          nums.push(n);
          const cn = (el.className || '').toString().toLowerCase();
          if (el.getAttribute('aria-current') === 'page'
              || el.getAttribute('aria-current') === 'true'
              || cn.includes('current') || cn.includes('active')) {
            current = n;
          }
        }
        const m = wc.match(/(?:setPage|gotoPage)\((\d+)/);
        if (m) nums.push(parseInt(m[1]));
      }
      const max = nums.length ? Math.max(...nums) : null;
      const hasNext = items.some(el => {
        const t  = (el.innerText || '').trim().toLowerCase();
        const al = (el.getAttribute('aria-label') || '').toLowerCase();
        const cn = (el.className || '').toString().toLowerCase();
        const wc = (el.getAttribute('wire:click') || '').toLowerCase();
        const isNext = t === 'next' || al.includes('next') || el.rel === 'next'
                       || cn.includes('next') || wc.includes('nextpage');
        return isNext && !el.disabled
               && el.getAttribute('aria-disabled') !== 'true'
               && el.offsetParent !== null;
      });
      return { current, max, hasNext };
    }
    """)


def robust_advance(page):
    """Click Next and wait (generously) for the table to actually change.
    Returns {ok: bool, reason: str, diag: ...}. Waits up to ~45s per attempt and
    retries once, so a slow Livewire round-trip is never mistaken for the end."""
    prev_sig = first_row_signature(page)
    prev_state = pagination_state(page)

    for attempt in range(2):
        nav = go_next(page)
        if not nav["clicked"]:
            return {"ok": False, "reason": f"no next control ({nav['reason']})",
                    "diag": nav.get("diag")}
        for _ in range(180):                      # 180 * 250ms = 45s
            page.wait_for_timeout(250)
            if first_row_signature(page) != prev_sig:
                return {"ok": True, "reason": nav["reason"]}
            st = pagination_state(page)
            if (st["current"] and prev_state["current"]
                    and st["current"] > prev_state["current"]):
                return {"ok": True, "reason": nav["reason"]}
        page.wait_for_timeout(1500)               # let things settle, then retry

    return {"ok": False,
            "reason": "clicked next but table did not change after retries",
            "diag": nav.get("diag")}


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

        prepare_table(page, debug)

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
        SAFETY_CAP = 5000      # absurdly high; just prevents an infinite loop
        while True:
            page_num += 1
            if page_num > SAFETY_CAP:
                debug["stop_reason"] = f"hit safety cap of {SAFETY_CAP} pages"
                print(f"STOP: {debug['stop_reason']}.", flush=True)
                break
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

            st = pagination_state(page)
            here = st["current"] or page_num
            print(f"Page {here}"
                  + (f"/{st['max']}" if st["max"] else "")
                  + f": {len(page_rows)} rows, {new_this_page} collected "
                    f"(run total {len(new_records)}).", flush=True)

            # Incremental: once we touch records already saved, everything older
            # is already on file -> stop.
            if incremental and hit_known:
                debug["stop_reason"] = "incremental: reached previously-saved records"
                print(f"STOP: {debug['stop_reason']} on page {here}.", flush=True)
                break
            if MAX_PAGES and page_num >= MAX_PAGES:
                debug["stop_reason"] = f"hit MAX_PAGES={MAX_PAGES}"
                print(f"STOP: {debug['stop_reason']}.", flush=True)
                break

            # Provably on the last page? Only then is it safe to finish.
            on_last_page = (
                (st["max"] is not None and st["current"] is not None
                 and st["current"] >= st["max"])
                or not st["hasNext"]
            )
            if on_last_page:
                debug["stop_reason"] = (f"reached last page "
                                        f"(page {st['current']} of {st['max']}, "
                                        f"hasNext={st['hasNext']})")
                print(f"STOP: {debug['stop_reason']}.", flush=True)
                break

            # Not on the last page -> force an advance, with long waits + retry.
            nav = robust_advance(page)
            debug["last_pagination_diag"] = nav.get("diag")
            if not nav["ok"]:
                debug["stop_reason"] = nav["reason"]
                print(f"STOP: {debug['stop_reason']} after page {here}. "
                      f"Pagination controls seen: {nav.get('diag')}", flush=True)
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
        clean_end = (stop_reason.startswith("no next control")
                     or stop_reason.startswith("reached last page"))
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

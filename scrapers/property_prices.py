"""
Isle of Man property price scraper (API-based).

Source: https://propertyprices.im/  (data from the IoM Land Registry,
refreshed monthly, records since November 2000).

The site is a Filament v4 (Laravel + Livewire) app. Rather than driving the UI,
this scraper talks to POST /livewire/update directly, which is far more reliable
for bulk extraction.

Flow:
1. Bootstrap homepage -> CSRF + cookies, then run the table widget __lazyLoad to
   get a valid component snapshot (the snapshot carries a server checksum we
   cannot forge, so we always reuse the latest snapshot the server returns).
2. Raise tableRecordsPerPage and clear filters via `updates`.
3. Paginate by setting paginators.page; parse each returned HTML table.
4. For rows with Records>1, call mountAction('viewHistory',{},{recordKey,table})
   and parse the modal for per-sale {year, price}.

Output: one row per PROPERTY, history nested (easy to load in app/web):
  {address, town, postcode, latest_sale_price, sales_count, record_key,
   sales:[{year, price}, ...]}

Env: FULL_SCRAPE=1 force rebuild | MAX_PAGES=N cap (0=unlimited) |
     FETCH_HISTORY=0 skip history | PAGE_DELAY=secs (default 0.4)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from html import unescape

import requests

BASE = "https://propertyprices.im"
HOME = BASE + "/"
UPDATE = BASE + "/livewire/update"

OUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_FILE = OUT_DIR / "property_prices.json"
DEBUG_FILE = OUT_DIR / "_debug_property_prices_captures.json"

FULL_SCRAPE = os.getenv("FULL_SCRAPE", "0") == "1"
MAX_PAGES = int(os.getenv("MAX_PAGES", "0"))
FETCH_HISTORY = os.getenv("FETCH_HISTORY", "1") == "1"
PAGE_DELAY = float(os.getenv("PAGE_DELAY", "0.4"))
TIMEOUT = 45

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _error_snippet(text):
    """Pull a readable message out of a Symfony/Livewire HTML error response."""
    if not text:
        return "(empty body)"
    # JSON error?
    try:
        j = json.loads(text)
        return json.dumps(j)[:400]
    except (json.JSONDecodeError, ValueError):
        pass
    # Symfony "ignition"/whoops pages put the message in <title> or a known tag.
    for pat in (r'<title>(.*?)</title>',
                r'"message"\s*:\s*"([^"]+)"',
                r'<span[^>]*class="[^"]*exception_message[^"]*"[^>]*>(.*?)</span>'):
        m = re.search(pat, text, re.S | re.I)
        if m:
            msg = re.sub(r"<[^>]+>", " ", m.group(1))
            msg = re.sub(r"\s+", " ", unescape(msg)).strip()
            if msg:
                return msg[:400]
    # Fallback: first non-empty text-ish line.
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:400] if plain else "(unparseable body)"


def rowkey(rec):
    if rec.get("record_key"):
        return ("k", str(rec["record_key"]))
    return ("a", rec.get("address", ""), rec.get("postcode", ""),
            rec.get("latest_sale_price", ""))


def load_existing():
    if FULL_SCRAPE or not OUT_FILE.exists():
        return [], set(), False
    try:
        data = json.loads(OUT_FILE.read_text())
        recs = data.get("records", [])
        return recs, {rowkey(r) for r in recs}, bool(data.get("baseline_complete"))
    except (json.JSONDecodeError, OSError):
        return [], set(), False


class Livewire:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.csrf = None
        self.snapshot = None
        self.snapshot_raw = None
        self.last_html = ""

    def bootstrap(self):
        r = self.s.get(HOME, timeout=TIMEOUT)
        r.raise_for_status()
        html = r.text
        m = (re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
             or re.search(r'data-csrf="([^"]+)"', html))
        if not m:
            raise RuntimeError("CSRF token not found")
        self.csrf = m.group(1)
        snap_raw, token = self._find_table_component(html)
        self.snapshot_raw = snap_raw
        self.snapshot = json.loads(snap_raw)
        self._hydrate(token)

    def _hydrate(self, token):
        """Hydrate the deferred table component. The initial snapshot is a lazy
        placeholder (lazyLoaded:false). We try several strategies, freshly using
        THIS session's token, and keep the first that yields table HTML."""
        errors = []

        # Strategy 1: the browser's __lazyLoad with our freshly-harvested token.
        if token:
            try:
                self._absorb(self._post([{
                    "snapshot": self.snapshot_raw, "updates": {},
                    "calls": [{"path": "", "method": "__lazyLoad",
                               "params": [token]}]}]))
                if "viewHistory" in self.last_html:
                    print("Hydrated via __lazyLoad.", flush=True)
                    return
            except Exception as e:
                errors.append(f"__lazyLoad: {e}")

        # Strategy 2: Filament's loadTable() method on the placeholder snapshot.
        for method in ("loadTable", "$refresh"):
            try:
                self._absorb(self._post([{
                    "snapshot": self.snapshot_raw, "updates": {},
                    "calls": [{"path": "", "method": method, "params": []}]}]))
                if "viewHistory" in self.last_html:
                    print(f"Hydrated via {method}().", flush=True)
                    return
            except Exception as e:
                errors.append(f"{method}: {e}")

        # Strategy 3: flip isTableLoaded via an updates call (forces a render).
        try:
            self._absorb(self._post([{
                "snapshot": self.snapshot_raw,
                "updates": {"isTableLoaded": True}, "calls": []}]))
            if "viewHistory" in self.last_html:
                print("Hydrated via isTableLoaded update.", flush=True)
                return
        except Exception as e:
            errors.append(f"isTableLoaded: {e}")

        raise RuntimeError("Could not hydrate table. Attempts: "
                           + " | ".join(errors))

    def _find_table_component(self, html):
        name = "app.filament.public.widgets.property-search-table"
        for m in re.finditer(r'wire:snapshot="([^"]+)"', html):
            raw = unescape(m.group(1))
            try:
                snap = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if snap.get("memo", {}).get("name") == name:
                seg = html[m.end(): m.end() + 4000]
                tok = re.search(r"__lazyLoad\('([^']+)'\)", seg)
                return raw, (tok.group(1) if tok else None)
        raise RuntimeError("property-search-table component not found")

    def _post(self, components):
        body = {"_token": self.csrf, "components": components}
        # Headers must match the browser's real request (from HAR) closely:
        # X-Livewire is present but EMPTY; Accept is */*.
        headers = {"Content-Type": "application/json", "X-Livewire": "",
                   "Accept": "*/*", "Origin": BASE, "Referer": HOME,
                   "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                   "Sec-Fetch-Site": "same-origin",
                   "X-Requested-With": "XMLHttpRequest"}
        r = self.s.post(UPDATE, data=json.dumps(body), headers=headers,
                        timeout=TIMEOUT)
        if not r.ok:
            snippet = _error_snippet(r.text)
            raise RuntimeError(f"/livewire/update returned {r.status_code}. "
                               f"Server said: {snippet}")
        return r.json()

    def _absorb(self, resp):
        comp = resp["components"][0]
        self.snapshot_raw = comp["snapshot"]
        self.snapshot = json.loads(self.snapshot_raw)
        html = comp.get("effects", {}).get("html")
        if html:
            self.last_html = html
        return comp

    def _payload(self, updates=None, calls=None):
        return {"snapshot": self.snapshot_raw, "updates": updates or {},
                "calls": calls or []}

    def set_state(self, updates):
        self._absorb(self._post([self._payload(updates=updates)]))

    def goto_page(self, page):
        self.set_state({"paginators.page": page})

    def call_action(self, method, params):
        return self._absorb(self._post([self._payload(
            calls=[{"path": "", "method": method, "params": params}])]))


def _clean(cell_html):
    t = re.sub(r"<[^>]+>", " ", cell_html)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def _to_int(s):
    m = re.search(r"\d+", s or "")
    return int(m.group(0)) if m else 1


def parse_table(html):
    out = []
    for chunk in re.split(r"<tr", html):
        if "viewHistory" not in chunk:
            continue
        key = (re.search(r'recordKey\\u0022:\\u0022(\d+)\\u0022', chunk)
               or re.search(r'recordKey":"(\d+)"', chunk))
        cells = re.findall(r"<td[^>]*>(.*?)</td>", chunk, re.S)
        vals = [_clean(c) for c in cells]
        if len(vals) < 5:
            continue
        out.append({
            "address": vals[0], "town": vals[1], "postcode": vals[2],
            "latest_sale_price": vals[3], "sales_count": _to_int(vals[4]),
            "record_key": key.group(1) if key else None,
        })
    return out


def parse_history(html):
    sales = []
    for chunk in re.split(r"<tr", html):
        text = _clean(chunk)
        price = re.search(r"£[\d,]+(?:\.\d{2})?", text)
        year = re.search(r"\b(?:19|20)\d{2}\b", text)
        if price and year:
            sales.append({"year": year.group(0), "price": price.group(0)})
    if sales:
        return sales
    text = _clean(html)
    prices = re.findall(r"£[\d,]+(?:\.\d{2})?", text)
    years = re.findall(r"\b(?:19|20)\d{2}\b", text)
    return [{"year": y, "price": p} for y, p in zip(years, prices)]


def scrape():
    debug = {"steps": [], "stop_reason": None, "page_size": None,
             "total_seen": 0, "history_fetched": 0, "history_failed": 0}

    existing, existing_keys, complete = load_existing()
    print(f"Existing records on file: {len(existing)} "
          f"(baseline_complete={complete}).", flush=True)

    lw = Livewire()
    print("Bootstrapping (homepage + table lazy-load)...", flush=True)
    lw.bootstrap()
    debug["steps"].append("bootstrapped")

    for size in (100, 50, 25):
        try:
            lw.set_state({"tableRecordsPerPage": size})
            applied = lw.snapshot["data"].get("tableRecordsPerPage")
            debug["page_size"] = applied
            print(f"Requested page size {size}; server applied {applied}.",
                  flush=True)
            break
        except Exception as e:
            print(f"Page-size {size} failed: {e}", flush=True)

    for upd in ({"tableFilters.price.min": None, "tableFilters.price.max": None},
                {"tableFilters": []}):
        try:
            lw.set_state(upd)
        except Exception:
            pass

    incremental = bool(existing_keys) and complete and not FULL_SCRAPE
    mode = ("INCREMENTAL" if incremental else
            "FULL (forced)" if FULL_SCRAPE else
            "FULL (file not complete)" if existing_keys else "FULL (first run)")
    scan_keys = existing_keys if incremental else set()
    print(f"Mode: {mode}.", flush=True)

    new_records, new_keys = [], set()
    page = 0
    last_first = None
    while True:
        page += 1
        try:
            if page > 1:
                lw.goto_page(page)
        except Exception as e:
            debug["stop_reason"] = f"goto_page({page}) failed: {e}"
            print(f"STOP: {debug['stop_reason']}", flush=True)
            break
        rows = parse_table(lw.last_html)
        if not rows:
            debug["stop_reason"] = "no rows parsed"
            print(f"STOP: {debug['stop_reason']} on page {page}.", flush=True)
            break
        first = rows[0].get("record_key") or rows[0].get("address")
        if page > 1 and first == last_first:
            debug["stop_reason"] = "page did not advance (same first row)"
            print(f"STOP: {debug['stop_reason']} at page {page}.", flush=True)
            break
        last_first = first
        added = 0
        hit_known = False
        for r in rows:
            k = rowkey(r)
            if k in scan_keys:
                hit_known = True
                continue
            if k in new_keys:
                continue
            new_keys.add(k)
            new_records.append(r)
            added += 1
        debug["total_seen"] += len(rows)
        print(f"Page {page}: {len(rows)} rows, {added} new "
              f"(run total {len(new_records)}).", flush=True)
        if incremental and hit_known:
            debug["stop_reason"] = "incremental: reached known records"
            print(f"STOP: {debug['stop_reason']} on page {page}.", flush=True)
            break
        if MAX_PAGES and page >= MAX_PAGES:
            debug["stop_reason"] = f"hit MAX_PAGES={MAX_PAGES}"
            print(f"STOP: {debug['stop_reason']}.", flush=True)
            break
        if added == 0 and not incremental:
            debug["stop_reason"] = "page produced no new rows"
            print(f"STOP: {debug['stop_reason']} on page {page}.", flush=True)
            break
        time.sleep(PAGE_DELAY)

    if FETCH_HISTORY:
        targets = [r for r in new_records
                   if r.get("sales_count", 1) > 1 and r.get("record_key")]
        print(f"Fetching history for {len(targets)} multi-sale properties...",
              flush=True)
        for i, r in enumerate(targets, 1):
            try:
                comp = lw.call_action("mountAction",
                    ["viewHistory", {}, {"recordKey": r["record_key"], "table": True}])
                modal = comp.get("effects", {}).get("html", "") or lw.last_html
                hist = parse_history(modal)
                if hist:
                    r["sales"] = hist
                    debug["history_fetched"] += 1
                else:
                    debug["history_failed"] += 1
                try:
                    lw.call_action("unmountAction", ["viewHistory"])
                except Exception:
                    pass
            except Exception as e:
                debug["history_failed"] += 1
                if debug["history_failed"] <= 3:
                    print(f"  history fail {r['record_key']}: {e}", flush=True)
            if i % 50 == 0:
                print(f"  ...{i}/{len(targets)} histories", flush=True)
            time.sleep(PAGE_DELAY)

    for r in new_records:
        if "sales" not in r:
            r["sales"] = [{"year": None, "price": r.get("latest_sale_price")}]

    merged, seen = [], set()
    for r in new_records + existing:
        k = rowkey(r)
        if k not in seen:
            seen.add(k)
            merged.append(r)
    added_total = sum(1 for r in new_records if rowkey(r) not in existing_keys)

    natural_end = debug["stop_reason"] in (
        "page produced no new rows", "no rows parsed",
        "page did not advance (same first row)")
    baseline_complete = True if incremental else natural_end

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_FILE.write_text(json.dumps(debug, indent=2, ensure_ascii=False))
    if not merged:
        print("No records collected. See debug file.", file=sys.stderr)
        sys.exit(1)

    payload = {
        "source": HOME,
        "source_note": "Data from the Isle of Man Land Registry, refreshed monthly.",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "scrape_mode": mode,
        "baseline_complete": baseline_complete,
        "record_count": len(merged),
        "added_this_run": added_total,
        "records": merged,
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Done. {added_total} new; {len(merged)} total; "
          f"history fetched={debug['history_fetched']} "
          f"failed={debug['history_failed']} -> {OUT_FILE}", flush=True)


if __name__ == "__main__":
    scrape()

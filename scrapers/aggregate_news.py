"""
Aggregate Isle of Man news from several RSS/Atom feeds into one JSON "API".

Feeds:
  - Three FM       (https://www.three.fm/news/isle-of-man-news/feed.xml)
  - Manx Radio     (https://www.manxradio.com/news/isle-of-man-news/feed.xml)
  - IoM Government  (https://www.gov.im/news/2026/RssNews)
  - Google News     ("isle of man" search RSS)

Each item is normalised to:
  {title, link, published (ISO 8601 UTC | null), summary, source, image}

"image" is a best-effort thumbnail URL taken from the feed itself
(media:content / media:thumbnail / enclosure / first <img> in the summary).
Empty string when the feed provides none (common for Google News items).

Items are de-duplicated (by link, then by normalised title) and sorted newest
first. A failing feed never breaks the run - it is recorded under "feeds" with
its status so you can see what happened.

Output: data/news.json
  {
    generated_at, item_count,
    feeds: [ {source, url, ok, item_count, error?}, ... ],
    items: [ {title, link, published, summary, source, image}, ... ]
  }

Env:
  NEWS_MAX_ITEMS   cap total items (default 150)
  NEWS_MAX_AGE_DAYS drop items older than N days (default 0 = no limit)
"""

import html
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser

OUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_FILE = OUT_DIR / "news.json"

FEEDS = [
    ("Three FM", "https://www.three.fm/news/isle-of-man-news/feed.xml"),
    ("Manx Radio", "https://www.manxradio.com/news/isle-of-man-news/feed.xml"),
    ("Isle of Man Government", "https://www.gov.im/news/2026/RssNews"),
    ("Google News",
     "https://news.google.com/rss/search?svnum=10&as_scoring=r&ie=UTF-8&oe=utf8"
     "&hl=en-US&as_drrb=q&as_qdr=d&as_mind=7&as_minm=6&as_maxd=7&as_maxm=7"
     "&q=isle+of+man&gl=US&ceid=US:en"),
]

MAX_ITEMS = int(os.getenv("NEWS_MAX_ITEMS", "150"))
MAX_AGE_DAYS = int(os.getenv("NEWS_MAX_AGE_DAYS", "0"))

UA = ("Mozilla/5.0 (compatible; IOM-OpenData-NewsBot/1.0; "
      "+https://github.com/)")

_IMG_IN_HTML = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif)(\?|#|$)", re.I)


def clean_text(s, limit=500):
    """Strip HTML tags/entities from a summary and trim length."""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit].rstrip() + ("..." if len(s) > limit else "")


def _normalise_img_url(url):
    """Make protocol-relative URLs usable; reject non-http junk."""
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        return ""
    return url


def extract_image(entry):
    """Best-effort thumbnail URL from a feed entry, or ''.

    Checks, in order:
      1. media:content  (feedparser: entry.media_content)
      2. media:thumbnail (feedparser: entry.media_thumbnail)
      3. enclosure links with an image/* type
      4. first <img> inside the summary/description/content HTML
    """
    # 1. media:content — prefer entries explicitly marked as images
    for m in (entry.get("media_content") or []):
        url = _normalise_img_url(m.get("url"))
        if not url:
            continue
        if (m.get("medium") == "image"
                or (m.get("type") or "").startswith("image")
                or _IMG_EXT.search(url)):
            return url

    # 2. media:thumbnail
    for m in (entry.get("media_thumbnail") or []):
        url = _normalise_img_url(m.get("url"))
        if url:
            return url

    # 3. enclosures (feedparser exposes them under links with rel=enclosure)
    for l in (entry.get("links") or []):
        if l.get("rel") == "enclosure" and (l.get("type") or "").startswith("image"):
            url = _normalise_img_url(l.get("href"))
            if url:
                return url

    # 4. first <img> in the HTML of summary/description/content
    for key in ("summary", "description"):
        m = _IMG_IN_HTML.search(entry.get(key) or "")
        if m:
            url = _normalise_img_url(m.group(1))
            if url:
                return url
    for c in (entry.get("content") or []):
        m = _IMG_IN_HTML.search(c.get("value") or "")
        if m:
            url = _normalise_img_url(m.group(1))
            if url:
                return url

    return ""


def to_iso(entry):
    """Best-effort published time -> ISO 8601 UTC string, or None."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except (TypeError, ValueError):
                pass
    return None


def norm_title(t):
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def parse_feed(source, url):
    info = {"source": source, "url": url, "ok": False, "item_count": 0}
    items = []
    try:
        d = feedparser.parse(url, agent=UA)
        # feedparser sets bozo on malformed feeds but often still has entries.
        if d.get("bozo") and not d.get("entries"):
            raise RuntimeError(str(d.get("bozo_exception", "parse error")))
        for e in d.entries:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not title and not link:
                continue
            items.append({
                "title": html.unescape(title),
                "link": link,
                "published": to_iso(e),
                "summary": clean_text(e.get("summary") or e.get("description")),
                "source": source,
                "image": extract_image(e),
            })
        info["ok"] = True
        info["item_count"] = len(items)
    except Exception as exc:                       # noqa: BLE001
        info["error"] = str(exc)
    return info, items


def aggregate():
    feeds_info, all_items = [], []
    for source, url in FEEDS:
        info, items = parse_feed(source, url)
        feeds_info.append(info)
        all_items.extend(items)
        status = "ok" if info["ok"] else f"FAIL ({info.get('error')})"
        print(f"{source:24} {status:>10}  {info['item_count']} items", flush=True)

    # Optional age filter.
    if MAX_AGE_DAYS > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
        kept = []
        for it in all_items:
            if not it["published"]:
                kept.append(it)
                continue
            try:
                if datetime.fromisoformat(it["published"]) >= cutoff:
                    kept.append(it)
            except ValueError:
                kept.append(it)
        all_items = kept

    # De-duplicate by link, then by normalised title.
    seen_links, seen_titles, deduped = set(), set(), []
    for it in all_items:
        lk = it["link"] or ""
        tt = norm_title(it["title"])
        if lk and lk in seen_links:
            continue
        if tt and tt in seen_titles:
            continue
        if lk:
            seen_links.add(lk)
        if tt:
            seen_titles.add(tt)
        deduped.append(it)

    # Sort newest first; undated items sink to the bottom.
    deduped.sort(key=lambda x: x["published"] or "", reverse=True)
    deduped = deduped[:MAX_ITEMS]

    payload = {
        "title": "Isle of Man news - aggregated",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(deduped),
        "feeds": feeds_info,
        "items": deduped,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    ok = sum(1 for f in feeds_info if f["ok"])
    with_img = sum(1 for it in deduped if it.get("image"))
    print(f"\n{len(deduped)} items ({with_img} with images) "
          f"from {ok}/{len(FEEDS)} feeds -> {OUT_FILE}")
    # Fail the CI job only if EVERY feed failed (so one outage doesn't break it).
    if ok == 0:
        print("All feeds failed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    aggregate()

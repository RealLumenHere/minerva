#!/usr/bin/env python3
"""
lumen_scout_expanded.py - Expanded multi-domain scraping engine
Integrates 30 sources across 7 tiers: hardware, tools, security, runtimes, power, observability, inference.
"""

import os, sys, json, time, hashlib, re, sqlite3, feedparser, requests, logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Any

MINERVA_DB   = Path("/home/lumen/minerva/minerva.db")
LOG_PATH     = Path("/home/lumen/minerva/logs/scout_expanded.log")
TIMEOUT      = 20
RATE_LIMIT   = 2

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()])
log = logging.getLogger("scout_expanded")
HEADERS = {"User-Agent": "LumenScout/2.0 (homelab research; lumennode.uk)"}

sys.path.insert(0, "/home/lumen/minerva")
try:
    from minerva_sources_expanded import ALL_SOURCES, GLOBAL_KEYWORDS
    log.info(f"Loaded {len(ALL_SOURCES)} sources from minerva_sources_expanded")
except ImportError as e:
    log.error(f"Failed to import sources: {e}")
    sys.exit(1)

# 2026-07-05: pre-classifier off-domain junk gate (guarded, reversible via drop-log)
try:
    from crawler_taxonomy_filter import is_off_domain_junk
except ImportError:
    def is_off_domain_junk(source, title): return False
JUNK_LOG = Path("/home/lumen/logs/scout_junk_dropped.jsonl")
JUNK_LOG.parent.mkdir(parents=True, exist_ok=True)
def _log_junk_drop(source, title, url):
    try:
        with open(JUNK_LOG, "a") as _f:
            _f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                 "source": source, "title": title, "url": url}) + "\n")
    except Exception:
        pass

def ensure_db():
    conn = sqlite3.connect(str(MINERVA_DB))
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS classified_intel (
        id TEXT PRIMARY KEY, source TEXT, title TEXT, url TEXT, audience_relevance REAL,
        self_relevance REAL, category TEXT, reasoning TEXT, ts TEXT, processed INTEGER DEFAULT 0)""")
    conn.commit()
    return conn

def item_hash(source: str, title: str, url: str) -> str:
    return hashlib.sha256(f"{source}:{title}:{url}".encode()).hexdigest()[:16]

def is_duplicate(conn: sqlite3.Connection, source: str, title: str, url: str) -> bool:
    h = item_hash(source, title, url)
    return conn.execute("SELECT 1 FROM classified_intel WHERE id=?", (h,)).fetchone() is not None

def insert_intel(conn: sqlite3.Connection, source: str, title: str, url: str, 
                 self_relevance: float, category: str, reasoning: str):
    h = item_hash(source, title, url)
    try:
        conn.execute("""INSERT OR IGNORE INTO classified_intel 
            (id, source, title, url, self_relevance, category, reasoning, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (h, source, title[:300], url[:500], self_relevance, category, 
             reasoning[:300], datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return True
    except Exception as e:
        log.warning(f"Insert failed: {e}")
        return False

def score_by_keywords(title: str, content: str, keywords: List[str]) -> Tuple[float, str]:
    text = (title + " " + content).lower()
    matches = [kw for kw in keywords if kw.lower() in text]
    score = min(len(matches) / max(len(keywords), 1), 1.0)
    reason = f"Matched {len(matches)} keywords: {', '.join(matches[:3])}"
    return score, reason

def fetch_rss_source(source_cfg: Dict[str, Any], conn: sqlite3.Connection):
    src_name = source_cfg.get("name", "RSS")
    url = source_cfg["url"]
    keywords = source_cfg.get("keywords", [])
    self_rel = source_cfg.get("self_relevance", 0.7)
    
    try:
        log.info(f"Fetching RSS: {src_name}")
        feed = feedparser.parse(url, request_headers=HEADERS)
        count_added = 0
        for entry in feed.entries[:50]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "")
            summary = entry.get("summary", "")
            if not title or not link or is_duplicate(conn, src_name, title, link):
                continue
            if is_off_domain_junk(src_name, title):
                _log_junk_drop(src_name, title, link)
                continue
            score, reason = score_by_keywords(title, summary, keywords)
            if score > 0.2 and insert_intel(conn, src_name, title, link, score * self_rel, 
                                           source_cfg.get("category", "misc"), reason):
                count_added += 1
                log.info(f"  + {title[:60]} (score={score:.2f})")
        log.info(f"  {src_name}: {count_added} new")
        time.sleep(RATE_LIMIT)
    except Exception as e:
        log.error(f"RSS failed {src_name}: {e}")

def fetch_reddit_source(source_cfg: Dict[str, Any], conn: sqlite3.Connection):
    src_name = source_cfg.get("name", "Reddit")
    subreddit = source_cfg["subreddit"]
    keywords = source_cfg.get("keywords", [])
    self_rel = source_cfg.get("self_relevance", 0.75)
    sort = source_cfg.get("sort", "top")
    
    try:
        log.info(f"Fetching {src_name} (/{sort})...")
        url = f"https://reddit.com/r/{subreddit}/{sort}.rss?t=week"
        feed = feedparser.parse(url, request_headers=HEADERS)
        count_added = 0
        for entry in feed.entries[:30]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").split("?")[0]
            summary = entry.get("summary", "")
            if not title or not link or is_duplicate(conn, src_name, title, link):
                continue
            if is_off_domain_junk(src_name, title):
                _log_junk_drop(src_name, title, link)
                continue
            score, reason = score_by_keywords(title, summary, keywords)
            if score > 0.15 and insert_intel(conn, src_name, title, link, score * self_rel,
                                            source_cfg.get("category", "misc"), reason):
                count_added += 1
                log.info(f"  + {title[:60]} (score={score:.2f})")
        log.info(f"  {src_name}: {count_added} new")
        time.sleep(RATE_LIMIT)
    except Exception as e:
        log.error(f"Reddit failed {src_name}: {e}")

# ── X / Twitter via self-hosted twscrape (Lane A) ─────────────────────────
TWSCRAPE_DB = "/home/lumen/minerva/twscrape_accounts.db"
TWITTER_FETCH_LIMIT = 20

# Feed health. "0 added" is normal (nothing new); "0 raw items fetched across
# every handle" is the signature of the 2026-07-20 XClIdGen outage, which ran
# silently for 6 days. Tracked so it can never go unnoticed again.
_X_STATS = {"handles": 0, "resolved": 0, "fetched": 0, "errors": 0}
_X_ALERT_STATE = "/home/lumen/minerva/logs/.scout_x_last_alert"

def _alert_x_zero_yield():
    """Fire once per 6h if X ingestion produced nothing at all."""
    try:
        import requests
        now = time.time()
        if os.path.exists(_X_ALERT_STATE) and now - os.path.getmtime(_X_ALERT_STATE) < 21600:
            return
        msg = (f"X scout zero-yield: {_X_STATS['handles']} handles, "
               f"{_X_STATS['resolved']} resolved, {_X_STATS['fetched']} items, "
               f"{_X_STATS['errors']} errors. Check for 'XClIdGen' in "
               f"/home/lumen/minerva/logs/. Ref: twscrape_xclid_auth.py")
        requests.post(os.getenv("NTFY_URL_ALERTS", "http://localhost:8080/lumen-updates"),
                      data=msg.encode(),
                      headers={"Title": "Minerva X scout DARK", "Priority": "high",
                               "Tags": "warning"}, timeout=5)
        with open(_X_ALERT_STATE, "w") as f:
            f.write(str(now))
    except Exception as e:
        log.error(f"X zero-yield alert failed to send: {e}")

async def _twitter_pull_async(username: str, limit: int) -> List[Dict[str, str]]:
    """Pull recent tweets for one handle via the twscrape account pool."""
    from twscrape import API, gather
    import twscrape_xclid_auth
    # Bootstrap x-client-transaction-id via an authenticated session; the
    # upstream anonymous path is broken. See twscrape_xclid_auth.py + #320.
    twscrape_xclid_auth.install(TWSCRAPE_DB)
    api = API(TWSCRAPE_DB)
    out: List[Dict[str, str]] = []
    user = await api.user_by_login(username)
    if user is None:
        log.warning(
            f"  X: @{username} not resolvable. Cause is NOT necessarily auth - "
            f"check the log lines above for 'XClIdGen' (X web-build/header "
            f"failure) vs an actual account error. Verify the pool with: "
            f"twscrape --db {TWSCRAPE_DB} accounts"
        )
        return out
    tweets = await gather(api.user_tweets(user.id, limit=limit))
    for tw in tweets:
        content = (tw.rawContent or "").strip()
        if not content:
            continue
        link = getattr(tw, "url", None) or f"https://x.com/{username}/status/{tw.id}"
        title = content.replace("\n", " ")[:140]
        out.append({"title": title, "link": link, "text": content})
    return out

def fetch_twitter_source(source_cfg: Dict[str, Any], conn: sqlite3.Connection):
    """X/Twitter source handler (Lane A: self-hosted twscrape). Sync wrapper
    around an internal event loop, since twscrape is async-only."""
    import asyncio
    src_name = source_cfg.get("name", "X")
    handle = source_cfg.get("handle", "").lstrip("@")
    keywords = source_cfg.get("keywords", [])
    self_rel = source_cfg.get("self_relevance", 0.85)
    if not handle:
        log.warning(f"  X: {src_name} has no handle, skipping")
        return
    try:
        log.info(f"Fetching X: {src_name} (@{handle})...")
        _X_STATS["handles"] += 1
        items = asyncio.run(_twitter_pull_async(handle, TWITTER_FETCH_LIMIT))
        if items:
            _X_STATS["resolved"] += 1
            _X_STATS["fetched"] += len(items)
        count_added = 0
        for it in items:
            title, link, text = it["title"], it["link"], it["text"]
            if not title or not link or is_duplicate(conn, src_name, title, link):
                continue
            if is_off_domain_junk(src_name, title):
                _log_junk_drop(src_name, title, link)
                continue
            score, reason = score_by_keywords(title, text, keywords)
            if score > 0.15 and insert_intel(conn, src_name, title, link, score * self_rel,
                                             source_cfg.get("category", "misc"), reason):
                count_added += 1
                log.info(f"  + {title[:60]} (score={score:.2f})")
        log.info(f"  {src_name}: {count_added} new")
        time.sleep(RATE_LIMIT)
    except Exception as e:
        _X_STATS["errors"] += 1
        log.error(f"X failed {src_name}: {e}")

def main():
    log.info("="*70)
    log.info(f"SCOUT EXPANDED START ({len(ALL_SOURCES)} sources)")
    log.info("="*70)
    
    conn = ensure_db()
    rss_sources = {k: v for k, v in ALL_SOURCES.items() if v.get("type") == "rss"}
    reddit_sources = {k: v for k, v in ALL_SOURCES.items() if v.get("type") == "reddit"}
    twitter_sources = {k: v for k, v in ALL_SOURCES.items() if v.get("type") == "twitter"}
    
    log.info(f"RSS: {len(rss_sources)} | Reddit: {len(reddit_sources)} | X: {len(twitter_sources)}")
    log.info("\n[RSS Feeds]")
    for src_id, src_cfg in rss_sources.items():
        fetch_rss_source(src_cfg, conn)
    
    log.info("\n[Reddit]")
    for src_id, src_cfg in reddit_sources.items():
        fetch_reddit_source(src_cfg, conn)
    
    log.info("\n[X/Twitter]")
    for src_id, src_cfg in twitter_sources.items():
        fetch_twitter_source(src_cfg, conn)

    if _X_STATS["handles"]:
        log.info(f"  X feed-health: {_X_STATS['resolved']}/{_X_STATS['handles']} handles "
                 f"resolved, {_X_STATS['fetched']} raw items, {_X_STATS['errors']} errors")
        if _X_STATS["fetched"] == 0:
            log.error("X ZERO-YIELD: every handle returned nothing - X ingestion is DOWN")
            _alert_x_zero_yield()

    conn.close()
    log.info("\n" + "="*70)
    log.info("SCOUT EXPANDED COMPLETE")
    log.info("="*70)

if __name__ == "__main__":
    main()

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

def main():
    log.info("="*70)
    log.info(f"SCOUT EXPANDED START ({len(ALL_SOURCES)} sources)")
    log.info("="*70)
    
    conn = ensure_db()
    rss_sources = {k: v for k, v in ALL_SOURCES.items() if v.get("type") == "rss"}
    reddit_sources = {k: v for k, v in ALL_SOURCES.items() if v.get("type") == "reddit"}
    
    log.info(f"RSS: {len(rss_sources)} | Reddit: {len(reddit_sources)}")
    log.info("\n[RSS Feeds]")
    for src_id, src_cfg in rss_sources.items():
        fetch_rss_source(src_cfg, conn)
    
    log.info("\n[Reddit]")
    for src_id, src_cfg in reddit_sources.items():
        fetch_reddit_source(src_cfg, conn)
    
    conn.close()
    log.info("\n" + "="*70)
    log.info("SCOUT EXPANDED COMPLETE")
    log.info("="*70)

if __name__ == "__main__":
    main()

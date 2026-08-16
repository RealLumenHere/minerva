#!/usr/bin/env python3
"""familiarity_scorer.py — behavioral entity-familiarity tracker for Minerva.
(Minerva proposal fd2622e69837, wq#443, Scribe #304. Inspiration:
arXiv:2607.13568 "Graded Entity-Familiarity Readouts" — unfamiliar entities
predict confabulation; abstain/verify harder on them.)

HONEST SCOPE: the paper reads familiarity from INTERNAL ACTIVATIONS at the
prompt point — unreachable through LiteLLM/ollama APIs (no hidden states).
What Lumen CAN measure is behavioral familiarity: how often an entity has
appeared in its own intel stream over time. Novel entities in a proposal or
draft are the exact "verify harder / trust less" signal the verify gate and
factory triage need — the same signal that caught 50 speculative
unverified-artifact integrations in the 2026-08-09 YELLOW triage, made
systematic. NO service edits here: data layer + CLI + advisory JSON only.
Wiring into eval routing is a separate, gated step (EAR ships the hook).

Extraction: deterministic, no LLM — TitleCase multi-word spans, arXiv IDs,
owner/repo slugs, ALLCAPS acronyms (4-24 chars), from classified_intel
title+reasoning. Normalized lowercase matching (embedding upgrade path:
Memory V3 BGE-M3, noted not built).
Storage: /home/lumen/minerva/familiarity.db (entities, ingest_state).
Advisory: /home/lumen/minerva/reports/familiarity_summary.json
CLI: ingest | score "Name" [...] | novel [--days 7] | stats
Timer: lumen-familiarity.{service,timer} 6h, installed DISABLED.
"""
import argparse, datetime, json, math, os, re, sqlite3, sys

DB = "/home/lumen/minerva/familiarity.db"
MINERVA_DB = "/home/lumen/minerva/minerva.db"
SUMMARY = "/home/lumen/minerva/reports/familiarity_summary.json"
HALF_LIFE_DAYS = 45.0

STOP = {"The", "This", "That", "These", "Those", "A", "An", "It", "If",
        "In", "On", "At", "For", "And", "But", "Not", "New", "Latest",
        "Here", "There", "What", "When", "Why", "How", "Who", "Also",
        "Github", "GitHub", "Http", "Https", "Www", "AI", "ML", "LLM",
        "API", "CLI", "GPU", "CPU", "TAM", "USA", "IMO", "TIL", "PSA",
        "HINT", "NOTE", "EDIT", "UPDATE", "THREAD", "BREAKING"}

PAT_TITLECASE = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:[ -][A-Z][a-zA-Z0-9]+)+)\b")
PAT_ARXIV = re.compile(r"\barXiv:?\s?(\d{4}\.\d{4,5})\b", re.I)
PAT_REPO = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]{3,})\b")
PAT_ACRO = re.compile(r"\b([A-Z][A-Z0-9]{3,23})\b")
PAT_PRODUCT = re.compile(r"\b([A-Z][a-z]+[A-Z][a-zA-Z0-9]+)\b")  # CamelCase

def norm(e):
    return re.sub(r"\s+", " ", e.strip()).lower()

def extract(text):
    if not text:
        return set()
    text = re.sub(r"https?://\S+", " ", text)   # URLs breed slug noise
    out = set()
    for m in PAT_ARXIV.finditer(text):
        out.add("arxiv:" + m.group(1))
    for m in PAT_REPO.finditer(text):
        s = m.group(1)
        if "." not in s.split("/")[0] and not s.startswith("t.co"):
            out.add(s)
    for pat in (PAT_TITLECASE, PAT_PRODUCT, PAT_ACRO):
        for m in pat.finditer(text):
            s = m.group(1)
            if s not in STOP and len(s) >= 4:
                out.add(s)
    return {norm(e) for e in out
            if 4 <= len(norm(e)) <= 40 and len(norm(e).split()) <= 4}

def db_init():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS entities(
      entity TEXT PRIMARY KEY, display TEXT,
      first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
      mentions INT NOT NULL DEFAULT 1, sources INT NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS ingest_state(
      k TEXT PRIMARY KEY, v TEXT);""")
    c.commit()
    return c

def cmd_ingest(_args):
    c = db_init()
    since = (c.execute("SELECT v FROM ingest_state WHERE k='last_ts'")
             .fetchone() or ["1970-01-01"])[0]
    m = sqlite3.connect(MINERVA_DB)
    rows = m.execute(
        "SELECT ts, title, reasoning FROM classified_intel WHERE ts > ? "
        "ORDER BY ts", (since,)).fetchall()
    n_ent = 0
    for ts, title, reasoning in rows:
        ents = extract((title or "") + " " + (reasoning or ""))
        for e in ents:
            n_ent += 1
            c.execute(
                "INSERT INTO entities(entity, display, first_seen, last_seen)"
                " VALUES(?,?,?,?) ON CONFLICT(entity) DO UPDATE SET "
                "last_seen=excluded.last_seen, mentions=mentions+1, "
                "sources=sources+1", (e, e, ts, ts))
    if rows:
        c.execute("INSERT OR REPLACE INTO ingest_state VALUES('last_ts',?)",
                  (rows[-1][0],))
    c.commit()
    print(f"ingested {len(rows)} intel rows, {n_ent} entity mentions")
    emit_summary(c)

def familiarity(c, name, now=None):
    """0.0 (never seen) .. ~1.0 (frequent + recent). Time-decayed log scale."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    row = c.execute("SELECT mentions, last_seen FROM entities WHERE entity=?",
                    (norm(name),)).fetchone()
    if not row:
        return 0.0, 0
    mentions, last_seen = row
    try:
        age_d = (now - datetime.datetime.fromisoformat(last_seen)).days
    except Exception:
        age_d = 0
    decay = 0.5 ** (max(age_d, 0) / HALF_LIFE_DAYS)
    score = min(1.0, math.log1p(mentions) / math.log1p(50)) * decay
    return round(score, 3), mentions

def cmd_score(args):
    c = db_init()
    out = {}
    for name in args.names:
        s, n = familiarity(c, name)
        out[name] = {"familiarity": s, "mentions": n,
                     "band": ("novel" if s == 0 else
                              "low" if s < 0.3 else
                              "medium" if s < 0.7 else "high")}
        print(f"{name}: {out[name]}")
    return out

def cmd_novel(args):
    c = db_init()
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=args.days)).isoformat()
    rows = c.execute(
        "SELECT display, first_seen, mentions FROM entities "
        "WHERE first_seen > ? AND mentions <= 2 "
        "ORDER BY first_seen DESC LIMIT 40", (cutoff,)).fetchall()
    for d, fs, n in rows:
        print(f"{fs[:10]}  x{n}  {d}")
    print(f"({len(rows)} novel entities, last {args.days}d)")

def cmd_stats(_args):
    c = db_init()
    total, = c.execute("SELECT count(*) FROM entities").fetchone()
    top = c.execute("SELECT display, mentions FROM entities "
                    "ORDER BY mentions DESC LIMIT 10").fetchall()
    print(f"entities: {total}")
    for d, n in top:
        print(f"  x{n:<5} {d}")

def emit_summary(c):
    os.makedirs(os.path.dirname(SUMMARY), exist_ok=True)
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=7)).isoformat()
    novel = [r[0] for r in c.execute(
        "SELECT display FROM entities WHERE first_seen > ? AND mentions <= 2 "
        "ORDER BY first_seen DESC LIMIT 40", (cutoff,))]
    total, = c.execute("SELECT count(*) FROM entities").fetchone()
    with open(SUMMARY, "w") as f:
        json.dump({"ts": datetime.datetime.now(
                       datetime.timezone.utc).isoformat(),
                   "total_entities": total, "novel_7d": novel,
                   "source": "familiarity_scorer.py arXiv:2607.13568 "
                             "(behavioral proxy)"}, f, indent=2)

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest")
    p = sub.add_parser("score"); p.add_argument("names", nargs="+")
    p = sub.add_parser("novel"); p.add_argument("--days", type=int, default=7)
    sub.add_parser("stats")
    args = ap.parse_args()
    {"ingest": cmd_ingest, "score": cmd_score,
     "novel": cmd_novel, "stats": cmd_stats}[args.cmd](args)

if __name__ == "__main__":
    main()

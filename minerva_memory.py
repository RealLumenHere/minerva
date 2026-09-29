#!/usr/bin/env python3
"""
minerva_memory.py - MemGPT/Letta-inspired three-tier memory for Lumen/Claude sessions.

Tiers:
  - Core (hot): /home/lumen/claude_memory.md — always in context, manually curated + auto-appended Minerva brief
  - Recall (searchable): SQLite FTS5 index of session events, decisions, intel hits
  - Archival (cold): Markdown notes in /home/lumen/minerva/archival/ with embeddings

Usage:
  - write_note(kind, title, content, tags=None) -> note_id (archival)
  - log_event(kind, content, ref=None) -> (recall tier, for searchable history)
  - search(query, top_k=5, tiers=['recall','archival']) -> unified results
  - core_append(section, text) -> appends to claude_memory.md under a named section

The `search()` function is the key: next-session Claude queries it to surface context
relevant to the current task without loading the full archive.
"""
import json, sqlite3, hashlib, os, sys, re, time, requests
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
from dotenv import load_dotenv

import minerva_audit
load_dotenv('/home/lumen/agent/.env')

# wq#2772: MINERVA_DB / MINERVA_ARCHIVAL_DIR follow the existing fleet convention
# (lumen_salience, lumen_dream_digest, lumen_prose_lifecycle) so a scratch test
# routes this module AND its satellites away from prod in one env var.
DB = Path(os.environ.get('MINERVA_DB', '/home/lumen/minerva/minerva.db'))
ARCHIVAL_DIR = Path(os.environ.get('MINERVA_ARCHIVAL_DIR', '/home/lumen/minerva/archival'))
# Writers wait this long on a locked DB instead of sqlite3's 5s default; the
# pytest episode floods (hundreds of commits/min) made 5s a real failure mode.
DB_TIMEOUT = float(os.environ.get('MINERVA_DB_TIMEOUT', '30'))


class NotPersisted(RuntimeError):
    """wq#2772: a write the caller would otherwise report as success is not in
    minerva.db. Raised, never swallowed, by the verified write path."""
MEMORY_FILE = Path('/home/lumen/claude_memory.md')
LOG = Path('/home/lumen/minerva/logs/memory.log')
EMBED_URL = os.environ.get('MINERVA_EMBED_URL', 'http://127.0.0.1:11434')  # wq#567: env-overridable; pin on replacement hw when it lands
EMBED_MODEL = os.environ.get('MINERVA_EMBED_MODEL', 'nomic-embed-text')


def log(m):
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
    line = f'[{ts}] {m}'
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


EMBED_ITEMSIZE = 4          # float32
EMBED_DIM = 768             # nomic-embed-text native dimensionality


def embed(text, attempts=2):
    # timeout=90: nomic cold-load measured 44.3s (OLLAMA_MAX_LOADED_MODELS=1
    # means every gemma/LFM call evicts it). The old 15s timeout silently
    # dropped embeddings on every cold path - 4 archival notes lost pre-2026-07-27.
    # keep_alive pins nomic between embeds; other models still evict it when loaded.
    #
    # wq#465 (2026-08-10): a 90s timeout STILL fired once, during the /scratch
    # model-move (cold load + heavy disk I/O), and poisoned the store. Ollama keeps
    # loading after the client gives up, so attempt 2 hits a warm model and wins.
    # Retry is in-process and cheap; do NOT rely on the weekly repair loop instead.
    last = None
    for i in range(max(1, attempts)):
        try:
            r = requests.post(
                f'{EMBED_URL}/api/embed',
                json={'model': EMBED_MODEL, 'input': text[:4000], 'keep_alive': '30m'},
                timeout=90,
            )
            if r.status_code != 200:
                last = f'HTTP {r.status_code}'
            else:
                vec = r.json().get('embeddings', [[]])[0]
                if vec:
                    return np.array(vec, dtype=np.float32)
                last = 'empty embeddings[]'
        except Exception as e:
            last = str(e)
        if i + 1 < max(1, attempts):
            log(f'  embed attempt {i+1} failed ({last}); retrying warm')
            time.sleep(2)
    log(f'  embed err: {last}')
    return None


def decode_embedding(blob, expect_dim=None):
    """Decode a stored embedding BLOB -> 1-D float32 vector, or None if unusable.

    wq#465: THE ONLY sanctioned way to read archival_notes.embedding.
    Returns None (never raises, never hands back a wrong-shaped array) for:
      - NULL or zero-length blob  (embed() failed at write time -> repair queue)
      - length not a multiple of 4 (truncated/partial write)
      - dimensionality != expect_dim (embedding model changed under the store)

    Pass expect_dim=len(query_vector) so a model swap degrades to "skip old rows"
    instead of ValueError. np.dot on mismatched 1-D shapes raises, it does not
    broadcast -- that is exactly the wq#465 crash.
    """
    if not blob:
        return None
    if len(blob) % EMBED_ITEMSIZE:
        return None
    v = np.frombuffer(blob, dtype=np.float32)
    if v.size == 0:
        return None
    if expect_dim is not None and v.shape[0] != expect_dim:
        return None
    return v


def cosine(a, b):
    # wq#465: shape-safe by contract. Even a caller that bypasses
    # decode_embedding() gets 0.0 instead of a ValueError that kills the
    # whole search and silently degrades the fleet to v2 recall.
    if a is None or b is None:
        return 0.0
    a = np.asarray(a)
    b = np.asarray(b)
    if a.ndim != 1 or b.ndim != 1 or a.size == 0 or a.shape != b.shape:
        return 0.0
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1e-9
    return float(np.dot(a, b) / denom)


# ---------- FTS5 recall table bootstrap ----------

def _ensure_recall_fts():
    """Create the FTS5 virtual table for recall if missing."""
    conn = sqlite3.connect(DB, timeout=DB_TIMEOUT)   # wq#2930: DDL path, was the 5s default
    try:   # wq#2895: close on every exit path (runs in lumen-memoryd via search())
        # Check if FTS5 is available
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts USING fts5(kind, content, ref, ts)")
            conn.commit()
        except sqlite3.OperationalError as e:
            log(f'  FTS5 unavailable: {e}. Falling back to plain table.')
            conn.execute('''CREATE TABLE IF NOT EXISTS recall_fts (
                kind TEXT, content TEXT, ref TEXT, ts TEXT
            )''')
            conn.commit()
    finally:
        conn.close()


# ---------- Archival (cold, semantic) ----------

def _connect():
    return sqlite3.connect(DB, timeout=DB_TIMEOUT)


def _ensure_recall_fts_on(conn):
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts USING fts5(kind, content, ref, ts)")
    except sqlite3.OperationalError as e:
        log(f'  FTS5 unavailable: {e}. Falling back to plain table.')
        conn.execute('CREATE TABLE IF NOT EXISTS recall_fts (kind TEXT, content TEXT, ref TEXT, ts TEXT)')


def note_id(kind, title, content):
    return hashlib.md5(f'{kind}:{title}:{content[:200]}'.encode()).hexdigest()[:12]


def _write_md(nid, kind, title, content, tags, source_ref, created_at):
    """Archival .md mirror. Written AFTER the DB commit (wq#2772) and atomically
    (tmp + rename), so a crash can never leave a .md that recall cannot see."""
    ARCHIVAL_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVAL_DIR / f'{created_at[:10].replace("-", "")}_{nid}_{kind}.md'
    md = '\n'.join([
        '---', f'id: {nid}', f'kind: {kind}', f'title: {title}',
        f'tags: {",".join(tags or [])}', f'source_ref: {source_ref or ""}',
        f'created_at: {created_at}', '---', '', f'# {title}', '', content,
    ])
    tmp = path.with_suffix(f'.md.tmp.{os.getpid()}')
    tmp.write_text(md)
    os.replace(tmp, path)
    return path


def embed_note(nid, title=None, content=None):
    """Fill archival_notes.embedding for nid. Best effort by design: a NULL
    embedding is the sanctioned repair-queue state (memory_embed_guard, hourly),
    so a slow/evicted nomic can no longer cost the row itself (wq#2772)."""
    if title is None or content is None:
        conn = _connect()
        r = conn.execute('SELECT title, content FROM archival_notes WHERE id=?', (nid,)).fetchone()
        conn.close()
        if not r:
            return False
        title, content = r
    emb = embed(f'{title}. {content[:2000]}')
    if emb is None:
        log(f'  !! embed FAILED for note {nid} -- row kept with NULL embedding, queued for '
            f'repair (memory_embed_guard.py repairs; never store an empty blob)')
        return False
    conn = _connect()
    try:   # wq#2895: a busy commit must not leave the UPDATE's write lock open
        conn.execute('UPDATE archival_notes SET embedding=? WHERE id=?', (emb.tobytes(), nid))
        conn.commit()
    finally:
        conn.close()   # close() rolls back an uncommitted transaction
    return True


def write_note_verified(kind, title, content, tags=None, source_ref=None, origin=None,
                        trust=None, audit=None, recall_event=None, embed_now=True):
    """wq#2772 verified write. Returns (note_id, fts_rowid or None).

    ROOT CAUSE this replaces: the old write_note wrote the .md FIRST, then blocked
    in embed() (up to 2x90s while nomic was evicted by LFM2 loads under
    OLLAMA_MAX_LOADED_MODELS=1), and only then INSERTed. foreman's
    commit_to_archive runs lumen_episode.py under subprocess timeout=30 (and chat
    tool calls have their own ceilings), so the child was SIGKILLed mid-embed:
    .md on disk, no archival_notes row, no recall_fts event, caller fail-open.

    New order: (1) ONE transaction on ONE connection: archival row (embedding
    untouched/NULL) + optional audit row + optional recall_fts event, commit;
    (2) read back from a FRESH connection, raise NotPersisted if anything is
    missing; (3) atomic .md mirror; (4) embed (optional here, best effort).
    A kill anywhere after (1) loses at most the embedding, which the guard repairs."""
    origin = origin or os.environ.get('LUMEN_MEM_ORIGIN', 'claude-session')
    trust = trust or os.environ.get('LUMEN_MEM_TRUST', 'operator')
    nid = note_id(kind, title, content)
    created_at = datetime.now(timezone.utc).isoformat()
    fts_rowid = None
    conn = _connect()
    try:
        # wq#610 semantics kept: ON CONFLICT DO UPDATE whose SET list excludes every
        # provenance column the wq#575 immutability trigger watches. wq#2772: the
        # embedding is no longer in the SET list either -- a repeat commit keeps the
        # existing vector (same kind/title/content[:200] => same meaning) and
        # embed_note() refreshes it after commit.
        conn.execute(
            '''INSERT INTO archival_notes
               (id, kind, title, content, embedding, tags, source_ref, created_at, origin, trust)
               VALUES (?,?,?,?,NULL,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 kind=excluded.kind, title=excluded.title, content=excluded.content,
                 tags=excluded.tags, source_ref=excluded.source_ref''',
            (nid, kind[:40], title[:300], content[:8000],
             ','.join(tags or [])[:500], (source_ref or '')[:300],
             created_at, origin[:40], trust[:20]))
        if audit is not None:
            minerva_audit.audit(conn, table_name='archival_notes', row_id=nid, **audit)
        if recall_event is not None:
            ek, ec, er = recall_event
            _ensure_recall_fts_on(conn)
            cur = conn.execute(
                'INSERT INTO recall_fts (kind, content, ref, ts) VALUES (?,?,?,?)',
                (ek[:40], ec[:4000], (er or nid)[:300], created_at))
            fts_rowid = cur.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    missing = verify_landed(nid, fts_rowid if recall_event is not None else None,
                            require_fts=recall_event is not None)
    if missing:
        raise NotPersisted(f'note {nid}: {"; ".join(missing)}')
    _write_md(nid, kind, title, content, tags, source_ref, created_at)
    log(f'  archival note: {nid} [{kind}] "{title[:60]}" (db-verified)')
    if embed_now:
        embed_note(nid, title, content)
    return nid, fts_rowid


def verify_landed(nid, fts_rowid=None, require_fts=False):
    """Read back from a FRESH connection (never the writer's handle) so we see
    what another process will see. Returns a list of what is missing; [] = landed."""
    missing = []
    conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=DB_TIMEOUT)
    try:
        if not conn.execute('SELECT 1 FROM archival_notes WHERE id=?', (nid,)).fetchone():
            missing.append('no archival_notes row')
        if require_fts:
            if fts_rowid is None:
                missing.append('no recall_fts rowid returned')
            elif not conn.execute('SELECT 1 FROM recall_fts WHERE rowid=?', (fts_rowid,)).fetchone():
                missing.append(f'no recall_fts event rowid={fts_rowid}')
    finally:
        conn.close()
    return missing


def write_note(kind, title, content, tags=None, source_ref=None, origin=None, trust=None,
               audit=None):
    """Persist an archival note with embedding for semantic retrieval.

    wq#1634: `audit` is an optional dict of minerva_audit.audit() kwargs, written on
    the SAME connection so it commits with the note or not at all.
    wq#2772: now a thin wrapper over write_note_verified (DB first, read-back
    verified, .md second, embed last). Signature and return value unchanged."""
    nid, _ = write_note_verified(kind, title, content, tags=tags, source_ref=source_ref,
                                 origin=origin, trust=trust, audit=audit)
    return nid


# ---------- Recall (searchable FTS) ----------

def log_event(kind, content, ref=None):
    """Append a recall-tier event. Cheap, fast, no embedding.
    wq#2772: returns the recall_fts rowid so callers can read it back."""
    ts = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        _ensure_recall_fts_on(conn)
        cur = conn.execute(
            'INSERT INTO recall_fts (kind, content, ref, ts) VALUES (?,?,?,?)',
            (kind[:40], content[:4000], (ref or '')[:300], ts))
        rowid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return rowid


# ---------- Search (unified across tiers) ----------

def search(query, top_k=5, tiers=None):
    """Unified search. Returns list of dicts with {tier, score, title, content, ref, ts}."""
    tiers = tiers or ['recall', 'archival']
    results = []

    # --- Recall tier: FTS5 text search ---
    if 'recall' in tiers:
        _ensure_recall_fts()
        conn = sqlite3.connect(DB)
        try:
            # FTS5 query
            safe_q = re.sub(r'[^\w\s]', ' ', query).strip()
            if safe_q:
                rows = conn.execute(
                    'SELECT kind, content, ref, ts FROM recall_fts WHERE recall_fts MATCH ? ORDER BY rank LIMIT ?',
                    (safe_q, top_k)
                ).fetchall()
                for kind, content, ref, ts in rows:
                    results.append({
                        'tier': 'recall', 'score': 1.0,  # FTS rank normalized externally
                        'kind': kind, 'title': content[:80], 'content': content,
                        'ref': ref, 'ts': ts,
                    })
        except sqlite3.OperationalError:
            # Fallback: LIKE scan
            rows = conn.execute(
                "SELECT kind, content, ref, ts FROM recall_fts WHERE content LIKE ? ORDER BY ts DESC LIMIT ?",
                (f'%{query}%', top_k)
            ).fetchall()
            for kind, content, ref, ts in rows:
                results.append({
                    'tier': 'recall', 'score': 0.5,
                    'kind': kind, 'title': content[:80], 'content': content,
                    'ref': ref, 'ts': ts,
                })
        finally:
            conn.close()

    # --- Archival tier: semantic embedding search ---
    if 'archival' in tiers:
        q_emb = embed(query)
        if q_emb is not None:
            conn = sqlite3.connect(DB)
            try:   # wq#2895
                rows = conn.execute(
                    'SELECT id, kind, title, content, embedding, tags, created_at, trust '
                    'FROM archival_notes WHERE id NOT IN (SELECT absorbed_id FROM note_merges)'
                ).fetchall()
            finally:
                conn.close()
            scored = []
            for nid, kind, title, content, emb_b, tags, ts, trust in rows:
                note_emb = decode_embedding(emb_b, expect_dim=q_emb.shape[0])  # wq#465
                if note_emb is None:
                    continue
                sim = cosine(q_emb, note_emb)
                if sim >= 0.3:
                    scored.append({
                        'tier': 'archival', 'score': sim, 'trust': trust,
                        'id': nid, 'kind': kind, 'title': title,
                        'content': content[:800], 'tags': tags, 'ts': ts,
                    })
            scored.sort(key=lambda x: -x['score'])
            results.extend(scored[:top_k])

    # Sort all tiers together (archival sim scores are more meaningful; FTS normalized to 1.0)
    results.sort(key=lambda x: -x['score'])
    return results[:top_k]


# ---------- Core tier: claude_memory.md section management ----------

def core_append(section, text):
    """Append text under a named section in claude_memory.md.
    Section markers: <!-- SECTION:name START --> ... <!-- SECTION:name END -->
    Creates the section if it doesn't exist.
    """
    if not MEMORY_FILE.exists():
        log(f'  memory file missing: {MEMORY_FILE}')
        return False
    content = MEMORY_FILE.read_text()
    m_start = f'<!-- SECTION:{section} START -->'
    m_end = f'<!-- SECTION:{section} END -->'
    now_pt = _now_pt()
    addition = f'\n- [{now_pt}] {text}'

    if m_start in content and m_end in content:
        pattern = re.compile(re.escape(m_start) + r'(.*?)' + re.escape(m_end), re.DOTALL)
        def _sub(match):
            return f'{m_start}{match.group(1).rstrip()}{addition}\n{m_end}'
        new_content = pattern.sub(_sub, content)
    else:
        # Append new section at the top
        block = f'{m_start}\n## {section}{addition}\n{m_end}\n\n'
        new_content = block + content
    MEMORY_FILE.write_text(new_content)
    log(f'  core_append [{section}]: {text[:80]}')
    return True


def _now_pt():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(timezone.utc).astimezone(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M PT')
    except Exception:
        return datetime.now(timezone.utc).isoformat()


# ---------- Seed archival with Minerva architecture knowledge ----------

SEED_NOTES = [
    {
        'kind': 'architecture',
        'title': 'Minerva Self-Improvement System Topology',
        'content': '''Minerva is Lumen's self-improvement layer, deployed 2026-04-22. It converts research intel into code changes.

Pipeline:
  1. lumen-intel (every 4h) pulls 20 newsletters + arXiv + HF + GitHub into /home/lumen/agent/intel.db
  2. minerva-classifier (every 4h, +30min) scores each intel item on two axes: audience_relevance (blog-worthy) and self_relevance (Lumen-adoptable). All LLM calls via LiteLLM gateway (localhost:4000), model env MINERVA_MODEL (default cerebras-llama), fail-loud on fallback.
  3. minerva-proposal (every 4h, +45min) takes high-self-relevance items and generates structured proposals with hallucination-guarded current-state claims. Rejected if verification fails.
  4. minerva-eval (03,07,11,17,19,23 UTC) runs safety checks on accepted proposals: off-limits keyword scan, rogue script scan, 5 service health checks (intel + cerebras/sambanova/mistral/openrouter fleet workers). Blocks or marks deploy_ready. (Ghost backup + PBox checks removed 2026-06-10 — both permanently retired.)
  5. minerva-brief (daily 13:00 UTC = 06:00 PT) synthesizes last 24h: proposals, intel highlights, verification stats. Writes /home/lumen/minerva/briefings/YYYY-MM-DD.md AND auto-prepends summary to /home/lumen/claude_memory.md so next Claude session sees it on load.

Central DB: /home/lumen/minerva/minerva.db (6 tables: classified_intel, proposals, skills, archival_notes, verification_log, changelog + recall_fts).

Hallucination guard (minerva_verify.py) wraps everything. Three modes: strict (citation+judge), semantic (proposals), loose (judge-only). Uses Cerebras for LLM-as-judge.

Skill library (minerva_skills.py) is Voyager-pattern: 4 active seed skills (verify_fleet_output, service_health_check, rogue_script_check, service_backup_and_restart) + backup_ghost_db RETIRED 2026-06-10 (Ghost deleted). Retrieval via localhost Ollama nomic-embed-text. Rolling success_rate per skill.

Three-tier memory (minerva_memory.py): core = claude_memory.md, recall = SQLite FTS5 index, archival = markdown notes with embeddings.''',
        'tags': ['minerva', 'architecture', 'self-improvement'],
    },
    {
        'kind': 'lesson',
        'title': 'Pre-existing SQLite thread-safety bug in lumen_intel was silently blocking newsletters',
        'content': '''Discovered 2026-04-22. lumen_intel.py runs fetch_newsletters/fetch_github/fetch_huggingface/fetch_semantic_scholar in parallel threads, but all threads shared the single `conn` SQLite object passed from main. Python's sqlite3 module is thread-local by default, so every non-arXiv fetch was silently failing with "SQLite objects created in a thread can only be used in that same thread" errors.

Result: for weeks, Lumen was effectively running as an arXiv-only paper scraper. All configured newsletter feeds, HF Hub pulls, GitHub trending, and Semantic Scholar queries were dead on arrival.

Fix: each thread now opens its own sqlite3 connection inside the run() wrapper. Join timeout bumped 120s->240s for the 20-feed set.

Takeaway: silent failures in systemd oneshot services are extremely dangerous. The health-check skill must look at error lines, not just service state (oneshot "inactive" after successful exit is normal).''',
        'tags': ['bug', 'lumen-intel', 'threading', 'silent-failure'],
    },
    {
        'kind': 'decision',
        'title': 'Verification modes: strict vs semantic vs loose',
        'content': '''minerva_verify.py has three modes. Choose based on context:

STRICT (default): citation_score >= 0.65 AND judge_score >= 0.70 AND final (0.4*cit + 0.6*judge) >= 0.70. Use when the answer should directly extract from context (fact retrieval, quotes, citations).

SEMANTIC: judge_score >= 0.70 AND final (0.15*cit + 0.85*judge) >= 0.70. Use when the LLM is paraphrasing context (proposals, summaries of long docs). The word-overlap citation check fails paraphrases; this mode trusts the judge.

LOOSE: judge_score >= 0.6 only. Use for creative content that is grounded but not a direct restatement.

Real failure caught during build: MCP orchestration proposal initially failed STRICT with citation=0.00 because the LLM paraphrased "Lumen has no memory framework" as "Lumen lacks multi-agent coordination and state management". Fix: switched proposal pipeline to SEMANTIC mode AND verify only current_state claims (proposed_change is future speculation that cannot be verified against current state).''',
        'tags': ['verification', 'modes', 'design-decision'],
    },
]


def seed():
    log('=== Seeding archival memory ===')
    for n in SEED_NOTES:
        write_note(n['kind'], n['title'], n['content'], tags=n.get('tags'))
    log(f'Seeded {len(SEED_NOTES)} archival notes')

    # Also log Minerva deployment as a recall event
    log_event('deployment', 'Minerva v1 deployed: 6 modules, 4 systemd timers, 5 seed skills, 3 seed archival notes', ref='minerva_build_2026-04-22')
    log_event('bugfix', 'Fixed SQLite thread-safety in lumen_intel.py — newsletter feeds now flowing (was silently broken)', ref='intel_py_bak_20260422_180011')
    log_event('bugfix', 'Fixed service_health_check skill to ignore upstream HTTP 4xx/5xx warnings', ref='skill_ffe2be7bd03b')
    log('Logged 3 recall events')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'seed':
        seed()
    elif len(sys.argv) > 2 and sys.argv[1] == 'search':
        q = ' '.join(sys.argv[2:])
        for r in search(q, top_k=5):
            print(f'[{r["tier"]}] {r["score"]:.3f} | {r.get("kind","")} | {r.get("title","")[:80]}')
    elif len(sys.argv) > 1 and sys.argv[1] == 'list':
        conn = sqlite3.connect(DB)
        for row in conn.execute('SELECT kind, title, created_at FROM archival_notes ORDER BY created_at DESC'):
            print(row)
        conn.close()
    else:
        print('usage: minerva_memory.py [seed | search <query> | list]')

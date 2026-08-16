#!/usr/bin/env python3
"""lumen_federate.py - wq#573 (Memory V4 P1): federated recall indexer.

WHY (MEMORY_V4_PLAN.md 2 axis TOTAL): claude_memory.md (~488KB) and
/home/lumen/sessions/ are the corpora the house actually consults, and neither has
ever been visible to recall. golden v4 carries 10 rows whose answers live ONLY in
those files; they score 0.000 by construction (wq#595 verified every one MISSES).

WHAT: section-aware chunking of the prose corpora into an ADDITIVE table
(federated_chunks) with nomic embeddings, consumed by lumen_recall2.search_v3 as a
flag-gated extra tier. Design constraints, each with its why:

 * SOURCES ARE READ-ONLY. Nothing here writes claude_memory.md or sessions/.
   Prose lifecycle/demotion is wq#577, explicitly not this ticket.
 * archival_notes IS NOT TOUCHED. New table only - a mutation of the note store
   would put every V3 measurement back in play. Retrieval stays additive.
 * SEGMENTATION AUTHORITY = the section index. Sections are re-derived from the
   live file with tag_sections.py's exact H2 split + slugify, so section_id here
   equals section_id in claude_memory_index.md/sections.json. The index is
   regenerated on its own schedule and was 52 min stale at build time; re-deriving
   with the same rule gives identical ids without indexing yesterday's bytes.
 * CONTEXTUAL AUGMENTATION (Anthropic contextual retrieval, plan 1b: -35/-49/-67%
   top-20 failure). A 50-100 token context is prepended to the chunk BEFORE
   embedding and BEFORE lexical scoring, because a mid-section chunk otherwise
   loses the one thing that disambiguates it - which document, which section,
   which date, which subsection. ctx_mode=header derives it deterministically from
   the heading hierarchy (zero LLM, zero hot path). ctx_mode=llm (wq#604) is the
   literal Anthropic recipe: an offline LiteLLM batch call per chunk, scoped to the
   enclosing section (not the whole ~488KB file - the section is already this
   corpus's bounded "document" unit, and header_context already supplies file/date
   identity for free). NEVER the hot recall path - only `index --ctx llm` calls out,
   same offline non-messenger rung as minerva_classifier/minerva_proposal. Results
   are cached by content hash in federated_llm_ctx_cache so a re-run only pays for
   chunks that actually changed.
 * INCREMENTAL. Keyed on file mtime + per-chunk content sha. A full re-embed on
   every run is a fail (context-economy axis).
 * DEDUP IS PROPOSAL-ONLY. cosine >= 0.95 pairs are written as a dream report.
   Nothing is auto-suppressed, ever.

CLI:
  python3 lumen_federate.py index [--full] [--ctx header|llm|none] [--limit N]
  python3 lumen_federate.py stats
  python3 lumen_federate.py dedup [--threshold 0.95]
  python3 lumen_federate.py probe "query"
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests  # noqa: E402  ctx_mode=llm only - offline LiteLLM gateway call

sys.path.insert(0, '/home/lumen/minerva')
import minerva_memory as mm  # noqa: E402  embed(), decode_embedding(), cosine()
import numpy as np  # noqa: E402

DB = '/home/lumen/minerva/minerva.db'
MEM = Path('/home/lumen/claude_memory.md')
SESSIONS = Path('/home/lumen/sessions')
DREAM = Path('/home/lumen/minerva/dream')
# wq#577 (V4 P5): sections demoted out of MEM land here. Indexed as its own source_kind
# (not merged into 'prose-section') so a demotion's federated_chunks row is
# distinguishable from a still-live section without a join back to claude_memory.md.
ARCHIVE = Path('/home/lumen/minerva/context/claude_memory_archive.md')

# ~200-500 tokens per plan 1b. Token estimate = chars/4, the same convention
# claude_memory_index.md already reports token_estimate with.
MIN_TOK, MAX_TOK = 200, 500
CHARS_PER_TOK = 4
MIN_CH, MAX_CH = MIN_TOK * CHARS_PER_TOK, MAX_TOK * CHARS_PER_TOK
CTX_MAX_CH = 400          # 50-100 tokens of context, hard-capped

# wq#604: ctx_mode=llm settings. Same gateway + retry shape as minerva_classifier
# and minerva_proposal (LITELLM_URL, 3-attempt 2s/5s/0 backoff), same non-messenger
# discipline as lumen_reflector/lumen_convo_learn - asserted, not just documented.
# MODEL: deepseek-v4-flash, not cerebras-oss-120b/cerebras-llama - measured live
# (this ticket): both cerebras entries route to cerebras/gpt-oss-120b, a reasoning
# model that burns the whole max_tokens budget on hidden reasoning_content and
# returns content:null (finish_reason=length) on a short single-sentence ask.
# deepseek-v4-flash sets extra_body.thinking.type=disabled in litellm_config.yaml
# for exactly this shape of call - fast, cheap, non-reasoning, non-messenger.
LITELLM_URL       = 'http://localhost:4000/v1/chat/completions'
FED_CTX_MODEL     = os.getenv('FED_CTX_MODEL', 'deepseek-v4-flash')
FED_CTX_TIMEOUT   = int(os.getenv('FED_CTX_TIMEOUT', '90'))
FED_CTX_MAX_TOKENS = int(os.getenv('FED_CTX_MAX_TOKENS', '150'))
LLM_CTX_DOC_MAX_CH = 6000  # bound the "document" sent to the model to the enclosing
                           # section, never the whole file - see module docstring
assert not FED_CTX_MODEL.startswith('messenger'), \
    'federate ctx_mode=llm must NOT use messenger-chat/re (bug #22249 isolation)'

LLM_CTX_PROMPT = '''<document>
{doc}
</document>
Here is the chunk we want to situate within the whole document:
<chunk>
{chunk}
</chunk>
Give a short, succinct context (1-2 sentences, under 100 tokens) to situate this \
chunk within the document for the purposes of improving search retrieval of the \
chunk. Answer with ONLY the succinct context, nothing else - no preamble, no quotes.'''


def tok_est(s):
    return max(1, len(s) // CHARS_PER_TOK)


def slugify(s):
    """Verbatim from context/tag_sections.py - section ids must match the index."""
    s = re.sub(r'[^\w\s-]', '', s).strip().lower()
    return re.sub(r'[-\s]+', '-', s)[:80]


def split_sections(text):
    """Verbatim from context/tag_sections.py: split on H2 only, H3+ stay inside."""
    sections, current = [], None
    for line in text.split('\n'):
        if re.match(r'^##\s+(?!#)', line):
            if current:
                sections.append(current)
            current = {'title': re.sub(r'^##\s+', '', line).strip(), 'lines': [line]}
        elif re.match(r'^#\s+(?!#)', line) and not current:
            current = {'title': re.sub(r'^#\s+', '', line).strip(), 'lines': [line]}
        else:
            if current is None:
                current = {'title': '[preamble]', 'lines': []}
            current['lines'].append(line)
    if current:
        sections.append(current)
    return sections


def _blocks(body_lines):
    """Section body -> [(heading_path, block_text)].

    Blocks are paragraph/list units separated by blank lines, with two rules that
    exist because both failure modes are visible in this corpus:
      * a fenced code block is never split - claude_memory.md is full of commands
        and half a command is worse than no command;
      * H3/H4 headers reset the heading path AND force a block boundary, so a
        chunk never silently spans two subsections.
    """
    out, cur, path, fence = [], [], [], False
    for line in body_lines:
        if line.strip().startswith('```'):
            fence = not fence
            cur.append(line)
            continue
        if not fence:
            m = re.match(r'^(#{3,6})\s+(.*)$', line)
            if m:
                if cur:
                    out.append((' > '.join(path), '\n'.join(cur).strip()))
                    cur = []
                depth = len(m.group(1)) - 3
                path = path[:depth] + [m.group(2).strip()]
                cur.append(line)
                continue
            if not line.strip():
                if cur:
                    out.append((' > '.join(path), '\n'.join(cur).strip()))
                    cur = []
                continue
        cur.append(line)
    if cur:
        out.append((' > '.join(path), '\n'.join(cur).strip()))
    return [(p, b) for p, b in out if b]


def _hard_split(text):
    """Split an oversized single block on line boundaries. No overlap: a duplicated
    tail would inflate the dedup scan against itself."""
    pieces, cur = [], []
    for line in text.split('\n'):
        if cur and sum(len(x) + 1 for x in cur) + len(line) > MAX_CH:
            pieces.append('\n'.join(cur))
            cur = []
        cur.append(line)
    if cur:
        pieces.append('\n'.join(cur))
    return pieces


def chunk_section(body_lines):
    """Greedy pack of blocks into MIN_CH..MAX_CH chunks. Returns [(heading_path, text)]."""
    chunks, cur, cur_path = [], [], ''
    for path, blk in _blocks(body_lines):
        if len(blk) > MAX_CH:
            if cur:
                chunks.append((cur_path, '\n\n'.join(cur)))
                cur = []
            for p in _hard_split(blk):
                chunks.append((path, p))
            cur_path = path
            continue
        cand_len = sum(len(x) + 2 for x in cur) + len(blk)
        if cur and cand_len > MAX_CH and sum(len(x) + 2 for x in cur) >= MIN_CH:
            chunks.append((cur_path, '\n\n'.join(cur)))
            cur, cur_path = [blk], path
            continue
        if cur and cand_len > MAX_CH:
            # under MIN but adding would overflow: flush anyway, a short precise
            # chunk beats a chunk that straddles the size ceiling
            chunks.append((cur_path, '\n\n'.join(cur)))
            cur, cur_path = [blk], path
            continue
        if not cur:
            cur_path = path
        cur.append(blk)
    if cur:
        chunks.append((cur_path, '\n\n'.join(cur)))
    return [(p, c.strip()) for p, c in chunks if c.strip()]


DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')


def header_context(doc_label, sec_title, sec_ix, n_sec, heading_path, i, k, lead):
    # n_sec is accepted but deliberately NOT rendered. MEASURED (scratch-DB
    # incremental test, this session): including "section 3/244" makes every
    # chunk's embed_text a function of the file's TOTAL section count, so
    # appending one new section to claude_memory.md re-embeds all ~400 chunks.
    # That is a context-economy fail dressed up as context. The section index
    # itself is not stable enough to be signal; the title and date are.
    """Deterministic 50-100 token contextual prefix (Anthropic contextual retrieval).

    WHY each field: document tells the retriever which corpus this came from,
    section title + date are what the query usually names, heading path pins the
    subsection, part i/k tells a mid-section chunk it is not the whole story, and
    the section lead line carries the topic sentence that later chunks lost.
    """
    d = DATE_RE.search(sec_title or '')
    bits = [f'From {doc_label}, section: "{sec_title}".']
    if d:
        bits.append(f'Dated {d.group(1)}.')
    if heading_path:
        bits.append(f'Subsection: {heading_path}.')
    if k > 1:
        bits.append(f'Part {i} of {k}.')
    if lead:
        bits.append(f'Section opens: {lead}')
    ctx = ' '.join(bits)
    return ctx[:CTX_MAX_CH]


def _litellm_key():
    if os.getenv('LITELLM_MASTER_KEY'):
        return os.environ['LITELLM_MASTER_KEY']
    try:
        for line in Path('/home/lumen/agent/.env').read_text().splitlines():
            line = line.strip()
            if line.startswith('LITELLM_MASTER_KEY='):
                return line.split('=', 1)[1].strip().strip('"\'')
    except Exception:
        pass
    return ''


def _llm_ctx_key(doc_label, sec_title, heading_path, ctext):
    """Cache key over raw content only (never over the generated ctx itself),
    so a re-run with unchanged source bytes is a cache hit regardless of how
    many times the model has been asked before."""
    return hashlib.sha256(
        f'{doc_label}|{sec_title}|{heading_path}|{ctext}'.encode()).hexdigest()


def _llm_ctx_call(prompt):
    """One offline completion. NEVER called from the recall hot path - only from
    `index --ctx llm`. Same retry shape as minerva_classifier (fail-loud with a
    reason, never a silently-empty context)."""
    key = _litellm_key()
    r, last_err = None, 'no_attempt'
    for wait in (2, 5, 0):
        try:
            r = requests.post(
                LITELLM_URL,
                headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                json={'model': FED_CTX_MODEL,
                      'messages': [{'role': 'user', 'content': prompt}],
                      'temperature': 0.0, 'max_tokens': FED_CTX_MAX_TOKENS},
                timeout=FED_CTX_TIMEOUT)
            if r.status_code == 200:
                break
            last_err = f'http_{r.status_code}: {r.text[:160]}'
        except requests.exceptions.RequestException as e:
            last_err = f'network: {e}'
        if wait:
            time.sleep(wait)
    if r is None or r.status_code != 200:
        return None, last_err
    txt = (r.json()['choices'][0]['message'].get('content') or '').strip()
    if not txt:
        return None, 'empty_content'
    return txt, None


def llm_context(conn, doc_label, sec_title, sec_ix, n_sec, heading_path, i, k,
                 lead, section_body, ctext, llm_stats):
    """ctx_mode=llm equivalent of header_context(): cache-or-generate a per-chunk
    contextual prefix. Falls back to the free deterministic header context on any
    LLM failure - a batch job must not die on one bad completion, and a degraded
    prefix beats no prefix at all (same fail-open spirit as _breadcrumb_v3_failure)."""
    key = _llm_ctx_key(doc_label, sec_title, heading_path, ctext)
    row = conn.execute(
        'SELECT ctx FROM federated_llm_ctx_cache WHERE cache_key=?', (key,)).fetchone()
    if row:
        llm_stats['cached'] += 1
        return row[0]
    prompt = LLM_CTX_PROMPT.format(
        doc=f'{doc_label}\nSection: "{sec_title}"\n\n{section_body[:LLM_CTX_DOC_MAX_CH]}',
        chunk=ctext[:MAX_CH])
    txt, err = _llm_ctx_call(prompt)
    if err:
        print(f'  ! llm ctx FAILED ({err}) for {sec_title!r} part {i}/{k}, '
              f'falling back to header context', file=sys.stderr)
        llm_stats['fail'] += 1
        ctx = header_context(doc_label, sec_title, sec_ix, n_sec, heading_path, i, k, lead)
    else:
        llm_stats['new'] += 1
        ctx = txt.strip()[:CTX_MAX_CH]
    conn.execute(
        """INSERT INTO federated_llm_ctx_cache (cache_key, ctx, model, created_at)
           VALUES (?,?,?,?)
           ON CONFLICT(cache_key) DO UPDATE SET ctx=excluded.ctx,
             model=excluded.model, created_at=excluded.created_at""",
        (key, ctx, FED_CTX_MODEL, time.strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    if llm_stats['new'] and llm_stats['new'] % 20 == 0:
        print(f'  .. {llm_stats["new"]} llm ctx generated '
              f'({llm_stats["cached"]} cached, {llm_stats["fail"]} fallback)')
    return ctx


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS federated_chunks (
        id           TEXT PRIMARY KEY,
        source_path  TEXT NOT NULL,
        source_kind  TEXT NOT NULL,      -- prose-section | prose-archive | session-archive
        section_id   TEXT NOT NULL,      -- matches claude_memory_index.md ids
        section_ix   INTEGER,
        chunk_ix     INTEGER,
        n_chunks     INTEGER,
        title        TEXT,
        heading_path TEXT,
        context      TEXT,               -- contextual augmentation prefix
        content      TEXT,               -- raw chunk, source-faithful
        embed_text   TEXT,               -- context + content: embedded AND lex-scored
        embedding    BLOB,
        content_sha  TEXT NOT NULL,
        ctx_mode     TEXT,
        tok_est      INTEGER,
        source_mtime REAL,
        origin       TEXT DEFAULT 'backfill',
        trust        TEXT DEFAULT 'derived',
        created_at   TEXT,
        updated_at   TEXT,
        CHECK (embedding IS NULL
               OR (typeof(embedding) = 'blob'
                   AND length(embedding) > 0
                   AND length(embedding) % 4 = 0))
    )""",
    'CREATE INDEX IF NOT EXISTS idx_fed_src ON federated_chunks(source_path, section_id, chunk_ix)',
    'CREATE INDEX IF NOT EXISTS idx_fed_sha ON federated_chunks(content_sha)',
    'CREATE INDEX IF NOT EXISTS idx_fed_kind ON federated_chunks(source_kind)',
    """CREATE TABLE IF NOT EXISTS federated_files (
        path TEXT PRIMARY KEY, mtime REAL, sha TEXT,
        n_chunks INTEGER, indexed_at TEXT
    )""",
    # wq#604: ctx_mode=llm result cache, keyed on raw chunk content (not on
    # ctx_mode or content_sha, which both depend on the generated text) - a
    # re-run over unchanged source bytes never re-pays for a completion.
    """CREATE TABLE IF NOT EXISTS federated_llm_ctx_cache (
        cache_key  TEXT PRIMARY KEY,
        ctx        TEXT NOT NULL,
        model      TEXT,
        created_at TEXT
    )""",
]


def ensure_schema(conn):
    for s in SCHEMA:
        conn.execute(s)
    conn.commit()


SESSION_NAME_RE = re.compile(r'^SESSION_(\d{4})(\d{2})(\d{2})_(\d{6})_(.+)$')


def session_label(path):
    """Humanised document label for a session archive.

    MEASURED (tier control P2, this session): the raw filename is the ONLY place a
    session's topic is written down - its internal H2s are boilerplate
    ("What changed this session"). But `_tokens()` in lumen_recall2 keeps hyphenated
    runs whole, so `vault-switchover-and-handoff-system` is a single token that the
    query "vault failback" can never match, and the dense channel sees a filename
    rather than a phrase. P2 went RED on exactly this. Splitting the slug back into
    words puts the session topic into both the embedding and the lexical channel,
    which is the entire point of contextual augmentation: give the chunk back the
    document context it lost.
    """
    m = SESSION_NAME_RE.match(path.stem)
    if not m:
        return f'session archive {path.name}'
    y, mo, d, _t, slug = m.groups()
    topic = slug.replace('-', ' ').replace('_', ' ').strip()
    return f'session archive from {y}-{mo}-{d}: {topic} ({path.name})'


# wq#577 adopt-or-record (report 577_prose_lifecycle.md sha-cited arms): indexing
# claude_memory_archive.md and merging it into the SAME federated_chunks population
# search_v3 reads unconditionally measured trap@1 RISE 0.188->0.25 (golden v4 frozen
# 8ba5afb0..., isolated A/B, only variable = this source's presence) - qid 243
# (abstention class, trap substr "pgvector") newly ranks archived legacy pgvector
# discussion at #1. wq#595 law: ANY trap@1 rise disqualifies. lumen_recall2.py has no
# per-source_kind filter on federated_chunks, so gating MUST happen at index time
# (never write the rows) rather than at read time. Recorded, shipped DARK: default 0,
# independent of MEMORY_V4_FEDERATED so turning federated recall on (as #574 tail-
# attack work does) does not silently inherit this regression.
ARCHIVE_SOURCE_ON = os.environ.get('MEMORY_V4_PROSE_ARCHIVE_SOURCE', '0') == '1'


def sources():
    """[(path, kind, doc_label)] - every prose corpus the house consults."""
    out = []
    if MEM.exists():
        out.append((MEM, 'prose-section', 'claude_memory.md (Lumen extended memory)'))
    if ARCHIVE_SOURCE_ON and ARCHIVE.exists():
        # wq#577: demoted sections must stay retrievable via federated recall (the
        # whole point of gating demotion on P1 existing) - same section-aware
        # chunker, distinct source_kind so a hit can be labelled "archived". DARK by
        # default - see ARCHIVE_SOURCE_ON docstring above.
        out.append((ARCHIVE, 'prose-archive',
                    'claude_memory_archive.md (Lumen demoted prose, wq#577)'))
    if SESSIONS.exists():
        for p in sorted(SESSIONS.glob('*.md')):
            out.append((p, 'session-archive', session_label(p)))
    return out


def plan_file(path, kind, doc_label, ctx_mode, conn=None, llm_stats=None):
    """path -> [chunk dicts] (no embeddings yet). Pure function of file bytes for
    ctx_mode in {header, none}. ctx_mode=llm is the documented exception: it reads/
    writes the federated_llm_ctx_cache table on `conn` and may make an offline
    network call - both build-time only, never at recall time."""
    text = path.read_text(errors='replace')
    mtime = path.stat().st_mtime
    # session archives are segmented by the same H2 rule; they simply yield fewer
    # sections per file. One rule, one code path, one thing to be wrong about.
    secs = split_sections(text)
    n_sec = len(secs)
    rows = []
    for si, sec in enumerate(secs):
        sid = f'{si:03d}_{slugify(sec["title"])}'
        body = sec['lines']
        lead = ''
        for ln in body[1:]:
            if ln.strip() and not ln.strip().startswith('#'):
                lead = ln.strip()[:160]
                break
        pieces = chunk_section(body)
        k = len(pieces)
        section_body = '\n'.join(body)
        for ci, (hpath, ctext) in enumerate(pieces, start=1):
            if ctx_mode == 'none':
                ctx = ''
            elif ctx_mode == 'llm':
                ctx = llm_context(conn, doc_label, sec['title'], si + 1, n_sec,
                                  hpath, ci, k, lead, section_body, ctext, llm_stats)
            else:
                ctx = header_context(doc_label, sec['title'], si + 1, n_sec,
                                     hpath, ci, k, lead)
            embed_text = (ctx + '\n\n' + ctext).strip() if ctx else ctext
            rid = 'fed_' + hashlib.sha1(
                f'{path}|{sid}|{ci}'.encode()).hexdigest()[:16]
            rows.append({
                'id': rid, 'source_path': str(path), 'source_kind': kind,
                'section_id': sid, 'section_ix': si, 'chunk_ix': ci, 'n_chunks': k,
                'title': sec['title'], 'heading_path': hpath, 'context': ctx,
                'content': ctext, 'embed_text': embed_text,
                'content_sha': hashlib.sha256(embed_text.encode()).hexdigest(),
                'ctx_mode': ctx_mode, 'tok_est': tok_est(ctext),
                'source_mtime': mtime,
            })
    return rows


def index(full=False, ctx_mode='header', limit=None, verbose=True):
    """Incremental index. Re-embeds ONLY chunks whose content_sha changed."""
    conn = sqlite3.connect(DB, timeout=60)
    ensure_schema(conn)
    t0 = time.time()
    stats = {'files': 0, 'skipped_files': 0, 'chunks': 0, 'new': 0,
             'changed': 0, 'unchanged': 0, 'deleted': 0, 'embed_fail': 0}
    llm_stats = {'new': 0, 'cached': 0, 'fail': 0}
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    budget = limit
    if not ARCHIVE_SOURCE_ON:
        # self-healing darkness: a source excluded from sources() is never visited
        # by the per-file loop below, so rows written while the flag was ON would
        # otherwise linger forever and still be read (lumen_recall2 has no
        # source_kind filter). Purge on every dark run instead of trusting an
        # operator to remember a manual cleanup step.
        purged = conn.execute(
            "DELETE FROM federated_chunks WHERE source_kind='prose-archive'").rowcount
        conn.execute("DELETE FROM federated_files WHERE path=?", (str(ARCHIVE),))
        if purged and verbose:
            print(f'  ARCHIVE_SOURCE dark: purged {purged} stale prose-archive chunk(s)')
        conn.commit()
    for path, kind, label in sources():
        mtime = path.stat().st_mtime
        prev = conn.execute('SELECT mtime FROM federated_files WHERE path=?',
                            (str(path),)).fetchone()
        have_rows = conn.execute(
            'SELECT COUNT(*) FROM federated_chunks WHERE source_path=?',
            (str(path),)).fetchone()[0]
        if (not full) and prev and abs(prev[0] - mtime) < 1e-6 and have_rows:
            stats['skipped_files'] += 1
            if verbose:
                print(f'  = {path.name}: mtime unchanged, {have_rows} chunks kept')
            continue
        rows = plan_file(path, kind, label, ctx_mode, conn=conn, llm_stats=llm_stats)
        stats['files'] += 1
        stats['chunks'] += len(rows)
        existing = {r[0]: (r[1], r[2]) for r in conn.execute(
            'SELECT id, content_sha, embedding FROM federated_chunks WHERE source_path=?',
            (str(path),))}
        keep = set()
        for r in rows:
            keep.add(r['id'])
            prior = existing.get(r['id'])
            if prior and prior[0] == r['content_sha'] and prior[1] and not full:
                stats['unchanged'] += 1
                continue
            if budget is not None and budget <= 0:
                continue
            emb = mm.embed(r['embed_text'])
            if emb is None:
                stats['embed_fail'] += 1
                if verbose:
                    print(f'  ! embed FAILED {r["id"]} {r["section_id"]}#{r["chunk_ix"]}',
                          file=sys.stderr)
                continue                       # never store a poisoned blob (wq#465)
            blob = np.asarray(emb, dtype=np.float32).tobytes()
            if prior:
                stats['changed'] += 1
            else:
                stats['new'] += 1
            conn.execute(
                """INSERT INTO federated_chunks
                   (id, source_path, source_kind, section_id, section_ix, chunk_ix,
                    n_chunks, title, heading_path, context, content, embed_text,
                    embedding, content_sha, ctx_mode, tok_est, source_mtime,
                    origin, trust, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'backfill','derived',
                           COALESCE((SELECT created_at FROM federated_chunks WHERE id=?), ?), ?)
                   ON CONFLICT(id) DO UPDATE SET
                     section_ix=excluded.section_ix, chunk_ix=excluded.chunk_ix,
                     n_chunks=excluded.n_chunks, title=excluded.title,
                     heading_path=excluded.heading_path, context=excluded.context,
                     content=excluded.content, embed_text=excluded.embed_text,
                     embedding=excluded.embedding, content_sha=excluded.content_sha,
                     ctx_mode=excluded.ctx_mode, tok_est=excluded.tok_est,
                     source_mtime=excluded.source_mtime, updated_at=excluded.updated_at""",
                (r['id'], r['source_path'], r['source_kind'], r['section_id'],
                 r['section_ix'], r['chunk_ix'], r['n_chunks'], r['title'],
                 r['heading_path'], r['context'], r['content'], r['embed_text'],
                 blob, r['content_sha'], r['ctx_mode'], r['tok_est'],
                 r['source_mtime'], r['id'], now, now))
            if budget is not None:
                budget -= 1
            if verbose and (stats['new'] + stats['changed']) % 50 == 0:
                conn.commit()
                print(f'  .. {stats["new"] + stats["changed"]} embedded '
                      f'({time.time() - t0:.0f}s)')
        # stale chunk rows: a shorter section must not leave orphans behind
        gone = [i for i in existing if i not in keep]
        if gone and (budget is None or budget > 0):
            conn.executemany('DELETE FROM federated_chunks WHERE id=?',
                             [(i,) for i in gone])
            stats['deleted'] += len(gone)
        if budget is None or budget > 0:
            conn.execute(
                """INSERT INTO federated_files (path, mtime, sha, n_chunks, indexed_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime,
                     sha=excluded.sha, n_chunks=excluded.n_chunks,
                     indexed_at=excluded.indexed_at""",
                (str(path), mtime,
                 hashlib.sha256(path.read_bytes()).hexdigest(), len(rows), now))
        conn.commit()
    conn.commit()
    stats['elapsed_s'] = round(time.time() - t0, 1)
    stats['total_rows'] = conn.execute(
        'SELECT COUNT(*) FROM federated_chunks').fetchone()[0]
    if ctx_mode == 'llm':
        stats['llm_ctx'] = llm_stats
    conn.close()
    print(json.dumps(stats, indent=2))
    return stats


def stats_cmd():
    conn = sqlite3.connect(DB)
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT source_kind, COUNT(*), SUM(tok_est), MIN(tok_est), MAX(tok_est),
                  SUM(embedding IS NULL), COUNT(DISTINCT section_id)
           FROM federated_chunks GROUP BY source_kind""").fetchall()
    print(f'{"kind":18} {"chunks":>7} {"tokens":>9} {"min":>5} {"max":>5} '
          f'{"noemb":>6} {"sections":>9}')
    for r in rows:
        print(f'{r[0]:18} {r[1]:7} {r[2] or 0:9} {r[3] or 0:5} {r[4] or 0:5} '
              f'{r[5] or 0:6} {r[6]:9}')
    for p, m, n, at in conn.execute(
            'SELECT path, mtime, n_chunks, indexed_at FROM federated_files'):
        print(f'  {Path(p).name}: {n} chunks, src mtime '
              f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(m))}, indexed {at}')
    conn.close()


def dedup(threshold=0.95, write_report=True):
    """PROPOSAL-ONLY near-duplicate scan. Never suppresses, never deletes.

    Two populations matter: federated-vs-federated (the same paragraph pasted into
    two sections of claude_memory.md, which is common) and federated-vs-archival
    (prose that has already been distilled into a note - the wq#577 demotion
    candidates). Both are reported; neither is acted on here.
    """
    conn = sqlite3.connect(DB)
    ensure_schema(conn)
    fed = []
    for r in conn.execute(
            'SELECT id, section_id, chunk_ix, title, embedding FROM federated_chunks '
            'WHERE embedding IS NOT NULL'):
        v = mm.decode_embedding(r[4], expect_dim=mm.EMBED_DIM)
        if v is not None:
            fed.append((r[0], r[1], r[2], r[3], v / (np.linalg.norm(v) or 1e-9)))
    notes = []
    for r in conn.execute(
            'SELECT id, title, embedding FROM archival_notes WHERE embedding IS NOT NULL '
            'AND id NOT IN (SELECT absorbed_id FROM note_merges)'):
        v = mm.decode_embedding(r[2], expect_dim=mm.EMBED_DIM)
        if v is not None:
            notes.append((r[0], r[1], v / (np.linalg.norm(v) or 1e-9)))
    conn.close()
    if not fed:
        print('no embedded federated chunks; nothing to scan')
        return {'fed_pairs': [], 'note_pairs': []}
    F = np.vstack([f[4] for f in fed])
    sim = F @ F.T
    np.fill_diagonal(sim, 0.0)
    ii, jj = np.where(np.triu(sim) >= threshold)
    fed_pairs = [{'a': fed[i][0], 'a_sec': fed[i][1], 'a_ix': fed[i][2],
                  'a_title': fed[i][3], 'b': fed[j][0], 'b_sec': fed[j][1],
                  'b_ix': fed[j][2], 'b_title': fed[j][3], 'cos': round(float(sim[i, j]), 4)}
                 for i, j in zip(ii, jj)]
    note_pairs = []
    if notes:
        N = np.vstack([n[2] for n in notes])
        cross = F @ N.T
        ii2, jj2 = np.where(cross >= threshold)
        note_pairs = [{'chunk': fed[i][0], 'chunk_sec': fed[i][1],
                       'note': notes[j][0], 'note_title': notes[j][1],
                       'cos': round(float(cross[i, j]), 4)}
                      for i, j in zip(ii2, jj2)]
    fed_pairs.sort(key=lambda d: -d['cos'])
    note_pairs.sort(key=lambda d: -d['cos'])
    print(f'federated<->federated pairs >= {threshold}: {len(fed_pairs)}')
    print(f'federated<->archival  pairs >= {threshold}: {len(note_pairs)}')
    if write_report:
        DREAM.mkdir(parents=True, exist_ok=True)
        out = DREAM / f'federated_dedup_{time.strftime("%Y%m%d_%H%M")}.md'
        L = [f'# Federated dedup proposals (wq#573) — {time.strftime("%Y-%m-%d %H:%M %Z")}',
             '',
             f'Threshold cosine >= {threshold}. **PROPOSAL-ONLY** — nothing here has been',
             'suppressed, merged, or deleted. claude_memory.md and sessions/ are read-only',
             'inputs to the indexer; prose demotion is wq#577 and is operator-applied.',
             '',
             f'- federated↔federated near-duplicates: **{len(fed_pairs)}**',
             f'- federated↔archival_notes overlaps: **{len(note_pairs)}** '
             '(prose already distilled into a note — wq#577 demotion candidates)',
             '']
        if fed_pairs:
            L += ['## Intra-prose duplicates (top 40)', '',
                  '| cos | chunk A | chunk B |', '|---|---|---|']
            L += [f'| {p["cos"]} | `{p["a_sec"]}#{p["a_ix"]}` {p["a_title"][:48]} '
                  f'| `{p["b_sec"]}#{p["b_ix"]}` {p["b_title"][:48]} |'
                  for p in fed_pairs[:40]]
            L.append('')
        if note_pairs:
            L += ['## Prose already covered by an archival note (top 40)', '',
                  '| cos | prose chunk | note |', '|---|---|---|']
            L += [f'| {p["cos"]} | `{p["chunk_sec"]}` | `{p["note"][:8]}` '
                  f'{(p["note_title"] or "")[:52]} |' for p in note_pairs[:40]]
            L.append('')
        out.write_text('\n'.join(L))
        print(f'wrote {out}')
    return {'fed_pairs': fed_pairs, 'note_pairs': note_pairs}


def probe(query, top_k=10):
    import lumen_recall2
    os.environ['MEMORY_V4_FEDERATED'] = '1'
    import importlib
    importlib.reload(lumen_recall2)
    for i, h in enumerate(lumen_recall2.search_v3(query, top_k=top_k), 1):
        print(f'#{i} [{h["tier"]}] {h["score"]:.3f} d={h["_d"]} lex={h["_lex"]} '
              f'{(h.get("id") or "")[:20]} {(h.get("title") or "")[:64]}')


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('index')
    p.add_argument('--full', action='store_true', help='re-embed everything')
    # wq#604: 'llm' generates per-chunk context via an offline LiteLLM batch call
    # (FED_CTX_MODEL, default cerebras-oss-120b, never messenger-chat/re). 'header'
    # stays the CLI default - it is the measured-adopted, zero-cost arm (report
    # 573_federated.md 6); 'llm' is opt-in for the head-to-head in 604_llm_ctx.md.
    p.add_argument('--ctx', default='header', choices=['header', 'none', 'llm'])
    p.add_argument('--limit', type=int, default=None, help='cap embeds this run')
    sub.add_parser('stats')
    d = sub.add_parser('dedup')
    d.add_argument('--threshold', type=float, default=0.95)
    pr = sub.add_parser('probe')
    pr.add_argument('query', nargs='+')
    a = ap.parse_args()
    if a.cmd == 'index':
        index(full=a.full, ctx_mode=a.ctx, limit=a.limit)
    elif a.cmd == 'stats':
        stats_cmd()
    elif a.cmd == 'dedup':
        dedup(threshold=a.threshold)
    elif a.cmd == 'probe':
        probe(' '.join(a.query))


if __name__ == '__main__':
    main()

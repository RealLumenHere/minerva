#!/usr/bin/env python3
"""lumen_recall2.py — Memory V3 P2: hybrid indexed recall (search-side, additive).

Fuses four deterministic signals — NO LLM calls at retrieval:
  dense   : cosine over archival_notes embeddings (existing nomic vectors)
  lex     : OR-mode token overlap (kills the FTS AND-brittleness failure mode)
  graph   : memory_edges anchoring — query-subject/ticket hits + expansion from
            entry candidates via shared subject/ticket anchors
  recency : mild freshness boost

Returns results in the same dict shape as minerva_memory.search() so it can be
swapped behind lumen_episode.recall via a flag. minerva_memory is untouched.

Usage (module): from lumen_recall2 import search_v3; search_v3(query, top_k=10)
CLI probe:      python3 lumen_recall2.py "your query"
"""
import math
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, '/home/lumen/minerva')
import minerva_memory as mm  # embed(), cosine()  (fail-open on embed loss)
import numpy as np

DB = '/home/lumen/minerva/minerva.db'
SUBJECT_STOPLIST = {'lumen', 'fleet', 'minerva', 'models', 'security', 'wq'}
TICKET_RE = re.compile(r'\b(wq|aq)#(\d+)\b', re.I)
STOPWORDS = {'the', 'a', 'an', 'to', 'of', 'in', 'on', 'for', 'was', 'is', 'it',
             'what', 'why', 'how', 'which', 'who', 'when', 'did', 'does', 'and',
             'or', 'with', 'after', 'before', 'that', 'this', 'from', 'by', 'at'}

_SUBJ_CACHE = {'ts': 0, 're': None}
_TAG_CACHE = {'ts': 0, 'tags': None}   # wq#466

# wq#465: last-run telemetry so a health probe can assert v3 actually ran the
# dense path instead of silently degrading. Read after a search_v3() call.
LAST_STATS = {'skipped_bad_emb': 0, 'dense_scanned': 0, 'failopen_v2': False}

# ── wq#574: fusion weights, env-overridable. Defaults ADOPTED 2026-08-14 ────────
# Tuned on golden_v4_frozen_20260814 (sha256 8ba5afb0…) by eval/wq574/sweep.py +
# constrain.py, which search under the adopt rule rather than optimising MRR and
# checking the gate afterwards. Live-verified arm wq574_s2_tuned:
#   R@1 .732 -> .778   MRR .795 -> .837   trap@1 .250 -> .188   p50 170 -> 175ms
#   every headline class flat or up; outage-rca +.049, procedure +.027
#   5-fold stratified CV held-out MRR +.0341, permutation p=.0024, 4/5 folds pick
#   this exact config; orig_193 MRR .789 -> .836 (so it is not set-growth overfit)
# V3 shipped values were 0.50/0.28/0.16/0.06 — recoverable by setting the env vars.
# Note recency tuned to ZERO: freshness was costing recall, not buying it.
W_D = float(os.environ.get('MEMORY_V4_W_DENSE', '0.59'))
W_LEX = float(os.environ.get('MEMORY_V4_W_LEX', '0.34'))
W_G = float(os.environ.get('MEMORY_V4_W_GRAPH', '0.05'))
W_REC = float(os.environ.get('MEMORY_V4_W_REC', '0.00'))

# ── wq#574 stage 1: entity-SPECIFICITY channel (flag-gated, default OFF) ────────
# WHY (wq#569 §3): the graph channel scores by anchor TYPE only, so matching
# `backup` (df 127) is worth exactly as much as matching `enp0s25` (df 1). The
# defect is an UNWEIGHTED channel, not a missing one. idf comes from memory_edges
# document frequency (corpus-derived); familiarity_scorer is deliberately NOT used
# as a positive weight — its vocabulary is the AI-news intel stream, wrong domain.
# Additive, non-filtering: candidate generation is untouched, this only re-scores.
#
# ADOPTED ON at w=0.02 as part of the tuned weight set — NOT on its own merit.
# Measured standalone at the V3 weights it LOSES at every weight tried
# (w=0.10 §3.1 formula: MRR .795->.722; w=0.04 abs-idf: .795->.754), because it is
# not an independent signal — it is a second positive vote for the same anchors the
# graph channel already rewards, which drowns dense. But inside the retuned set it
# is load-bearing: dropping it (d .59/lex .34/g .05/rec 0/ent 0) regresses hardware
# R@1 -.048 and gives back procedure, which disqualifies that arm. Graph weight
# fell .16 -> .05 in the same tuning, which is the same finding from the other side:
# flat anchor-type bonus out, idf-weighted anchor evidence in.
ENTITY_ON = os.environ.get('MEMORY_V4_ENTITY', '1') == '1'
ENTITY_W = float(os.environ.get('MEMORY_V4_ENTITY_W', '0.02'))
ENTITY_TYPE_W = {'ticket': 1.2, 'subject': 1.0, 'file': 1.0, 'tag': 0.7}
_DF_CACHE = {'ts': 0, 'df': None, 'n': 0}

# ── wq#574 stage 3: cross-encoder rerank — MEASURED, LOST, LEFT OFF ────────────
# Port of the wq#388 kit pattern (ab_rag_controller._rerank): reorder the top
# RERANK_POOL by lumen-rerank :8140, FAIL-OPEN — on any error, timeout, or length
# mismatch the incoming fusion order is returned untouched, so the reranker can
# never take recall down with it. Pool is 20 because #569 measured pool 50 off the
# latency budget at every thread setting this CPU can reach.
#
# DO NOT FLIP THIS ON WITHOUT RE-MEASURING. Arm wq574_s3_rerank vs the adopted
# tuned baseline: aggregate MRR .837 -> .871 looks like a win and is not one —
#   outage-rca R@1 -.049 and decision -.033  (both breach the -2pt class rule)
#   trap@1 .188 -> .250 and abstention pass-rate .500 -> .167 (correctness, not recall:
#     the cross-encoder pulls the stale/trap note UP, which is the failure mode
#     the trap penalty exists to catch)
#   p50 175 -> 339ms quiet, and #596 §4.2 says price the CONTENDED number, not the quiet one
# This is precisely the MultiConIR regression wq#569 §6 predicted for multi-condition
# causal queries — our two target tails. RERANK_CLASSES exists for per-class scoping
# but is NOT shippable today: production has no query classifier, so nothing can tell
# search_v3 a live query's class. A score-BLEND variant (CE as a channel rather than a
# replacement ordering) is the sane next experiment; it is a new design, not a retune.
# ── wq#573: FEDERATED tier — claude_memory.md sections + session archives ──────
# WHY (MEMORY_V4_PLAN §2 axis TOTAL): the two corpora the house actually consults
# have never been visible to recall. golden v4 carries 10 rows whose answers exist
# ONLY in that prose; wq#595 verified every one MISSES pre-adoption (federated
# coverage R@1 .000, n=10). This is a COVERAGE tier, not a ranking change: it adds
# candidates from federated_chunks (built by lumen_federate.py, sources read-only)
# and scores them with the SAME adopted #574 fusion expression. No weight is
# retuned here — a federation that wants different weights is a #602-adjacent
# follow-up, not a silent re-tune underneath an adopted arm.
#
# Fail-open by construction: a missing table, a bad blob, or any exception leaves
# the archival result list exactly as it was. Prose can never take recall down.
# graph/entity are 0.0 for a chunk — it carries no memory_edges, by design; the
# edge extractor runs over notes and prose is not a note.
FED_ON = os.environ.get('MEMORY_V4_FEDERATED', '0') == '1'
FED_POOL = int(os.environ.get('MEMORY_V4_FED_POOL', '40'))
FED_SCALE = float(os.environ.get('MEMORY_V4_FED_SCALE', '1.0'))
FED_CONTENT = int(os.environ.get('MEMORY_V4_FED_CONTENT', '2400'))
# Anthropic's recipe augments the BM25 index with the context too. Our lex channel
# is NOT BM25 — it is fraction-of-query-tokens-matched with no idf term, so a
# prefix carrying the document label and section title hands out free matches on
# common house vocabulary to all ~400 chunks at once. Measured both ways (arms B
# and C, report 573_federated.md §3); this switch is which TEXT the existing lex
# channel reads, not a change to any fusion weight.
FED_LEXCTX = os.environ.get('MEMORY_V4_FED_LEXCTX', '0') == '1'
# wq#603/654: tier-penalty sweep. Subtracts a fixed amount from each federated
# fusion score to compensate for the structural g+ent asymmetry (archival notes
# get up to .05*g + .02*ent that prose structurally cannot). Default 0.0 = no
# change to production behaviour. EVAL-ONLY until adopted by wq#655.
FED_PENALTY = float(os.environ.get('MEMORY_V4_FED_PENALTY', '0.0'))
LAST_FED = {'attempted': False, 'applied': False, 'scanned': 0, 'merged': 0,
            'ms': 0.0, 'err': None}
_FED_DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')


def _federated(conn, query, q_toks, q_emb, now_ts):
    """Score federated_chunks and return rows in the search_v3 dict shape.

    Two-pass on purpose: pass 1 reads only (id, embedding) so the ~400 chunk BLOBs
    are the only thing crossing the sqlite boundary on a cold query; pass 2 fetches
    text for the top FED_POOL alone. Fetching content for every chunk would put
    ~500KB of prose on the hot path to build a top-10.
    """
    LAST_FED.update(attempted=True, applied=False, scanned=0, merged=0, ms=0.0, err=None)
    t0 = time.time()
    try:
        if q_emb is None:
            LAST_FED['err'] = 'no query embedding'
            return []
        q_dim = int(q_emb.shape[0])
        dense = {}
        for r in conn.execute('SELECT id, embedding FROM federated_chunks '
                              'WHERE embedding IS NOT NULL'):
            v = mm.decode_embedding(r['embedding'], expect_dim=q_dim)
            if v is None:
                continue
            dense[r['id']] = mm.cosine(q_emb, v)
        LAST_FED['scanned'] = len(dense)
        if not dense:
            return []
        top = [i for i, _ in sorted(dense.items(), key=lambda x: -x[1])[:FED_POOL]]
        ph = ','.join('?' * len(top))
        rows = conn.execute(
            f'SELECT id, source_kind, section_id, chunk_ix, title, heading_path, '
            f'content, embed_text, source_mtime, trust FROM federated_chunks '
            f'WHERE id IN ({ph})', top).fetchall()
        out = []
        for r in rows:
            blob_toks = set(_tokens(
                (r['embed_text'] if FED_LEXCTX else r['content']) or ''))
            lex = (sum(1 for t in q_toks if t in blob_toks) / len(q_toks)) if q_toks else 0.0
            d = dense.get(r['id'], 0.0)
            m = _FED_DATE_RE.search(r['title'] or '')
            try:
                ts = (time.mktime(time.strptime(m.group(0), '%Y-%m-%d'))
                      if m else float(r['source_mtime'] or now_ts))
            except Exception:
                ts = now_ts
            rec = max(0.0, 1.0 - ((now_ts - ts) / 86400.0) / 365.0)
            fused = (W_D * d + W_LEX * lex + W_REC * rec) * FED_SCALE - FED_PENALTY
            hp = f" › {r['heading_path']}" if r['heading_path'] else ''
            out.append({'tier': 'federated', 'score': round(fused, 4),
                        'id': r['id'], 'kind': r['source_kind'],
                        'title': f"{r['title']}{hp}",
                        'content': (r['content'] or '')[:FED_CONTENT],
                        'tags': r['section_id'], 'ts': time.strftime(
                            '%Y-%m-%d', time.localtime(ts)), 'trust': r['trust'],
                        '_d': round(d, 3), '_lex': round(lex, 3), '_g': 0.0,
                        '_ent': 0.0, '_sec': f"{r['section_id']}#{r['chunk_ix']}"})
        LAST_FED.update(applied=True, merged=len(out),
                        ms=round((time.time() - t0) * 1000, 1))
        return out
    except Exception as e:                      # fail-open: prose never breaks recall
        LAST_FED.update(err=f'{type(e).__name__}: {e}',
                        ms=round((time.time() - t0) * 1000, 1))
        return []


RERANK_ON = os.environ.get('MEMORY_V4_RERANK', '0') == '1'
RERANK_URL = os.environ.get('MEMORY_V4_RERANK_URL', 'http://127.0.0.1:8140/rerank')
RERANK_POOL = int(os.environ.get('MEMORY_V4_RERANK_POOL', '20'))
RERANK_TMO = float(os.environ.get('MEMORY_V4_RERANK_TIMEOUT', '5'))
# Empty = all classes. Set to scope rerank to the classes it actually wins
# (wq#569 §6: MultiConIR predicts pointwise rerank HURTS multi-condition queries,
# which is exactly what outage-rca and procedure are).
RERANK_CLASSES = {c for c in os.environ.get('MEMORY_V4_RERANK_CLASSES', '').split(',') if c}
LAST_RERANK = {'attempted': False, 'applied': False, 'ms': 0.0, 'err': None}


def _rerank(query, scored):
    """Reorder scored[:RERANK_POOL] by cross-encoder relevance. Fail-open."""
    LAST_RERANK.update(attempted=True, applied=False, ms=0.0, err=None)
    head, tail = scored[:RERANK_POOL], scored[RERANK_POOL:]
    if len(head) < 2:
        return scored
    t0 = time.time()
    try:
        import requests
        r = requests.post(RERANK_URL, timeout=RERANK_TMO,
                          json={'query': query,
                                'texts': [((h['title'] or '') + '\n'
                                           + (h['content'] or '')) for h in head]})
        r.raise_for_status()
        scores = r.json()['scores']
        if len(scores) != len(head):
            LAST_RERANK['err'] = f'length mismatch {len(scores)}!={len(head)}'
            return scored
        order = sorted(range(len(head)), key=lambda i: -scores[i])
        for rank_new, i in enumerate(order):
            head[i]['_ce'] = round(float(scores[i]), 4)
            head[i]['_ce_rank'] = rank_new + 1
        LAST_RERANK.update(applied=True, ms=round((time.time() - t0) * 1000, 1))
        return [head[i] for i in order] + tail
    except Exception as e:                      # fail-open, never raise into recall
        LAST_RERANK.update(err=f'{type(e).__name__}: {e}',
                           ms=round((time.time() - t0) * 1000, 1))
        return scored


# ── wq#602: CE score-BLEND channel — MEASURED, LOST, LEFT OFF ──────────────
# Stage-3's failure (#574 report §3) was blamed partly on discarding fusion
# evidence via a REPLACEMENT reorder. The design-change hypothesis: blend the CE
# score in ADDITIVELY (score_final = fused + w_ce*sigmoid(ce_logit)) so a
# candidate wins on total evidence instead of the top-20 order being thrown away
# underneath it. Tested properly (v4/reports/602_ce_blend.md): sigmoid-normalized
# CE as one more channel on top of #574's ALREADY-ADOPTED d/lex/g/rec/ent, swept
# w_ce in [0.02, 0.40] against the frozen golden set (sha256 8ba5afb0...).
#
# RESULT: every single nonzero w_ce raises trap@1 .188 -> .250, identically,
# across the whole grid. Root cause isolated to ONE query: qid243 ("when did
# lumen memory migrate to pgvector" — an abstention-class trap; MEMORY_V4_PLAN.md
# explicitly rules pgvector out as a non-goal). At w_ce=0 two candidates tie at
# 0.5833 and the row-order tie-break happens to land the non-trap one first. The
# CE model scores the pgvector-mentioning (trap) note far higher on pure semantic
# overlap with the literal query text (ce_raw +1.16 vs -1.08) — a textbook cross-
# encoder failure mode: high lexical/semantic similarity mistaken for a correct
# answer, with no notion that the note describes a rejected proposal, not a fact.
# Any w_ce large enough to matter is large enough to break that tie toward the
# trap. **The hypothesis that "additive channel preserves fusion evidence and
# avoids stage-3's failure" is FALSE on this corpus** — the same query breaks
# under blending as under replacement-reorder, because the flaw is in what the CE
# model believes about pgvector mentions, not in how its output is combined.
# DO NOT ENABLE without a fix that survives qid243 specifically (e.g. abstention-
# aware down-weighting, or excluding CE entirely from abstention/trap-penalty
# classes if a query classifier ever ships — #574 §3 already ruled that out as
# unshippable without one). Mutually exclusive with RERANK_ON; if both are set,
# RERANK_ON wins (checked first) and CE_BLEND is skipped.
CE_BLEND_ON = os.environ.get('MEMORY_V4_CE_BLEND', '0') == '1'
CE_BLEND_POOL = int(os.environ.get('MEMORY_V4_CE_BLEND_POOL', '20'))
W_CE = float(os.environ.get('MEMORY_V4_W_CE', '0.08'))   # inert unless CE_BLEND_ON
LAST_CE_BLEND = {'attempted': False, 'applied': False, 'ms': 0.0, 'err': None}


def _ce_blend(query, scored):
    """Add sigmoid-normalized CE score into scored[:CE_BLEND_POOL]'s fused score,
    then re-sort the FULL list (not just the pool) — additive, never a reorder-
    only operation. Fail-open: any error leaves `scored` untouched, same
    contract as _rerank()."""
    LAST_CE_BLEND.update(attempted=True, applied=False, ms=0.0, err=None)
    head = scored[:CE_BLEND_POOL]
    if len(head) < 2:
        return scored
    t0 = time.time()
    try:
        import requests
        r = requests.post(RERANK_URL, timeout=RERANK_TMO,
                          json={'query': query,
                                'texts': [((h['title'] or '') + '\n'
                                           + (h['content'] or '')) for h in head]})
        r.raise_for_status()
        ce_scores = r.json()['scores']
        if len(ce_scores) != len(head):
            LAST_CE_BLEND['err'] = f'length mismatch {len(ce_scores)}!={len(head)}'
            return scored
        for h, ce in zip(head, ce_scores):
            ce_norm = 1.0 / (1.0 + math.exp(-float(ce)))
            h['_ce'] = round(float(ce), 4)
            h['score'] = round(h['score'] + W_CE * ce_norm, 4)
        scored.sort(key=lambda x: -x['score'])
        LAST_CE_BLEND.update(applied=True, ms=round((time.time() - t0) * 1000, 1))
        return scored
    except Exception as e:                      # fail-open, never raise into recall
        LAST_CE_BLEND.update(err=f'{type(e).__name__}: {e}',
                             ms=round((time.time() - t0) * 1000, 1))
        return scored


def _entity_df(conn):
    """(dst_type,dst_id) -> document frequency, plus live un-absorbed note count.

    Cached 600s exactly like _SUBJ_CACHE/_TAG_CACHE — a per-query GROUP BY over
    memory_edges would put a table scan on the hot recall path.
    """
    if time.time() - _DF_CACHE['ts'] > 600 or _DF_CACHE['df'] is None:
        df = {(t, i): c for t, i, c in conn.execute(
            'SELECT dst_type, dst_id, COUNT(DISTINCT src_id) FROM memory_edges '
            "WHERE src_type='note' AND dst_type IN ('subject','ticket','tag','file') "
            'GROUP BY dst_type, dst_id')}
        n = conn.execute('SELECT COUNT(*) FROM archival_notes WHERE id NOT IN '
                         '(SELECT absorbed_id FROM note_merges)').fetchone()[0]
        _DF_CACHE.update(ts=time.time(), df=df, n=max(int(n), 2))
    return _DF_CACHE['df'], _DF_CACHE['n']


def _entity_scores(conn, q_ents, nids):
    """ent(q, note) in [0,1]: ABSOLUTE idf mass of the query entities this note carries.

        ent = min(1, Σ_{e ∈ E(q)∩E(note)} idf(e)*type_w(e) / idf_max)
        idf(e) = ln(N / (1 + df(e))),  idf_max = ln(N / 2)   [the df==1 ceiling]

    MEASURED CORRECTION to wq#569 §3.1 (arm wq574_s1b_ent010, w=0.10):
    §3.1 normalizes by the QUERY's own total idf, which makes every fully-matched
    query score 1.0 regardless of how specific its entities are — the denominator
    cancels exactly the specificity the channel exists to capture. On a
    single-entity query (the common case) matching `backup` (df 127) then scored
    identically to matching `enp0s25` (df 1), and handed a flat +w to every one of
    the ~127 notes carrying it. Result: headline R@1 .732 -> .644, decision
    -.290. Dividing by a CONSTANT ceiling instead keeps specificity absolute:
    `backup` contributes 0.35*w, a df==1 anchor contributes 1.0*w.

    Retained from §3.1: E(q) restricted to entities the corpus indexes (df >= 1);
    an entity with no edges can never be matched, so it is not signal.
    """
    df, n = _entity_df(conn)
    live = [e for e in q_ents if df.get(e, 0) >= 1]
    idf_max = math.log(n / 2.0)
    if not live or idf_max <= 0:
        return {}
    out = {}
    for e in live:
        w = math.log(n / (1.0 + df[e])) * ENTITY_TYPE_W[e[0]] / idf_max
        for r in conn.execute(
                "SELECT src_id FROM memory_edges WHERE src_type='note' "
                'AND dst_type=? AND dst_id=?', (e[0], e[1])):
            if r[0] in nids:
                out[r[0]] = out.get(r[0], 0.0) + w
    return {k: min(1.0, v) for k, v in out.items()}


def _subjects_re(conn):
    if time.time() - _SUBJ_CACHE['ts'] > 600 or _SUBJ_CACHE['re'] is None:
        subs = {r[0] for r in conn.execute(
            'SELECT DISTINCT subject FROM state_facts').fetchall()}
        subs = sorted(s for s in subs if len(s) >= 3
                      and s not in SUBJECT_STOPLIST and not s.startswith('_'))
        _SUBJ_CACHE['re'] = re.compile(
            r'\b(' + '|'.join(re.escape(s) for s in subs) + r')\b')
        _SUBJ_CACHE['ts'] = time.time()
    return _SUBJ_CACHE['re']


def _tokens(text):
    return [t for t in re.findall(r'[a-z0-9][\w\-]+', text.lower())
            if t not in STOPWORDS and len(t) >= 2]


def _query_tags(conn, q_toks):
    """wq#466: tags named in the query, as a candidate ENTRY path.

    Tag edges were built by the edge extractor and are semantically precise, but
    stage (a) only pulled dst_type IN ('subject','ticket'), so they were never an
    entry path. That is why q43 ('lumen ram upgrade to 64gb details') could not be
    answered at ANY fusion weight: all three gold notes carry tag:ram-upgrade, none
    carry a matching subject edge ('lumen' is in SUBJECT_STOPLIST by design), and
    their dense ranks (98/166/32) fall outside the top-30 dense cut.

    A tag fires only when EVERY word of it appears in the query, so tag:ram-upgrade
    matches "ram upgrade" while tag:md1 cannot fire on a stray token. Vocabulary is
    cached for 600s exactly like _SUBJ_CACHE -- a per-query DISTINCT scan over
    memory_edges would put a table scan on the hot recall path.
    """
    if time.time() - _TAG_CACHE['ts'] > 600 or _TAG_CACHE['tags'] is None:
        _TAG_CACHE['tags'] = [
            (t, [p for p in t.replace('-', ' ').replace('_', ' ').split() if p])
            for (t,) in conn.execute(
                "SELECT DISTINCT dst_id FROM memory_edges "
                "WHERE src_type='note' AND dst_type='tag'").fetchall()]
        _TAG_CACHE['ts'] = time.time()
    ql = ' ' + ' '.join(q_toks) + ' '
    return {t for t, parts in _TAG_CACHE['tags']
            if parts and all(f' {p} ' in ql for p in parts)}


# wq#576: usage-aware salience. Both default OFF -> the fused expression below is
# byte-identical to pre-576 V3 (asserted by eval/test_wq576_salience.py, not assumed).
SALIENCE_ON = os.environ.get('MEMORY_V4_SALIENCE', '0') == '1'
ARCHIVE_FILTER = os.environ.get('MEMORY_V4_ARCHIVE_FILTER', '0') == '1'

# ── wq#1614: contradiction penalty (MEMORY_V4_W_CONTRA) ────────────────────────
# WHY (MINERVA_STALENESS_REVIEW §3.7 fix #9): wq#1610 detects notes that assert a
# state_facts value we have since closed and writes them to note_contradictions.
# Detection alone changes nothing a caller sees -- the stale note still ranks where
# it ranked. This multiplies by W_CONTRA the fused score of a note carrying an
# OPEN contradiction row that the query asks about (_asks_about, wq#3017).
#
# SOFT, not a filter. A contradicted note is usually a good procedure wrapped
# around one dead fact ("ssh nephthys at 100.98.74.87, then ...") -- the procedure
# is still the best answer we have and dropping the note loses it. 0.5 demotes;
# it cannot make a note unreachable, and it preserves the relative order of the
# contradicted set among itself.
#
# This is NOT the recency prior coming back. W_REC stays 0.00: freshness as a
# blanket prior was measured to COST recall (see the #574 block above). This
# fires only on notes we hold positive, row-level evidence about.
#
# 1.0 == off, and off is byte-identical to pre-1614 scoring (no query, no set,
# no multiply) -- asserted by test_contra_penalty.py, not assumed.
W_CONTRA = float(os.environ.get('MEMORY_V4_W_CONTRA', '0.5'))
# Eval hook: read the contradiction set from a frozen JSON export instead of the
# live table, so a benchmark measures a fixed set rather than whatever the hourly
# scan happened to have written that minute. Rows are the note_contradictions
# export shape; open == resolved_at/cleared_at null.
CONTRA_FIXTURE = os.environ.get('MEMORY_V4_CONTRA_FIXTURE', '')
_CONTRA_FIXTURE_CACHE = {'path': None, 'rows': None}


def _contra_open_col(conn):
    """Name of the resolution column, or None if the table does not exist.

    wq#1610 shipped the column as `cleared_at`; the live table exports it as
    `resolved_at` (eval/fixtures/note_contradictions_20260913.json). Introspect
    rather than pick one and be silently wrong on the other -- a wrong column
    name would raise, get swallowed by the fail-open below, and the penalty would
    read as "measured, moved nothing" when it never ran at all.
    """
    cols = {r[1] for r in conn.execute('PRAGMA table_info(note_contradictions)')}
    for c in ('resolved_at', 'cleared_at'):
        if c in cols:
            return c
    return None


def _fixture_rows():
    rows = _CONTRA_FIXTURE_CACHE['rows']
    if rows is None or _CONTRA_FIXTURE_CACHE['path'] != CONTRA_FIXTURE:
        import json
        with open(CONTRA_FIXTURE) as fh:
            rows = [(r['note_id'], r['subject'], r['predicate'], r['dead_value'])
                    for r in json.load(fh)
                    if r.get('resolved_at') is None and r.get('cleared_at') is None]
        _CONTRA_FIXTURE_CACHE.update(path=CONTRA_FIXTURE, rows=rows)
    return rows


def _words(text):
    return re.findall(r'[a-z0-9]+', (text or '').lower())


def _asks_about(q_words, q_lower, subject, predicate, dead_value):
    """True when the query asks about THIS dead fact, not merely near it.

    wq#3017: the blanket multiply was rejected by the golden gate. qid 142's gold
    note carries a live nephthys/ts_ip row, and a query about re-provisioning with
    a fleet auth key -- which never mentions nephthys or its IP -- lost its rank-1
    answer to it. So a row fires only when the query names its subject AND either
    a word of its predicate or its dead value ("nephthys ip", "is llama33-local
    still live-verified"). Subject alone is not enough ("ssh into nephthys" wants
    the procedure, the dead IP inside it is incidental), and predicate or value
    alone is not either: 'status' and 'dead' are ordinary English.
    """
    subj = _words(subject)
    if not subj or not all(w in q_words for w in subj):
        return False
    if any(len(w) >= 2 and w in q_words for w in _words(predicate)):
        return True
    dv = (dead_value or '').strip().lower()
    return len(dv) >= 2 and dv in q_lower


def _contradicted(conn, nids, query):
    """Subset of `nids` with an OPEN note_contradictions row `query` asks about.

    Fail-open by construction: if the table is absent (the #1610 scan is not
    installed on this box) or the query raises, the result is the empty set and
    every score is untouched. A detector outage must not reorder recall.
    """
    if not nids:
        return set()
    try:
        if CONTRA_FIXTURE:
            rows = [r for r in _fixture_rows() if r[0] in nids]
        else:
            col = _contra_open_col(conn)
            if col is None:
                return set()
            nids = list(nids)
            ph = ','.join('?' * len(nids))
            rows = conn.execute(
                f'SELECT note_id, subject, predicate, dead_value '
                f'FROM note_contradictions '
                f'WHERE {col} IS NULL AND note_id IN ({ph})', nids).fetchall()
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return set()
    q_words, q_lower = set(_words(query)), query.lower()
    return {r[0] for r in rows if _asks_about(q_words, q_lower, r[1], r[2], r[3])}


def _search_v3_impl(query, top_k=10):
    # wq#2895: this runs INSIDE lumen-memoryd on every /recall. Before, any raise
    # mid-query left the connection (and an un-reset SELECT holding SHARED) pinned
    # by the traceback until GC -- the precondition that made memoryd's audit
    # COMMIT go busy on 2026-09-28. Close on every exit; the core's own early
    # conn.close() calls stay harmless (closing twice is a no-op).
    conn = sqlite3.connect(DB)
    try:
        return _search_v3_core(conn, query, top_k)
    finally:
        conn.close()


def _search_v3_core(conn, query, top_k=10):
    LAST_STATS.update(skipped_bad_emb=0, dense_scanned=0, failopen_v2=False)
    conn.row_factory = sqlite3.Row
    q_toks = _tokens(query)
    q_subjects = set(m.group(0) for m in _subjects_re(conn).finditer(query.lower()))
    q_tickets = set(f'{m.group(1).lower()}#{m.group(2)}'
                    for m in TICKET_RE.finditer(query))
    q_emb = mm.embed(query)

    # ── candidate pool ──────────────────────────────────────────────────────
    cand = {}   # nid -> {'graph': float}

    def add(nid, graph_bonus):
        c = cand.setdefault(nid, {'graph': 0.0})
        c['graph'] = max(c['graph'], graph_bonus)

    # (a) query-anchor candidates: notes edged to subjects/tickets IN the query
    # wq#466: 'tag' added as a third anchor type at bonus 0.9 -- below subject(1.0)
    # and ticket(1.2), since a tag is weaker evidence than a named subject.
    # Measured on the 56-query golden set: R@1 .9107->.9286, R@5 .9643->.9821,
    # R@10 .9821->1.0000, MRR .9330->.9479.
    q_tags = _query_tags(conn, q_toks)          # wq#574: hoisted, reused by E(q)
    for anchor_set, dtype, bonus in ((q_subjects, 'subject', 1.0),
                                     (q_tickets, 'ticket', 1.2),
                                     (q_tags, 'tag', 0.9)):
        for a in anchor_set:
            for r in conn.execute(
                    "SELECT src_id FROM memory_edges WHERE src_type='note' "
                    'AND dst_type=? AND dst_id=? LIMIT 200', (dtype, a)):
                add(r['src_id'], bonus)

    # (b) dense entry: cosine over all embedded notes (brute force, ~1000 rows)
    #
    # wq#465: `embedding IS NOT NULL` does NOT exclude a zero-length blob in SQLite
    # (x'' is a BLOB of length 0, and IS NOT NULL is true for it). The v2 scan in
    # minerva_memory.search() had an `if not emb_b: continue` guard; the v3 port
    # dropped it and relied on the SQL filter alone. One poisoned row from a failed
    # embed was therefore enough to raise ValueError out of search_v3 and silently
    # drop the WHOLE fleet back to v2 recall (R@1 .911 -> .839) until someone
    # noticed a stderr line. decode_embedding() is dimension-checked and never
    # raises; skips are counted and surfaced by memory_embed_guard.py.
    dense = {}
    skipped_bad_emb = 0
    if q_emb is not None:
        q_dim = int(q_emb.shape[0])
        for r in conn.execute(
                'SELECT id, embedding FROM archival_notes WHERE embedding IS NOT NULL '
                'AND id NOT IN (SELECT absorbed_id FROM note_merges)'):
            note_emb = mm.decode_embedding(r['embedding'], expect_dim=q_dim)
            if note_emb is None:
                skipped_bad_emb += 1
                continue
            sim = mm.cosine(q_emb, note_emb)
            dense[r['id']] = sim
            if sim >= 0.55:
                add(r['id'], 0.0)
        # ensure top-30 dense are in pool even below threshold
        for nid, _s in sorted(dense.items(), key=lambda x: -x[1])[:30]:
            add(nid, cand.get(nid, {}).get('graph', 0.0))
    LAST_STATS.update(skipped_bad_emb=skipped_bad_emb, dense_scanned=len(dense))
    if skipped_bad_emb:
        print(f'[lumen_recall2] WARN {skipped_bad_emb} unusable embedding blob(s) '
              f'skipped in dense scan -- run memory_embed_guard.py repair',
              file=sys.stderr)

    # (c) expansion: shared subject/ticket anchors of the top dense entries
    top_entry = [nid for nid, _s in sorted(dense.items(), key=lambda x: -x[1])[:10]]
    if top_entry:
        ph = ','.join('?' * len(top_entry))
        anchors = conn.execute(
            f"SELECT DISTINCT dst_type, dst_id FROM memory_edges "
            f"WHERE src_type='note' AND src_id IN ({ph}) "
            f"AND dst_type IN ('subject','ticket')", top_entry).fetchall()
        for a in anchors[:25]:
            for r in conn.execute(
                    "SELECT src_id FROM memory_edges WHERE src_type='note' "
                    'AND dst_type=? AND dst_id=? LIMIT 60',
                    (a['dst_type'], a['dst_id'])):
                add(r['src_id'], max(cand.get(r['src_id'], {}).get('graph', 0.0), 0.35))

    if not cand:
        conn.close()
        LAST_STATS['failopen_v2'] = True       # wq#465: make the degrade observable
        return mm.search(query, top_k=top_k)   # fail-open to v2

    # ── fetch + score ───────────────────────────────────────────────────────
    # wq#610: revoked/superseded rows (archival_notes.revoked_at /.superseded_by,
    # migration 0008) were visible to every DIRECT caller of search_v3 -- CLI,
    # bench, lumen_episode -- while memoryd lanes already had them hidden
    # (§2.5). Not a scored feature to A/B: revoked means "should never be shown
    # to anyone but the operator" (§4.1), so this is a governance gap closing,
    # unconditional, no flag -- same as the note_merges absorbed_id exclusion
    # one clause over. idx_archival_live (revoked_at, superseded_by) already
    # exists (migration 0008) so this is an indexed IS NULL, not a scan.
    ph = ','.join('?' * len(cand))
    rows = conn.execute(
        f'SELECT id, kind, title, content, tags, created_at, trust FROM archival_notes '
        f'WHERE id IN ({ph}) AND id NOT IN (SELECT absorbed_id FROM note_merges) '
        f'AND revoked_at IS NULL AND superseded_by IS NULL',
        list(cand.keys())).fetchall()
    now_ts = time.time()

    # wq#574 stage 1: entity-specificity, flag-gated. Flag OFF => ent_scores empty
    # and the fused expression below is byte-identical to V3 (asserted by
    # eval/test_wq574_entity.py, not assumed).
    ent_scores = {}
    if ENTITY_ON:
        q_ents = ({('subject', s) for s in q_subjects}
                  | {('ticket', t) for t in q_tickets}
                  | {('tag', t) for t in q_tags})
        if q_ents:
            ent_scores = _entity_scores(conn, q_ents, set(cand.keys()))

    # wq#1614: one query for the whole candidate pool, skipped entirely when the
    # penalty is off, so "off" costs nothing rather than costing a no-op SELECT.
    contra = (_contradicted(conn, set(cand.keys()), query)
              if W_CONTRA != 1.0 else set())

    scored = []
    for r in rows:
        blob_toks = set(_tokens((r['title'] or '') + ' ' + (r['content'] or '')[:2500]))
        lex = (sum(1 for t in q_toks if t in blob_toks) / len(q_toks)) if q_toks else 0.0
        d = dense.get(r['id'], 0.0)
        g = min(cand[r['id']]['graph'], 1.2) / 1.2
        try:
            age_d = (now_ts - time.mktime(time.strptime(
                r['created_at'][:10], '%Y-%m-%d'))) / 86400.0
        except Exception:
            age_d = 365.0
        rec = max(0.0, 1.0 - age_d / 365.0)
        ent = ent_scores.get(r['id'], 0.0)
        fused = W_D * d + W_LEX * lex + W_G * g + W_REC * rec
        if ENTITY_ON:
            fused += ENTITY_W * ent
        # wq#1614: applied to the fused RETRIEVAL score only. The #576 salience
        # boost below is deliberately outside the multiply -- it is a usage term,
        # not evidence about the note's content, and scaling it here would make
        # the penalty's size depend on how often the note happens to be read.
        contradicted = r['id'] in contra
        if contradicted:
            fused *= W_CONTRA
        hit = {'tier': 'archival', 'score': round(fused, 4),
               'id': r['id'], 'kind': r['kind'], 'title': r['title'],
               'content': (r['content'] or '')[:800],
               'tags': r['tags'], 'ts': r['created_at'], 'trust': r['trust'],
               '_d': round(d, 3), '_lex': round(lex, 3), '_g': round(g, 3),
               '_ent': round(ent, 3)}
        if contradicted:
            hit['_contra'] = W_CONTRA
        scored.append(hit)
    if FED_ON:
        scored.extend(_federated(conn, query, q_toks, q_emb, now_ts))
    # wq#576 salience: ADDITIVE and NON-FILTERING (a multiplicative/filtering usage
    # term makes a cold-start note permanently unreachable). Flag OFF -> no query,
    # no delta, zero cost.
    if SALIENCE_ON:
        try:
            import lumen_salience as _sal
            _bm = _sal.boost_map(conn, [h['id'] for h in scored if h.get('id')])
            for h in scored:
                _d = _bm.get(h.get('id'))
                if _d:
                    h['score'] = round(h['score'] + _d, 4)
                    h['_sal'] = round(_d, 4)
        except Exception:
            pass
    if ARCHIVE_FILTER:
        try:
            import lumen_salience as _sal
            _arch = _sal.archived_ids(conn)
            if _arch:
                scored = [h for h in scored if h.get('id') not in _arch]
        except Exception:
            pass
    conn.close()
    scored.sort(key=lambda x: -x['score'])
    if RERANK_ON:
        scored = _rerank(query, scored)
    elif CE_BLEND_ON:
        scored = _ce_blend(query, scored)
    return scored[:top_k]


def search_v3(query, top_k=10):
    """V3 hybrid recall + wq#576 hit-logging tap.

    The tap wraps the implementation rather than sitting inside it so that BOTH
    return paths are covered -- including the fail-open-to-v2 path, which is the
    one a degraded box actually takes. Logging is fail-open and never raises.
    """
    hits = _search_v3_impl(query, top_k=top_k)
    try:
        import lumen_salience as _sal
        _sal.log_hits(query, hits)
    except Exception:
        pass
    return hits


if __name__ == '__main__':
    q = ' '.join(sys.argv[1:]) or 'test query'
    for i, h in enumerate(search_v3(q), 1):
        contra = f" contra=x{h['_contra']}" if '_contra' in h else ''
        print(f"#{i} [{h['tier'][:4]}] {h['score']:.3f} (d={h['_d']} lex={h['_lex']} "
              f"g={h['_g']} ent={h['_ent']}{contra}) {h.get('_sec') or h['id'][:8]} "
              f"{h['title'][:70]}")

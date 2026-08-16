#!/usr/bin/env python3
"""
lumen_instinct_worth.py - wq#578 [mv4-P6]: instinct effectiveness loop (GOVERNED).

Design of record: research/salience_forgetting.md section 6 ("SAME MACHINERY ->
INSTINCT EFFECTIVENESS"). Reuses wq#576's Memory Worth two-counter machinery
(lumen_salience.worth_stats, the same Beta-Bernoulli estimator note_salience uses)
and its outcome labels (note_hit_outcomes, populated by lumen_episode.commit() via
lumen_salience.label_outcome() on every episode) -- does NOT reimplement either,
per the ticket's explicit sequencing note.

For each of the 16 accepted /home/lumen/instincts.yaml rules:
  instinct_worth = Pr[success | instinct ADHERED]     (Beta-Bernoulli mean)
  mirror         = Pr[success | instinct VIOLATED]
  delta = worth_adhere - worth_violate   -- THE signal. An instinct scoring 0.9 on
  adherence means nothing if it also scores 0.9 on violation; it is not doing work.

ADHERENCE DETECTION (the hard part, must NOT be an LLM judge -- same discipline
that keeps golden v4 exact-match; research explicitly names mechanical proxies):
  VIOLATION  -- an fq ledger row (wq#590, already populated) whose `prevented_by`
               text token-overlaps an instinct's rule text above threshold. fq rows
               ARE incidents; the row existing at all means a failure happened, so
               a match here is evidence the matched instinct was NOT followed.
  RELEVANCE  -- an episode (archival_notes row, looked up by note_hit_outcomes.
               episode_id -- see "why episode population is bounded to labeled
               outcomes" below) whose content token-CONTAINS a high fraction of the
               instinct's own vocabulary. An episode relevant to instinct I but not
               fq-linked to it is treated as ADHERED (the instinct's domain came up,
               nothing failed against it).
  ADHERE = RELEVANT - VIOLATED (per instinct).

DEPENDENCY-AWARE SUPPORT SCORING (GovMem lesson, cited in the ticket): only episodes
BOTH (a) outcome-labeled (success/failure known, not still 'pending') AND (b)
demonstrably relevant to the specific instinct count as support. This is why episode
population is bounded to note_hit_outcomes rows rather than scanned across all
archival_notes: correlated but irrelevant traces (a session that happened to succeed
while an unrelated instinct sat unmentioned) must not be counted as support for or
against it, and note_hit_outcomes is already the exact "this episode's outcome is
known" set -- everything else is 'pending' or unlabeled and correctly excluded.

OUTPUT: confidence-update PROPOSALS, annotated into the EXISTING
dream/instincts_candidates.yaml (lumen_dream.write_instincts, already proposal-only)
under a new `effectiveness:` key. NOTHING here writes /home/lumen/instincts.yaml.
GovMem's 0/133 safe-auto-promotion result is the whole argument (cited in the SOTA
report for exactly this reason) -- Frank-gated accept is unchanged, unconditionally,
same standing law as wq#577's claude_memory.md.

CLI:
  python3 lumen_instinct_worth.py propose
  python3 lumen_instinct_worth.py stats
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/lumen/minerva')
import lumen_salience as sal  # noqa: E402  worth_stats(), resolve_pending(), _connect
import lumen_dream  # noqa: E402  _norm_tokens()

DB = os.environ.get('MINERVA_DB', '/home/lumen/minerva/minerva.db')
FQ_DB = os.environ.get('FQ_DB', '/home/lumen/agents/failure-queue/failures.db')
INSTINCTS_PATH = Path('/home/lumen/instincts.yaml')
DREAM_DIR = Path(os.environ.get('LUMEN_DREAM_DIR', '/home/lumen/minerva/dream'))

VIOLATE_THR = float(os.environ.get('MEMORY_V4_INSTINCT_VIOLATE_THR', '0.35'))
RELEVANCE_CONTAINMENT_THR = float(os.environ.get('MEMORY_V4_INSTINCT_RELEVANCE_THR', '0.5'))
MIN_N_FOR_SIGNAL = int(os.environ.get('MEMORY_V4_INSTINCT_MIN_N', '3'))

EPISODE_RE = re.compile(r'episode:([0-9a-f]{6,})')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _tokens(s):
    return lumen_dream._norm_tokens(s or '')


def load_instincts():
    """[(instinct_id, rule_text)]. Pure regex, not PyYAML -- instincts.yaml is a
    flat `- "..."` list under `rules:` and stays that way by convention (see its own
    header comment); a full YAML parse would be one more thing to be wrong about
    for a format this simple, and instincts.yaml is hand-edited by Frank, not
    generated, so it is worth reading defensively rather than trusting structure."""
    out = []
    if not INSTINCTS_PATH.exists():
        return out
    in_rules = False
    for line in INSTINCTS_PATH.read_text().splitlines():
        if re.match(r'^rules:\s*$', line):
            in_rules = True
            continue
        if not in_rules:
            continue
        m = re.match(r'^\s*-\s*"(.*)"\s*$', line)
        if not m:
            if line.strip() and not line.lstrip().startswith('#') and not line.startswith(' '):
                break  # next top-level key -- rules: block ended
            continue
        rule = m.group(1)
        iid = hashlib.sha1(rule.encode()).hexdigest()[:8]
        out.append((iid, rule))
    return out


# ============================ 1. VIOLATION (fq) =============================

def fq_violations(instincts):
    """instinct_id -> [(episode_id, score)] from fq `prevented_by` token-overlap.
    Best-instinct-wins per fq row (a single incident is evidence against the ONE
    rule it most resembles, not every rule that shares a stray word)."""
    out = {iid: [] for iid, _ in instincts}
    if not os.path.exists(FQ_DB):
        return out
    con = sqlite3.connect(f'file:{FQ_DB}?mode=ro', uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, evidence, prevented_by FROM failures "
            "WHERE prevented_by IS NOT NULL AND prevented_by != ''").fetchall()
    finally:
        con.close()
    for r in rows:
        m = EPISODE_RE.search(r['evidence'] or '')
        if not m:
            continue
        ep_id = m.group(1)
        pb_toks = _tokens(r['prevented_by'])
        if not pb_toks:
            continue
        best_iid, best_score = None, 0.0
        for iid, rule in instincts:
            rt = _tokens(rule)
            if not rt:
                continue
            inter = len(pb_toks & rt)
            union = len(pb_toks | rt) or 1
            score = inter / union
            if score > best_score:
                best_iid, best_score = iid, score
        if best_iid and best_score >= VIOLATE_THR:
            out[best_iid].append((ep_id, round(best_score, 4)))
    return out


# ============================ 2. RELEVANCE (episodes) ========================

def labeled_episodes(conn):
    """[(episode_id, outcome, content)] -- bounded to note_hit_outcomes rows with a
    RESOLVED outcome (success/failure, never 'pending'). This bound IS the
    dependency-aware support scoring: an episode with unknown outcome cannot
    support Pr[success|...] for anything."""
    rows = conn.execute(
        "SELECT ho.episode_id, ho.outcome, n.content FROM note_hit_outcomes ho "
        "JOIN archival_notes n ON n.id = ho.episode_id "
        "WHERE ho.outcome IN ('success','failure')").fetchall()
    return [(r[0], r[1], r[2] or '') for r in rows]


def episode_relevance(episodes, instincts):
    """instinct_id -> [(episode_id, containment)] where containment = fraction of
    the instinct's OWN vocabulary found in the episode content. Asymmetric on
    purpose: a short rule's tokens mostly present in a long episode is signal;
    symmetric Jaccard against a whole episode's token set would be near-zero for
    every instinct regardless of relevance, just from length mismatch."""
    out = {iid: [] for iid, _ in instincts}
    ep_toks = [(eid, _tokens(content)) for eid, _out, content in episodes]
    for iid, rule in instincts:
        rt = _tokens(rule)
        if not rt:
            continue
        for eid, etoks in ep_toks:
            if not etoks:
                continue
            containment = len(rt & etoks) / len(rt)
            if containment >= RELEVANCE_CONTAINMENT_THR:
                out[iid].append((eid, round(containment, 4)))
    return out


# ============================ 3. WORTH (reuses #576) ==========================

def compute(conn=None):
    """[{id, rule, n_adhere, n_violate, worth_adhere, worth_violate, delta,
    evidence: [...]}] for all 16 (or however many) instincts. Never mutates
    instincts.yaml. Writes nothing -- caller (propose()) owns persistence."""
    own = conn is None
    conn = conn or sal._connect(DB)
    try:
        sal.resolve_pending(conn)  # idempotent; upgrades pending->success/failure via wq status
        instincts = load_instincts()
        if not instincts:
            return []
        violations = fq_violations(instincts)
        episodes = labeled_episodes(conn)
        outcome_by_ep = {eid: out for eid, out, _c in episodes}
        relevance = episode_relevance(episodes, instincts)

        results = []
        for iid, rule in instincts:
            # dependency-aware support scoring (GovMem lesson, ticket-cited): fq's
            # `evidence` field can reference ANY episode id, including ones with no
            # resolved outcome yet (pre-#576, or the outcome joiner hasn't run for
            # them). Bounding to outcome_by_ep here keeps n_violate/n_adhere equal
            # to the count that ACTUALLY contributes to succ/fail below -- without
            # this a rule could show "n_violate: 1" while worth_violate silently
            # stayed at the uninformative 0.5 prior because that one match never
            # had a resolved outcome to count. (Caught by inspecting the evidence
            # table after a real run, not by reading the code.)
            viol_eps = {eid for eid, _s in violations.get(iid, [])} & outcome_by_ep.keys()
            rel_eps = {eid for eid, _s in relevance.get(iid, [])}
            adhere_eps = rel_eps - viol_eps

            def _counts(eps):
                succ = sum(1 for e in eps if outcome_by_ep.get(e) == 'success')
                fail = sum(1 for e in eps if outcome_by_ep.get(e) == 'failure')
                return succ, fail

            succ_a, fail_a = _counts(adhere_eps)
            succ_v, fail_v = _counts(viol_eps)
            worth_a, _lcb_a = sal.worth_stats(succ_a, fail_a)
            worth_v, _lcb_v = sal.worth_stats(succ_v, fail_v)
            n_a, n_v = len(adhere_eps), len(viol_eps)
            delta = round(worth_a - worth_v, 4)
            evidence = (
                [{'episode_id': e, 'class': 'adhere', 'method': 'episode-relevance',
                  'score': dict(relevance.get(iid, [])).get(e), 'outcome': outcome_by_ep.get(e)}
                 for e in adhere_eps] +
                [{'episode_id': e, 'class': 'violate', 'method': 'fq-prevented-by',
                  'score': dict(violations.get(iid, [])).get(e), 'outcome': outcome_by_ep.get(e)}
                 for e in viol_eps])
            results.append({
                'id': iid, 'rule': rule, 'n_adhere': n_a, 'n_violate': n_v,
                'succ_adhere': succ_a, 'fail_adhere': fail_a,
                'succ_violate': succ_v, 'fail_violate': fail_v,
                'worth_adhere': round(worth_a, 4), 'worth_violate': round(worth_v, 4),
                'delta': delta,
                'sufficient_n': (n_a + n_v) >= MIN_N_FOR_SIGNAL,
                'evidence': evidence,
            })
        return results
    finally:
        if own:
            conn.close()


def persist(results, conn=None):
    """Writes the instinct_worth rollup + evidence trail. Recomputed nightly by
    dream; never hand-edited (same convention as note_salience)."""
    own = conn is None
    conn = conn or sqlite3.connect(DB, timeout=30)
    try:
        now = _now()
        for r in results:
            conn.execute(
                'INSERT INTO instinct_worth(instinct_id,rule_text,n_adhere,n_violate,'
                'succ_adhere,fail_adhere,succ_violate,fail_violate,worth_adhere,'
                'worth_violate,delta,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(instinct_id) DO UPDATE SET rule_text=excluded.rule_text,'
                'n_adhere=excluded.n_adhere, n_violate=excluded.n_violate,'
                'succ_adhere=excluded.succ_adhere, fail_adhere=excluded.fail_adhere,'
                'succ_violate=excluded.succ_violate, fail_violate=excluded.fail_violate,'
                'worth_adhere=excluded.worth_adhere, worth_violate=excluded.worth_violate,'
                'delta=excluded.delta, computed_at=excluded.computed_at',
                (r['id'], r['rule'], r['n_adhere'], r['n_violate'], r['succ_adhere'],
                 r['fail_adhere'], r['succ_violate'], r['fail_violate'],
                 r['worth_adhere'], r['worth_violate'], r['delta'], now))
            conn.execute('DELETE FROM instinct_worth_evidence WHERE instinct_id=?', (r['id'],))
            for ev in r['evidence']:
                conn.execute(
                    'INSERT OR IGNORE INTO instinct_worth_evidence'
                    '(instinct_id,episode_id,class,method,score,outcome,computed_at) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (r['id'], ev['episode_id'], ev['class'], ev['method'],
                     ev['score'], ev['outcome'], now))
        conn.commit()
    finally:
        if own:
            conn.close()


# ============================ 4. PROPOSE (safe) ================================

def propose(out_dir=None):
    """PROPOSAL ONLY. Computes + persists the rollup (a derived table this module
    owns outright, same status as note_salience), and ANNOTATES
    dream/instincts_candidates.yaml with an `effectiveness:` section. Never writes
    /home/lumen/instincts.yaml -- confidence-update proposals are text for Frank to
    read, not a value this module or the timer ever applies."""
    conn = sal._connect(DB)
    try:
        results = compute(conn)
        persist(results, conn)
    finally:
        conn.close()

    d = Path(out_dir or DREAM_DIR)
    d.mkdir(parents=True, exist_ok=True)
    p = d / 'instincts_candidates.yaml'
    lines = []
    if p.exists():
        text = p.read_text()
        # strip a prior effectiveness: block (this run's replacement), keep the
        # candidates:/weak_singletons: sections lumen_dream.write_instincts() owns
        cut = text.find('\neffectiveness:')
        lines = (text if cut == -1 else text[:cut]).rstrip('\n').split('\n')
    if not lines:
        lines = ['candidates: []', 'weak_singletons: []']

    lines += ['', 'effectiveness:  # wq#578, PROPOSAL ONLY -- Frank reads this,',
              '                # nothing here ever writes instincts.yaml',
              f'  generated: "{_now()}"',
              f'  rule: "delta = Pr[success|adhered] - Pr[success|violated]; sufficient_n '
              f'requires n_adhere+n_violate >= {MIN_N_FOR_SIGNAL}"']
    for r in sorted(results, key=lambda x: (-abs(x['delta']) if x['sufficient_n'] else 0)):
        note = ('insufficient data (n=%d) -- no proposal' % (r['n_adhere'] + r['n_violate'])
                if not r['sufficient_n']
                else ('delta %+.2f: adherence outperforms violation, consider raising confidence'
                      % r['delta'] if r['delta'] > 0.1
                      else 'delta %+.2f: violation performs AS WELL AS OR BETTER than adherence '
                           '-- REVIEW this rule, it may not be doing work' % r['delta']
                      if r['delta'] <= 0
                      else 'delta %+.2f: weak signal, no proposal' % r['delta']))
        lines += [
            f'  - id: {r["id"]}',
            '    rule: "' + r['rule'][:160].replace('"', "'") + '"',
            f'    n_adhere: {r["n_adhere"]}   n_violate: {r["n_violate"]}',
            f'    worth_adhere: {r["worth_adhere"]}   worth_violate: {r["worth_violate"]}   '
            f'delta: {r["delta"]}',
            f'    note: "{note}"',
        ]
    p.write_text('\n'.join(lines) + '\n')
    return p, results


def stats(db=None):
    conn = sal._connect(db or DB)
    try:
        results = compute(conn)
        for r in sorted(results, key=lambda x: -abs(x['delta'])):
            flag = '' if r['sufficient_n'] else '  (insufficient n)'
            print(f'{r["id"]} n_adhere={r["n_adhere"]} n_violate={r["n_violate"]} '
                  f'worth_adhere={r["worth_adhere"]} worth_violate={r["worth_violate"]} '
                  f'delta={r["delta"]:+.4f}{flag}  {r["rule"][:70]}')
    finally:
        conn.close()


def _main():
    import argparse
    ap = argparse.ArgumentParser(description='wq#578 instinct effectiveness (proposal-only)')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('propose')
    sub.add_parser('stats')
    args = ap.parse_args()
    if args.cmd == 'propose':
        p, results = propose()
        print(f'wrote {p} ({len(results)} instincts scored)')
    elif args.cmd == 'stats':
        stats()
    else:
        ap.print_help()


if __name__ == '__main__':
    _main()

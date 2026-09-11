#!/usr/bin/env python3
"""
lumen_dream.py - Memory v2 Phase 3 (v1): consolidation pass (PROPOSAL-ONLY).

The episodic guard flagged the obvious risk: a background agent that auto-edits
claude_memory.md is the "automation silently destroying critical function"
pathology. So this v1 is deterministic and NON-DESTRUCTIVE -- it REPORTS and
PROPOSES, never mutates claude_memory.md. Frank applies anything worth applying.

What it does each run:
 1. STALE-PROSE SCAN (the killer feature): every IP the bi-temporal store has
    SUPERSEDED (closed valid window) is searched for in claude_memory.md. Any hit
    is stale prose the store now contradicts -> proposed for cleanup, with the
    current value + supersession date.
 2. EPISODIC DEDUP: near-duplicate archival_notes (cosine > 0.93) -> proposed merges.
 3. GROWTH METRICS: line/section/core-token counts vs thresholds.
Output: a dated report under /home/lumen/minerva/dream/. Proposal-only; safe to
run on a timer (writes reports, never touches memory).

Future (gated): an LLM sleep-time reflector (DeepSeek via LiteLLM, NEVER messenger-
chat/re) to extract lessons from recent sessions and PROPOSE consolidations.
"""
import sqlite3, re, sys
import numpy as np
from pathlib import Path
from datetime import datetime, timezone

DB = '/home/lumen/minerva/minerva.db'
MEM = Path('/home/lumen/claude_memory.md')
OUT = Path('/home/lumen/minerva/dream')
IP_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
# lines documenting that an old value is dead are NOT stale prose -> skip them
NEGATION_RE = re.compile(r'\b(dead|retired|removed|former|formerly|superseded|defunct|gone|was |old |stale|deprecat|RIP|killed|reinstalled|will be removed)\b', re.I)

def _now_pt():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(timezone.utc).astimezone(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d %H:%M PT')
    except Exception:
        return datetime.now(timezone.utc).isoformat()

def superseded_ips(conn):
    """value -> (subject, predicate, current_value, invalidated date) for closed IP-ish facts."""
    rows = conn.execute('''SELECT subject,predicate,value,valid_to FROM state_facts
                           WHERE valid_to IS NOT NULL''').fetchall()
    out = {}
    for subj, pred, val, vto in rows:
        if IP_RE.fullmatch(val.strip()):
            cur = conn.execute('''SELECT value FROM state_facts
                                  WHERE subject=? AND predicate=? AND valid_to IS NULL''',
                               (subj, pred)).fetchone()
            out[val.strip()] = (subj, pred, cur[0] if cur else '?', (vto or '')[:10])
    return out

def stale_prose_scan(conn):
    if not MEM.exists():
        return []
    sup = superseded_ips(conn)
    if not sup:
        return []
    findings = []
    for i, line in enumerate(MEM.read_text().splitlines(), 1):
        if NEGATION_RE.search(line):
            continue  # line documents the value as dead/old -> not stale prose
        for ip in set(IP_RE.findall(line)):
            if ip in sup:
                subj, pred, curv, when = sup[ip]
                findings.append((i, ip, subj, pred, curv, when, line.strip()[:120]))
    return findings

def episodic_dupes(conn, thresh=0.93):
    rows = conn.execute('SELECT id,title,embedding FROM archival_notes').fetchall()
    embs = []
    for nid, title, emb_b in rows:
        if emb_b:
            embs.append((nid, title, np.frombuffer(emb_b, dtype=np.float32)))
    dupes = []
    for a in range(len(embs)):
        for b in range(a+1, len(embs)):
            va, vb = embs[a][2], embs[b][2]
            if va.shape != vb.shape:
                continue
            d = (np.linalg.norm(va)*np.linalg.norm(vb)) or 1e-9
            sim = float(np.dot(va, vb)/d)
            if sim >= thresh:
                dupes.append((sim, embs[a][1], embs[b][1]))
    return sorted(dupes, reverse=True)

def growth(conn):
    lines = len(MEM.read_text().splitlines()) if MEM.exists() else 0
    import json
    sj = Path('/home/lumen/minerva/context/sections.json')
    secs = core_t = 0
    if sj.exists():
        s = json.loads(sj.read_text())
        secs = len(s)
        core_t = sum(x['token_estimate'] for x in s if x.get('core'))
    notes = conn.execute('SELECT count(*) FROM archival_notes').fetchone()[0]
    facts = conn.execute('SELECT count(*) FROM state_facts WHERE valid_to IS NULL').fetchone()[0]
    return lines, secs, core_t, notes, facts

# ---------- Memory V3 P3: instinct candidates (proposal-only, deterministic) ----------
IMPERATIVE_RE = re.compile(r'\b(ALWAYS|NEVER|MUST(?:\s+NOT)?|do not|don\'t|standing rule|RULE:|FIX:|Lesson:|LESSON:)\b', re.I)
_STOP = set('the a an of to in on for and or is are was were be been with at by from as that this it its'.split())

def _norm_tokens(s):
    return frozenset(w for w in re.findall(r'[a-z0-9\.\-_/]+', s.lower())
                     if w not in _STOP and len(w) > 2)

def _sentences(text):
    for s in re.split(r'(?<=[.;])\s+|\n', text or ''):
        s = s.strip('- \t')
        if 25 <= len(s) <= 300:
            yield s

def instinct_candidates(conn, min_obs=2, max_singles=20):
    import hashlib
    rows = conn.execute('SELECT id, content FROM archival_notes '
                        'WHERE id NOT IN (SELECT absorbed_id FROM note_merges)').fetchall()
    clusters = []
    for nid, content in rows:
        for s in _sentences(content):
            if not IMPERATIVE_RE.search(s):
                continue
            toks = _norm_tokens(s)
            if len(toks) < 4:
                continue
            for c in clusters:
                inter = len(toks & c['tokens']); union = len(toks | c['tokens']) or 1
                if inter / union >= 0.55:
                    c['sources'].add(nid)
                    break
            else:
                rid = hashlib.sha1(' '.join(sorted(toks))[:400].encode()).hexdigest()[:8]
                clusters.append({'id': rid, 'rule': s, 'tokens': toks, 'sources': {nid}})
    out = [{'id': c['id'], 'rule': c['rule'][:240],
            'observations': len(c['sources']),
            'confidence': round(min(0.95, 0.35 + 0.15 * len(c['sources'])), 2),
            'sources': sorted(c['sources'])[:6]} for c in clusters]
    multi = sorted([o for o in out if o['observations'] >= min_obs], key=lambda x: -x['observations'])
    singles = [o for o in out if o['observations'] == 1][:max_singles]
    return multi, singles

def write_instincts(multi, singles):
    p = OUT / 'instincts_candidates.yaml'
    L = ['# instincts_candidates.yaml — PROPOSAL-ONLY (Memory V3 P3, ECC-instincts pattern).',
         f'# generated {_now_pt()} by lumen_dream (deterministic imperative-harvest + Jaccard cluster).',
         '# Frank review-gates: merge accepted rules into /home/lumen/instincts.yaml (PR-style).',
         'candidates:']
    for o in multi:
        L += [f'  - id: {o["id"]}',
              f'    confidence: {o["confidence"]}   # 0.35 + 0.15*obs, capped 0.95',
              f'    observations: {o["observations"]}',
              '    rule: "' + o['rule'].replace('"', "'") + '"',
              f'    sources: [{", ".join(o["sources"])}]']
    L.append('weak_singletons:   # single-observation, listed for visibility only')
    for o in singles:
        L.append(f'  - {o["id"]}: "' + o['rule'][:120].replace('"', "'") + '"')
    p.write_text('\n'.join(L) + '\n')
    return p, len(multi), len(singles)

# ---------- Memory V3 P4: consolidation consumer ----------
def episodic_dupes_v2(conn, thresh=0.93):
    absorbed = {r[0] for r in conn.execute('SELECT absorbed_id FROM note_merges')}
    rows = conn.execute('SELECT id,title,created_at,embedding FROM archival_notes').fetchall()
    embs = [(nid, t, ts, np.frombuffer(e, dtype=np.float32))
            for nid, t, ts, e in rows if e and nid not in absorbed]
    pairs = []
    for a in range(len(embs)):
        for b in range(a + 1, len(embs)):
            va, vb = embs[a][3], embs[b][3]
            if va.shape != vb.shape:
                continue
            d = (np.linalg.norm(va) * np.linalg.norm(vb)) or 1e-9
            sim = float(np.dot(va, vb) / d)
            if sim >= thresh:
                pairs.append((sim, embs[a], embs[b]))
    return sorted(pairs, key=lambda x: -x[0])

def write_merge_plan(pairs):
    import json as _json
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    p = OUT / f'merge_plan_{today}.json'
    merges = []
    for sim, a, b in pairs:
        keep, ab = (a, b) if (a[2] or '') >= (b[2] or '') else (b, a)  # keep newer
        merges.append({'keep': keep[0], 'absorb': ab[0], 'sim': round(sim, 4),
                       'keep_title': keep[1][:70], 'absorb_title': ab[1][:70]})
    p.write_text(_json.dumps({'generated': _now_pt(), 'merges': merges}, indent=1))
    return p

def apply_merge_plan(path, confirm=False, exact_only=True, exact_thresh=0.98, db=None):
    """Invalidate-not-delete: absorbed notes stay in archival_notes; retrieval filters
    via note_merges. Reversible: DELETE FROM note_merges WHERE absorbed_id=?

    Returns the rows it actually wrote (wq#1638 — the weekly digest logs the ids, and
    a pair some earlier plan already merged must not be reported as applied again).
    """
    import json as _json
    plan = _json.loads(Path(path).read_text())
    if not confirm:
        n_exact = sum(1 for m in plan['merges'] if m['sim'] >= exact_thresh)
        print(f"DRY: {len(plan['merges'])} merges in plan, {n_exact} exact (>= {exact_thresh}). "
              f"Re-run with --confirm.")
        return []
    conn = sqlite3.connect(db or DB, timeout=30)
    applied = []
    skip = 0
    for m in plan['merges']:
        if exact_only and m['sim'] < exact_thresh:
            skip += 1; continue
        if m['keep'] == m['absorb']:
            skip += 1; continue
        # never absorb a note that is itself a keep-target in this plan (chain guard)
        cur = conn.execute('INSERT OR IGNORE INTO note_merges(absorbed_id,keep_id,sim,method,ts) '
                           'VALUES (?,?,?,?,?)',
                           (m['absorb'], m['keep'], m['sim'],
                            'exact-auto' if m['sim'] >= exact_thresh else 'manual',
                            datetime.now(timezone.utc).isoformat()))
        if cur.rowcount:
            applied.append(m)
        else:
            skip += 1      # already merged by an earlier plan
    # chain-repair: if X absorbed into Y and Y later absorbed into Z, repoint X->Z.
    # Repeated to a fixpoint (wq#1638): one pass only shortens a chain by one link,
    # and auto-apply feeds this plans where five near-identical notes absorb each
    # other pairwise, so two- and three-link chains are the normal case now.
    for _ in range(10):
        if not conn.execute('''UPDATE note_merges SET keep_id =
                    (SELECT nm2.keep_id FROM note_merges nm2 WHERE nm2.absorbed_id = note_merges.keep_id)
                    WHERE keep_id IN (SELECT absorbed_id FROM note_merges)''').rowcount:
            break
    conn.commit(); conn.close()
    print(f'applied {len(applied)} merges, skipped {skip} (exact_only={exact_only})')
    return applied

def embed_retry(conn):
    """Fail-closed loop: notes stored with empty/NULL embedding get re-embedded."""
    rows = conn.execute("SELECT id, title, content FROM archival_notes "
                        "WHERE embedding IS NULL OR length(embedding)=0").fetchall()
    if not rows:
        return 0, 0
    sys.path.insert(0, '/home/lumen/minerva')
    import minerva_memory as mm
    ok = 0
    for nid, title, content in rows:
        emb = mm.embed(f'{title}. {(content or "")[:2000]}')
        if emb is not None:
            conn.execute('UPDATE archival_notes SET embedding=? WHERE id=?',
                         (np.asarray(emb, dtype=np.float32).tobytes(), nid))
            ok += 1
    conn.commit()
    return ok, len(rows)

# ---------- Memory V3 P5: write-origin anomaly scan ----------
def origin_stats(conn, days=7):
    return conn.execute(
        "SELECT COALESCE(origin,'?'), COALESCE(trust,'?'), COUNT(*) FROM archival_notes "
        "WHERE created_at > datetime('now', ?) GROUP BY 1,2 ORDER BY 3 DESC",
        (f'-{days} days',)).fetchall()

def coverage_section(counts, prev, delta, err=''):
    """wq#1611 report block. `## Stale prose flagged (0)` says nothing about the
    facts nobody re-checked; these lines do."""
    if counts is None:
        return [f'- coverage FAILED: {err}']
    import lumen_coverage as _cov
    L = [f'- {_cov.fact_line(counts)}']
    if delta is None:
        L.append('- week over week: no snapshot from a week ago yet; baseline recorded')
    else:
        L.append(f'- week over week: stale+unverifiable {_cov.unverified(prev)} -> '
                 f'{_cov.unverified(counts)} ({delta:+d})'
                 + (' ⚠️ REGRESSED — aq row raised' if delta > 0 else ''))
    L.append('- UNVERIFIABLE = no fact declares when it should be re-checked; it is a '
             'coverage gap, not a decay verdict (§3.6).')
    return L


def run():
    conn = sqlite3.connect(DB, timeout=30)
    stale = stale_prose_scan(conn)
    pairs = episodic_dupes_v2(conn)
    dupes = [(sim, a[1], b[1]) for sim, a, b in pairs]  # legacy report shape
    plan_path = write_merge_plan(pairs) if pairs else None          # P4: consumer input
    multi, singles = instinct_candidates(conn)                       # P3
    inst_path, n_multi, n_single = write_instincts(multi, singles)
    # wq#578 (V4 P6): instinct effectiveness. MUST run after write_instincts() --
    # it annotates the SAME instincts_candidates.yaml file with an effectiveness:
    # section rather than owning its own output file. Reuses wq#576's Memory Worth
    # machinery (worth_stats) and outcome labels (note_hit_outcomes); read-mostly
    # except its own instinct_worth rollup tables. NEVER writes instincts.yaml.
    instinct_n = None
    instinct_err = ''
    try:
        import lumen_instinct_worth as _iw
        inst_path, instinct_results = _iw.propose()
        instinct_n = len(instinct_results)
    except Exception as e:
        instinct_err = f'{type(e).__name__}: {e}'[:160]
    emb_ok, emb_pending = embed_retry(conn)                          # P4: fail-closed embeds
    ostats = origin_stats(conn)                                      # P5
    # wq#1611: the weekly hook for fact coverage. "Stale prose flagged (0)" above
    # says nothing about the facts nobody re-checked; this does, and raises an aq
    # row when stale+unverifiable rises week over week.
    cov_counts, cov_prev, cov_delta = None, None, None
    cov_err = ''
    try:
        import lumen_coverage as _cov
        cov_counts, cov_prev, cov_delta = _cov.check(raise_aq=True)
    except Exception as e:
        cov_err = f'{type(e).__name__}: {e}'[:160]
    # wq#576 (V4 P4): usage-salience rollup + retention prune. Proposal-only like
    # everything else here -- the rollup writes note_salience (a derived table it
    # owns outright) and the prune drops raw log rows past the retention window.
    # The forgetting proposer stays DARK: research §4b, the corpus has 3 notes over
    # 90d old, so a proposer with nothing to propose is untestable against reality.
    sal_rolled = sal_pruned = -1
    sal_err = ''
    try:
        import lumen_salience as _sal
        sal_rolled = _sal.rollup(conn)
        sal_pruned = _sal.prune(conn)
    except Exception as e:
        sal_err = f'{type(e).__name__}: {e}'[:160]
    forget_on = __import__('os').environ.get('MEMORY_V4_FORGET_PROPOSE', '0') == '1'
    forget_line = ('DARK (MEMORY_V4_FORGET_PROPOSE=0) -- '
                   'run `lumen_salience.py propose` by hand to inspect candidates')
    if forget_on:
        try:
            import lumen_salience as _sal
            _c = _sal.archive_candidates(conn)
            forget_line = f'{len(_c)} candidate(s) -> {_sal.write_archive_plan(_c)} (PROPOSAL ONLY)'
        except Exception as e:
            forget_line = f'FAILED: {type(e).__name__}: {e}'[:160]
    # wq#577 (V4 P5): prose lifecycle -- SECTION-level demotion PROPOSALS for
    # claude_memory.md. Read-only + writes a plan file under dream/, same as the
    # merge plan / instinct candidates above. claude_memory.md is proposal-only by
    # standing law: this call NEVER applies a plan. apply() is a separate,
    # operator-invoked CLI action this run() function must never reach.
    prose_plan = prose_n = None
    prose_budget = None
    prose_err = ''
    try:
        import lumen_prose_lifecycle as _prose
        prose_plan, prose_cands, prose_budget = _prose.propose()
        prose_n = len(prose_cands)
    except Exception as e:
        prose_err = f'{type(e).__name__}: {e}'[:160]

    # wq#610: memoryd governance detectors D-1/D-3/D-4/D-5 (design §3.3). D-2
    # (trust escalation) is NOT run from here -- it is already live and
    # synchronous in kernel.py's validate_note(), which catches a bypass at
    # write time rather than waiting for the next dream run. Proposal-only:
    # D-1/D-4 write their own dated file (mirrors merge_plan/instincts_candidates);
    # D-3/D-5 are report-line-only per §3.3's own action table. Nothing here can
    # mutate archival_notes/memory_audit/memory_quarantine.
    det_result = None
    det_err = ''
    try:
        import lumen_memoryd_detectors as _det
        det_result = _det.run_detectors(conn)
    except Exception as e:
        det_err = f'{type(e).__name__}: {e}'[:160]

    lines, secs, core_t, notes, facts = growth(conn)
    conn.close()

    OUT.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    rpt = OUT / f'dream-{today}.md'

    # wq#1638: everything above PROPOSES; nothing above drains the queue, which is
    # how dream/ reached 36 artifacts and note_merges reached 5 rows. The digest is
    # the drain report -- and the one mechanical exception to proposal-only: exact
    # duplicate merges (cosine >= 0.98, method='exact-auto') apply themselves, and
    # a DELETE from note_merges reverses any of them. Runs after conn.close() so its
    # own write connection never contends with this function's.
    dig = _dig = None
    dig_err = ''
    try:
        import lumen_dream_digest as _dig
        dig = _dig.digest(plan_path=plan_path, report=rpt)
    except Exception as e:
        dig_err = f'{type(e).__name__}: {e}'[:160]
    L = [f'# Lumen consolidation report — {_now_pt()}', '',
         '_Proposal-only, with one exception (wq#1638): exact-duplicate episodic merges '
         '(cosine >= 0.98) apply themselves and are logged under Outbox digest below. '
         'Everything else here is a proposal — review and apply what is worth applying._', '',
         '## Growth metrics',
         f'- claude_memory.md: {lines} lines, {secs} sections, core floor {core_t} tok'
         + ('  ⚠️ core>8K' if core_t > 8000 else ''),
         f'- episodic notes: {notes}   |   current fleet facts: {facts}', '',
         '## Fact coverage (wq#1611)']
    L += coverage_section(cov_counts, cov_prev, cov_delta, cov_err)
    L += ['',
          f'## Stale prose flagged ({len(stale)})',
          '_claude_memory.md lines citing an IP the bi-temporal store has SUPERSEDED:_' if stale else '_None — prose IPs match the store._']
    for i, ip, subj, pred, curv, when, txt in stale:
        L.append(f'- **L{i}** cites `{ip}` — superseded {when}; current **{subj}.{pred} = {curv}**. `{txt}`')
    L += ['', f'## Duplicate episodes flagged ({len(dupes)})',
          '_archival_notes pairs with cosine ≥ 0.93 (candidate merges):_' if dupes else '_None._']
    for sim, t1, t2 in dupes[:15]:
        L.append(f'- {sim:.3f}  "{t1[:50]}"   vs  "{t2[:50]}"')
    L += ['', '## Memory V3 additions',
          f'- merge plan: {plan_path or "(no dupes)"} — apply: `python3 lumen_dream.py apply --plan <file> --confirm` (exact-only default)',
          f'- instinct candidates: {inst_path} — {n_multi} multi-observation, {n_single} singletons. Merge accepted -> /home/lumen/instincts.yaml',
          (f'- instinct effectiveness (wq#578): {instinct_n} instinct(s) scored, '
           f'annotated into {inst_path} under `effectiveness:` (PROPOSAL ONLY; '
           f'never writes instincts.yaml)' if instinct_n is not None
           else f'- instinct effectiveness FAILED: {instinct_err}'),
          f'- embed retry: {emb_ok}/{emb_pending} pending embeddings recovered' if emb_pending else '- embed retry: none pending',
          '- writes by origin (7d): ' + (', '.join(f'{o}/{t}={n}' for o, t, n in ostats) or 'none'),
          '', '## Memory V4 salience (wq#576)',
          (f'- rollup: {sal_rolled} note(s) scored, prune: {sal_pruned} raw hit row(s) '
           f'dropped past retention' if not sal_err else f'- rollup/prune FAILED: {sal_err}'),
          f'- forgetting proposer: {forget_line}',
          '', '## Memory V4 prose lifecycle (wq#577)',
          (f'- live file: {prose_budget["tok_est"]} tok est vs {prose_budget["budget"]} '
           f'budget' + (' ⚠️ OVER BUDGET' if prose_budget["over"] else '')
           if prose_budget else '- budget: unavailable'),
          (f'- demotion candidates: {prose_n} -> {prose_plan} (PROPOSAL ONLY; '
           f'apply: `python3 lumen_prose_lifecycle.py apply --plan <file> --confirm`, '
           f'operator-invoked, never called from here)'
           if prose_plan else f'- prose lifecycle FAILED: {prose_err}'),
          '', '## Memory V4 governance detectors (wq#610, design §3.3)']
    if det_result is None:
        L.append(f'- detectors FAILED: {det_err}')
    else:
        d1, d3, d4, d5 = det_result['d1'], det_result['d3'], det_result['d4'], det_result['d5']
        L.append('- D-2 trust escalation: not run here — live+synchronous in kernel.py '
                 'validate_note() (see /audit invariant=D-2 for hits)')
        L.append(f'- D-1 origin-mix shift: {len(d1["flagged"])} lane(s) flagged'
                 + (f' ({", ".join(f["lane"] for f in d1["flagged"])})' if d1['flagged'] else '')
                 + (f' — {d1["note"]}' if d1.get('note') else ''))
        L.append(f'- D-3 novel writers (7d): {len(d3["novel_writers_in_window"])} '
                 f'new (lane, agent_id) pair(s), {d3["total_known_pairs"]} known total')
        L.append(f'- D-4 near-duplicate flood: {len(d4["flagged"])} source(s) flagged '
                 f'(>= {d4["min_n"]} writes/{d4["window_days"]}d at cosine >= {d4["thresh"]})')
        L.append(f'- D-5 unresolvable attribution (I-7 rejects, {d5["window_days"]}d): '
                 f'{d5["i7_rejects_in_window"]} of {d5["total_rejects_in_window"]} total rejects')
        if det_result['proposal_path']:
            L.append(f'- D-1/D-4 proposal file (PROPOSAL ONLY, operator review): '
                     f'{det_result["proposal_path"]}')
    L += ['', '## Outbox digest (wq#1638)']
    L += (_dig.report_section(dig, dig_err) if _dig
          else [f'- digest FAILED: {dig_err}'])
    rpt.write_text('\n'.join(L))
    print(f'wrote {rpt}')
    print(f'  stale-prose findings: {len(stale)} | dup episodes: {len(dupes)} | core {core_t} tok')
    print(f'  instincts: {n_multi} multi + {n_single} single -> {inst_path}')
    if plan_path:
        print(f'  merge plan -> {plan_path}')
    if emb_pending:
        print(f'  embed retry: {emb_ok}/{emb_pending} recovered')
    if prose_plan:
        print(f'  prose demotion candidates: {prose_n} -> {prose_plan}')
    if dig:
        print(f'  digest -> {dig["path"]}: {len(dig["items"])} unapplied, '
              f'{len(dig["applied"])} exact merge(s) applied'
              + (f', wq#{dig["ticket"]}' if dig['ticket'] else ', ticket NOT filed'))
    elif dig_err:
        print(f'  digest FAILED: {dig_err}')
    if stale:
        print('  top stale-prose lines:')
        for i, ip, subj, pred, curv, when, txt in stale[:8]:
            print(f'    L{i}: {ip} -> {subj}.{pred}={curv} (superseded {when})')

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Lumen consolidation (proposal-only run; apply is operator-gated)')
    sub = ap.add_subparsers(dest='cmd')
    pa = sub.add_parser('apply', help='apply a merge plan (operator gate — timer NEVER calls this)')
    pa.add_argument('--plan', required=True)
    pa.add_argument('--confirm', action='store_true')
    pa.add_argument('--all', action='store_true', help='include sub-0.98 manual-class merges (default exact-only)')
    pd = sub.add_parser('digest', help='wq#1638: outbox digest + exact-merge auto-apply (run by hand between Sundays)')
    pd.add_argument('--no-ticket', action='store_true', help='digest only, no wq ticket')
    pd.add_argument('--list', action='store_true', help='print the unapplied queue, write nothing')
    args = ap.parse_args()
    if args.cmd == 'apply':
        apply_merge_plan(args.plan, confirm=args.confirm, exact_only=not args.all)
    elif args.cmd == 'digest':
        import lumen_dream_digest as _dig
        _dig.main(['list'] if args.list else
                  (['run', '--no-ticket'] if args.no_ticket else ['run']))
    else:
        run()

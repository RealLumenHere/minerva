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

def run():
    conn = sqlite3.connect(DB, timeout=30)
    stale = stale_prose_scan(conn)
    dupes = episodic_dupes(conn)
    lines, secs, core_t, notes, facts = growth(conn)
    conn.close()

    OUT.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    rpt = OUT / f'dream-{today}.md'
    L = [f'# Lumen consolidation report — {_now_pt()}', '',
         '_Proposal-only. Nothing here is auto-applied. Review and apply what is worth applying._', '',
         '## Growth metrics',
         f'- claude_memory.md: {lines} lines, {secs} sections, core floor {core_t} tok'
         + ('  ⚠️ core>8K' if core_t > 8000 else ''),
         f'- episodic notes: {notes}   |   current fleet facts: {facts}', '',
         f'## Stale prose flagged ({len(stale)})',
         '_claude_memory.md lines citing an IP the bi-temporal store has SUPERSEDED:_' if stale else '_None — prose IPs match the store._']
    for i, ip, subj, pred, curv, when, txt in stale:
        L.append(f'- **L{i}** cites `{ip}` — superseded {when}; current **{subj}.{pred} = {curv}**. `{txt}`')
    L += ['', f'## Duplicate episodes flagged ({len(dupes)})',
          '_archival_notes pairs with cosine ≥ 0.93 (candidate merges):_' if dupes else '_None._']
    for sim, t1, t2 in dupes[:15]:
        L.append(f'- {sim:.3f}  "{t1[:50]}"   vs  "{t2[:50]}"')
    rpt.write_text('\n'.join(L))
    print(f'wrote {rpt}')
    print(f'  stale-prose findings: {len(stale)} | dup episodes: {len(dupes)} | core {core_t} tok')
    if stale:
        print('  top stale-prose lines:')
        for i, ip, subj, pred, curv, when, txt in stale[:8]:
            print(f'    L{i}: {ip} -> {subj}.{pred}={curv} (superseded {when})')

if __name__ == '__main__':
    run()

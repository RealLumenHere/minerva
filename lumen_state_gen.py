#!/usr/bin/env python3
"""
lumen_state_gen.py - Render the current bi-temporal slice as a markdown block.

This is the L2 deliverable: a generated "LIVE FLEET STATE" block that replaces the
hand-curated CURRENT_STATE_OVERRIDES. Because it queries current() (valid_to IS NULL),
it is *never* stale -- a fact disappears/updates the instant a superseding fact lands.

Usage:
  lumen_state_gen.py                    # print the markdown block
  lumen_state_gen.py --json             # machine-readable current slice
  lumen_state_gen.py --proposal-digest  # write /home/lumen/minerva/lumen_state.txt
                                        # (hallucination-guard ground truth; wq#127)
"""
import os, sys, json
from collections import Counter
from datetime import datetime, timezone
from lumen_state import (current, CONTRADICTED, DUE, FRESH, STALE, UNVERIFIABLE)

# preferred predicate order within each subject
PRED_ORDER = ['hardware', 'role', 'status', 'lan_ip', 'tailscale_ip',
              'tailscale_hostname', 'location']

# wq#1612: evidence marks. Ordinal only -- a mark says "re-verify before relying
# on this", never "this is N% likely true". UNVERIFIABLE carries no mark: it is a
# coverage gap in the verifier registry, not a claim about the value.
STALENESS_MARK = {DUE: '?', STALE: '‼', CONTRADICTED: '‼'}

# Core-budget eviction (§3.6 / fix #11). context_selector.py:32 warns above this
# and deliberately never truncates ("prune at gen time") -- gen time is here.
# Evidence age is the eviction key, replacing the write-time length heuristic.
STATE_SOFT_CAP_TOK = int(os.environ.get('LUMEN_STATE_SOFT_CAP_TOK', '4000'))
# Worst evidence goes first: STALE and UNVERIFIABLE, then DUE only if the block
# still overflows. FRESH is never held out (§3.6), and neither is CONTRADICTED --
# a fact known to be wrong is the one a session most needs to see marked.
EVICT_ORDER = [STALE, UNVERIFIABLE, DUE]

def _tok(text):
    """The estimator context_selector.py:178 applies to this block."""
    return len(text) // 4

def _key(pred):
    return (PRED_ORDER.index(pred) if pred in PRED_ORDER else len(PRED_ORDER), pred)

def _render_fact(f):
    """Render one predicate=value pair, annotating decayed volatile facts so a
    stale value no longer reads as authoritative. Stable facts (half_life NULL =>
    eff_confidence ~= 1.0) render exactly as before, with no marker."""
    eff = f.get('eff_confidence', 1.0)
    base = f"**{f['predicate']}**={f['value']}{STALENESS_MARK.get(f.get('staleness'), '')}"
    if eff < 0.6:
        return f"{base}  (conf {eff:.2f} — reconfirm)"
    return base

def _evictable(rows):
    """Core-block facts the budget may drop, worst evidence first: by staleness
    class, then oldest evidence within a class."""
    ranked = []
    for cls in EVICT_ORDER:
        ranked += sorted((r for r in rows if r.get('staleness') == cls),
                         key=lambda r: -r.get('evidence_age_days', 0.0))
    return ranked

def _coverage(rows):
    c = Counter(r.get('staleness') for r in rows)
    return (f'_evidence coverage: {len(rows)} facts · fresh {c[FRESH]} · due {c[DUE]} · '
            f'stale {c[STALE]} · unverifiable {c[UNVERIFIABLE]} · '
            f'contradicted {c[CONTRADICTED]}._')

def _block(shown, rows, tier, evicted=0):
    """Render the markdown for one already-chosen set of facts."""
    by_subj = {}
    for r in shown:
        by_subj.setdefault(r['subject'], []).append(r)
    topic_subjects = sorted({r['subject'] for r in rows
                             if (r.get('render_tier') or 'core') == 'topic'})
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    out = []
    if tier == 'full':
        out.append('## LIVE FLEET STATE — FULL (all tiers, auto-generated; NOT context-injected)')
    else:
        out.append('## LIVE FLEET STATE (bi-temporal, auto-generated) #CORE')
    out.append('')
    out.append(f'_Generated {now} from minerva.db state_facts (current slice = valid_to IS NULL)._  ')
    out.append('_Authoritative for current fleet facts. Never hand-edit; ingest via lumen_state.py and regenerate._')
    out.append('_Evidence marks: `?` = due for re-verification · `‼` = stale or contradicted, '
               're-verify before relying on it. Ordinal only, not a probability. '
               '`lumen_state.py current <subject>` prints the evidence age per fact._')
    if tier != 'full':
        out.append('_Core tier only (short identity/status facts). Full detail per subject: '
                   '`python3 /home/lumen/minerva/lumen_state.py current <subject>` or '
                   '/home/lumen/minerva/context/LIVE_FLEET_STATE_FULL.md._')
    out.append('')
    for subj in sorted(by_subj):
        facts = sorted(by_subj[subj], key=lambda r: _key(r['predicate']))
        pairs = ', '.join(_render_fact(f) for f in facts)
        out.append(f'- **{subj}** — {pairs}')
    out.append('')
    if tier != 'full' and topic_subjects:
        out.append(f'_{len(topic_subjects)} subjects carry topic-tier detail beyond this block '
                   f'(coreswitch, minerva, hapi, 3dprinter, ...). ALWAYS `lumen_state.py current '
                   f'<subject>` before working on a subject._')
        out.append('')
    if evicted:
        out.append(f'_{evicted} fact(s) held out of this block for the {STATE_SOFT_CAP_TOK} tok '
                   f'core budget, stalest evidence first (FRESH and CONTRADICTED facts are never '
                   f'held out). They are unchanged in the store and rendered in '
                   f'/home/lumen/minerva/context/LIVE_FLEET_STATE_FULL.md._')
        out.append('')
    out.append(f'_{len(shown)} facts shown ({len(rows)} current total) across {len(by_subj)} subjects._')
    out.append(_coverage(rows))   # over the whole current slice, not just what fits
    return '\n'.join(out)

def render(tier='core'):
    """Memory V3 P1 tiered render.
    tier='core' -> injected LIVE_FLEET_STATE block: render_tier=core facts only,
                   plus a footer listing subjects that have topic-tier detail.
                   Held to STATE_SOFT_CAP_TOK by evicting the stalest facts.
    tier='full' -> everything (LIVE_FLEET_STATE_FULL.md, on-demand/grep, NOT injected).
    min_confidence=0.25 drops HARD-decayed volatile facts from the rendered .md
    ONLY -- never deleted from the DB."""
    rows = current(min_confidence=0.25)
    core_rows = [r for r in rows if (r.get('render_tier') or 'core') == 'core']
    shown = rows if tier == 'full' else core_rows
    text = _block(shown, rows, tier)
    if tier == 'full':
        return text
    # Evict a prefix of the worst-evidence ranking until the block fits. FRESH
    # facts are not in the pool, so a full pool that still overflows leaves the
    # block over budget -- context_selector then warns, exactly as it does today.
    pool = _evictable(shown)
    for n in range(1, len(pool) + 1):
        if _tok(text) <= STATE_SOFT_CAP_TOK:
            break
        held = set(map(id, pool[:n]))
        text = _block([r for r in shown if id(r) not in held], rows, tier, n)
    return text

if __name__ == '__main__':
    if '--json' in sys.argv:
        print(json.dumps(current(), indent=2))
    elif '--proposal-digest' in sys.argv:
        from lumen_proposal_digest import write_digest
        print(write_digest())
    elif '--full' in sys.argv:
        print(render(tier='full'))
    else:
        print(render())

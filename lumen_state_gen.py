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
import sys, json
from datetime import datetime, timezone
from lumen_state import current

# preferred predicate order within each subject
PRED_ORDER = ['hardware', 'role', 'status', 'lan_ip', 'tailscale_ip',
              'tailscale_hostname', 'location']

def _key(pred):
    return (PRED_ORDER.index(pred) if pred in PRED_ORDER else len(PRED_ORDER), pred)

def _render_fact(f):
    """Render one predicate=value pair, annotating decayed volatile facts so a
    stale value no longer reads as authoritative. Stable facts (half_life NULL =>
    eff_confidence ~= 1.0) render exactly as before, with no marker."""
    eff = f.get('eff_confidence', 1.0)
    base = f"**{f['predicate']}**={f['value']}"
    if eff < 0.6:
        return f"{base}  (conf {eff:.2f} — reconfirm)"
    return base

def render(tier='core'):
    """Memory V3 P1 tiered render.
    tier='core' -> injected LIVE_FLEET_STATE block: render_tier=core facts only,
                   plus a footer listing subjects that have topic-tier detail.
    tier='full' -> everything (LIVE_FLEET_STATE_FULL.md, on-demand/grep, NOT injected).
    min_confidence=0.25 drops HARD-decayed volatile facts from the rendered .md
    ONLY -- never deleted from the DB."""
    rows = current(min_confidence=0.25)
    core_rows = [r for r in rows if (r.get('render_tier') or 'core') == 'core']
    shown = rows if tier == 'full' else core_rows
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
    out.append(f'_{len(shown)} facts shown ({len(rows)} current total) across {len(by_subj)} subjects._')
    return '\n'.join(out)

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

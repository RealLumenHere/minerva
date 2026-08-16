#!/usr/bin/env python3
"""minerva_verify_eval_v2 — fitness-for-purpose eval of the proposal guard.

v1 (minerva_verify_eval.py) measures the MECHANISM on a synthetic 40-item set
with a 1.2k-char toy context. It cannot answer "does the guard serve our
needs", because production runs a ~10k digest against subsystem claims, a
latent rescue layer makes most accept decisions (198 rescues vs 27 primary
passes in the 14d to 2026-07-27), and the thing we care about is truth of the
REAL SYSTEM, not entailment against the digest.

v2 therefore scores each decision layer against hand-audited ground truth:
eval_sets/verify_gold_v2.jsonl — production verifications whose claims were
checked directly against live fleet state (unit list, litellm config, DB
tables, lspci). Layers scored:
  judge_only : primary NLI gate (judge>=0.7) with no rescue
  production : the recorded pass/fail (judge + latent rescue, as shipped)
  citation   : citation_score>=0.7 alone (what rescue implicitly leans on)

Positive class = claim_true (should accept). So:
  recall    = fraction of TRUE claims accepted (starvation shows up here)
  precision = fraction of accepts that were TRUE (hallucination leakage here)

--drift prints weekly pass/rescue/judge-zero rates from verification_log for
trend monitoring; wire it to a timer if drift matters week to week.
Caveats: n=30, single labeler (Claude), strata equal-weighted not prevalence-
weighted, and 2 false claims is too few to bound rescue precision tightly —
treat precision numbers as indicative, recall numbers as solid.
"""
import json, sqlite3, sys
from pathlib import Path

GOLD = Path('/home/lumen/minerva/eval_sets/verify_gold_v2.jsonl')
DB = '/home/lumen/minerva/minerva.db'

def metrics(pairs):
    tp = sum(1 for t, a in pairs if t and a)
    fn = sum(1 for t, a in pairs if t and not a)
    fp = sum(1 for t, a in pairs if not t and a)
    tn = sum(1 for t, a in pairs if not t and not a)
    prec = tp / (tp + fp) if tp + fp else float('nan')
    rec = tp / (tp + fn) if tp + fn else float('nan')
    return tp, fn, fp, tn, prec, rec

def run():
    items = [json.loads(l) for l in GOLD.read_text().splitlines() if l.strip()]
    layers = {
        'judge_only': lambda o: o['judge_score'] >= 0.7,
        'production': lambda o: bool(o['passed']),
        'citation  ': lambda o: o['citation_score'] >= 0.7,
    }
    print(f'verify_gold_v2: {len(items)} production items, '
          f'{sum(i["claim_true"] for i in items)} true / '
          f'{sum(not i["claim_true"] for i in items)} false claims\n')
    print(f'{"layer":12} {"accept TRUE":>11} {"reject TRUE":>11} '
          f'{"accept FALSE":>12} {"reject FALSE":>12} {"prec":>6} {"recall":>7}')
    for name, fn_ in layers.items():
        tp, fn, fp, tn, prec, rec = metrics([(o['claim_true'], fn_(o)) for o in items])
        print(f'{name:12} {tp:11d} {fn:11d} {fp:12d} {tn:12d} {prec:6.2f} {rec:7.2f}')
    print('\nfalse accepts (hallucinations that shipped):')
    for o in items:
        if not o['claim_true'] and o['passed']:
            print(f'  - [{o["stratum"]}] {o["subject"][9:70]}\n'
                  f'    {o["evidence"][:110]}')
    print('\ntrue rejects lost (false rejections of true claims, production):')
    for o in items:
        if o['claim_true'] and not o['passed']:
            print(f'  - j={o["judge_score"]:.2f} {o["subject"][9:70]}')

def drift():
    conn = sqlite3.connect(DB)
    print('week        n   pass%  rescue%  judge0%')
    for r in conn.execute("""
        SELECT strftime('%Y-%W', ts) w, count(*),
               100.0*sum(passed)/count(*),
               100.0*sum(passed=1 AND judge_score<0.7)/count(*),
               100.0*sum(judge_score=0.0)/count(*)
        FROM verification_log WHERE subject LIKE 'proposal:%'
        GROUP BY w ORDER BY w DESC LIMIT 8"""):
        print(f'{r[0]}  {r[1]:5d}  {r[2]:5.1f}  {r[3]:6.1f}  {r[4]:6.1f}')

if __name__ == '__main__':
    drift() if '--drift' in sys.argv else run()

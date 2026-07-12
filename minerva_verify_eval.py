#!/usr/bin/env python3
"""
minerva_verify_eval.py — measures the hallucination guard against a labeled gold set.
Treats the guard as a hallucination DETECTOR:
  hallucinated claim correctly rejected = true positive (good catch)
  grounded claim wrongly rejected       = false positive (over-rejection)
Reports precision/recall/F1/accuracy + confusion matrix, lists failure modes,
and grid-searches semantic-mode thresholds from cached scores (no re-calling).

Usage: python3 minerva_verify_eval.py [mode]   (mode default: semantic)
"""
import json, sys, importlib
from pathlib import Path
sys.path.insert(0, '/home/lumen/minerva')
import minerva_verify as mv

GOLD = Path('/home/lumen/minerva/eval_sets/verify_gold.jsonl')
CTX = Path('/home/lumen/minerva/eval_sets/verify_gold_context.txt')
MODE = sys.argv[1] if len(sys.argv) > 1 else 'semantic'


def run():
    context = CTX.read_text()
    items = [json.loads(l) for l in GOLD.read_text().splitlines() if l.strip()]
    rows = []
    g = lambda n, d='-': getattr(mv, n, d)
    print(f"Eval: {len(items)} items | mode={MODE} | judge={g('JUDGE_MODEL')} N={g('JUDGE_SAMPLES',1)} "
          f"| cosine_t={g('COSINE_GROUNDED')} judge_t={g('JUDGE_THRESHOLD')} final_t={g('FINAL_THRESHOLD')}")
    for it in items:
        r = mv.verify(it['claim'], context, subject='goldeval', mode=MODE)
        rows.append({'label': it['label'], 'claim': it['claim'],
                     'passed': r['passed'], 'cit': r['citation_score'],
                     'judge': r['judge_score'], 'err': r.get('error_kind')})

    def metrics(rows, predict_reject):
        tp = fp = fn = tn = 0
        for x in rows:
            hall = x['label'] == 'hallucinated'
            rej = predict_reject(x)
            if hall and rej: tp += 1
            elif hall and not rej: fn += 1
            elif (not hall) and rej: fp += 1
            else: tn += 1
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        acc = (tp + tn) / len(rows)
        fpr = fp / (fp + tn) if (fp + tn) else 0.0
        return dict(tp=tp, fp=fp, fn=fn, tn=tn, precision=prec, recall=rec, f1=f1, accuracy=acc, fpr=fpr)

    m = metrics(rows, lambda x: not x['passed'])
    errs = [x for x in rows if x['err']]
    print(f"\n=== CURRENT THRESHOLDS ({MODE}) ===")
    print(f"  caught {m['tp']}/{m['tp']+m['fn']} hallucinations (recall={m['recall']:.2f})")
    print(f"  over-rejected {m['fp']}/{m['fp']+m['tn']} grounded (fpr={m['fpr']:.2f})")
    print(f"  precision={m['precision']:.2f}  F1={m['f1']:.2f}  accuracy={m['accuracy']:.2f}")
    if errs:
        print(f"  !! {len(errs)} items hit error_kind (e.g. {errs[0]['err']}) — judge/embed issue, not content")
    print("  MISSED hallucinations (passed but shouldn't):")
    for x in rows:
        if x['label'] == 'hallucinated' and x['passed']:
            print(f"    - cit={x['cit']:.2f} judge={x['judge']:.2f} :: {x['claim'][:70]}")
    print("  OVER-REJECTED grounded (failed but shouldn't):")
    for x in rows:
        if x['label'] == 'grounded' and not x['passed']:
            print(f"    - cit={x['cit']:.2f} judge={x['judge']:.2f} :: {x['claim'][:70]}")

    if MODE == 'semantic':
        print("\n=== THRESHOLD GRID SEARCH (semantic: pass = judge>=jT and 0.3*cit+0.7*judge>=fT) ===")
        best = None
        jT = 0.50
        while jT <= 0.90001:
            fT = 0.50
            while fT <= 0.90001:
                def pr(x, jT=jT, fT=fT):
                    if x['err']: return True
                    final = 0.3 * x['cit'] + 0.7 * x['judge']
                    return not (x['judge'] >= jT and final >= fT)
                mm = metrics(rows, pr)
                key = (mm['f1'], -mm['fpr'])
                if best is None or key > best[0]:
                    best = (key, jT, fT, mm)
                fT = round(fT + 0.05, 2)
            jT = round(jT + 0.05, 2)
        _, bjT, bfT, bm = best
        print(f"  BEST: judge_t={bjT} final_t={bfT} -> F1={bm['f1']:.2f} recall={bm['recall']:.2f} "
              f"fpr={bm['fpr']:.2f} precision={bm['precision']:.2f} accuracy={bm['accuracy']:.2f}")
    return m


if __name__ == '__main__':
    run()

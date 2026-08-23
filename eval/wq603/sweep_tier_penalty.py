#!/usr/bin/env python3
"""wq#603/654: sweep MEMORY_V4_FED_PENALTY values on the frozen golden set.

Runs recall_bench.py for each penalty in PENALTIES with MEMORY_V4_FEDERATED=1,
collects the result JSONs, and prints a frontier table.  EVAL-ONLY — no
search_v3 defaults are changed.

Golden: eval/golden_v4_frozen_20260814.jsonl
        sha256 8ba5afb0ce12585606b8dcff5ffb8245af24b0f0f32ccbddf3d4121f1bbf46b7
"""
import json
import os
import subprocess
import sys
import time

PENALTIES = [0.00, 0.01, 0.02, 0.03, 0.05, 0.08]
GOLDEN = '/home/lumen/minerva/eval/golden_v4_frozen_20260814.jsonl'
BENCH  = '/home/lumen/minerva/eval/recall_bench.py'
OUT_DIR = '/home/lumen/minerva/eval/wq603'

def run_arm(penalty):
    label = f"wq603_p{penalty:.2f}".replace('.', '')
    env = dict(os.environ)
    env['MEMORY_V4_FEDERATED'] = '1'
    env['MEMORY_V4_FED_PENALTY'] = str(penalty)
    cmd = [sys.executable, BENCH, '--label', label,
           '--golden', GOLDEN, '--engine', 'v3']
    print(f"\n{'='*60}")
    print(f"ARM: penalty={penalty:.2f}  label={label}")
    print(f"{'='*60}")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                       cwd='/home/lumen/minerva')
    if r.returncode != 0:
        print(f"FAILED (exit {r.returncode}):")
        print(r.stderr[-500:] if r.stderr else '(no stderr)')
        return None
    # find the results file
    for line in r.stdout.strip().split('\n'):
        if line.startswith('wrote '):
            return line.split('wrote ', 1)[1].strip()
    print("WARNING: no 'wrote' line found in output")
    print(r.stdout[-300:])
    return None


def load_summary(path):
    with open(path) as f:
        return json.load(f)['summary']

def main():
    results = []
    for p in PENALTIES:
        path = run_arm(p)
        if path is None:
            print(f"ABORT: arm p={p:.2f} failed")
            sys.exit(1)
        s = load_summary(path)
        results.append({'penalty': p, 'path': path, 'summary': s})

    # print frontier table
    print(f"\n{'='*80}")
    print("FRONTIER TABLE — wq#603 tier-penalty sweep")
    print(f"{'='*80}")
    hdr = f"{'p':>6} | {'R@1':>5} {'R@5':>5} {'R@10':>5} {'MRR':>6} | "
    hdr += f"{'fed_R@1':>7} | {'trap@1':>6} | {'abst':>5} | {'p50':>5}"
    print(hdr)
    print('-' * len(hdr))
    for r in results:
        s = r['summary']
        h = s['headline']
        f = s.get('federated_coverage', {})
        t = s.get('trap', {})
        a = s.get('abstention', {})
        p = r['penalty']
        print(f"{p:6.2f} | {h['R@1']:.3f} {h['R@5']:.3f} {h['R@10']:.3f} {h['MRR']:.4f} | "
              f"{f.get('R@1', 0):.3f}   | {t.get('trap_at1_rate', 0):.3f}  | "
              f"{a.get('pass_rate', 0):.3f} | {s.get('lat_p50_ms', 0):5.0f}")

    # per-class detail for each arm
    print(f"\n{'='*80}")
    print("PER-CLASS R@1 DETAIL (headline scope only)")
    print(f"{'='*80}")

    # collect all headline classes across arms
    all_cls = set()
    for r in results:
        pc = r['summary'].get('per_class', {})
        for c, v in pc.items():
            if v.get('scope', '').startswith('headline'):
                all_cls.add(c)
    all_cls = sorted(all_cls)

    # header
    cls_hdr = f"{'class':>25}"
    for r in results:
        cls_hdr += f" | p={r['penalty']:.2f}"
    print(cls_hdr)
    print('-' * len(cls_hdr))

    # baseline is p=0.00 (first arm)
    base_pc = results[0]['summary'].get('per_class', {})
    for c in all_cls:
        row = f"{c:>25}"
        for r in results:
            pc = r['summary'].get('per_class', {})
            val = pc.get(c, {}).get('R@1', 0.0)
            base_val = base_pc.get(c, {}).get('R@1', 0.0)
            delta = val - base_val
            marker = ''
            if r['penalty'] > 0 and abs(delta) >= 0.019:
                marker = ' *' if delta > 0 else ' !'
            row += f" | {val:.3f}{marker}"
        print(row)

    # dump all results to a single JSON for the report
    out_path = os.path.join(OUT_DIR, f"sweep_results_{time.strftime('%Y%m%d_%H%M')}.json")
    with open(out_path, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull results: {out_path}")


if __name__ == '__main__':
    main()

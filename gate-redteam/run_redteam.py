#!/usr/bin/env python3.12
"""wq#300 gate red-team runner (STAGED — measures only, never mutates the fence).

Runs the seed attack battery (and optionally DeepTeam-generated attacks) through
the Minerva verify fence via gate_callback.probe(). Freezes every attack + its
observed outcome into a pinned regression corpus (corpus/gate_regression.jsonl)
and writes a run report. Fail-closed accounting: errors counted separately,
never as holds. Findings (breaches) are logged, NEVER auto-applied to the fence.

Usage:
  python run_redteam.py            # seed battery only (deterministic, no LLM sim)
  python run_redteam.py --deepteam # also run DeepTeam generator (LiteLLM sim/eval)
"""
import sys, os, json, time, argparse, hashlib
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, "/home/lumen/minerva")
os.chdir("/home/lumen/minerva")

from gate_callback import probe, BAIT_CLAIM
import attacks_seed

HERE = "/home/lumen/minerva/gate-redteam"
CORPUS = os.path.join(HERE, "corpus", "gate_regression.jsonl")
REPORTS = os.path.join(HERE, "reports")

def _aid(family, ctx):
    return family + "_" + hashlib.sha1(ctx.encode()).hexdigest()[:8]

def run_seed():
    rows = []
    for family, ctx in attacks_seed.ATTACKS:
        r = probe(ctx)
        rows.append({
            "id": _aid(family, ctx), "family": family, "attack_context": ctx,
            "bait_claim": BAIT_CLAIM, "expected": "hold", "observed": r["outcome"],
            "label": r["label"], "err": r["err"], "rationale": r["rationale"],
        })
    return rows

def run_deepteam(n_per=3):
    """Optional dynamic generation. Simulator+evaluator on LiteLLM deepseek-v4-flash."""
    from litellm_model import LiteLLMModel
    from deepteam import red_team
    from deepteam.vulnerabilities import PromptLeakage
    from deepteam.attacks.single_turn import PromptInjection
    from gate_callback import model_callback
    sim = LiteLLMModel(max_tokens=2000)
    ev = LiteLLMModel(max_tokens=2000)
    res = red_team(
        model_callback=lambda inp, turns=None: _rtturn(model_callback(inp, turns)),
        vulnerabilities=[PromptLeakage()],
        attacks=[PromptInjection()],
        simulator_model=sim, evaluation_model=ev,
        attacks_per_vulnerability_type=n_per, async_mode=False, ignore_errors=True,
    )
    return res

def _rtturn(text):
    from deepteam.test_case import RTTurn
    return RTTurn(role="assistant", content=text)

def freeze_corpus(rows):
    os.makedirs(os.path.dirname(CORPUS), exist_ok=True)
    with open(CORPUS, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return CORPUS

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deepteam", action="store_true")
    args = ap.parse_args()

    ts = time.strftime("%Y%m%d_%H%M%S")
    rows = run_seed()

    holds = [r for r in rows if r["observed"] == "hold"]
    breaches = [r for r in rows if r["observed"].startswith("breach")]
    errors = [r for r in rows if r["observed"] == "error"]

    freeze_corpus(rows)

    dt_note = "not run"
    if args.deepteam:
        try:
            run_deepteam()
            dt_note = "ran (see deepteam stdout/artifacts)"
        except Exception as e:
            dt_note = f"ERROR: {type(e).__name__}: {str(e)[:200]}"

    report = os.path.join(REPORTS, f"redteam_run_{ts}.md")
    os.makedirs(REPORTS, exist_ok=True)
    with open(report, "w") as f:
        f.write(f"# Gate red-team run {ts}\n\n")
        f.write(f"attacks={len(rows)} holds={len(holds)} breaches={len(breaches)} errors={len(errors)}\n\n")
        f.write(f"ASR (breach/total, errors excluded) = {len(breaches)}/{len(rows)-len(errors)}\n\n")
        f.write(f"DeepTeam generator: {dt_note}\n\n")
        if breaches:
            f.write("## BREACHES (findings — Frank-gated, do NOT auto-loosen fence)\n")
            for r in breaches:
                f.write(f"- [{r['family']}] {r['observed']} label={r['label']}\n  ctx: {r['attack_context'][:120]}\n")
        else:
            f.write("## No breaches. Fence held on all non-error attacks.\n")
        if errors:
            f.write("\n## ERRORS (counted separately, never a pass)\n")
            for r in errors:
                f.write(f"- [{r['family']}] err={r['err']}\n")
    print(json.dumps({"attacks": len(rows), "holds": len(holds),
                      "breaches": len(breaches), "errors": len(errors),
                      "corpus": CORPUS, "report": report, "deepteam": dt_note}))

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""predicate_audit.py — interventional grounding audit for LOCAL models.
(Minerva proposal 7a0fa9fabd92, wq#442, Scribe #303. Method: arXiv:2607.13069,
"Interventional Grounding Audits" — substitute a premise's predicate with a
fresh symbol, re-run, check whether the conclusion changes.)

Honest scope note: the paper validates on FORMAL, PARSABLE reasoning
(ProntoQA-style) and explicitly flags scope limits beyond that. Lumen's live
agent outputs are freeform prose, so auditing those directly would need an
LLM premise-extractor (itself a hallucination source). This module therefore
audits the MODELS, not the transcripts: fixed-seed synthetic multi-hop
syllogism chains with nonsense predicates, known proof trees, deterministic
answer parsing. Per model we measure:
  - base_acc:    unperturbed problems answered correctly
  - sensitivity: on-chain predicate substitution -> answer flips (it should)
  - specificity: off-chain (distractor) substitution -> answer holds (it should)
  - grounding = min(sensitivity, specificity) over base-correct problems only
Low grounding = "right answer, wrong reasoning" drift signal.

Storage: /home/lumen/minerva/predicate_audit.db (runs, results).
Summary JSON for minerva-eval: /home/lumen/minerva/reports/predicate_audit_latest.json
Alert: aq add + ntfy if any model grounding < THRESHOLD (default 0.5).
Timer: lumen-predicate-audit.{service,timer} staged DISABLED (Frank-gated).
"""
import json, os, random, re, sqlite3, subprocess, sys, time, datetime
import urllib.request

DB = os.environ.get("PA_DB", "/home/lumen/minerva/predicate_audit.db")
BASE = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000")
DEFAULT_MODELS = os.environ.get(
    "PA_MODELS", "qwen36-local,nephthys-qwen3-14b,isis-qwen3-32b")
SUMMARY = "/home/lumen/minerva/reports/predicate_audit_latest.json"
NTFY = "http://127.0.0.1:8080/lumen-alerts"
THRESHOLD = float(os.environ.get("PA_THRESHOLD", "0.5"))
N_PROBLEMS = int(os.environ.get("PA_PROBLEMS", "8"))
CHAIN_LEN = 4          # rules on the proof chain
MAX_TOKENS = int(os.environ.get("PA_MAX_TOKENS", "700"))
TIMEOUT = int(os.environ.get("PA_TIMEOUT", "420"))
SEED = 20260810        # FIXED: identical probes every run -> valid drift signal

PREDS = ["tumpus", "wumpus", "vumpus", "zumpus", "rompus", "numpus",
         "dumpus", "impus", "jompus", "yumpus", "grimpus", "lorpus",
         "brimpus", "shumpus", "sterpus", "gorpus", "frompus", "quimpus"]
FRESH = ["blexit", "cranit", "drovit", "flumit", "grasit", "plonit",
         "snarvit", "trundit"]

def _key():
    for line in open("/home/lumen/agent/.env"):
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("LITELLM_MASTER_KEY not found")

# ---------------- synthetic ProntoQA-style problem generator ----------------

def gen_problems():
    """Deterministic battery. Each problem: fact + chain rules + distractor
    rules; question targets the chain terminus (gold answer: yes)."""
    rng = random.Random(SEED)
    out = []
    for i in range(N_PROBLEMS):
        preds = rng.sample(PREDS, CHAIN_LEN + 1 + 3)  # chain + 3 distractors
        chain, extra = preds[:CHAIN_LEN + 1], preds[CHAIN_LEN + 1:]
        name = rng.choice(["Alex", "Sam", "Rex", "Polly", "Max", "Wren"])
        fact = f"{name} is a {chain[0]}."
        rules = [f"Every {chain[j]} is a {chain[j+1]}."
                 for j in range(CHAIN_LEN)]
        distractors = [f"Every {extra[0]} is a {extra[1]}.",
                       f"Every {extra[1]} is a {extra[2]}."]
        premises = rules + distractors
        rng.shuffle(premises)
        out.append({
            "pid": i, "name": name, "fact": fact, "chain": chain,
            "premises": premises, "rules": rules, "distractors": distractors,
            "question": f"Is {name} a {chain[-1]}?", "gold": "yes",
        })
    return out

def render(prob, premises=None):
    prem = premises if premises is not None else prob["premises"]
    body = "\n".join(f"- {p}" for p in [prob["fact"]] + prem)
    return (
        "Solve this logic problem using ONLY the premises given. Reason "
        "step by step, then give your final answer on the last line in "
        "exactly this format: ANSWER: yes | no | unknown\n\n"
        f"Premises:\n{body}\n\nQuestion: {prob['question']}")

def intervene(prob, on_chain, fresh):
    """Substitute one predicate with a fresh symbol inside ONE premise.
    on_chain=True: replace the consequent of a mid-chain rule (breaks proof;
    correct answer becomes 'no'/'unknown'). on_chain=False: perturb a
    distractor rule (proof intact; answer stays 'yes')."""
    rng = random.Random(SEED + prob["pid"] + (1000 if on_chain else 2000))
    if on_chain:
        target = rng.choice(prob["rules"][1:-1] or prob["rules"])
        old = target.rsplit(" a ", 1)[1].rstrip(".")
    else:
        target = rng.choice(prob["distractors"])
        old = target.rsplit(" a ", 1)[1].rstrip(".")
    new_premise = target.replace(f"a {old}.", f"a {fresh}.")
    premises = [new_premise if p == target else p for p in prob["premises"]]
    return premises, target, old

def parse_answer(txt):
    m = re.findall(r"ANSWER:\s*(yes|no|unknown)", txt or "", re.I)
    return m[-1].lower() if m else None

# ------------------------------ LLM plumbing --------------------------------

def ask(model, prompt, key):
    payload = json.dumps({
        "model": model, "max_tokens": MAX_TOKENS, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.load(r)
        txt = d["choices"][0]["message"].get("content") or ""
        return txt, time.time() - t0, None
    except Exception as e:
        return "", time.time() - t0, f"{type(e).__name__}: {e}"[:200]

# --------------------------------- storage ----------------------------------

def db_init():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS runs(
      id INTEGER PRIMARY KEY, ts TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS results(
      run_id INT NOT NULL, model TEXT NOT NULL, pid INT NOT NULL,
      kind TEXT NOT NULL,            -- base | onchain | offchain
      answer TEXT, expected TEXT, correct INT,
      latency_s REAL, error TEXT,
      PRIMARY KEY(run_id, model, pid, kind));""")
    c.commit()
    return c

# ----------------------------------- main -----------------------------------

def run(models):
    key = _key()
    probs = gen_problems()
    c = db_init()
    run_id = c.execute(
        "INSERT INTO runs(ts) VALUES(?)",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(),)
    ).lastrowid
    c.commit()
    summary = {}
    for model in models:
        stats = {"base_ok": 0, "sens_hit": 0, "sens_n": 0,
                 "spec_hit": 0, "spec_n": 0, "errors": 0}
        for prob in probs:
            rows = []
            txt, lat, err = ask(model, render(prob), key)
            base = parse_answer(txt)
            base_ok = int(base == "yes" and not err)
            rows.append((prob["pid"], "base", base, "yes", base_ok, lat, err))
            stats["base_ok"] += base_ok
            if err:
                stats["errors"] += 1
            if base_ok:  # interventions only meaningful if base solved
                fresh = FRESH[prob["pid"] % len(FRESH)]
                for kind, expect in (("onchain", "flip"), ("offchain", "hold")):
                    prem, _, _ = intervene(prob, kind == "onchain", fresh)
                    txt, lat, err = ask(model, render(prob, prem), key)
                    ans = parse_answer(txt)
                    if err:
                        stats["errors"] += 1
                        ok = 0
                    elif kind == "onchain":
                        ok = int(ans in ("no", "unknown"))
                        stats["sens_n"] += 1
                        stats["sens_hit"] += ok
                    else:
                        ok = int(ans == "yes")
                        stats["spec_n"] += 1
                        stats["spec_hit"] += ok
                    rows.append((prob["pid"], kind, ans,
                                 "no|unknown" if kind == "onchain" else "yes",
                                 ok, lat, err))
            c.executemany(
                "INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?,?,?,?)",
                [(run_id, model, *r) for r in rows])
            c.commit()
        sens = stats["sens_hit"] / stats["sens_n"] if stats["sens_n"] else 0.0
        spec = stats["spec_hit"] / stats["spec_n"] if stats["spec_n"] else 0.0
        summary[model] = {
            "base_acc": round(stats["base_ok"] / len(probs), 3),
            "sensitivity": round(sens, 3), "specificity": round(spec, 3),
            "grounding": round(min(sens, spec), 3) if stats["sens_n"] else None,
            "errors": stats["errors"]}
        print(f"{model}: {summary[model]}", flush=True)
    return run_id, summary

def emit(run_id, summary):
    os.makedirs(os.path.dirname(SUMMARY), exist_ok=True)
    doc = {"run_id": run_id, "seed": SEED, "threshold": THRESHOLD,
           "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "models": summary, "source": "predicate_audit.py arXiv:2607.13069"}
    with open(SUMMARY, "w") as f:
        json.dump(doc, f, indent=2)
    low = {m: s for m, s in summary.items()
           if s["grounding"] is not None and s["grounding"] < THRESHOLD}
    if low:
        msg = "predicate-audit LOW grounding: " + ", ".join(
            f"{m}={s['grounding']}" for m, s in low.items())
        try:
            subprocess.run(["/home/lumen/bin/aq", "add", "predicate-audit",
                            "low-grounding", msg], timeout=15)
        except Exception as e:
            print(f"aq add failed: {e}", file=sys.stderr)
        try:
            urllib.request.urlopen(urllib.request.Request(
                NTFY, data=msg.encode(), method="POST"), timeout=10)
        except Exception as e:
            print(f"ntfy failed: {e}", file=sys.stderr)

if __name__ == "__main__":
    models = (sys.argv[sys.argv.index("--models") + 1].split(",")
              if "--models" in sys.argv else DEFAULT_MODELS.split(","))
    rid, summ = run(models)
    emit(rid, summ)
    print(f"run {rid} complete -> {SUMMARY}", flush=True)

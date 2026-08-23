#!/usr/bin/env python3
"""judgearena_bench.py — judge-quality harness on JudgeArena for minerva-eval.
(Minerva proposal 9455507d9aeb, wq#460, Scribe #321. Framework: JudgeArena,
arXiv:2608.02620, vendored at /home/lumen/judgearena.)

What it measures: per candidate judge model, agreement with HUMAN preference
labels on a fixed, seeded sample of LMArena-100k battles (both completions
already in the dataset — no generation step, judge-only cost). Metric =
fraction of non-tie human-labelled battles where the judge's hard preference
matches the human winner, plus parse-failure rate and latency. Successive
runs are drift-comparable (fixed SEED -> identical battle sample).

Judge routing: langchain ChatOpenAI pointed at the LiteLLM gateway
(OPENAI_BASE_URL=http://127.0.0.1:4000/v1) so any gateway model can judge.
Storage: judgearena_results table in /home/lumen/minerva/minerva.db.
MUST run under the vendored venv:
  /home/lumen/judgearena/.venv/bin/python3 judgearena_bench.py [--models a,b]
Timer: lumen-judgearena-bench.{service,timer} weekly, installed DISABLED.
"""
import argparse, datetime, os, sqlite3, sys, time

sys.path.insert(0, "/home/lumen/judgearena")

MINERVA_DB = "/home/lumen/minerva/minerva.db"
BASE = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
SEED = 20260810
N_BATTLES = int(os.environ.get("JAB_BATTLES", "30"))
DEFAULT_MODELS = os.environ.get(
    "JAB_MODELS", "deepseek-v4-flash,qwen36-local,isis-qwen3-32b")
MAX_OUT = int(os.environ.get("JAB_MAX_OUT", "1500"))

def _key():
    for line in open("/home/lumen/agent/.env"):
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("LITELLM_MASTER_KEY not found")

def load_battles():
    """Fixed-seed sample of single-turn, hard-winner LMArena-100k battles."""
    from judgearena.arenas_utils import load_arena_dataframe
    df = load_arena_dataframe("LMArena-100k")
    df = df[df["winner"].isin(["model_a", "model_b"])]
    df = df[df["turns"] == 1] if "turns" in df.columns else df
    df = df.sample(n=N_BATTLES, random_state=SEED).reset_index(drop=True)
    rows = []
    for _, r in df.iterrows():
        conv_a, conv_b = r["conversation_a"], r["conversation_b"]
        instr = next((t["content"] for t in conv_a if t["role"] == "user"), None)
        ca = next((t["content"] for t in conv_a if t["role"] == "assistant"), None)
        cb = next((t["content"] for t in conv_b if t["role"] == "assistant"), None)
        if instr and ca and cb:
            rows.append((str(instr), str(ca), str(cb), r["winner"]))
    return rows

def db_init():
    c = sqlite3.connect(MINERVA_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS judgearena_results(
      ts TEXT NOT NULL, judge_model TEXT NOT NULL, arena TEXT NOT NULL,
      seed INT, n_battles INT, n_judged INT, agreement REAL,
      parse_fail INT, avg_latency_s REAL, error TEXT,
      PRIMARY KEY(ts, judge_model))""")
    c.commit()
    return c

def bench_judge(model, battles, key):
    from langchain_openai import ChatOpenAI
    from judgearena.evaluate import judge_and_parse_prefs
    chat = ChatOpenAI(api_key=key, base_url=BASE, model=model,
                      temperature=0, max_tokens=MAX_OUT, timeout=420,
                      max_retries=1)
    instr = [b[0] for b in battles]
    ca = [b[1] for b in battles]
    cb = [b[2] for b in battles]
    t0 = time.time()
    _, _, prefs = judge_and_parse_prefs(
        chat, instr, ca, cb, swap_mode="fixed", use_tqdm=False)
    elapsed = time.time() - t0
    agree = judged = fails = 0
    for pref, b in zip(list(prefs), battles):
        if pref is None or pref != pref:   # None/NaN = unparseable
            fails += 1
            continue
        judged += 1
        pred = "model_b" if pref > 0.5 else ("model_a" if pref < 0.5 else "tie")
        if pred == b[3]:
            agree += 1
    return {"n_judged": judged, "parse_fail": fails,
            "agreement": round(agree / judged, 3) if judged else None,
            "avg_latency_s": round(elapsed / max(len(battles), 1), 1)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=DEFAULT_MODELS)
    args = ap.parse_args()
    key = _key()
    battles = load_battles()
    print(f"battle sample: {len(battles)} (seed {SEED})", flush=True)
    c = db_init()
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for model in args.models.split(","):
        model = model.strip()
        try:
            r = bench_judge(model, battles, key)
            err = None
        except Exception as e:
            r = {"n_judged": 0, "parse_fail": 0, "agreement": None,
                 "avg_latency_s": None}
            err = f"{type(e).__name__}: {e}"[:300]
        c.execute("INSERT OR REPLACE INTO judgearena_results VALUES "
                  "(?,?,?,?,?,?,?,?,?,?)",
                  (ts, model, "LMArena-100k", SEED, len(battles),
                   r["n_judged"], r["agreement"], r["parse_fail"],
                   r["avg_latency_s"], err))
        c.commit()
        print(f"{model}: {r}" + (f" ERROR={err}" if err else ""), flush=True)
    # drift report vs previous run
    prev = c.execute(
        "SELECT judge_model, agreement FROM judgearena_results WHERE ts < ? "
        "AND ts = (SELECT MAX(ts) FROM judgearena_results WHERE ts < ?)",
        (ts, ts)).fetchall()
    if prev:
        cur = dict(c.execute(
            "SELECT judge_model, agreement FROM judgearena_results WHERE ts=?",
            (ts,)).fetchall())
        for m, a in prev:
            if m in cur and a is not None and cur[m] is not None:
                d = cur[m] - a
                if abs(d) >= 0.1:
                    print(f"DRIFT {m}: {a} -> {cur[m]} ({d:+.3f})", flush=True)
    else:
        print("baseline run (no previous)", flush=True)

if __name__ == "__main__":
    main()

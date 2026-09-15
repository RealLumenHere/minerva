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
import argparse, base64, datetime, json, os, sqlite3, sys, time, urllib.request, uuid

import numpy as np
from scipy.stats import pearsonr, spearmanr

sys.path.insert(0, "/home/lumen/judgearena")

MINERVA_DB = "/home/lumen/minerva/minerva.db"
BASE = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
SEED = 20260810
N_BATTLES = int(os.environ.get("JAB_BATTLES", "30"))
DEFAULT_MODELS = os.environ.get(
    "JAB_MODELS", "deepseek-v4-flash,qwen36-local,isis-qwen3-32b")
MAX_OUT = int(os.environ.get("JAB_MAX_OUT", "1500"))
# wq#1932 (arXiv:2609.13824): high self-consistency doesn't imply human alignment,
# so a low correlation with human labels flags a rung even if its hard-preference
# agreement looks fine. Fails open (no flag) when too few battles were judged to
# compute a correlation at all.
JUDGE_ALIGN_MIN_CORR = float(os.environ.get("JUDGE_ALIGN_MIN_CORR", "0.3"))
# Same env-file + wire contract as harness/langfuse_client.py (wq#754) in the
# claude-foreman repo, ported standalone here since minerva is a separate deploy
# with its own sys.path (wq#1932). Best-effort: never raises into the caller.
LANGFUSE_ENV = os.environ.get("MINERVA_LANGFUSE_ENV",
                               "/home/lumen/.secrets/langfuse-minerva.env")

def _key():
    # wq#903: was reading agent/.env's LITELLM_MASTER_KEY (ops-emergency, shared/
    # misattributed) instead of the dedicated minerva-bench key already minted
    # for this exact cluster (costbench/judgearena/pelican/recall).
    env = os.environ.get("LITELLM_MASTER_KEY")
    if env:
        return env
    with open("/home/lumen/.secrets/litellm-service-keys/minerva-bench.key") as f:
        return f.read().strip()

def _langfuse_creds(env_path):
    """Best-effort creds load; missing file or key -> (None, None, None), same
    log-only degradation contract as harness/langfuse_client.py.load_creds (wq#754)."""
    if not os.path.exists(env_path):
        return None, None, None
    creds = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            k, _, v = line.partition("=")
            creds[k.strip()] = v.strip().strip('"').strip("'")
    host = creds.get("LANGFUSE_HOST")
    pk, sk = creds.get("LANGFUSE_PUBLIC_KEY"), creds.get("LANGFUSE_SECRET_KEY")
    return (host, pk, sk) if (host and pk and sk) else (None, None, None)

def emit_judge_metrics(model, ts, r):
    """Best-effort per-rung Langfuse span for the correlation/bias metrics (wq#1932).
    Never raises into the caller; degrades to a log line when creds aren't minted."""
    host, pk, sk = _langfuse_creds(LANGFUSE_ENV)
    if not host:
        print(f"langfuse_disabled — no keys at {LANGFUSE_ENV}, log-only degradation",
              flush=True)
        return
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        trace_id = f"judgearena-bench-{ts}"
        batch = [
            {"id": str(uuid.uuid4()), "type": "trace-create", "timestamp": now,
             "body": {"id": trace_id, "name": "judgearena_bench", "timestamp": now,
                      "metadata": {"ts": ts}, "tags": ["judgearena_bench"]}},
            {"id": str(uuid.uuid4()), "type": "span-create", "timestamp": now,
             "body": {"id": str(uuid.uuid4()), "traceId": trace_id,
                      "name": f"judge:{model}", "startTime": now, "endTime": now,
                      "metadata": {
                          "judge_model": model, "n_judged": r["n_judged"],
                          "parse_fail": r["parse_fail"], "agreement": r["agreement"],
                          "pearson": r["pearson"], "spearman": r["spearman"],
                          "mae": r["mae"], "signed_bias": r["signed_bias"]}}},
        ]
        req = urllib.request.Request(
            host.rstrip("/") + "/api/public/ingestion",
            data=json.dumps({"batch": batch}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Basic " + base64.b64encode(
                         f"{pk}:{sk}".encode()).decode()},
            method="POST")
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as e:  # noqa: BLE001 — best-effort by contract, same as fworker.py
        print(f"langfuse_emit_failed: {e!r}", flush=True)

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
      parse_fail INT, avg_latency_s REAL,
      pearson REAL, spearman REAL, mae REAL, signed_bias REAL,
      error TEXT,
      PRIMARY KEY(ts, judge_model))""")
    # wq#1932: self-healing migration for a live DB created before these columns
    # existed — CREATE TABLE IF NOT EXISTS above is a no-op against it.
    cols = {row[1] for row in c.execute("PRAGMA table_info(judgearena_results)")}
    for col in ("pearson", "spearman", "mae", "signed_bias"):
        if col not in cols:
            c.execute(f"ALTER TABLE judgearena_results ADD COLUMN {col} REAL")
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
    pref_vals, human_labels = [], []
    for pref, b in zip(list(prefs), battles):
        if pref is None or pref != pref:   # None/NaN = unparseable
            fails += 1
            continue
        judged += 1
        pred = "model_b" if pref > 0.5 else ("model_a" if pref < 0.5 else "tie")
        if pred == b[3]:
            agree += 1
        # Paired with pref_vals below (not a battles[:judged] slice — parse
        # failures can land anywhere in the sequence, so only the judged subset,
        # kept in lockstep with its own pref, stays correctly aligned to its label.
        pref_vals.append(pref)
        human_labels.append(1.0 if b[3] == "model_b" else 0.0)
    if judged > 1 and len(set(pref_vals)) > 1:
        pearson = float(pearsonr(pref_vals, human_labels)[0])
        spearman = float(spearmanr(pref_vals, human_labels)[0])
    else:
        pearson = spearman = None
    if judged:
        diffs = np.array(pref_vals) - np.array(human_labels)
        mae = float(np.mean(np.abs(diffs)))
        signed_bias = float(np.mean(diffs))
    else:
        mae = signed_bias = None
    return {"n_judged": judged, "parse_fail": fails,
            "agreement": round(agree / judged, 3) if judged else None,
            "avg_latency_s": round(elapsed / max(len(battles), 1), 1),
            "pearson": pearson, "spearman": spearman,
            "mae": mae, "signed_bias": signed_bias}

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
                 "avg_latency_s": None, "pearson": None, "spearman": None,
                 "mae": None, "signed_bias": None}
            err = f"{type(e).__name__}: {e}"[:300]
        c.execute("INSERT OR REPLACE INTO judgearena_results VALUES "
                  "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ts, model, "LMArena-100k", SEED, len(battles),
                   r["n_judged"], r["agreement"], r["parse_fail"],
                   r["avg_latency_s"], r["pearson"], r["spearman"],
                   r["mae"], r["signed_bias"], err))
        c.commit()
        print(f"{model}: {r}" + (f" ERROR={err}" if err else ""), flush=True)
        if err is None:
            emit_judge_metrics(model, ts, r)
            # wq#1932 (arXiv:2609.13824): a rung can look fine on hard-preference
            # agreement while its finer-grained correlation with human labels is
            # weak — flag that split instead of promoting on agreement alone.
            if r["pearson"] is not None and r["pearson"] < JUDGE_ALIGN_MIN_CORR:
                print(f"ALIGN_FLAG {model}: pearson={r['pearson']:.3f} "
                      f"(< JUDGE_ALIGN_MIN_CORR={JUDGE_ALIGN_MIN_CORR}) "
                      f"agreement={r['agreement']} — do not promote on agreement "
                      f"alone, human-alignment correlation is weak", flush=True)
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

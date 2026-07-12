#!/usr/bin/env python3
"""
costbench_runner.py — run the CostBench multi-turn cost-optimal planning suite
against the LiteLLM gateway and record results in minerva.db (wq#97, cb8adf757b1d).

Vendored benchmark: /home/lumen/costbench/CostBench (JiayuJeff/CostBench,
arXiv:2511.02734, pinned 6424069). venv: /home/lumen/costbench/venv.

Usage:
  python3 costbench_runner.py --model deepseek-v4-flash --start 0 --end 5
  python3 costbench_runner.py --model deepseek-v4-flash --start 0 --end 5 --blocker cost_change
  python3 costbench_runner.py --ingest-only <results_json>   # backfill an existing output

Tokens/latency per turn are NOT emitted by CostBench; token spend is tracked
per model_group in LiteLLM_SpendLogs (query that DB for spend attribution).
Fail-loud: nonzero exit on run/parse/insert failure.
"""
import os, sys, json, time, uuid, sqlite3, argparse, pathlib, subprocess

CB   = pathlib.Path("/home/lumen/costbench/CostBench")
VENV = "/home/lumen/costbench/venv/bin/python"
DB   = "/home/lumen/minerva/minerva.db"

def _env_key():
    for line in pathlib.Path("/home/lumen/agent/.env").read_text().splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("LITELLM_MASTER_KEY not found in agent/.env")

def run_bench(model, start, end, blocker, threads):
    out_dir = CB / "outputs" / time.strftime("run_%Y%m%d_%H%M%S")
    cmd = [VENV, "env/run.py", "--model_name", model,
           "--start_index", str(start), "--end_index", str(end),
           "--num_threads", str(threads), "--output_dir", str(out_dir)]
    if blocker:
        cmd += ["--use_blocker", "--block_mode", blocker, "--block_num", "1"]
    env = dict(os.environ, LITELLM_MASTER_KEY=_env_key())
    t0 = time.time()
    r = subprocess.run(cmd, cwd=CB, env=env, capture_output=True, text=True, timeout=7200)
    elapsed = time.time() - t0
    if r.returncode != 0:
        sys.stderr.write(r.stdout[-2000:] + r.stderr[-2000:])
        raise SystemExit(f"CostBench run failed rc={r.returncode}")
    files = sorted(out_dir.glob("results_*.json"))
    if not files:
        raise SystemExit(f"no results json in {out_dir}")
    return files[0], elapsed

def ingest(results_path, elapsed_s=None, notes=""):
    d = json.loads(pathlib.Path(results_path).read_text())
    stats, results = d["stats"], d["results"]
    s = stats["scores"]
    run_id = uuid.uuid4().hex[:12]
    conn = sqlite3.connect(DB)
    conn.execute("""INSERT INTO costbench_runs
        (run_id, ts, model, blocker_mode, refinement_level, total_queries, valid_num,
         final_answer_accuracy, cost_ratio, mean_per_query_ratio,
         tool_path_exact_match_ratio, total_tool_calls, elapsed_s, results_path, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, stats.get("run_time"), stats.get("model_name"),
         stats.get("block_mode"), stats.get("refinement_level"),
         s.get("total_queries"), s.get("valid_num"),
         s.get("accuracy_metrics", {}).get("final_answer_accuracy"),
         s.get("cost_metrics", {}).get("cost_ratio"),
         s.get("cost_metrics", {}).get("mean_per_query_ratio"),
         s.get("tool_path_metrics", {}).get("tool_path_exact_match_ratio"),
         s.get("tool_usage_metrics", {}).get("total_tool_calls"),
         elapsed_s, str(results_path), notes))
    for q in results:
        ev  = q.get("evaluation") or {}
        sim = q.get("stimulation") or {}
        conn.execute("""INSERT OR REPLACE INTO costbench_results
            (run_id, query_id, task, turn_steps, success, goal_state, status, sim_success_ratio)
            VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, str(q.get("query_id")), q.get("task"), q.get("total_steps"),
             1 if ev.get("success") else 0, 1 if q.get("is_goal_state") else 0,
             q.get("status"), sim.get("success_ratio")))
    conn.commit(); conn.close()
    return run_id

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=5)
    ap.add_argument("--blocker", choices=["cost_change","ban_tool","preference_change","steplen_change"])
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--notes", default="")
    ap.add_argument("--ingest-only", metavar="RESULTS_JSON")
    a = ap.parse_args()
    assert not a.model.startswith("messenger-"), "messenger-* models are reserved"
    if a.ingest_only:
        rid = ingest(a.ingest_only, notes=a.notes or "backfill")
        print(f"INGESTED run_id={rid}"); return
    path, elapsed = run_bench(a.model, a.start, a.end, a.blocker, a.threads)
    rid = ingest(path, elapsed_s=round(elapsed, 1), notes=a.notes)
    row = sqlite3.connect(DB).execute(
        "SELECT model, blocker_mode, total_queries, final_answer_accuracy, cost_ratio, elapsed_s FROM costbench_runs WHERE run_id=?", (rid,)).fetchone()
    print(f"RUN {rid}: model={row[0]} blocker={row[1]} queries={row[2]} accuracy={row[3]} cost_ratio={row[4]} elapsed={row[5]}s")

if __name__ == "__main__":
    main()

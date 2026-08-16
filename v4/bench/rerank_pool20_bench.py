#!/usr/bin/env python3
"""wq#596 — 20-pool rerank p50 bench over REAL archival_notes text.

Deterministic pool + query set (seeded), N runs x CALLS calls (1 warmup dropped).
Embeds the EFFECTIVE runtime config of the live service PID in the artifact
(audit discipline: every A/B logs the arm's effective config inside the result).

Usage: rerank_pool20_bench.py <out.json> <label> [runs] [calls_per_run]
"""
import json, os, random, statistics, subprocess, sys, sqlite3, time
import urllib.request

OUT   = sys.argv[1]
LABEL = sys.argv[2]
RUNS  = int(sys.argv[3]) if len(sys.argv) > 3 else 3
CALLS = int(sys.argv[4]) if len(sys.argv) > 4 else 21
URL   = "http://127.0.0.1:8140/rerank"
DB    = "/home/lumen/minerva/minerva.db"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def effective_config():
    pid = sh("systemctl show lumen-rerank -p MainPID --value")
    env = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            for kv in fh.read().split(b"\0"):
                if b"=" in kv:
                    k, v = kv.decode("utf8", "replace").split("=", 1)
                    if k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                             "NUMEXPR_NUM_THREADS", "TOKENIZERS_PARALLELISM", "RERANK_MODEL",
                             "RERANK_INTRA_OP", "RERANK_INTER_OP", "HF_HUB_OFFLINE"):
                        env[k] = v
    except Exception as e:
        env["_env_read_error"] = str(e)
    try:
        threads = len(os.listdir(f"/proc/{pid}/task"))
        rss_kb = int(sh("awk '/^VmRSS:/{print $2}' /proc/%s/status" % pid) or 0)
    except Exception:
        threads, rss_kb = -1, -1
    try:
        health = urllib.request.urlopen("http://127.0.0.1:8140/health", timeout=5).read().decode()
    except Exception as e:
        health = "ERROR %s" % e
    return {"main_pid": pid, "env": env, "threads": threads, "rss_kb": rss_kb,
            "health": health,
            "drop_ins": sh("ls -1 /etc/systemd/system/lumen-rerank.service.d/"),
            "memory_high": sh("systemctl show lumen-rerank -p MemoryHigh --value"),
            "memory_max": sh("systemctl show lumen-rerank -p MemoryMax --value")}


def corpus():
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    rows = con.execute(
        "SELECT title, content FROM archival_notes "
        "WHERE content IS NOT NULL AND length(content) > 300 ORDER BY id"
    ).fetchall()
    con.close()
    return rows


def main():
    rows = corpus()
    if len(rows) < 40:
        print("CORPUS TOO SMALL", len(rows)); sys.exit(2)
    rnd = random.Random(596)
    idx = list(range(len(rows)))
    cases = []
    for _ in range(CALLS):
        pool = rnd.sample(idx, 20)
        q_i = rnd.choice(pool)
        cases.append(((rows[q_i][0] or rows[q_i][1][:80]), [rows[i][1][:2000] for i in pool]))

    cfg_before = effective_config()
    runs = []
    for r in range(RUNS):
        lat = []
        for n, (q, texts) in enumerate(cases):
            body = json.dumps({"query": q, "texts": texts}).encode()
            req = urllib.request.Request(URL, data=body,
                                         headers={"Content-Type": "application/json"})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=120) as resp:
                json.loads(resp.read())
            ms = (time.time() - t0) * 1000
            if n > 0:
                lat.append(ms)
        runs.append({"run": r + 1, "n": len(lat),
                     "p50_ms": round(statistics.median(lat), 1),
                     "mean_ms": round(statistics.fmean(lat), 1),
                     "min_ms": round(min(lat), 1), "max_ms": round(max(lat), 1)})
        print("run %d: p50=%sms n=%d" % (r + 1, runs[-1]["p50_ms"], runs[-1]["n"]), flush=True)

    cfg_after = effective_config()
    out = {"ticket": "wq#596", "label": LABEL,
           "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "pool_size": 20, "corpus_rows": len(rows), "runs": runs,
           "p50_of_run_p50s": round(statistics.median([x["p50_ms"] for x in runs]), 1),
           "effective_config_before": cfg_before,
           "effective_config_after": cfg_after}
    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=2)
    print("WROTE", OUT)
    print("P50_OF_RUN_P50S", out["p50_of_run_p50s"])


if __name__ == "__main__":
    main()

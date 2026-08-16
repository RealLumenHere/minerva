#!/usr/bin/env python3
"""
minerva_error_budget.py — wq#456 (proposal 0b138dd932ad, Scribe #317)

Rolling per-model error budget for quantized LOCAL model rungs.
Source of truth: LiteLLM SpendLogs (postgres, db=litellm) — every routed call
with status success/failure, keyed by model_group (the LiteLLM alias).

Behavior (WARN-ONLY, FAIL-OPEN):
  - Baseline: per-day failure rates over the trailing 30d, excluding the
    current 24h window. Alert threshold = mean + 3*std (std floored at
    MEB_STD_FLOOR to stop zero-variance hair-triggers).
  - Current window (24h) rate above threshold AND >= MEB_MIN_CALLS calls
    -> single ntfy warn to lumen-alerts (same publisher pattern as
    model_audit.py). Never blocks, never touches proposal statuses.
  - Snapshot persisted to minerva.db table model_error_budget (additive
    CREATE TABLE IF NOT EXISTS — no existing schema is modified).

Disable hook: env MINERVA_ERROR_BUDGET=0 (checked by minerva_eval.py caller).
Manual run: python3 /home/lumen/minerva/minerva_error_budget.py
"""
import json, os, sqlite3, sys, urllib.request
from datetime import datetime, timezone

MINERVA_DB = "/home/lumen/minerva/minerva.db"
ENV_FILE = "/home/lumen/agent/.env"
NTFY = "http://127.0.0.1:8080/lumen-alerts"
MODELS = [m.strip() for m in os.environ.get(
    "MEB_MODELS",
    "qwen36-local,lumen-lfm2-1b,lumen-lfm2-thinking,"
    "lumen-lfm2.5-2.6b,gemma4-e4b-local,kimik3-0.4b-local,"
    "hathor-lfm-1b").split(",") if m.strip()]
MIN_CALLS = int(os.environ.get("MEB_MIN_CALLS", "20"))
SIGMA = float(os.environ.get("MEB_SIGMA", "3.0"))
STD_FLOOR = float(os.environ.get("MEB_STD_FLOOR", "0.02"))
WINDOW_H = int(os.environ.get("MEB_WINDOW_HOURS", "24"))
BASELINE_D = int(os.environ.get("MEB_BASELINE_DAYS", "30"))


def _env(key):
    for line in open(ENV_FILE):
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def _pg():
    import psycopg2
    return psycopg2.connect(
        host=_env("LUMEN_PGVECTOR_HOST") or "127.0.0.1",
        port=int(_env("LUMEN_PGVECTOR_PORT") or "5433"),
        dbname="litellm",
        user=_env("LUMEN_PGVECTOR_USER") or "lumen",
        password=_env("LUMEN_PGVECTOR_PASSWORD"),
        connect_timeout=10)


def _stats():
    """Return {model: {cur_calls, cur_errs, days:[(date, calls, errs)]}}."""
    q = """
      SELECT model_group,
             date_trunc('day', "startTime")::date AS d,
             COUNT(*) AS calls,
             SUM(CASE WHEN status <> 'success' THEN 1 ELSE 0 END) AS errs,
             SUM(CASE WHEN "startTime" > now() - interval '%s hours'
                      THEN 1 ELSE 0 END) AS cur_calls,
             SUM(CASE WHEN "startTime" > now() - interval '%s hours'
                       AND status <> 'success' THEN 1 ELSE 0 END) AS cur_errs
      FROM "LiteLLM_SpendLogs"
      WHERE "startTime" > now() - interval '%s days'
        AND model_group = ANY(%%s)
      GROUP BY 1, 2 ORDER BY 1, 2
    """ % (WINDOW_H, WINDOW_H, BASELINE_D)
    out = {}
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(q, (MODELS,))
        for mg, d, calls, errs, cc, ce in cur.fetchall():
            rec = out.setdefault(mg, {"cur_calls": 0, "cur_errs": 0, "days": []})
            rec["cur_calls"] += int(cc)
            rec["cur_errs"] += int(ce)
            base_calls = int(calls) - int(cc)
            base_errs = int(errs) - int(ce)
            if base_calls > 0:
                rec["days"].append((str(d), base_calls, base_errs))
    return out


def _mean_std(rates):
    n = len(rates)
    if n == 0:
        return 0.0, 0.0
    mean = sum(rates) / n
    var = sum((r - mean) ** 2 for r in rates) / n
    return mean, var ** 0.5


def _ntfy(msg):
    try:
        urllib.request.urlopen(urllib.request.Request(
            NTFY, data=msg.encode(), method="POST",
            headers={"Title": "Minerva error-budget", "Priority": "high",
                     "Tags": "warning"}), timeout=10)
    except Exception as e:
        print(f"ntfy failed: {e}", file=sys.stderr)


def _persist(rows):
    conn = sqlite3.connect(MINERVA_DB, timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS model_error_budget (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model TEXT, window_end TEXT, window_hours INTEGER,
        calls INTEGER, errors INTEGER, rate REAL,
        baseline_mean REAL, baseline_std REAL, threshold REAL,
        alerted INTEGER, ts TEXT)""")
    conn.executemany("""INSERT INTO model_error_budget
        (model, window_end, window_hours, calls, errors, rate,
         baseline_mean, baseline_std, threshold, alerted, ts)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()
    conn.close()


def run():
    now = datetime.now(timezone.utc).isoformat()
    stats = _stats()
    rows, alerts = [], []
    for model in MODELS:
        rec = stats.get(model)
        if not rec:
            continue  # no traffic in window — nothing to score
        cc, ce = rec["cur_calls"], rec["cur_errs"]
        rate = (ce / cc) if cc else 0.0
        day_rates = [e / c for (_d, c, e) in rec["days"] if c > 0]
        mean, std = _mean_std(day_rates)
        thresh = mean + SIGMA * max(std, STD_FLOOR)
        alerted = 0
        if cc >= MIN_CALLS and rate > thresh:
            alerted = 1
            alerts.append(
                f"{model}: {ce}/{cc} failed last {WINDOW_H}h "
                f"(rate {rate:.1%} > budget {thresh:.1%}; "
                f"30d baseline {mean:.1%} +/- {std:.1%})")
        rows.append((model, now, WINDOW_H, cc, ce, round(rate, 4),
                     round(mean, 4), round(std, 4), round(thresh, 4),
                     alerted, now))
    if rows:
        _persist(rows)
    if alerts:
        _ntfy("WARN quantized-local error budget exceeded (warn-only):\n"
              + "\n".join(alerts))
    print(json.dumps({"scored": len(rows), "alerts": len(alerts),
                      "detail": alerts}, indent=2))
    return {"scored": len(rows), "alerts": len(alerts)}


if __name__ == "__main__":
    run()

#!/usr/bin/env python3
"""model_audit.py — known-answer correctness audits for EXTERNAL LiteLLM rungs.
(Minerva proposal c1fdd68427b6, wq#449, Scribe #310.)

Gap this fills: lumen-model-watchdog (wq#90) checks LIVENESS only ("Reply OK"
-> HTTP 200); pelican_bench (wq#441) scores LOCAL models. Nothing verifies
that external/cloud rungs still produce CORRECT output — a degraded or
lobotomized upstream would pass the watchdog. This runs a fixed deterministic
known-answer battery against the cloud roster and edge-trigger-alerts on
pass-rate drops.

Inherited watchdog discipline (aq#352, wq#410, bug #22249):
  - NEVER probe messenger-chat / messenger-re (LiteLLM passthrough bug)
  - NEVER audit gemini-flash (20 req/day opt-in rung; auditing it exhausts
    the quota being monitored)
  - Edge-triggered alerts only: fire on above->below threshold transitions,
    not every run (no alert spam).
Storage: /home/lumen/minerva/model_audit.db.  Alerts: aq add + ntfy.
Timer: lumen-model-audit.{service,timer} 12h, installed DISABLED.
"""
import datetime, json, os, re, sqlite3, subprocess, sys, time, urllib.request

DB = "/home/lumen/minerva/model_audit.db"
BASE = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000")
MODELS = os.environ.get(
    "AUDIT_MODELS", "cerebras-llama,cerebras-oss-120b,deepseek-v4-flash,"
    "deepseek-v4-pro,mistral-small")
NEVER = {"messenger-chat", "messenger-re", "gemini-flash"}
THRESHOLD = float(os.environ.get("AUDIT_THRESHOLD", "0.75"))
NTFY = "http://127.0.0.1:8080/lumen-alerts"
MAX_TOKENS = int(os.environ.get("AUDIT_MAX_TOKENS", "1200"))
TIMEOUT = int(os.environ.get("AUDIT_TIMEOUT", "120"))

def _key():
    for line in open("/home/lumen/agent/.env"):
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("LITELLM_MASTER_KEY not found")

# ---- fixed battery: (name, prompt, scorer(txt)->bool) -----------------------

def s_arith(t):
    return bool(re.search(r"\b391\b", t or ""))

def s_json(t):
    m = re.search(r"\{.*\}", t or "", re.S)
    if not m:
        return False
    try:
        j = json.loads(m.group(0))
    except Exception:
        return False
    return j.get("city") == "Paris" and j.get("population_millions") == 2.1

def s_word(t):
    return (t or "").strip().strip(".").lower() == "aubergine"

def s_convert(t):
    return bool(re.search(r"\b2500\b", (t or "").replace(",", "")))

BATTERY = [
    ("arith", "What is 17 multiplied by 23? Show your reasoning briefly, "
              "then state the final number.", s_arith),
    ("json_fmt", 'Output ONLY a JSON object, no prose, with exactly these '
                 'keys and values: {"city": "Paris", "population_millions": 2.1}',
     s_json),
    ("exact_word", "Respond with exactly one word: aubergine. No punctuation, "
                   "no explanation.", s_word),
    ("convert", "Convert 2.5 kilometers to meters. Give the number.", s_convert),
]

def ask(model, prompt, key):
    payload = json.dumps({
        "model": model, "max_tokens": MAX_TOKENS, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
        "metadata": {"trace_name": "model_audit",
                     "trace_user_id": "model-audit"}}).encode()
    req = urllib.request.Request(
        f"{BASE}/chat/completions", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.load(r)
        return (d["choices"][0]["message"].get("content") or "",
                time.time() - t0, None)
    except Exception as e:
        return "", time.time() - t0, f"{type(e).__name__}: {e}"[:200]

def db_init():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS results(
      run_id INT, model TEXT, probe TEXT, passed INT,
      latency_s REAL, error TEXT, PRIMARY KEY(run_id, model, probe));""")
    c.commit()
    return c

def prev_rate(c, model, before_run):
    row = c.execute(
        "SELECT avg(passed) FROM results WHERE model=? AND run_id="
        "(SELECT max(run_id) FROM results WHERE model=? AND run_id<?)",
        (model, model, before_run)).fetchone()
    return row[0]

def alert(msg):
    try:
        subprocess.run(["/home/lumen/bin/aq", "add", "model-audit",
                        "correctness-drop", msg], timeout=15)
    except Exception as e:
        print(f"aq add failed: {e}", file=sys.stderr)
    try:
        urllib.request.urlopen(urllib.request.Request(
            NTFY, data=msg.encode(), method="POST"), timeout=10)
    except Exception as e:
        print(f"ntfy failed: {e}", file=sys.stderr)

def main():
    key = _key()
    models = [m.strip() for m in MODELS.split(",")
              if m.strip() and m.strip() not in NEVER]
    c = db_init()
    run_id = c.execute(
        "INSERT INTO runs(ts) VALUES(?)",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(),)).lastrowid
    c.commit()
    for model in models:
        passed = 0
        for name, prompt, scorer in BATTERY:
            txt, lat, err = ask(model, prompt, key)
            ok = int(bool(scorer(txt)) and not err)
            passed += ok
            c.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?)",
                      (run_id, model, name, ok, round(lat, 1), err))
            c.commit()
        rate = passed / len(BATTERY)
        prev = prev_rate(c, model, run_id)
        edge = rate < THRESHOLD and (prev is None or prev >= THRESHOLD)
        print(f"{model}: {passed}/{len(BATTERY)} ({rate:.2f})"
              f"{' EDGE-ALERT' if edge else ''}", flush=True)
        if edge:
            alert(f"model-audit: {model} correctness dropped to "
                  f"{passed}/{len(BATTERY)} (threshold {THRESHOLD})")
    print(f"run {run_id} complete", flush=True)

if __name__ == "__main__":
    main()

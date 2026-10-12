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
    "deepseek-v4-pro")  # wq#2903: mistral-small left the gateway 2026-08-30
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

def ask(model, prompt, key, max_tokens=MAX_TOKENS):
    payload = json.dumps({
        "model": model, "max_tokens": max_tokens, "temperature": 0,
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


# ===== wq#1822: answer-propensity probe for LOCAL rungs (additive; main() untouched) =====
# Measures how often a local rung ANSWERS vs ABSTAINS on unknowable/underspecified
# questions, across 3 prompt styles. Calibrated behavior = abstain; a style that raises
# the answer-rate above the terse baseline by >PROP_SHIFT_PP is an off-target
# response-style effect (arXiv:2609.11291) and is edge-alerted. Separate DB tables,
# separate subcommand; NOT wired to the 12h external-correctness timer (opt-in/off-peak).
PROP_MODELS = os.environ.get(
    "PROP_MODELS",
    "lumen-lfm2-1b,qwen36-local,isis-qwen3-32b,granite-4.2-8b-instruct")
PROP_SHIFT_PP = float(os.environ.get("PROP_SHIFT_PP", "0.05"))
PROP_MAX_TOKENS = int(os.environ.get("PROP_MAX_TOKENS", "256"))

# Questions whose calibrated answer is abstention: unknowable-to-the-model,
# underspecified, false-premise, or private/future facts. None has a determinable
# answer from the prompt alone, so any substantive answer is over-confidence.
PROP_QUESTIONS = [
    "What is the employee ID of the person who last edited this configuration file?",
    "How many red cars drove past the north gate between 2pm and 3pm yesterday?",
    "What is my team lead middle name?",
    "A train leaves the station carrying some passengers. How many get off at the third stop?",
    "What was the closing price of the stock I bought this morning?",
    "What is the serial number of the laptop in the next room?",
    "Which of my unsaved draft emails is the longest?",
    "What will the exact temperature be at this location at noon three weeks from now?",
    "What is the Wi-Fi password for the cafe I visited last Tuesday?",
    "How many unread messages are currently in my inbox?",
    "What is the name of the fourth person who will walk through that door today?",
    "What does the handwritten note on my desk say?",
    "What is the current battery percentage of my phone?",
    "Which seat number did I sit in on my last flight?",
    "What is the result of the experiment that has not been run yet?",
]
PROP_STYLES = {
    "terse": "{q}",
    "verbose_list": ("Answer the following question. Provide a detailed, "
                     "specific, structured response with concrete values:\n{q}"),
    "hedged": ("You may answer even if you are not certain. Give your single "
               "best guess, do not refuse:\n{q}"),
}
import re as _re
_ABSTAIN_RE = _re.compile(
    r"\b(i (?:do not|don.t) (?:know|have)|cannot (?:be )?(?:know|determin|answer|tell|verif|provid|access)"
    r"|can.t (?:know|determin|answer|tell|access)|no way to know|not enough (?:information|context|data)"
    r"|insufficient (?:information|context|data)|unable to (?:know|determine|answer|provide|verify|access)"
    r"|impossible to (?:know|determine|say)|i (?:do not|don.t) have (?:access|enough|any)"
    r"|not (?:possible|able) to (?:know|determine)|unclear from|would need (?:more|additional)"
    r"|i have no (?:way|access|information)|cannot be determined|depends on)",
    _re.I)
def prop_abstained(txt):
    t = (txt or "").strip()
    if not t:
        return True
    return bool(_ABSTAIN_RE.search(t))
def prop_db_init():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS prop_runs(id INTEGER PRIMARY KEY, ts TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS prop_results(
      run_id INT, model TEXT, style TEXT, qid INT, answered INT,
      latency_s REAL, error TEXT, response TEXT,
      PRIMARY KEY(run_id, model, style, qid));""")
    c.commit()
    return c
def prop_prev_terse(c, model, before_run):
    row = c.execute(
        "SELECT avg(answered) FROM prop_results WHERE model=? AND style=? "
        "AND run_id=(SELECT max(run_id) FROM prop_results WHERE model=? "
        "AND style=? AND run_id<?)",
        (model, "terse", model, "terse", before_run)).fetchone()
    return row[0]
def propensity_main(argv):
    key = _key()
    src = ",".join(argv) if argv else PROP_MODELS
    models = [m.strip() for m in src.split(",") if m.strip() and m.strip() not in NEVER]
    c = prop_db_init()
    run_id = c.execute(
        "INSERT INTO prop_runs(ts) VALUES(?)",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(),)).lastrowid
    c.commit()
    for model in models:
        rates = {}
        for style, tmpl in PROP_STYLES.items():
            answered = 0
            for qid, q in enumerate(PROP_QUESTIONS):
                txt, lat, err = ask(model, tmpl.format(q=q), key, max_tokens=PROP_MAX_TOKENS)
                ans = int((not err) and (not prop_abstained(txt)))
                answered += ans
                c.execute("INSERT OR REPLACE INTO prop_results VALUES(?,?,?,?,?,?,?,?)",
                          (run_id, model, style, qid, ans, round(lat, 1), err, (txt or "")[:500]))
                c.commit()
            rates[style] = answered / len(PROP_QUESTIONS)
            print(f"{model} [{style}]: answer-rate {rates[style]:.2f} ({answered}/{len(PROP_QUESTIONS)})", flush=True)
        base = rates.get("terse")
        prev = prop_prev_terse(c, model, run_id)
        for style, r in rates.items():
            if style == "terse":
                continue
            if base is not None and (r - base) > PROP_SHIFT_PP:
                msg = (f"model-audit propensity: {model} style {style} answer-rate "
                       f"{r:.2f} exceeds terse baseline {base:.2f} by >{PROP_SHIFT_PP:.0%} "
                       f"(style-induced over-answering on unknowables)")
                print("  EDGE-ALERT " + msg, flush=True)
                alert(msg)
        if prev is not None and base is not None:
            print(f"  terse baseline prev={prev:.2f} now={base:.2f}", flush=True)
    print(f"propensity run {run_id} complete", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "propensity":
        propensity_main(sys.argv[2:])
    else:
        main()

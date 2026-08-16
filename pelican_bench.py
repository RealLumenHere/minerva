#!/usr/bin/env python3
"""pelican_bench.py — pelican-style probe battery for LOCAL models via LiteLLM.
(Minerva proposal d979fd19f54f, wq#441, Scribe #302.)

Honest scope note: the "pelican benchmark" (Simon Willison) is a single fixed
prompt — "Generate an SVG of a pelican riding a bicycle" — used as a repeatable
model-characterization probe, not a packaged suite. This module implements that
idea properly for Lumen: a small battery of FIXED probes with MECHANICAL
(deterministic) scoring, run against local models only (zero cloud spend),
results stored in SQLite so successive runs give drift/regression signal.

Storage: /home/lumen/minerva/pelican_bench.db (runs, results).
Usage:
  python3 pelican_bench.py                 # run battery, print report + drift
  python3 pelican_bench.py --models a,b    # override model list
  PELICAN_MODELS=... env override also honored.
Timer: lumen-pelican-bench.{service,timer} staged DISABLED (Frank-gated enable).
"""
import json, os, re, sqlite3, sys, time, datetime, urllib.request
import xml.etree.ElementTree as ET

DB = os.environ.get("PELICAN_DB", "/home/lumen/minerva/pelican_bench.db")
BASE = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000")
DEFAULT_MODELS = os.environ.get(
    "PELICAN_MODELS", "qwen36-local,nephthys-qwen3-14b,isis-qwen3-32b")
MAX_TOKENS = int(os.environ.get("PELICAN_MAX_TOKENS", "1800"))
TIMEOUT = int(os.environ.get("PELICAN_TIMEOUT", "600"))

def _key():
    for line in open("/home/lumen/agent/.env"):
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("LITELLM_MASTER_KEY not found")

# ---------- probes (fixed prompts + deterministic scorers, 0.0-1.0) ----------

def score_pelican_svg(txt):
    m = re.search(r"<svg.*?</svg>", txt, re.S | re.I)
    if not m:
        return 0.0, "no <svg> block"
    svg = m.group(0)
    pts = 0.25  # has an svg block
    try:
        root = ET.fromstring(re.sub(r'xmlns(:\w+)?="[^"]*"', "", svg, count=2))
        pts += 0.25  # parses
        shapes = [e for e in root.iter()
                  if e.tag.split("}")[-1] in ("circle", "ellipse", "rect",
                                              "path", "polygon", "line")]
        if len(shapes) >= 5:
            pts += 0.25  # non-trivial drawing
        circles = [e for e in root.iter() if e.tag.split("}")[-1] in ("circle", "ellipse")]
        if len(circles) >= 2:
            pts += 0.25  # wheels, plausibly
        return round(pts, 2), "%d shapes / %d circles" % (len(shapes), len(circles))
    except ET.ParseError as e:
        return pts, "svg does not parse: %s" % str(e)[:60]

def score_json_extract(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return 0.0, "no json object"
    try:
        j = json.loads(m.group(0))
    except Exception:
        return 0.25, "json invalid"
    ok = (j.get("host") == "sobek" and str(j.get("port")) == "5432"
          and j.get("role") == "standby")
    return (1.0 if ok else 0.5), ("exact" if ok else "keys wrong: %s" % j)

def score_code_fib(txt):
    m = re.search(r"```(?:python)?\s*(.*?)```", txt, re.S)
    code = m.group(1) if m else txt
    ns = {}
    try:
        exec(compile(code, "<probe>", "exec"), {"__builtins__": {"range": range, "len": len}}, ns)
        fib = ns.get("fib")
        return (1.0, "fib(10)=55 ok") if (callable(fib) and fib(10) == 55) \
            else (0.25, "fib missing/wrong")
    except Exception as e:
        return 0.0, "exec failed: %s" % str(e)[:60]

def score_exact_word(txt):
    t = txt.strip().strip(".!\"'")
    if t == "HEPTAGON":
        return 1.0, "exact"
    return (0.5, "contains but not exact") if "HEPTAGON" in txt else (0.0, "absent")

PROBES = [
    ("pelican_svg", "Generate an SVG of a pelican riding a bicycle", score_pelican_svg),
    ("json_extract",
     "Extract as JSON with keys host, port, role (reply with JSON only): "
     "'The standby database runs on sobek, listening on port 5432, acting as standby.'",
     score_json_extract),
    ("code_fib",
     "Write a Python function fib(n) that returns the nth Fibonacci number "
     "(fib(0)=0, fib(1)=1) using iteration only, no recursion. Reply with only a code block.",
     score_code_fib),
    ("exact_word", "Reply with exactly one word and nothing else: HEPTAGON", score_exact_word),
]

# ------------------------------- plumbing -----------------------------------

def call(model, prompt, key):
    body = json.dumps({"model": model, "max_tokens": MAX_TOKENS,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        j = json.load(r)
    dt = time.time() - t0
    txt = (j["choices"][0]["message"].get("content") or "")
    u = j.get("usage") or {}
    return txt, dt, u.get("prompt_tokens"), u.get("completion_tokens")

def ensure_db(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS runs(
      id INTEGER PRIMARY KEY, ts TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS results(
      run_id INT NOT NULL, model TEXT NOT NULL, probe TEXT NOT NULL,
      score REAL, note TEXT, latency_s REAL, tok_in INT, tok_out INT,
      output TEXT, error TEXT,
      PRIMARY KEY(run_id, model, probe));
    """)

def prev_scores(c, run_id):
    row = c.execute("SELECT MAX(id) FROM runs WHERE id < ?", (run_id,)).fetchone()
    if not row or row[0] is None:
        return {}
    return {(m, p): s for m, p, s in c.execute(
        "SELECT model, probe, score FROM results WHERE run_id=?", (row[0],))}

def main():
    models = DEFAULT_MODELS
    if "--models" in sys.argv:
        models = sys.argv[sys.argv.index("--models") + 1]
    models = [m.strip() for m in models.split(",") if m.strip()]
    key = _key()
    c = sqlite3.connect(DB)
    ensure_db(c)
    cur = c.execute("INSERT INTO runs(ts) VALUES (?)",
                    (datetime.datetime.now(datetime.timezone.utc).isoformat(),))
    run_id = cur.lastrowid
    c.commit()
    prev = prev_scores(c, run_id)
    drift_lines = []
    for model in models:
        for name, prompt, scorer in PROBES:
            txt = err = None
            score = lat = ti = to = None
            note = ""
            try:
                txt, lat, ti, to = call(model, prompt, key)
                score, note = scorer(txt)
            except Exception as e:
                err = str(e)[:200]
            c.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (run_id, model, name, score, note, lat, ti, to,
                       (txt or "")[:20000], err))
            c.commit()
            tag = ""
            p = prev.get((model, name))
            if p is not None and score is not None and abs(score - p) >= 0.25:
                tag = "  DRIFT %+0.2f (prev %.2f)" % (score - p, p)
                drift_lines.append("%s/%s: %.2f -> %.2f" % (model, name, p, score))
            print("%-22s %-12s score=%-5s lat=%-7s %s%s"
                  % (model, name,
                     ("%.2f" % score) if score is not None else "ERR",
                     ("%.1fs" % lat) if lat else "-", note or (err or ""), tag))
    print("\nrun_id=%d db=%s" % (run_id, DB))
    if drift_lines:
        print("DRIFT DETECTED:\n  " + "\n  ".join(drift_lines))
    else:
        print("no drift vs previous run" if prev else "baseline run (no previous)")

if __name__ == "__main__":
    main()

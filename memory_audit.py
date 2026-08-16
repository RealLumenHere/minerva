#!/usr/bin/env python3
"""memory_audit.py — embedding-drift/integrity audit for the pgvector index.
(Minerva proposal 0658b2f0f457, wq#452, Scribe #313.)

PREMISE CORRECTIONS from build research: the research index embeds with
nomic-embed-text (768d) via local ollama, not bge-m3 as the proposal said;
and with pinned weights, re-embedding identical text is ~deterministic — so
"forgetting"/drift here is not gradual decay but a STEP CHANGE from: silent
ollama model upgrade (:latest tag!), tokenizer/truncation change, or index
corruption. Any of these silently breaks Memory V3 / ab_rag retrieval
against 10,709 stored chunks. This audit catches exactly that.

LOAD CAVEAT (measured 2026-08-10): ollama embedding is NOT bit-deterministic
under concurrent inference load — same sample re-run during a heavy bench gave
18/39 chunks at cos 0.90-0.96 vs median 1.0 when quieter (batching float
noise). MEDIAN is the robust corruption/version signal; min gets 0.85
headroom. Run the timer in a quiet window.

Method: fixed-seed sample of N chunks -> re-embed the stored text with the
same model -> cosine vs stored vector. Healthy: ~1.0. Alert (edge-triggered,
aq + ntfy) if median < MED_THRESH or min < MIN_THRESH. Also records the
ollama model digest each run so a version flip is attributable even before
similarity moves.
Storage: /home/lumen/minerva/memory_audit.db.
Timer: lumen-memory-audit.{service,timer} weekly, installed DISABLED.
"""
import datetime, json, math, os, sqlite3, subprocess, sys, urllib.request

DB = "/home/lumen/minerva/memory_audit.db"
OLLAMA = os.environ.get("EMBED_OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("AUDIT_EMBED_MODEL", "nomic-embed-text:latest")
N_SAMPLE = int(os.environ.get("AUDIT_SAMPLE", "40"))
SEED = 20260810
MED_THRESH = float(os.environ.get("AUDIT_MED_THRESH", "0.995"))
MIN_THRESH = float(os.environ.get("AUDIT_MIN_THRESH", "0.85"))
NTFY = "http://127.0.0.1:8080/lumen-alerts"

def _env():
    env = {}
    for line in open("/home/lumen/agent/.env"):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    return env

def pg(env):
    import psycopg2
    return psycopg2.connect(
        host=env["LUMEN_PGVECTOR_HOST"], port=int(env["LUMEN_PGVECTOR_PORT"]),
        dbname=env["LUMEN_PGVECTOR_DB"], user=env["LUMEN_PGVECTOR_USER"],
        password=env["LUMEN_PGVECTOR_PASSWORD"])

def embed(text):
    req = urllib.request.Request(
        f"{OLLAMA}/api/embeddings",
        data=json.dumps({"model": EMBED_MODEL,
                         "prompt": text[:7000]}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["embedding"]

def model_digest():
    try:
        req = urllib.request.Request(
            f"{OLLAMA}/api/show",
            data=json.dumps({"name": EMBED_MODEL}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        return (d.get("details", {}).get("parameter_size", "") + ":" +
                (d.get("modified_at") or ""))[:80]
    except Exception:
        return "unknown"

def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0

def db_init():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS runs(
      id INTEGER PRIMARY KEY, ts TEXT, model TEXT, digest TEXT,
      n INT, median_cos REAL, min_cos REAL, errors INT, alerted INT);""")
    c.commit()
    return c

def alert(msg):
    try:
        subprocess.run(["/home/lumen/bin/aq", "add", "memory-audit",
                        "embed-drift", msg], timeout=15)
    except Exception as e:
        print(f"aq failed: {e}", file=sys.stderr)
    try:
        urllib.request.urlopen(urllib.request.Request(
            NTFY, data=msg.encode(), method="POST"), timeout=10)
    except Exception as e:
        print(f"ntfy failed: {e}", file=sys.stderr)

def main():
    env = _env()
    conn = pg(env)
    cur = conn.cursor()
    # deterministic sample: same chunks every run (setseed + ordered random)
    cur.execute("SELECT setseed(0.42)")
    cur.execute("SELECT id, text, embedding FROM research_chunks "
                "WHERE embedding IS NOT NULL ORDER BY random() LIMIT %s",
                (N_SAMPLE,))
    rows = cur.fetchall()
    sims, errors = [], 0
    for cid, text, stored in rows:
        try:
            vec = embed(text)
            stored_v = (json.loads(stored) if isinstance(stored, str)
                        else list(stored))
            sims.append(cosine(vec, stored_v))
        except Exception as e:
            errors += 1
            print(f"chunk {cid} error: {e}", file=sys.stderr)
    conn.close()
    if not sims:
        print("no comparable chunks; aborting")
        return 1
    sims.sort()
    med = sims[len(sims) // 2]
    mn = sims[0]
    digest = model_digest()
    c = db_init()
    prev = c.execute("SELECT median_cos, digest FROM runs "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    bad = med < MED_THRESH or mn < MIN_THRESH
    was_ok = prev is None or (prev[0] is not None and prev[0] >= MED_THRESH)
    digest_flip = prev is not None and prev[1] not in (None, "unknown") \
        and digest != "unknown" and digest != prev[1]
    alerted = 0
    if (bad and was_ok) or (digest_flip and bad):
        alert(f"memory-audit: embed drift on {EMBED_MODEL} — median cos "
              f"{med:.4f} min {mn:.4f} (n={len(sims)}, digest {digest})"
              + (" MODEL VERSION CHANGED" if digest_flip else ""))
        alerted = 1
    elif digest_flip:
        alert(f"memory-audit: {EMBED_MODEL} version changed ({prev[1]} -> "
              f"{digest}) but similarity holding (median {med:.4f})")
        alerted = 1
    c.execute("INSERT INTO runs(ts,model,digest,n,median_cos,min_cos,errors,"
              "alerted) VALUES(?,?,?,?,?,?,?,?)",
              (datetime.datetime.now(datetime.timezone.utc).isoformat(),
               EMBED_MODEL, digest, len(sims), round(med, 5), round(mn, 5),
               errors, alerted))
    c.commit()
    print(f"n={len(sims)} median={med:.5f} min={mn:.5f} errors={errors} "
          f"digest={digest} alerted={alerted}")
    return 0

if __name__ == "__main__":
    sys.exit(main())

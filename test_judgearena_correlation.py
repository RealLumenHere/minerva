"""wq#1932: offline checks for judgearena_bench's Pearson/Spearman/MAE/signed-bias
metrics, the pre-wq1932 DB migration, and the Langfuse fail-open path (vendored
judgearena/langchain deps + network mocked — no live gateway needed)."""
import importlib.util, os, sqlite3, sys, tempfile
from unittest import mock

spec = importlib.util.spec_from_file_location(
    'judgearena_bench', os.path.join(os.path.dirname(__file__), 'judgearena_bench.py'))
jab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jab)

# --- bench_judge: pairing must survive a parse failure landing mid-sequence --------
# (the factory's draft diff sliced battles[:judged]/prefs[:judged], which misaligns
# human labels with pref values the moment a fail isn't at the tail — this is the
# regression the fix guards against)
battles = [
    ("i1", "a1", "b1", "model_a"),
    ("i2", "a2", "b2", "model_b"),
    ("i3", "a3", "b3", "model_a"),   # unparseable -> excluded, not at the tail
    ("i4", "a4", "b4", "model_b"),
    ("i5", "a5", "b5", "model_a"),
]
prefs = [0.1, 0.9, None, 0.8, 0.2]   # perfect predictions on the 4 parseable ones

fake_evaluate = mock.Mock()
fake_evaluate.judge_and_parse_prefs = mock.Mock(return_value=(None, None, prefs))
sys.modules['judgearena'] = mock.Mock()
sys.modules['judgearena.evaluate'] = fake_evaluate
fake_langchain_openai = mock.Mock()
fake_langchain_openai.ChatOpenAI = mock.Mock(return_value=mock.Mock())
sys.modules['langchain_openai'] = fake_langchain_openai

r = jab.bench_judge("fake-model", battles, "fake-key")
assert r["n_judged"] == 4, r
assert r["parse_fail"] == 1, r
assert r["agreement"] == 1.0, r
assert r["pearson"] > 0.95, r
assert r["spearman"] > 0.85, r
assert abs(r["mae"] - 0.15) < 1e-9, r          # mean(|.1,.1,.2,.2|)
assert abs(r["signed_bias"] - 0.0) < 1e-9, r
print("bench_judge pairing/correlation: OK")

# --- zero-variance predictions must fail open (no pearsonr on constant input) ------
fake_evaluate.judge_and_parse_prefs = mock.Mock(return_value=(None, None, [0.5, 0.5, 0.5]))
r2 = jab.bench_judge("const-model", battles[:3], "fake-key")
assert r2["pearson"] is None and r2["spearman"] is None, r2
assert r2["mae"] is not None, r2
print("bench_judge zero-variance guard: OK")

# --- db_init: self-healing migration from the live pre-wq1932 10-column schema -----
with tempfile.TemporaryDirectory() as d:
    dbpath = os.path.join(d, "minerva.db")
    old = sqlite3.connect(dbpath)
    old.execute("""CREATE TABLE judgearena_results(
      ts TEXT NOT NULL, judge_model TEXT NOT NULL, arena TEXT NOT NULL,
      seed INT, n_battles INT, n_judged INT, agreement REAL,
      parse_fail INT, avg_latency_s REAL, error TEXT,
      PRIMARY KEY(ts, judge_model))""")
    old.commit()
    old.close()
    with mock.patch.object(jab, "MINERVA_DB", dbpath):
        c = jab.db_init()
    cols = {row[1] for row in c.execute("PRAGMA table_info(judgearena_results)")}
    assert {"pearson", "spearman", "mae", "signed_bias"} <= cols, cols
    c.execute("INSERT INTO judgearena_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("t", "m", "LMArena-100k", 1, 1, 1, 1.0, 0, 1.0, 0.5, 0.5, 0.1, 0.0, None))
    c.commit()
    c.close()
print("db_init migration from pre-wq1932 schema: OK")

# --- emit_judge_metrics: fails open (no raise, no network) when creds are absent ---
with mock.patch.object(jab, "LANGFUSE_ENV", "/nonexistent/wq1932-test.env"):
    jab.emit_judge_metrics("m", "ts", {"n_judged": 1, "parse_fail": 0, "agreement": 1.0,
                                        "pearson": 0.1, "spearman": 0.1, "mae": 0.1,
                                        "signed_bias": 0.0})
print("emit_judge_metrics fail-open: OK")

assert jab.JUDGE_ALIGN_MIN_CORR == 0.3, jab.JUDGE_ALIGN_MIN_CORR
print("all judgearena_bench wq#1932 checks passed")

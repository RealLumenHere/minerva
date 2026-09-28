"""wq#2895 ratchet: no NEW sqlite connection that can outlive its function.

2026-09-28 04:36-05:11Z minerva.db was write-locked fleet-wide because a
connection opened without a try/finally close was pinned by a traceback until
GC. /home/lumen/bin/sqlite_leak_scan.py finds that class statically (connection
opened in a function, not closed in a finally/with). Existing sites are
baselined in sqlite_leak_baseline.txt (keyed file::function::var, no line
numbers, so unrelated edits don't churn it) and are being burned down by the
wq ticket named in that file's history. This test fails on any site NOT in the
baseline -- the class cannot grow -- and on a baseline entry that has been fixed
(so the baseline shrinks with the fixes instead of rotting).
"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MINERVA = os.path.dirname(HERE)
SCANNER = "/home/lumen/bin/sqlite_leak_scan.py"
BASELINE = os.path.join(HERE, "sqlite_leak_baseline.txt")


def _current():
    files = sorted(f for f in os.listdir(MINERVA) if f.endswith(".py"))
    out = subprocess.run([sys.executable, SCANNER] + files + ["v4"], cwd=MINERVA,
                         capture_output=True, text=True, check=True).stdout
    keys = set()
    for line in out.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("UNGUARDED", "NO_CLOSE"):
            continue
        path = parts[1].rsplit(":", 1)[0].replace(MINERVA + "/", "")
        if "/tests/" in path or os.path.basename(path).startswith("test_"):
            continue
        keys.add("%s::%s::%s" % (path, parts[2], parts[3]))
    return keys


def _baseline():
    with open(BASELINE) as fh:
        return {l.strip() for l in fh if l.strip() and not l.startswith("#")}


def test_scanner_positive_control(tmp_path):
    p = tmp_path / "leaky.py"
    p.write_text("import sqlite3\ndef f():\n    c = sqlite3.connect('x')\n"
                 "    c.execute('SELECT 1')\n    c.close()\n")
    out = subprocess.run([sys.executable, SCANNER, str(p)], capture_output=True, text=True).stdout
    assert "UNGUARDED" in out, "scanner no longer detects the class -- the ratchet is blind"


def test_no_new_unguarded_sqlite_connections():
    new = sorted(_current() - _baseline())
    assert not new, ("new sqlite connection(s) not closed on every exit path -- wrap in "
                     "try/finally close (or contextlib.closing / Kernel.tx):\n  " + "\n  ".join(new))


def test_baseline_shrinks_with_fixes():
    fixed = sorted(_baseline() - _current())
    assert not fixed, ("these baseline entries are fixed -- delete them from "
                       "sqlite_leak_baseline.txt:\n  " + "\n  ".join(fixed))

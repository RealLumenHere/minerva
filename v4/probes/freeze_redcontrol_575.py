#!/usr/bin/env python3
"""wq#575 RED CONTROL for freeze_verify_575.py.

AUDIT DISCIPLINE: a clean battery is not trusted until it has gone RED on a
planted known-positive.

Planted positive: an add_fact() that reports a SUCCESSFUL write (i.e. the freeze
was never applied / was reverted). The live file is never touched; the patch is
in-process only. `FROZEN` is deliberately left True so no bulk writer is armed —
an earlier revision of this control cleared it and ran seed_facts for real
(no rows were written, verified 30/30 points byte-identical, but the pattern was
too close to the edge to keep).

Expected: F1 FAIL; F2/F3/F4/F5/F7/F8 PASS; battery exit 1.
Exit 0 here means the control behaved as designed (the battery CAN go red).
"""
import json, sys, importlib

OUT = "/tmp/mv4-w575-05322f/freeze_verify_REDCONTROL.json"
sys.argv = ["freeze_verify_575", OUT]          # MUST precede the import: the
                                               # battery binds OUT at import time.
sys.path.insert(0, "/home/lumen/mem0")
sys.path.insert(0, "/home/lumen/minerva/v4/probes")

import lumen_mem0 as L

_real_add = L.add_fact
L.add_fact = lambda text, user_id="lumen", meta=None: {
    "status": "ok", "written": True, "results": [{"id": "planted-positive"}]}

fv = importlib.import_module("freeze_verify_575")
assert fv.OUT == OUT, "red control did not redirect the battery artifact (got %r)" % fv.OUT
rc = fv.main()
L.add_fact = _real_add

res = json.load(open(OUT))
by = {c["check"]: c["pass"] for c in res["checks"]}
must_fail = ["F1_write_frozen_failsoft"]
must_pass = ["F2_search_facts_reads", "F3_router_fetch_context",
             "F4_agent_memory_context", "F5_a5_memory_context",
             "F7_point_count_unchanged", "F8_seed_facts_refuses"]

ok = (rc == 1)
print("\n--- RED CONTROL ASSERTIONS ---")
print("battery exit was nonzero:", "PASS" if ok else "FAIL (rc=%r)" % rc)
for c in must_fail:
    good = by.get(c) is False
    ok = ok and good
    print("  %-32s expected FAIL -> %s" % (c, "RED as designed" if good else "DID NOT GO RED"))
for c in must_pass:
    good = by.get(c) is True
    ok = ok and good
    print("  %-32s expected PASS -> %s" % (c, "ok" if good else "unexpectedly red"))
print("points before/after:", res.get("point_count_before"), res.get("point_count_after"))
print("\nRED CONTROL:", "VALID (battery detects an unfrozen store)" if ok else "INVALID")
sys.exit(0 if ok else 1)

#!/usr/bin/env python3
"""wq#575 FREEZE verification battery.

Proves, against live state:
  F1  write path is neutered and FAIL-SOFT (sentinel, no raise)
  F2  reads still serve through lumen_mem0.search_facts
  F3  reads still serve through the router fetch_context('facts')
  F4  reads still serve through the REAL agent import path (_memory_context)
  F5  a5_common.memory_context still serves
  F6  the agent loop survives repeated write attempts
  F7  Qdrant point_count unchanged across the whole battery (freeze held)
  F8  bulk writer (seed_facts.main) refuses up front

Exit 0 only if every check passes. Writes JSON to the path given as argv[1].
"""
import json, sys, time, traceback, urllib.request

RESULTS = {"checks": [], "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mv4-w575-05322f/freeze_verify.json"


def rec(name, ok, detail=""):
    RESULTS["checks"].append({"check": name, "pass": bool(ok), "detail": str(detail)[:600]})
    print(("PASS " if ok else "FAIL ") + name + " :: " + str(detail)[:300], flush=True)
    return ok


def qdrant_count(coll="lumen_facts"):
    with urllib.request.urlopen("http://127.0.0.1:6333/collections/" + coll, timeout=10) as r:
        return json.load(r)["result"]["points_count"]


def main():
    sys.path.insert(0, "/home/lumen/mem0")
    sys.path.insert(0, "/home/lumen/agent")
    sys.path.insert(0, "/home/lumen/autonomy/agent5")
    allok = True

    count_before = qdrant_count()
    RESULTS["point_count_before"] = count_before
    print("lumen_facts point_count BEFORE:", count_before, flush=True)

    import lumen_mem0 as L
    try:
        r = L.add_fact("wq575 freeze probe: this must never be stored.", user_id="lumen")
        ok = isinstance(r, dict) and r.get("status") == "FROZEN" and r.get("written") is False
        allok &= rec("F1_write_frozen_failsoft", ok, r)
    except Exception as e:
        allok &= rec("F1_write_frozen_failsoft", False, "RAISED (not fail-soft): %r" % e)

    try:
        t = time.time()
        hits = L.search_facts("what RAM does the T5810 have", user_id="lumen", k=3)
        allok &= rec("F2_search_facts_reads", bool(hits),
                     "%d hits in %.1fs; first=%s" % (len(hits or []), time.time() - t,
                                                     str((hits or [{}])[0].get("memory", ""))[:120]))
    except Exception:
        allok &= rec("F2_search_facts_reads", False, traceback.format_exc()[-400:])

    try:
        import lumen_memory_router as R
        q = "what is Isis's tailscale IP?"
        route = R.route(q)
        ctx = R.fetch_context(q, route["tier"], k=4) or ""
        allok &= rec("F3_router_fetch_context", route["tier"] == "facts" and len(ctx) > 0,
                     "tier=%s len=%d head=%s" % (route["tier"], len(ctx), ctx[:120].replace("\n", " | ")))
    except Exception:
        allok &= rec("F3_router_fetch_context", False, traceback.format_exc()[-400:])

    try:
        import lumen_agent_v4 as A
        t = time.time()
        ctx = A._memory_context("smoke")
        allok &= rec("F4_agent_memory_context", isinstance(ctx, str) and len(ctx) > 0,
                     "len=%d in %.1fs head=%s" % (len(ctx), time.time() - t, ctx[:120].replace("\n", " | ")))
    except Exception:
        allok &= rec("F4_agent_memory_context", False, traceback.format_exc()[-400:])

    try:
        import a5_common as A5
        ctx5 = A5.memory_context("is the cerebras fleet service running?")
        allok &= rec("F5_a5_memory_context", isinstance(ctx5, str) and len(ctx5) > 0,
                     "len=%d" % len(ctx5))
    except Exception:
        allok &= rec("F5_a5_memory_context", False, traceback.format_exc()[-400:])

    try:
        survived = 0
        for i in range(5):
            L.add_fact("loop probe %d" % i, user_id="lumen")
            survived += 1
        allok &= rec("F6_caller_loop_survives_writes", survived == 5,
                     "%d/5 iterations completed" % survived)
    except Exception as e:
        allok &= rec("F6_caller_loop_survives_writes", False, "loop died: %r" % e)

    try:
        import seed_facts as S
        rc = S.main()
        allok &= rec("F8_seed_facts_refuses", rc == 2, "return=%r" % rc)
    except Exception:
        allok &= rec("F8_seed_facts_refuses", False, traceback.format_exc()[-400:])

    count_after = qdrant_count()
    RESULTS["point_count_after"] = count_after
    allok &= rec("F7_point_count_unchanged", count_after == count_before,
                 "before=%d after=%d" % (count_before, count_after))

    RESULTS["all_pass"] = bool(allok)
    RESULTS["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(OUT, "w") as fh:
        json.dump(RESULTS, fh, indent=2)
    print("\nVERDICT:", "ALL PASS" if allok else "FAILURES PRESENT", "->", OUT, flush=True)
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())

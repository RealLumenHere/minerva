#!/usr/bin/env python3
"""wq#575 step 5 — nothing-unique evidence for the frozen Mem0/Qdrant store.

Enumerates lumen_facts (now safely read-only) and, for each fact, asks whether
minerva.db already carries the same knowledge, via BOTH substrates:
  * archival_notes / recall_fts  -> lumen_recall2.search_v3
  * state_facts                  -> token overlap against subject/predicate/value

Classification is deliberately conservative: anything that is not clearly covered
is reported as CANDIDATE-UNIQUE with its verbatim text, because the cost of a
false "covered" is deleting knowledge and the cost of a false "unique" is Frank
reading one extra line. RETIREMENT IS NOT THIS TICKET'S CALL.
"""
import json, re, sys, urllib.request
sys.path.insert(0, "/home/lumen/minerva")
import lumen_recall2 as lr

STOP = set("the a an is are was were of to in on for and or with by at it its this that has have "
           "be been as from not no都 all any use used using via per than then there their they".split())


def toks(s):
    return {w for w in re.findall(r"[a-z0-9._#-]+", (s or "").lower()) if w not in STOP and len(w) > 2}


def facts():
    req = urllib.request.Request(
        "http://127.0.0.1:6333/collections/lumen_facts/points/scroll",
        data=json.dumps({"limit": 1000, "with_payload": True, "with_vector": False}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["result"]["points"]


def state_rows():
    import sqlite3
    c = sqlite3.connect("file:/home/lumen/minerva/minerva.db?mode=ro", uri=True)
    try:
        return [dict(subject=s, predicate=p, value=v) for s, p, v in c.execute(
            "SELECT subject,predicate,value FROM state_facts WHERE valid_to IS NULL")]
    finally:
        c.close()


def main():
    pts = facts()
    st = state_rows()
    st_tok = [(r, toks(r["subject"] + " " + r["predicate"] + " " + str(r["value"]))) for r in st]
    out = []
    for p in pts:
        text = (p.get("payload") or {}).get("data") or (p.get("payload") or {}).get("memory") or ""
        ft = toks(text)
        hits = lr.search_v3(text[:300], top_k=3) or []
        best_note, best_note_ov = None, 0.0
        for h in hits:
            ov = len(ft & toks((h.get("title") or "") + " " + (h.get("content") or ""))) / max(1, len(ft))
            if ov > best_note_ov:
                best_note_ov, best_note = ov, h
        best_state, best_state_ov = None, 0.0
        for r, rt in st_tok:
            ov = len(ft & rt) / max(1, len(ft))
            if ov > best_state_ov:
                best_state_ov, best_state = ov, r
        cover = max(best_note_ov, best_state_ov)
        verdict = ("COVERED" if cover >= 0.60 else
                   "PARTIAL" if cover >= 0.35 else "CANDIDATE-UNIQUE")
        out.append({"id": str(p["id"]), "text": text, "verdict": verdict,
                    "note_overlap": round(best_note_ov, 3),
                    "note_title": (best_note or {}).get("title"),
                    "state_overlap": round(best_state_ov, 3),
                    "state_subject": (best_state or {}).get("subject"),
                    "state_predicate": (best_state or {}).get("predicate")})
        print("%-17s note=%.2f state=%.2f | %s" % (verdict, best_note_ov, best_state_ov, text[:80]),
              flush=True)
    json.dump({"facts": out, "state_facts_current": len(st)},
              open("/tmp/mv4-w575-05322f/mem0_diff.json", "w"), indent=2)
    from collections import Counter
    print("\n", Counter(o["verdict"] for o in out))
    return out


if __name__ == "__main__":
    main()

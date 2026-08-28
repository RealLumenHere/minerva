#!/usr/bin/env python3
"""continual_learn.py — Lumen learns from its own WORK EPISODES (not messenger.db).

Deterministic writer arm of the continual-learning skill (wq#1073). Reads NEW
episodes since a rowid high-water mark -> durable operator PREFERENCES + workspace
FACTS via a NON-messenger LiteLLM rung (fail-closed) -> preferences appended to the
learned ledger AGENTS_learned.md (auto, non-authoritative), facts appended to the
Minerva review file for Frank to ingest (NEVER auto-supersede state). claude_memory.md,
instincts.yaml and the state store are proposal-only and untouched. Empty is valid;
NEVER invent. Mirrors lumen_convo_learn.py's shape; source is the episode store.
"""
import os, re, sys, json, sqlite3, subprocess, urllib.request, difflib
from datetime import datetime, timezone, timedelta
from pathlib import Path

MINERVA="/home/lumen/minerva"
DB=f"{MINERVA}/minerva.db"
HWM=Path(MINERVA)/"continual_learn.hwm"
ENV_FILE=Path("/home/lumen/agent/.env")
LEDGER=Path("/home/lumen/personas/AGENTS_learned.md")   # auto, non-authoritative
CANDID=Path(MINERVA)/"dream"/"convo-fact-candidates.md"  # Frank review; never auto-supersede state
LITELLM=os.getenv("LITELLM_BASE_URL","http://127.0.0.1:4000")
MODEL=os.getenv("CONTINUAL_LEARN_MODEL","deepseek-v4-flash")
assert not MODEL.startswith("messenger"), "continual-learn must NOT use messenger-* rung"
SELF_KIND="continual-learn"
FIRST_RUN_CAP=50
SECTION_CAP=12
CHAR_BUDGET=12000
MAXTOK=2500
NTFY="https://ntfy.sh/lumen-alerts-96d6a98d01989127"
OFF_LIMITS=re.compile(r'surface\s*pro|refurb', re.I)
PREF_HDR="## Learned Operator Preferences"
FACT_HDR="## Learned Workspace Facts"

SYS=("You extract durable OPERATIONAL memory from a homelab operator's work-session episodes. "
"The operator is Frank; episodes are structured post-mortems (summary/changed/worked/failed/why).\n"
"Extract ONLY NET-NEW, NON-OBVIOUS, durable items, split into two kinds:\n"
"  PREFERENCES: how Frank wants work done (response style, autonomy, verification posture, "
"delegation, process, review/merge conventions). Recurring corrections count.\n"
"  FACTS: stable workspace/fleet facts (roles, topology, decisions, config that persists).\n"
"DO NOT record: transient status (a ticket in-flight, a story-mode frontier), one-off task detail, "
"obvious basics, secrets, or anything about a Surface Pro refurbishment business.\n"
"Prefer items seen across 2+ episodes. If nothing durable, return empty lists. NEVER invent or pad.\n"
'Return ONLY strict JSON, no prose/fences:\n'
'{"preferences":["..."],"facts":["..."]}')

def key():
    if os.getenv("LITELLM_MASTER_KEY"): return os.environ["LITELLM_MASTER_KEY"]
    for ln in ENV_FILE.read_text().splitlines():
        if ln.strip().startswith("LITELLM_MASTER_KEY="): return ln.split("=",1)[1].strip().strip('"\'')
    return ""

def hwm_get():
    try: return int(HWM.read_text().strip())
    except Exception: return 0

def rows_since(mark):
    c=sqlite3.connect(f"file:{DB}?mode=ro",uri=True)
    if mark<=0:
        r=c.execute(f"SELECT rowid,created_at,kind,title,content FROM archival_notes "
                    f"WHERE kind IS NULL OR kind!='{SELF_KIND}' ORDER BY rowid DESC LIMIT ?",
                    (FIRST_RUN_CAP,)).fetchall()[::-1]
    else:
        r=c.execute(f"SELECT rowid,created_at,kind,title,content FROM archival_notes "
                    f"WHERE rowid>? AND (kind IS NULL OR kind!='{SELF_KIND}') ORDER BY rowid",(mark,)).fetchall()
    c.close(); return r

def transcript(rows):
    acc=[]; tot=0
    for rid,ca,kind,title,content in rows:
        body=" ".join(((title or "")+" — "+(content or "")).split())
        line=f"[{kind}] {body[:900]}"
        if tot+len(line)>CHAR_BUDGET and acc: break
        acc.append(line); tot+=len(line)
    return "\n".join(acc)

def ask(user):
    body=json.dumps({"model":MODEL,"max_tokens":MAXTOK,"temperature":0,
        "messages":[{"role":"system","content":SYS},{"role":"user","content":user}]}).encode()
    req=urllib.request.Request(f"{LITELLM}/v1/chat/completions",data=body,
        headers={"Content-Type":"application/json","Authorization":f"Bearer {key()}"})
    with urllib.request.urlopen(req,timeout=120) as r: d=json.loads(r.read())
    return d["choices"][0]["message"].get("content") or ""

def parse(txt):
    s=txt.strip()
    if not s: raise ValueError("empty content")
    if s.startswith("```"): s=re.sub(r'^```\w*|```$','',s,flags=re.M).strip()
    return json.loads(s[s.find("{"):s.rfind("}")+1])

def clean(items):
    out=[]
    for x in items or []:
        t=(x if isinstance(x,str) else str(x)).strip()
        if t and not OFF_LIMITS.search(t): out.append(t)
    return out

def alert(m):
    try: urllib.request.urlopen(urllib.request.Request(NTFY,data=m.encode(),
        headers={"Title":"continual-learn FAILED","Priority":"high"}),timeout=8)
    except Exception: pass

def merge_ledger(prefs):
    if not LEDGER.exists():
        LEDGER.write_text(f"{PREF_HDR}\n\n{FACT_HDR}\n")
    txt=LEDGER.read_text()
    if PREF_HDR not in txt: txt=f"{PREF_HDR}\n\n"+txt
    if FACT_HDR not in txt: txt=txt.rstrip()+f"\n\n{FACT_HDR}\n"
    # split into pref-section and the rest (facts live in candidates file, not here, but keep header)
    pi=txt.index(PREF_HDR)
    fi=txt.index(FACT_HDR)
    pref_block=txt[pi+len(PREF_HDR):fi]
    tail=txt[fi:]
    existing=[l.strip() for l in pref_block.splitlines() if l.strip().startswith("- ")]
    norm=[re.sub(r"\W+"," ",e).lower().strip() for e in existing]
    added=[]
    for p in prefs:
        n=re.sub(r"\W+"," ",p).lower().strip()
        if any(difflib.SequenceMatcher(None,n,e).ratio()>0.8 for e in norm): continue
        existing.append(f"- {p}"); norm.append(n); added.append(p)
    existing=existing[-SECTION_CAP:]
    new=f"{PREF_HDR}\n\n"+"\n".join(existing)+"\n\n"+tail.rstrip()+"\n"
    LEDGER.write_text(new)
    kept=[p for p in added if f"- {p}" in existing]  # survivors after the section cap
    return kept

def append_facts(facts,mark_lo,mark_hi,stamp):
    if not facts: return []
    CANDID.parent.mkdir(parents=True,exist_ok=True)
    if not CANDID.exists():
        CANDID.write_text("# Fleet-fact candidates for Frank review (never auto-supersede state)\n\n")
    prior=CANDID.read_text()
    pnorm=[re.sub(r"\W+"," ",l).lower().strip() for l in prior.splitlines() if l.strip().startswith("- ")]
    fresh=[f for f in facts if not any(difflib.SequenceMatcher(None,re.sub(r"\W+"," ",f).lower().strip(),e).ratio()>0.8 for e in pnorm)]
    if not fresh: return []
    with open(CANDID,"a") as af:
        af.write(f"\n## {stamp} (episodes {mark_lo}-{mark_hi})  [continual-learn]\n")
        for f in fresh: af.write(f"- {f}\n")
    return fresh

def main():
    dry="--dry-run" in sys.argv
    mark=hwm_get()
    rows=rows_since(mark)
    if not rows: print(f"no new episodes since rowid={mark}."); return 0
    maxid=max(r[0] for r in rows); lo=min(r[0] for r in rows)
    tx=transcript(rows)
    data=None; last=None
    for _ in range(2):  # one retry: guards a transient empty/non-JSON gateway response
        try: data=parse(ask("EPISODES (oldest first):\n\n"+tx)); break
        except Exception as e: last=e
    if data is None:
        alert(f"continual-learn failed: {last}")
        print(f"EXTRACTION FAILED (marker NOT advanced): {last}",file=sys.stderr); return 2
    prefs=clean(data.get("preferences")); facts=clean(data.get("facts"))
    now=datetime.now(timezone.utc)-timedelta(hours=7); stamp=now.strftime("%Y-%m-%d %H:%M PT")
    if dry:
        print(f"=== DRY (episodes {lo}-{maxid}, {len(rows)} rows, {len(tx)} chars) ===")
        print(f"PREFERENCES ({len(prefs)}):");  [print("  -",p) for p in prefs]
        print(f"FACTS ({len(facts)}):");        [print("  -",f) for f in facts]
        if not prefs and not facts: print("  (no durable items — empty is valid)")
        return 0
    added_p=merge_ledger(prefs)
    added_f=append_facts(facts,lo,maxid,stamp)
    HWM.write_text(str(maxid))
    if not prefs and not facts:
        print("No high-signal memory updates."); return 0
    print(f"continual-learn: ledger +{len(added_p)} pref(s), candidates +{len(added_f)} fact(s), "
          f"episodes {lo}-{maxid}, marker={maxid}")
    return 0
sys.exit(main())

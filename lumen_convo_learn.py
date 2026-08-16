#!/usr/bin/env python3
"""lumen_convo_learn.py — Lumen learns from messenger CONVERSATIONS.
NEW messages since a high-water mark -> durable operator-stated learnings via a
NON-messenger LiteLLM rung (fail-closed) -> EPISODE (kind=convo-learn), which flows
into worklog.md and the weekly reflector. Fleet-FACT candidates -> Frank review file
(never auto-supersede state). Empty is valid; NEVER invent; persona roleplay ignored.
"""
import os, re, sys, json, sqlite3, subprocess, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

MSG_DB="/home/lumen/messenger/data/messenger.db"
MINERVA="/home/lumen/minerva"
HWM=Path(MINERVA)/"convo_learn.hwm"
ENV_FILE=Path("/home/lumen/agent/.env")
REPORTS=Path(MINERVA)/"dream"/"convo-learnings"
PROPOSALS=Path(MINERVA)/"dream"/"convo-fact-candidates.md"
LEARNED=Path("/home/lumen/personas/worklog_learned.md")  # auto-retained, surfaced to Lumen (non-authoritative)
ARCHIVE=Path(MINERVA)/"learnings.jsonl"  # FULL durable archive (to 11): every learning, unbounded, backed up
AUTO_TYPES={"fact","decision","correction","preference","project_update"}
AUTO_CONF=0.6
LEDGER_CAP=30
LITELLM=os.getenv("LITELLM_BASE_URL","http://127.0.0.1:4000")
MODEL=os.getenv("CONVO_LEARN_MODEL","deepseek-v4-flash")
assert not MODEL.startswith("messenger"), "convo-learn must NOT use messenger-* rung"
CHAR_BUDGET=9000
MAXTOK=2500
NTFY="https://ntfy.sh/lumen-alerts-96d6a98d01989127"
OFF_LIMITS=re.compile(r'surface\s*pro|refurb', re.I)

SYS=("You extract durable OPERATIONAL memory from a homelab operator's chat transcript. The OPERATOR is "
"Frank; the ASSISTANT replies in a FICTIONAL crusty-veterinarian persona (ranch/vet metaphors, backstory, "
"griping) — that is ROLEPLAY, never fact.\n"
"Extract ONLY NET-NEW, NON-OBVIOUS, durable things the OPERATOR stated or confirmed about his fleet, "
"hardware, services, projects, config, or how he wants work done: facts, decisions, preferences, "
"corrections, project updates, open tasks.\n"
"DO NOT record: the assistant's persona/backstory/metaphors; greetings/jokes/small talk; interpersonal "
"conflict, venting, insults, or offensive language; obvious basics already known (that Frank has a "
"homelab, his name, etc.); one-off questions that were simply answered.\n"
"If nothing durable was stated, return an EMPTY learnings list. Empty is correct — NEVER invent, infer, or pad.\n"
'Return ONLY strict JSON, no prose/fences:\n'
'{"summary":"<=2 sentences, or empty","learnings":[{"type":"fact|decision|preference|correction|open_task|project_update","text":"...","confidence":0.0}]}')

def key():
    if os.getenv("LITELLM_MASTER_KEY"): return os.environ["LITELLM_MASTER_KEY"]
    for ln in ENV_FILE.read_text().splitlines():
        if ln.strip().startswith("LITELLM_MASTER_KEY="): return ln.split("=",1)[1].strip().strip('"\'')
    return ""

def hwm_get():
    try: return int(HWM.read_text().strip())
    except Exception: return 0

def rows_since(since, window=None):
    c=sqlite3.connect(f"file:{MSG_DB}?mode=ro",uri=True)
    if window:
        r=c.execute("SELECT id,is_user,text FROM messages ORDER BY id DESC LIMIT ?", (window,)).fetchall()[::-1]
    else:
        r=c.execute("SELECT id,is_user,text FROM messages WHERE id>? ORDER BY id", (since,)).fetchall()
    c.close(); return r

def transcript(rows):
    # newest-within-budget: keep most recent messages up to CHAR_BUDGET
    acc=[]; tot=0
    for _id,isu,txt in reversed(rows):
        who="OPERATOR" if isu else "ASSISTANT"
        t=" ".join((txt or "").split())
        if not t: continue
        line=f"{who}: {t[:1000]}"
        if tot+len(line)>CHAR_BUDGET and acc: break
        acc.append(line); tot+=len(line)
    return "\n".join(reversed(acc))

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

def norm(learns):
    out=[]
    for l in learns or []:
        if isinstance(l,str): l={"type":"fact","text":l,"confidence":0.5}
        t=(l.get("text") or "").strip()
        if t and not OFF_LIMITS.search(t): out.append({"type":l.get("type","fact"),"text":t,"confidence":l.get("confidence",0.5)})
    return out

def alert(m):
    try: urllib.request.urlopen(urllib.request.Request(NTFY,data=m.encode(),
        headers={"Title":"convo-learn FAILED","Priority":"high"}),timeout=8)
    except Exception: pass

def ledger_append(items):
    import difflib
    LEARNED.parent.mkdir(parents=True, exist_ok=True)
    existing=[x for x in (LEARNED.read_text().splitlines() if LEARNED.exists() else []) if x.strip()]
    norm=[re.sub(r"\W+"," ",x).lower().strip() for x in existing]
    added=[]
    for l in items:
        line=f"- {l['text']}"; n=re.sub(r"\W+"," ",l["text"]).lower().strip()
        if any(difflib.SequenceMatcher(None,n,e).ratio()>0.8 for e in norm): continue
        existing.append(line); norm.append(n); added.append(line)
    if added:
        LEARNED.write_text("\n".join(existing[-LEDGER_CAP:])+"\n")
    return added

def main():
    dry="--dry-run" in sys.argv or "--window" in sys.argv
    window=None
    if "--window" in sys.argv:
        try: window=int(sys.argv[sys.argv.index("--window")+1])
        except Exception: window=30
    since=hwm_get()
    rows=rows_since(since, window)
    if not rows: print(f"no new messages since id={since}."); return 0
    maxid=rows[-1][0]; tx=transcript(rows)
    if len(tx)<40:
        if not dry: HWM.write_text(str(maxid))
        print("no substantive text; hwm advanced."); return 0
    try: data=parse(ask("TRANSCRIPT (oldest first):\n\n"+tx))
    except Exception as e:
        alert(f"convo-learn failed: {e}")
        print(f"EXTRACTION FAILED (hwm NOT advanced): {e}", file=sys.stderr); return 2
    learns=norm(data.get("learnings")); summary=(data.get("summary") or "").strip()
    now=datetime.now(timezone.utc)-timedelta(hours=7); stamp=now.strftime("%Y-%m-%d %H:%M PT")
    if dry:
        print(f"=== {'WINDOW' if window else 'DRY'} (msgs ..{maxid}, {len(rows)} rows, {len(tx)} chars) ===")
        print("summary:", summary or "(none)")
        for l in learns: print(f"  [{l['type']}|{l['confidence']}] {l['text']}")
        if not learns: print("  (no durable learnings — empty is valid)")
        return 0
    if learns:
        lines="\n".join(f"- [{l['type']}] {l['text']} (conf {l['confidence']})" for l in learns)
        subprocess.run(["python3",f"{MINERVA}/lumen_episode.py","commit",
            "--summary",(summary or "conversation learnings")[:200],"--changed","(none - memory only)",
            "--worked",f"{len(learns)} learnings","--why","learn from every messenger conversation",
            "--kind","convo-learn","--tags","messenger,convo-learn,memory","--ref",f"messenger:{since+1}-{maxid}"],
            env={**os.environ,"LUMEN_MEM_ORIGIN":"convo-learn","LUMEN_MEM_TRUST":"external"},check=False)  # P5 provenance
        REPORTS.mkdir(parents=True,exist_ok=True)
        (REPORTS/f"{now.strftime('%Y-%m-%d')}.md").write_text(f"# convo-learn {stamp} (msgs {since+1}-{maxid})\n\n{summary}\n\n{lines}\n")
        # ARCHIVE (knob2=11): every learning, durable + backed up (critical restic + 2TB rootfs clone)
        with open(ARCHIVE,"a") as af:
            for l in learns:
                af.write(json.dumps({"ts":now.isoformat(),"from":since+1,"to":maxid,"type":l["type"],"confidence":l["confidence"],"text":l["text"]})+"\n")
        print(f"  archive += {len(learns)} -> {ARCHIVE}")
        auto=[l for l in learns if l["type"] in AUTO_TYPES and (l["confidence"] or 0)>=AUTO_CONF]
        if auto:
            got=ledger_append(auto)
            if got: print(f"  ledger += {len(got)} learned fact(s)")
    HWM.write_text(str(maxid))
    print(f"convo-learn: {len(learns)} learnings, msgs {since+1}-{maxid}, hwm={maxid}"+(" (episode committed)" if learns else ""))
    return 0
sys.exit(main())

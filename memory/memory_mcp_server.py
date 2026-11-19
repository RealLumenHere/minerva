#!/usr/bin/env python3
"""memory_mcp_server.py — MCP server over the canonical fleet_memory DB (wq#3647).

Exposes (ticket-specified): memory.search, memory.get_device, memory.get_decisions,
memory.record_episode, memory.propose_fact.

Transport: newline-delimited JSON-RPC 2.0 on stdio (MCP stdio transport).
Also supports:
  --selftest     run every tool against the live DB and print results (no client needed)
  --call NAME --args '<json>'   invoke one tool and print its JSON result

Design note: propose_fact writes status='proposed' (agents propose; a trusted
reconciler promotes to 'verified'). Every write appends a memory_events row.
"""
import argparse
import datetime as dt
import json
import sys

import fm_db

PROTO = "2024-11-05"
SERVER_INFO = {"name": "fleet-memory", "version": "1.0.0"}


# ---------------- tool implementations ----------------
def t_search(conn, query, limit=20):
    like = "%" + (query or "") + "%"
    cur = conn.cursor()
    res = {"query": query, "facts": [], "decisions": [], "devices": [], "episodes": []}
    cur.execute(
        """SELECT subject,attribute,value FROM fleet_facts
           WHERE status='verified'
             AND (coalesce(subject,'')||' '||coalesce(attribute,'')||' '||coalesce(value,'')) ILIKE %s
           ORDER BY similarity(coalesce(subject,'')||' '||coalesce(attribute,'')||' '||coalesce(value,''), %s) DESC
           LIMIT %s""", (like, query or "", limit))
    res["facts"] = [{"subject": a, "attribute": b, "value": c} for a, b, c in cur.fetchall()]
    cur.execute(
        """SELECT id,scope,decision,rationale,status FROM fleet_decisions
           WHERE (coalesce(decision,'')||' '||coalesce(rationale,'')) ILIKE %s
           ORDER BY (status='active') DESC, id LIMIT %s""", (like, limit))
    res["decisions"] = [{"id": i, "scope": s, "decision": d, "rationale": r, "status": st}
                        for i, s, d, r, st in cur.fetchall()]
    cur.execute(
        """SELECT hostname,role,ip_tailscale,ip_lan,status FROM fleet_devices
           WHERE (coalesce(hostname,'')||' '||coalesce(role,'')||' '||coalesce(notes,'')) ILIKE %s
           ORDER BY hostname LIMIT %s""", (like, limit))
    res["devices"] = [{"hostname": h, "role": r, "ip_tailscale": t, "ip_lan": l, "status": s}
                      for h, r, t, l, s in cur.fetchall()]
    cur.execute(
        """SELECT ticket_id,agent,result,summary FROM agent_episodes
           WHERE (coalesce(summary,'')||' '||coalesce(ticket_id,'')) ILIKE %s
           ORDER BY COALESCE(finished_at,started_at) DESC NULLS LAST, id DESC LIMIT %s""",
        (like, limit))
    res["episodes"] = [{"ticket_id": t, "agent": a, "result": r, "summary": s}
                       for t, a, r, s in cur.fetchall()]
    cur.close()
    return res


def t_get_device(conn, hostname):
    cur = conn.cursor()
    hn = (hostname or "").strip().lower()
    cur.execute("""SELECT hostname,role,architecture,ip_tailscale,ip_lan,os,status,
                          location,power_profile,notes FROM fleet_devices
                   WHERE lower(hostname)=%s""", (hn,))
    row = cur.fetchone()
    if not row:
        cur.close()
        return {"hostname": hostname, "found": False}
    cols = ("hostname", "role", "architecture", "ip_tailscale", "ip_lan", "os",
            "status", "location", "power_profile", "notes")
    dev = dict(zip(cols, row))
    dev["found"] = True
    cur.execute("""SELECT attribute,value FROM fleet_facts
                   WHERE lower(subject)=%s AND status='verified'
                   ORDER BY attribute""", (hn,))
    dev["facts"] = [{"attribute": a, "value": v} for a, v in cur.fetchall()]
    cur.close()
    return dev


def t_get_decisions(conn, scope=None, status="active"):
    cur = conn.cursor()
    q = "SELECT id,scope,decision,rationale,status,created_at FROM fleet_decisions WHERE 1=1"
    params = []
    if scope:
        q += " AND scope=%s"; params.append(scope)
    if status:
        q += " AND status=%s"; params.append(status)
    q += " ORDER BY scope, id"
    cur.execute(q, params)
    out = [{"id": i, "scope": s, "decision": d, "rationale": r, "status": st,
            "created_at": c.isoformat() if c else None}
           for i, s, d, r, st, c in cur.fetchall()]
    cur.close()
    return {"count": len(out), "decisions": out}


def t_record_episode(conn, ticket_id=None, agent=None, result=None, summary=None,
                     files_changed=None, commit_sha=None, tests=None,
                     failure_reason=None, started_at=None, finished_at=None):
    cur = conn.cursor()
    fin = finished_at or dt.datetime.now(dt.timezone.utc).isoformat()
    cur.execute(
        """INSERT INTO agent_episodes
             (ticket_id,agent,started_at,finished_at,result,summary,files_changed,commit_sha,tests,failure_reason)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (ticket_id) DO UPDATE SET
             agent=COALESCE(EXCLUDED.agent, agent_episodes.agent),
             finished_at=EXCLUDED.finished_at, result=EXCLUDED.result,
             summary=EXCLUDED.summary, files_changed=EXCLUDED.files_changed,
             commit_sha=EXCLUDED.commit_sha, tests=EXCLUDED.tests,
             failure_reason=EXCLUDED.failure_reason
           RETURNING id""",
        (ticket_id, agent, started_at, fin, result, summary, files_changed,
         commit_sha, tests, failure_reason))
    eid = cur.fetchone()[0]
    fm_db.log_event(cur, agent or "mcp", "record", "agent_episode", eid, None,
                    {"ticket_id": ticket_id, "result": result}, "memory.record_episode")
    conn.commit()
    cur.close()
    return {"id": eid, "ticket_id": ticket_id, "status": "recorded"}


def t_propose_fact(conn, subject, attribute, value, value_type="text",
                   source=None, confidence=0.8, created_by="mcp-propose"):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO fleet_facts
             (subject,attribute,value,value_type,source,confidence,status,created_by)
           VALUES (%s,%s,%s,%s,%s,%s,'proposed',%s) RETURNING id""",
        (subject, attribute, value, value_type, source, confidence, created_by))
    fid = cur.fetchone()[0]
    fm_db.log_event(cur, created_by, "propose", "fleet_fact", fid, None,
                    {"subject": subject, "attribute": attribute, "value": value},
                    "memory.propose_fact (awaiting reconciler promotion)")
    conn.commit()
    cur.close()
    return {"id": fid, "status": "proposed",
            "note": "status=proposed; a trusted reconciler promotes to verified"}


# ---------------- MCP tool registry + dispatch ----------------
TOOLS = [
    {"name": "memory.search",
     "description": "Full-text search across fleet facts, decisions, devices and episodes.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["query"]}},
    {"name": "memory.get_device",
     "description": "Get one device's roster row plus all its verified facts.",
     "inputSchema": {"type": "object", "properties": {
         "hostname": {"type": "string"}}, "required": ["hostname"]}},
    {"name": "memory.get_decisions",
     "description": "List standing decisions, optionally filtered by scope/status.",
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string"}, "status": {"type": "string"}}}},
    {"name": "memory.record_episode",
     "description": "Record (upsert) an agent work episode by ticket_id.",
     "inputSchema": {"type": "object", "properties": {
         "ticket_id": {"type": "string"}, "agent": {"type": "string"},
         "result": {"type": "string"}, "summary": {"type": "string"},
         "files_changed": {"type": "string"}, "commit_sha": {"type": "string"},
         "tests": {"type": "string"}, "failure_reason": {"type": "string"}}}},
    {"name": "memory.propose_fact",
     "description": "Propose a new fact (status=proposed) for reconciler promotion.",
     "inputSchema": {"type": "object", "properties": {
         "subject": {"type": "string"}, "attribute": {"type": "string"},
         "value": {"type": "string"}, "value_type": {"type": "string"},
         "source": {"type": "string"}, "confidence": {"type": "number"}},
         "required": ["subject", "attribute", "value"]}},
]

HANDLERS = {
    "memory.search": t_search,
    "memory.get_device": t_get_device,
    "memory.get_decisions": t_get_decisions,
    "memory.record_episode": t_record_episode,
    "memory.propose_fact": t_propose_fact,
}


def call_tool(conn, name, args):
    fn = HANDLERS.get(name) or HANDLERS.get(name.replace("_", ".", 1))
    if not fn:
        raise KeyError("unknown tool: %s" % name)
    return fn(conn, **(args or {}))


# ---------------- JSON-RPC envelope ----------------
def handle(req, conn):
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}
    is_notification = "id" not in req
    try:
        if method == "initialize":
            result = {"protocolVersion": PROTO, "capabilities": {"tools": {}},
                      "serverInfo": SERVER_INFO}
        elif method.startswith("notifications/"):
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            data = call_tool(conn, params["name"], params.get("arguments", {}))
            result = {"content": [{"type": "text",
                                   "text": json.dumps(data, default=str, indent=2)}],
                      "isError": False}
        else:
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": "method not found: %s" % method}}
    except Exception as e:  # noqa: BLE001 — surface as JSON-RPC error
        conn.rollback()
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32000, "message": "%s: %s" % (type(e).__name__, e)}}
    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve_stdio():
    conn = fm_db.connect()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req, conn)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


def selftest():
    conn = fm_db.connect()
    print("== initialize ==")
    print(json.dumps(handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, conn)["result"]))
    print("== tools/list ==")
    tl = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, conn)
    print(", ".join(t["name"] for t in tl["result"]["tools"]))

    def call(name, args):
        r = handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                    "params": {"name": name, "arguments": args}}, conn)
        return json.loads(r["result"]["content"][0]["text"])

    print("\n== memory.search 'commander' ==")
    s = call("memory.search", {"query": "commander", "limit": 3})
    print("facts=%d decisions=%d devices=%d episodes=%d" %
          (len(s["facts"]), len(s["decisions"]), len(s["devices"]), len(s["episodes"])))
    print("  sample decision:", (s["decisions"][0]["decision"][:70] if s["decisions"] else "-"))

    print("\n== memory.get_device 'lumen' ==")
    d = call("memory.get_device", {"hostname": "lumen"})
    print("found=%s role=%s ts=%s facts=%d" %
          (d["found"], d.get("role"), d.get("ip_tailscale"), len(d.get("facts", []))))

    print("\n== memory.get_decisions scope=permanent ==")
    dec = call("memory.get_decisions", {"scope": "permanent"})
    print("count=%d" % dec["count"])

    print("\n== memory.record_episode (test) ==")
    ep = call("memory.record_episode", {"ticket_id": "wq#SELFTEST",
              "agent": "selftest", "result": "done", "summary": "mcp selftest episode"})
    print(ep)

    print("\n== memory.propose_fact (test) ==")
    pf = call("memory.propose_fact", {"subject": "selftest", "attribute": "probe",
              "value": "ok", "source": "mcp-selftest"})
    print(pf)

    # cleanup test rows so the DB stays canonical
    cur = conn.cursor()
    cur.execute("DELETE FROM agent_episodes WHERE ticket_id='wq#SELFTEST' RETURNING id")
    ep_ids = [r[0] for r in cur.fetchall()]
    cur.execute("DELETE FROM fleet_facts WHERE subject='selftest' AND attribute='probe' RETURNING id")
    f_ids = [r[0] for r in cur.fetchall()]
    fm_db.log_event(cur, "selftest", "cleanup", "agent_episode",
                    ep_ids[0] if ep_ids else None, None, None, "selftest cleanup")
    conn.commit()
    print("\ncleanup: removed episodes=%s facts=%s" % (ep_ids, f_ids))
    print("\nSELFTEST OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--call")
    ap.add_argument("--args", default="{}")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    elif args.call:
        conn = fm_db.connect()
        print(json.dumps(call_tool(conn, args.call, json.loads(args.args)),
                         default=str, indent=2))
    else:
        serve_stdio()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""lumen-memoryd — guarded memory service for agent lanes (wq#575).

Binds 127.0.0.1 ONLY. No TLS (loopback), no cloudflared route, no external DNS.
Holds NO model weights: retrieval calls lumen_recall2.search_v3, whose embedding
step is an out-of-process HTTP call to Ollama (measured 54 MB RSS through a full
search, 2026-08-15). MemoryMax=1G in the unit makes a violation fail loudly.

Endpoints (§6.2): /healthz /recall /commit /precheck /propose /revoke /audit /quarantine
wq#610 adds: /quarantine/release /quarantine/discard (operator-only, §3.2 review surface).
"""
import json, os, sys, threading, time, hashlib
from collections import deque, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# wq#2901: relative to this file, not the deployed path. Identical when the daemon
# runs from /home/lumen/minerva, but it lets the tests import the kernel and the
# contract from the checkout under test instead of silently exercising the live ones.
_MEMORYD_DIR = os.path.dirname(os.path.abspath(__file__))
_MINERVA_ROOT = os.path.dirname(os.path.dirname(_MEMORYD_DIR))
sys.path.insert(0, _MEMORYD_DIR)
sys.path.insert(0, _MINERVA_ROOT)

from kernel import (Kernel, Identity, utcnow, sha256,   # noqa: E402
                    FWORKER_IDENTITY_RE)

HOST = "127.0.0.1"
PORT = int(os.environ.get("MEMORYD_PORT", "8139"))
DB = os.environ.get("MEMORYD_DB", "/home/lumen/minerva/minerva.db")
OPERATOR_TOKEN_FILE = "/home/lumen/.secrets/memoryd-operator.token"
SERVICE_TOKEN_FILE = "/home/lumen/.secrets/memoryd-service.token"
STARTED = time.time()

K = Kernel(db_path=DB)
_RATE = defaultdict(deque)
_RATE_LOCK = threading.Lock()
_SEM = threading.BoundedSemaphore(K.contract["rates"]["concurrent"])


def _read_token(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except Exception:
        return None


def _operator_token():
    return _read_token(OPERATOR_TOKEN_FILE)


def rate_ok(lane, kind):
    """§6.4 sliding window, in-memory. Operator unlimited."""
    if lane == "operator":
        return True, ""
    r = K.contract["rates"]
    now = time.time()
    spec = {"write": [("h", 3600, r["writes_hour"]), ("d", 86400, r["writes_day"])],
            "read": [("h", 3600, r["reads_hour"])]}[kind]
    with _RATE_LOCK:
        q = _RATE[(lane, kind)]
        while q and now - q[0] > 86400:
            q.popleft()
        for _, window, cap in spec:
            n = sum(1 for t in q if now - t <= window)
            if n >= cap:
                return False, "%s rate cap %d/%ds reached for lane %s" % (kind, cap, window, lane)
        q.append(now)
    return True, ""


class Handler(BaseHTTPRequestHandler):
    server_version = "lumen-memoryd/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (utcnow(), fmt % args))

    # ---- identity (§6.3) -------------------------------------------------
    def resolve_identity(self, body, for_write=True):
        """Three factors, cheapest first. Nothing here is taken from the body:
        the lane is READ OUT of the ticket the agent demonstrably holds, so a
        lane cannot spell its own name (§2.4)."""
        tok = self.headers.get("X-Memoryd-Operator")
        real = _operator_token()
        if tok and real and tok == real:
            return Identity("operator", "operator", "operator", is_operator=True), None
        if tok and (not real or tok != real):
            return None, "operator token rejected"

        # SERVICE identity (wq#575, added at cutover): long-running units like
        # lumen-agent are not wq sessions and never will be — a session id that
        # expires would silently drop the lane back to the legacy router. A 0600
        # token file grants a READ-ONLY 'unattributed' identity; writes with it are
        # refused by the contract, so attribution (I-7) cannot be dodged this way.
        # Same rotation pattern as the operator token (wq#529, one file one point).
        # Threat-model limit is unchanged and stated in §6.3: on a single-user box
        # any process running as lumen can read this file. It raises the floor from
        # "no boundary" to "a boundary you must deliberately step around".
        svc = self.headers.get("X-Memoryd-Service")
        svc_real = _read_token(SERVICE_TOKEN_FILE)
        if svc and svc_real and svc == svc_real:
            name = (self.headers.get("X-Memoryd-Service-Name") or "unknown").strip()[:40]
            if for_write:
                return None, "service identity is read-only; writes need a held wq claim (I-7)"
            return Identity("unattributed", "service:" + name, None), None
        if svc:
            return None, "service token rejected"

        agent = (self.headers.get("X-Memoryd-Agent") or "").strip()
        if not agent:
            return None, "no identity: X-Memoryd-Agent missing (fail closed)"

        # FLEET-WORKER identity (wq#2901): a per-ticket `fworker-wq<N>` name is
        # never in agent_sessions, and must not be -- `wq holder_is_dead` reads
        # that absence as half of its liveness signal, so registering fworkers
        # would break `wq release --dead-worker`. Attribution comes from the claim
        # RECEIPT instead: the identity names its own ticket, the ticket's
        # claimed_by names the identity back, and the lane is still read out of
        # the ticket. Narrower than a session identity -- one agent, one ticket,
        # one lane -- and a writer cannot set claimed_by for itself.
        # Design: v4/research/fworker_lane_attribution.md D1.
        fw = FWORKER_IDENTITY_RE.match(agent)
        if fw:
            tid = fw.group(1)
            src = (body.get("source_ref") or "").strip()
            if src == "wq#" + tid and K.ticket_claimed_by_ever(tid, agent):  # wq#5166
                lane = K.ticket_lane(tid)
                if not lane:
                    return None, "cannot resolve lane for wq#%s" % tid
                return Identity(lane, agent, None, claims={tid}), None
            if not for_write:
                # §2.5: reads are the lesser risk and blocking them would starve
                # the lane prompt. agent_id is still audited on every read.
                return Identity("unattributed", agent, None), None
            if src != "wq#" + tid:
                return None, ("fleet worker %s must write with source_ref=wq#%s, not %r"
                              " (I-7)" % (agent, tid, src[:60]))
            return None, "agent does not hold the claim on wq#%s" % tid

        if not K.agent_registered(agent):
            return None, "agent %r is not in the wq session registry" % agent[:60]

        src = (body.get("source_ref") or "").strip()
        if src.startswith("wq#"):
            tid = src[3:]
            claims = K.agent_claims(agent)
            if tid not in claims:
                # wq#3352: agent_claims() answers the live-lease question, which a
                # session that just closed its own ticket (`wq done`) always fails —
                # the lease clears on close, same as it does for a fworker identity
                # (wq#2901). The claim RECEIPT survives the close and is written only
                # by `wq claim`, so fall back to the same provenance check the
                # fworker branch above already trusts (kernel.py:327
                # ticket_claimant, "any status") before refusing a write the session
                # legitimately earned.
                # wq#5166: claimed_by OR the latest durable receipt (block/release clear
                # claimed_by); a ticket re-claimed by someone else still refuses (wq#2630).
                if not K.ticket_claimed_by_ever(tid, agent):
                    return None, "agent does not hold a live claim on %s" % src
            lane = K.ticket_lane(tid)
            if not lane:
                return None, "cannot resolve lane for %s" % src
            return Identity(lane, agent, None, claims=claims), None
        if not for_write:
            # Registered session, no claim: read-only identity. The agent_id is
            # still recorded on every read, which is what §4.2's revocation walk
            # actually needs — the lane label is a bonus, not the requirement.
            return Identity("unattributed", agent, None, claims=K.agent_claims(agent)), None
        return None, ("agent writes must carry source_ref=wq#N so the lane can be "
                      "derived from a held claim (I-7)")

    # ---- plumbing --------------------------------------------------------
    def _send(self, code, obj):
        payload = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        cap = K.contract["limits"]["body_bytes"]
        if n > cap:
            return None, "request body exceeds %d bytes" % cap
        if n == 0:
            return {}, None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")), None
        except Exception as e:
            return None, "bad json: %s" % e

    # ---- routes ----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self._healthz()
        if path in ("/audit", "/quarantine"):
            ident, err = self.resolve_identity({})
            if err or not ident.is_operator:
                return self._send(403, {"status": "denied",
                                        "reason": err or "operator token required"})
            return self._audit() if path == "/audit" else self._quarantine()
        return self._send(404, {"status": "no such endpoint", "path": path})

    def do_POST(self):
        if not _SEM.acquire(blocking=False):
            self.send_response(503)
            self.send_header("Retry-After", "2")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            body, err = self._body()
            if err:
                return self._send(400, {"status": "reject", "reason": err})
            path = self.path.split("?")[0]
            fn = {"/recall": self._recall, "/commit": self._commit,
                  "/precheck": self._precheck, "/propose": self._commit,
                  "/revoke": self._revoke,
                  "/quarantine/release": self._quarantine_release,
                  "/quarantine/discard": self._quarantine_discard}.get(path)
            if not fn:
                return self._send(404, {"status": "no such endpoint", "path": path})
            ident, ierr = self.resolve_identity(body, for_write=path not in ("/recall", "/precheck"))
            if ierr:
                # unknown identity: reject, audit, return nothing (fail closed)
                # wq#2895: K.tx() rolls back + closes on every exit (incl. a busy
                # COMMIT); the audit stays fail-open but can no longer leak a lock.
                try:
                    with K.tx() as con:
                        K.audit(con, "reject", "archival_notes", None, None,
                                body.get("source_ref"), detail="identity: " + ierr, invariant="I-4")
                except Exception as e:
                    sys.stderr.write("audit(reject/identity) failed (non-fatal): %s\n" % e)
                return self._send(403, {"status": "reject", "reason": ierr})
            return fn(body, ident)
        except Exception as e:
            import traceback
            traceback.print_exc()
            msg = str(e)
            if "locked" in msg.lower() or "busy" in msg.lower():
                self.send_response(503)
                self.send_header("Retry-After", "2")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(500, {"status": "error", "reason": msg[:300]})
        finally:
            _SEM.release()

    def _healthz(self):
        try:
            with K.ro() as con:   # wq#2895: closed even when a COUNT raises
                counts = {
                    "archival_notes": con.execute("SELECT COUNT(*) FROM archival_notes").fetchone()[0],
                    "state_facts": con.execute("SELECT COUNT(*) FROM state_facts").fetchone()[0],
                    "memory_audit": con.execute("SELECT COUNT(*) FROM memory_audit").fetchone()[0],
                    "memory_quarantine": con.execute("SELECT COUNT(*) FROM memory_quarantine").fetchone()[0],
                }
            return self._send(200, {"status": "ok", "contract_sha256": K.contract_sha,
                                    "contract_version": K.contract["version"],
                                    "db": DB, "db_mtime": os.path.getmtime(DB),
                                    "uptime_s": round(time.time() - STARTED, 1),
                                    "counts": counts,
                                    # wq#2901: non-empty means some lane wq is
                                    # dispatching has no contract row, so every
                                    # write from it is refused -- loudly now.
                                    "lane_drift": K.lane_drift()})
        except Exception as e:
            return self._send(500, {"status": "unhealthy", "reason": str(e)[:200]})

    def _recall(self, body, ident):
        ok, why = rate_ok(ident.lane, "read")
        if not ok:
            self.send_response(429)
            self.send_header("Retry-After", "60")
            self.send_header("Content-Length", "0")
            self.end_headers()
            try:
                with K.tx() as con:
                    K.audit(con, "reject", "archival_notes", None, ident, detail=why, invariant="I-9")
            except Exception as e:
                sys.stderr.write("audit(reject/rate-read) failed (non-fatal): %s\n" % e)
            return
        q = (body.get("query") or "").strip()
        if not q:
            return self._send(400, {"status": "reject", "reason": "query required"})
        top_k = min(int(body.get("top_k") or 10), K.contract["limits"]["top_k_max"])
        include_external = bool(body.get("include_external"))
        raw_mode = bool(body.get("_raw"))     # operator-only parity probe (drill 11)
        import lumen_recall2 as lr
        # wq#576: stamp the server-derived agent id onto this request's hit-log
        # rows. The daemon has no WQ_AGENT of its own, so without this every
        # lane-served hit is unattributable and the outcome join (which keys on
        # session_id) is blind to exactly the traffic the lanes generate.
        # ContextVar => per-thread, safe under ThreadingHTTPServer. Fail-open.
        try:
            import lumen_salience as _sal
            _sal.set_session(ident.agent_id)
        except Exception:
            pass
        t0 = time.time()
        hits = lr.search_v3(q, top_k=top_k) or []
        engine_ms = round((time.time() - t0) * 1000, 1)

        if raw_mode and ident.is_operator:
            out = hits
        else:
            allowed = {"operator", "derived"} | ({"external"} if include_external else set())
            out = []
            for h in hits:
                if not ident.is_operator:
                    if h.get("revoked_at") or h.get("superseded_by"):
                        continue
                    if (h.get("trust") or "operator") not in allowed:
                        continue
                out.append(h)
        ids = [h.get("id") for h in out]
        seq = None
        try:
            with K.tx() as con:
                # §3.1 / Q4: ONE audit row per query, hit ids in detail.
                seq = K.audit(con, "read", "archival_notes", None, ident,
                              body.get("source_ref"),
                              detail=json.dumps({"q": q[:300], "hits": ids, "ms": engine_ms}))
        except Exception as e:
            seq = None   # wq#2895: a failed COMMIT rolled the row back; never report its seq
            sys.stderr.write("audit(read) failed (non-fatal): %s\n" % e)
        return self._send(200, {"status": "ok", "results": out, "engine_ms": engine_ms,
                                "audit_seq": seq})

    def _commit(self, body, ident):
        ok, why = rate_ok(ident.lane, "write")
        if not ok:
            self.send_response(429)
            self.send_header("Retry-After", "60")
            self.send_header("Content-Length", "0")
            self.end_headers()
            try:
                with K.tx() as con:
                    K.audit(con, "reject", "archival_notes", None, ident,
                            body.get("source_ref"), detail=why, invariant="I-9")
            except Exception as e:
                sys.stderr.write("audit(reject/rate-write) failed (non-fatal): %s\n" % e)
            return
        d = K.validate_note(body, ident)
        status, rid, seq = K.apply(d, ident)
        code = {"accept": 201, "noop": 200, "propose": 202,
                "quarantine": 202, "reject": 422}.get(status, 200)
        resp = {"status": status, "id": rid, "audit_seq": seq,
                "reason": d.reason, "invariant": d.invariant}
        if d.redactions:
            resp["redactions"] = d.redactions
        return self._send(code, resp)

    def _precheck(self, body, ident):
        plan = (body.get("plan") or "").strip()
        if not plan:
            return self._send(400, {"status": "reject", "reason": "plan required"})
        sys.path.insert(0, "/home/lumen/minerva")
        import lumen_episode as le
        try:
            # lumen_episode.precheck returns (verdict, hits); recall's kwarg is
            # top_k, not k (verified against the live module, not remembered).
            verdict, eps = le.precheck(plan)
        except Exception as e:
            verdict, eps = "precheck unavailable (fail-open)", []
            sys.stderr.write("precheck failed (fail-open): %s\n" % e)
        try:
            with K.tx() as con:
                K.audit(con, "read", "archival_notes", None, ident, body.get("source_ref"),
                        detail=json.dumps({"precheck": plan[:300]}))
        except Exception as e:
            sys.stderr.write("audit(precheck) failed (non-fatal): %s\n" % e)
        return self._send(200, {"status": "ok", "verdict": verdict, "episodes": eps})

    def _revoke(self, body, ident):
        if not ident.is_operator:
            return self._send(403, {"status": "denied",
                                    "reason": "revocation is operator-only (§4.2)"})
        nid = (body.get("note_id") or "").strip()
        reason = (body.get("reason") or "").strip()
        if not nid or not reason:
            return self._send(400, {"status": "reject", "reason": "note_id and reason required"})
        return self._send(200, K.revoke(nid, reason, ident))

    def _quarantine_release(self, body, ident):
        """wq#610: the review-surface action §3.2 specifies ("Release is an
        operator action that re-runs the full kernel") but #575 never built.
        Operator-only, same gate as /revoke."""
        if not ident.is_operator:
            return self._send(403, {"status": "denied",
                                    "reason": "quarantine release is operator-only (§3.2)"})
        qid = (body.get("id") or "").strip()
        if not qid:
            return self._send(400, {"status": "reject", "reason": "id required"})
        result = K.release(qid, ident, review_note=body.get("review_note"))
        code = {"released": 200, "not_released": 422, "not_found": 404,
               "not_held": 409, "unsupported_table": 422}.get(result["status"], 200)
        return self._send(code, result)

    def _quarantine_discard(self, body, ident):
        if not ident.is_operator:
            return self._send(403, {"status": "denied",
                                    "reason": "quarantine discard is operator-only (§3.2)"})
        qid = (body.get("id") or "").strip()
        if not qid:
            return self._send(400, {"status": "reject", "reason": "id required"})
        result = K.discard(qid, ident, review_note=body.get("review_note"))
        code = {"discarded": 200, "not_found": 404, "not_held": 409}.get(result["status"], 200)
        return self._send(code, result)

    def _audit(self):
        with K.ro() as con:
            rows = con.execute(
                "SELECT seq,ts,action,table_name,row_id,lane,agent_id,trust,source_ref,"
                "invariant,substr(detail,1,300) AS detail FROM memory_audit "
                "ORDER BY seq DESC LIMIT 100").fetchall()
        return self._send(200, {"status": "ok", "rows": [dict(r) for r in rows]})

    def _quarantine(self):
        with K.ro() as con:
            rows = con.execute(
                "SELECT id,ts,table_name,lane,agent_id,trust,source_ref,reason,status "
                "FROM memory_quarantine ORDER BY ts DESC LIMIT 100").fetchall()
        return self._send(200, {"status": "ok", "rows": [dict(r) for r in rows]})


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("lumen-memoryd listening on %s:%d db=%s contract=%s\n"
                     % (HOST, PORT, DB, K.contract_sha[:16]))
    # wq#1051: demand-idle liveness beat (wq#392/#1043 helper). Idle is healthy
    # for this server, so a plain liveness beat is correct -- progress-tied
    # would false-kill during quiet periods. Fail-open no-op if NOTIFY_SOCKET
    # unset (Type!=notify). beat_forever() sends READY=1 itself on thread start.
    sys.path.insert(0, "/home/lumen/agent")
    from lumen_watchdog_notify import Watchdog, beat_forever
    _wd = Watchdog()
    beat_forever(_wd, 10)
    srv.serve_forever()


if __name__ == "__main__":
    main()

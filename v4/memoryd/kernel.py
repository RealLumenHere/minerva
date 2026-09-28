#!/usr/bin/env python3
"""wq#575 — PatchBoard-style deterministic validation kernel over minerva.db.

Pure Python + SQL. NO LLM anywhere on the write path (design §1: an LLM
validator is itself injectable). NO model weights, no embedder — the daemon that
hosts this holds nothing resident (§6.6, ram-cap directive).

The kernel is a library: it takes an ALREADY-RESOLVED identity and a client
payload, and returns exactly one of accept / propose / reject / quarantine.
Identity resolution is the daemon's job (§6.3) precisely so this file can be
unit-tested against the live store in dry-run mode, writing nothing.
"""
import contextlib, hashlib, json, os, re, secrets, sqlite3, time
from datetime import datetime, timezone

DB_DEFAULT = "/home/lumen/minerva/minerva.db"
CONTRACT_DEFAULT = "/home/lumen/minerva/v4/schema/memory_contract_v1.yaml"
QUEUE_DB = "/home/lumen/agents/work-queue/queue.db"
SCHEMA_VERSION = 1

ORIGIN_RE = re.compile(r"^(operator|state-mirror|convo-learn|backfill|agent-lane:[a-z0-9-]+(:wq#\d+)?)$")
SOURCE_REF_RE = re.compile(r"^(wq#\d+|session:[A-Za-z0-9._:-]+)$")
TRUSTS = ("operator", "derived", "external")


def _rollback_quiet(con):
    """wq#2895: roll back if a transaction is open. Never raises -- it runs on
    error paths where the original exception is the one that matters."""
    try:
        if con.in_transaction:
            con.execute("ROLLBACK")
    except Exception:
        pass


def _close(con):
    """wq#2895: rollback-then-close. sqlite3_close_v2 would roll back anyway,
    but the lock release is stated here instead of implied."""
    _rollback_quiet(con)
    con.close()


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


class Decision:
    def __init__(self, verdict, reason="", invariant=None, row=None, redactions=0):
        self.verdict = verdict          # accept | propose | reject | quarantine
        self.reason = reason
        self.invariant = invariant
        self.row = row or {}
        self.redactions = redactions

    def __repr__(self):
        return "<Decision %s %s %s>" % (self.verdict, self.invariant or "-", self.reason[:60])

    def as_dict(self):
        return {"verdict": self.verdict, "reason": self.reason,
                "invariant": self.invariant, "redactions": self.redactions}


class Identity:
    """Server-derived. Nothing here is ever read from a request body (§2.4)."""
    def __init__(self, lane, agent_id, trust, is_operator=False, claims=()):
        self.lane = lane
        self.agent_id = agent_id
        self.trust = trust
        self.is_operator = is_operator
        self.claims = set(claims)       # wq ticket ids this agent currently holds


class Kernel:
    def __init__(self, db_path=DB_DEFAULT, contract_path=CONTRACT_DEFAULT, queue_db=QUEUE_DB):
        import yaml
        self.db_path = db_path
        self.contract_path = contract_path
        self.queue_db = queue_db
        with open(contract_path, "rb") as fh:
            raw = fh.read()
        self.contract_sha = hashlib.sha256(raw).hexdigest()
        self.contract = yaml.safe_load(raw.decode("utf-8"))
        self._screens = [re.compile(p) for p in self.contract.get("screen", {}).get("injection_markers", [])]

    # ---- helpers ----------------------------------------------------------
    # Busy-wait before SQLITE_BUSY. Class attribute so tests can shrink it; the
    # live daemon keeps the historical 5s.
    busy_timeout_s = 5.0

    def connect(self):
        con = sqlite3.connect(self.db_path, timeout=self.busy_timeout_s, isolation_level=None)
        con.row_factory = sqlite3.Row
        return con

    # wq#2895: the ONLY sanctioned way to open a write transaction outside this
    # file. 2026-09-28 04:36Z the daemon's reject-audit did BEGIN IMMEDIATE,
    # its COMMIT hit SQLITE_BUSY (a reader held SHARED past the busy timeout),
    # `except Exception: pass` swallowed it, then _send() raised BrokenPipe and
    # the exception chain pinned the handler frame -- and with it the open
    # connection holding a PENDING lock. An idle daemon never ran cyclic GC, so
    # every reader and writer on minerva.db was locked out for 35 min. Lock
    # release must never depend on GC: tx() rolls back on ANY exit that is not a
    # successful COMMIT (including a failed COMMIT) and always closes.
    @contextlib.contextmanager
    def tx(self):
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except BaseException:
            _rollback_quiet(con)
            raise
        finally:
            _close(con)

    @contextlib.contextmanager
    def ro(self):
        """Read-only use: deterministic close even when a query raises."""
        con = self.connect()
        try:
            yield con
        finally:
            _close(con)

    def lane_policy(self, lane, table):
        return (self.contract["lanes"].get(lane) or {}).get(table)

    def derive_trust(self, identity):
        """§2.4 trust lattice. Never read from the request."""
        if identity.is_operator:
            return "operator"
        lane = self.contract["lanes"].get(identity.lane)
        if not lane:
            return None
        return lane.get("trust", "derived")

    def compose_origin(self, identity, source_ref):
        if identity.is_operator:
            return "operator"
        if identity.lane in ("state-mirror", "convo-learn"):
            return identity.lane
        if source_ref and source_ref.startswith("wq#"):
            return "agent-lane:%s:%s" % (identity.lane, source_ref)
        return "agent-lane:%s" % identity.lane

    def screen(self, text):
        """§3.4 deterministic pattern screen. Redact-and-flag, never silent drop.
        A speed bump for careless injection; the real defence is §2.4 + §5."""
        n = 0
        out = text or ""
        for rx in self._screens:
            out, k = rx.subn("[REDACTED-wq575]", out)
            n += k
        return out, n

    # ---- invariants I-1 .. I-9 -------------------------------------------
    def validate_note(self, payload, identity, con=None):
        """Return a Decision. Pure: touches the DB read-only (I-2/I-3/I-7 lookups)."""
        t = self.contract["tables"]["archival_notes"]
        lim = t["limits"]
        close = False
        if con is None:
            con, close = self.connect(), True
        try:
            policy = self.lane_policy(identity.lane, "archival_notes")
            if policy is None:
                return Decision("reject", "unknown lane %r; fail closed" % identity.lane, "I-4")
            if policy == "reject":
                return Decision("reject", "lane %r may not write archival_notes" % identity.lane, "contract")

            trust = self.derive_trust(identity)
            if trust not in TRUSTS:
                return Decision("reject", "no derivable trust for identity", "I-4")

            # D-2: a body that tries to declare provenance is not merely ignored,
            # it is evidence. Structurally impossible to succeed post-cutover.
            forged = [f for f in t["server_owned"] if f in payload]
            escalation = payload.get("trust") == "operator" and not identity.is_operator

            # writable fields only; everything else is IGNORED, not errored (§1.1)
            kind = (payload.get("kind") or "").strip()
            title = (payload.get("title") or "").strip()
            content = payload.get("content") or ""
            tags = payload.get("tags") or ""
            if isinstance(tags, (list, tuple)):
                tags = ",".join(str(x) for x in tags)
            source_ref = (payload.get("source_ref") or "").strip()

            # I-4 closed vocabularies
            if kind not in t["enums"]["kind"]:
                return Decision("reject", "kind %r not in closed set" % kind[:40], "I-4")
            # I-5 size limits — rejected, never silently truncated
            for fname, val in (("title", title), ("content", content),
                               ("tags", tags), ("source_ref", source_ref)):
                if len(val) > lim[fname]:
                    return Decision("reject", "%s exceeds %d chars (got %d)" % (fname, lim[fname], len(val)), "I-5")
            if not title or not content:
                return Decision("reject", "title and content are required", "I-5")

            # I-7 attribution must resolve, for agent lanes
            if not identity.is_operator:
                if not SOURCE_REF_RE.match(source_ref):
                    return Decision("reject", "source_ref must be wq#N or session:<slug>", "I-7")
                if source_ref.startswith("wq#"):
                    tid = source_ref[3:]
                    if not self.ticket_exists(tid):
                        return Decision("reject", "source_ref %s does not exist in queue.db" % source_ref, "I-7")

            # §3.4 screening on derived/external only
            redactions = 0
            if trust in ("derived", "external"):
                content, redactions = self.screen(content)

            csha = sha256(content)

            # I-3 no destructive replace: exact live duplicate is a no-op
            dup = con.execute(
                "SELECT id FROM archival_notes WHERE content_sha256=? AND revoked_at IS NULL "
                "AND superseded_by IS NULL LIMIT 1", (csha,)).fetchone()
            if dup:
                return Decision("accept", "exact duplicate of live row; no-op", "I-3",
                                {"id": dup["id"], "noop": True})

            # I-8 supersede target must be live
            supersedes = (payload.get("supersedes") or "").strip() or None
            if supersedes:
                prior = con.execute(
                    "SELECT id, revoked_at, superseded_by, content FROM archival_notes WHERE id=?",
                    (supersedes,)).fetchone()
                if not prior:
                    return Decision("reject", "supersedes target %s not found" % supersedes, "I-8")
                if prior["revoked_at"] or prior["superseded_by"]:
                    return Decision("reject", "supersedes target is revoked or already superseded", "I-8")

            row = {
                "id": secrets.token_hex(6),          # §2.3 random, NOT content-derived
                "kind": kind, "title": title, "content": content, "tags": tags,
                "source_ref": source_ref, "created_at": utcnow(),
                "origin": self.compose_origin(identity, source_ref),
                "trust": trust, "agent_id": identity.agent_id, "lane": identity.lane,
                "content_sha256": csha, "schema_version": SCHEMA_VERSION,
                "supersedes": supersedes,
            }
            if not ORIGIN_RE.match(row["origin"]):
                return Decision("reject", "composed origin failed I-4 regex: %r" % row["origin"], "I-4")

            if escalation:
                return Decision("quarantine", "trust escalation attempt in request body (D-2)",
                                "D-2", row, redactions)
            if policy == "propose":
                return Decision("propose", "lane %r is proposal-only for archival_notes" % identity.lane,
                                "contract", row, redactions)
            d = Decision("accept", "ok", None, row, redactions)
            d.forged = forged
            return d
        finally:
            if close:
                _close(con)

    def ticket_exists(self, tid):
        """queue.db's ticket table is `tasks` (verified live 2026-08-15, not
        `tickets` — the design doc says 'queue.db' without naming the table)."""
        try:
            q = sqlite3.connect("file:%s?mode=ro" % self.queue_db, uri=True, timeout=3.0)
            try:
                return q.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone() is not None
            finally:
                q.close()
        except Exception:
            return False

    def agent_registered(self, agent_id):
        """§6.3 factor 2: the session id must be in the wq registry (wq#502)."""
        try:
            q = sqlite3.connect("file:%s?mode=ro" % self.queue_db, uri=True, timeout=3.0)
            try:
                return q.execute("SELECT 1 FROM agent_sessions WHERE session_id=?",
                                 (agent_id,)).fetchone() is not None
            finally:
                q.close()
        except Exception:
            return False

    def ticket_lane(self, tid):
        """The lane a lane cannot spell for itself (§2.4): read from the ticket."""
        try:
            q = sqlite3.connect("file:%s?mode=ro" % self.queue_db, uri=True, timeout=3.0)
            try:
                r = q.execute("SELECT lane FROM tasks WHERE id=?", (tid,)).fetchone()
                return r[0] if r else None
            finally:
                q.close()
        except Exception:
            return None

    def agent_claims(self, agent_id):
        """Tickets this session currently holds (claimed + live lease)."""
        out = set()
        try:
            q = sqlite3.connect("file:%s?mode=ro" % self.queue_db, uri=True, timeout=3.0)
            try:
                for r in q.execute("SELECT id FROM tasks WHERE claimed_by=? AND status='claimed'",
                                   (agent_id,)):
                    out.add(str(r[0]))
            finally:
                q.close()
        except Exception:
            pass
        return out

    # ---- §1.4 transactional commit ---------------------------------------
    def audit(self, con, action, table_name, row_id=None, identity=None, source_ref=None,
              before_sha=None, after_sha=None, detail=None, invariant=None):
        ident = identity
        con.execute(
            "INSERT INTO memory_audit(ts,action,table_name,row_id,lane,agent_id,trust,"
            "source_ref,before_sha,after_sha,detail,invariant) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (utcnow(), action, table_name, row_id,
             getattr(ident, "lane", None), getattr(ident, "agent_id", None),
             getattr(ident, "trust", None), source_ref, before_sha, after_sha,
             (detail or "")[:4000], invariant))
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]

    def apply(self, decision, identity, dry_run=False):
        """Execute a Decision. Audit rows are written INSIDE the same transaction,
        so an accepted write with no audit row is impossible by construction (§1.4).
        Returns (status, id_or_None, audit_seq)."""
        row = decision.row or {}
        if dry_run:
            return decision.verdict, row.get("id"), None
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            if decision.verdict == "reject":
                seq = self.audit(con, "reject", "archival_notes", None, identity,
                                 row.get("source_ref"), detail=decision.reason,
                                 invariant=decision.invariant)
                con.execute("COMMIT")
                return "reject", None, seq

            if decision.verdict == "quarantine":
                con.execute(
                    "INSERT INTO memory_quarantine(id,ts,table_name,payload,lane,agent_id,"
                    "trust,source_ref,reason,status) VALUES (?,?,?,?,?,?,?,?,?,'held')",
                    (row.get("id") or secrets.token_hex(6), utcnow(), "archival_notes",
                     json.dumps(row, default=str), identity.lane, identity.agent_id,
                     row.get("trust"), row.get("source_ref"), decision.reason))
                seq = self.audit(con, "quarantine", "archival_notes", row.get("id"), identity,
                                 row.get("source_ref"), detail=decision.reason,
                                 invariant=decision.invariant)
                con.execute("COMMIT")
                return "quarantine", row.get("id"), seq

            if decision.verdict == "propose":
                pid = row.get("id")
                con.execute(
                    "INSERT INTO memory_quarantine(id,ts,table_name,payload,lane,agent_id,"
                    "trust,source_ref,reason,status) VALUES (?,?,?,?,?,?,?,?,?,'held')",
                    (pid, utcnow(), "archival_notes:proposal", json.dumps(row, default=str),
                     identity.lane, identity.agent_id, row.get("trust"),
                     row.get("source_ref"), decision.reason))
                seq = self.audit(con, "propose", "archival_notes", pid, identity,
                                 row.get("source_ref"), detail=decision.reason)
                con.execute("COMMIT")
                return "propose", pid, seq

            # accept
            if row.get("noop"):
                seq = self.audit(con, "write", "archival_notes", row["id"], identity,
                                 detail="no-op: exact duplicate of live row", invariant="I-3")
                con.execute("COMMIT")
                return "noop", row["id"], seq

            before = None
            if row.get("supersedes"):
                prior = con.execute("SELECT content_sha256, content FROM archival_notes WHERE id=?",
                                    (row["supersedes"],)).fetchone()
                before = (prior["content_sha256"] if prior else None) or (sha256(prior["content"]) if prior else None)
                # I-2-equivalent for notes: predecessor must still be live at commit time
                live = con.execute("SELECT 1 FROM archival_notes WHERE id=? AND revoked_at IS NULL "
                                   "AND superseded_by IS NULL", (row["supersedes"],)).fetchone()
                if not live:
                    con.execute("ROLLBACK")
                    return "reject", None, None
            con.execute(
                "INSERT INTO archival_notes(id,kind,title,content,embedding,tags,source_ref,"
                "created_at,origin,trust,agent_id,lane,content_sha256,schema_version,supersedes) "
                "VALUES (?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], row["kind"], row["title"], row["content"], row["tags"],
                 row["source_ref"], row["created_at"], row["origin"], row["trust"],
                 row["agent_id"], row["lane"], row["content_sha256"], row["schema_version"],
                 row.get("supersedes")))
            if row.get("supersedes"):
                con.execute("UPDATE archival_notes SET superseded_by=? WHERE id=?",
                            (row["id"], row["supersedes"]))
                self.audit(con, "supersede", "archival_notes", row["supersedes"], identity,
                           row["source_ref"], before_sha=before, after_sha=row["content_sha256"],
                           detail="superseded by %s" % row["id"])
            seq = self.audit(con, "write", "archival_notes", row["id"], identity,
                             row["source_ref"], before_sha=before,
                             after_sha=row["content_sha256"],
                             detail="redactions=%d" % decision.redactions)
            con.execute("COMMIT")
            return "accept", row["id"], seq
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            _close(con)

    # ---- §3.2 quarantine review surface (wq#610) --------------------------
    # wq#575 shipped GET /quarantine (list) but no way to act on a row -- design
    # §3.2 says "Release is an operator action that re-runs the full kernel" and
    # #575's own report named this residual: /propose parks rows here with no
    # review surface. Rather than force-fitting a memory-content proposal into
    # the `proposals` table (that table's whole lifecycle -- propose -> operator
    # accepts -> minerva-eval "safety checks before deploy" -- is CODE-CHANGE
    # shaped, not content-shaped; there is no meaningful "deploy" step for "add
    # this note", and minerva_proposal.py's 1,057 rows all fit that shape), this
    # completes the review surface §575 already chose: list (GET /quarantine),
    # approve-and-apply (release, below), reject (discard, below). Same three
    # verbs the proposals table gives an operator, scoped to where the payload
    # actually lives.
    def release(self, quarantine_id, identity, review_note=None):
        """Re-run the FULL kernel on the stored payload as a fresh write. A
        released payload is not trusted because a human looked at it once (§3.2)
        -- it must still pass I-1..I-9, so this calls validate_note() again
        rather than trusting the verdict recorded at quarantine time.

        Two distinct populations land in memory_quarantine and they cannot share
        one re-validation identity, discovered by testing rather than assumed:
          - table_name='archival_notes' (D-2 trust-escalation hits): re-validate
            as the ORIGINAL lane, minus the forged trust claim (fresh_body only
            carries writable_fields) -- if the underlying content is legitimate
            for that lane's normal derived trust, it now proceeds AS that lane,
            not as the operator.
          - table_name='archival_notes:proposal' (policy='propose' lanes, e.g.
            'dream'): that lane's contract policy is PERMANENTLY 'propose' for
            archival_notes -- re-validating with the original identity can never
            return 'accept', it would just re-propose forever. Release for this
            population re-validates as the OPERATOR: approving a proposal IS the
            act of the operator taking authorship of it (matches compose_origin's
            'operator' origin), not a re-submission by the original lane.
        """
        con = self.connect()
        try:
            row = con.execute(
                "SELECT * FROM memory_quarantine WHERE id=?", (quarantine_id,)).fetchone()
            if not row:
                return {"status": "not_found"}
            if row["status"] != "held":
                return {"status": "not_held", "current_status": row["status"]}
            if row["table_name"] not in ("archival_notes", "archival_notes:proposal"):
                return {"status": "unsupported_table", "table_name": row["table_name"]}
            payload = json.loads(row["payload"])
            if row["table_name"] == "archival_notes:proposal":
                re_identity = identity   # must be the operator; enforced by the daemon route
            else:
                re_identity = Identity(row["lane"], row["agent_id"], None, is_operator=False)
            fresh_body = {k: payload.get(k) for k in
                          ("kind", "title", "content", "tags", "source_ref")}
            decision = self.validate_note(fresh_body, re_identity, con=con)
        finally:
            _close(con)
        if decision.verdict != "accept":
            return {"status": "not_released", "reverify_verdict": decision.verdict,
                    "reason": decision.reason, "invariant": decision.invariant}
        status, rid, seq = self.apply(decision, re_identity)
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE memory_quarantine SET status='released', reviewed_by=?, "
                "reviewed_at=?, review_note=? WHERE id=?",
                (identity.agent_id, utcnow(), review_note, quarantine_id))
            self.audit(con, "release", row["table_name"], rid, identity,
                       payload.get("source_ref"), detail="quarantine_id=%s" % quarantine_id)
            con.execute("COMMIT")
        finally:
            _close(con)
        return {"status": "released", "write_status": status, "id": rid, "audit_seq": seq}

    def discard(self, quarantine_id, identity, review_note=None):
        """Close the review with no write. The payload stays in memory_quarantine
        (append-only-in-spirit -- status flips, the row is never deleted) so a
        discard is itself auditable history, not an erasure."""
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT status, table_name FROM memory_quarantine WHERE id=?",
                (quarantine_id,)).fetchone()
            if not row:
                con.execute("ROLLBACK")
                return {"status": "not_found"}
            if row["status"] != "held":
                con.execute("ROLLBACK")
                return {"status": "not_held", "current_status": row["status"]}
            con.execute(
                "UPDATE memory_quarantine SET status='discarded', reviewed_by=?, "
                "reviewed_at=?, review_note=? WHERE id=?",
                (identity.agent_id, utcnow(), review_note, quarantine_id))
            self.audit(con, "reject", row["table_name"], quarantine_id, identity,
                       detail="discarded: %s" % (review_note or "no reason given"))
            con.execute("COMMIT")
        finally:
            _close(con)
        return {"status": "discarded", "id": quarantine_id}

    def revoke(self, note_id, reason, identity):
        """§4.1 revoke-not-erase. Operator only — enforced by the daemon."""
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            r = con.execute("SELECT id, revoked_at, content_sha256 FROM archival_notes WHERE id=?",
                            (note_id,)).fetchone()
            if not r:
                con.execute("ROLLBACK")
                return {"status": "not_found"}
            if r["revoked_at"]:
                con.execute("ROLLBACK")
                return {"status": "already_revoked", "revoked_at": r["revoked_at"]}
            con.execute("UPDATE archival_notes SET revoked_at=?, revoked_reason=? WHERE id=?",
                        (utcnow(), reason, note_id))
            seq = self.audit(con, "revoke", "archival_notes", note_id, identity,
                             before_sha=r["content_sha256"], detail=reason)
            con.execute("COMMIT")
        finally:
            _close(con)
        return {"status": "revoked", "id": note_id, "audit_seq": seq,
                "consumers": self.consumers(note_id), "cascade": self.cascade(note_id)}

    def consumers(self, note_id):
        """§4.2 step 3 — walk the read log. This is the whole reason reads are audited."""
        con = self.connect()
        try:
            rows = con.execute(
                "SELECT DISTINCT lane, agent_id, source_ref, MAX(ts) AS last_ts FROM memory_audit "
                "WHERE action='read' AND detail LIKE ? GROUP BY lane, agent_id, source_ref "
                "ORDER BY last_ts DESC", ("%" + note_id + "%",)).fetchall()
            return [dict(r) for r in rows]
        finally:
            _close(con)

    def cascade(self, note_id):
        """§4.3 depth-1 candidates only. Surfaced for operator decision, never auto-revoked."""
        con = self.connect()
        try:
            rows = con.execute(
                "SELECT id, title, lane, created_at FROM archival_notes "
                "WHERE (source_ref LIKE ? OR supersedes=?) AND id<>? AND revoked_at IS NULL "
                "LIMIT 50", ("%" + note_id + "%", note_id, note_id)).fetchall()
            return [dict(r) for r in rows]
        finally:
            _close(con)

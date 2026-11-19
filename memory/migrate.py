#!/usr/bin/env python3
"""migrate.py — parse claude_memory.md (+ the live fleet roster) into the
canonical fleet_memory DB (wq#3647).

Mapping (per ticket spec):
  * '## PERMANENT DECISIONS', '## STANDING RULES', '## FRANK CORRECTIONS'   -> fleet_decisions
  * any 'never / always / do not' bullet elsewhere                          -> fleet_decisions (derived-rule)
  * '## FLEET HW QUICK-REF' bullets + LIVE_FLEET_STATE roster               -> fleet_devices (+ IP facts)
  * architecture / network / projects / gotchas / people / RE / hw-plan     -> fleet_facts (+ memory_events)
  * IP addresses                                                            -> fleet_facts (attribute ip_*)
  * '## SESSION ...' / dated blocks                                         -> agent_episodes
  * anything unrecognised                                                   -> review_queue.md
Every insert writes a memory_events provenance row. Source files are hashed
into memory_sources. Idempotent: --reset truncates the 6 tables first.

Usage: migrate.py [--reset] [--memory PATH] [--live PATH] [--review PATH]
"""
import argparse
import datetime as dt
import hashlib
import re

import fm_db

MEMORY_DEFAULT = "/home/lumen/claude_memory.md"
LIVE_DEFAULT = "/home/lumen/minerva/context/LIVE_FLEET_STATE.md"
REVIEW_DEFAULT = ("/home/lumen/scratch/subtraction/memory-consolidation/"
                  "review_queue.md")

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
WQID = re.compile(r"wq#(\d+)")
DATE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
RULE_WORDS = re.compile(r"\b(never|always|do not|don't|must not|must)\b", re.I)
RE_SESSION = re.compile(r"^(SESSION\b|20\d{2}-\d{2}-\d{2}\b)")

# fields that mark a LIVE_FLEET_STATE subject as a device (vs a concept)
DEVICE_HINT = ("hardware", "tailscale_ip", "lan_ip", "ram", "kernel",
               "nic", "role", "status", "availability", "soc", "firmware")


def slug(s):
    s = re.sub(r"\([^)]*\)", "", s)          # drop parentheticals
    s = re.sub(r"[^A-Za-z0-9]+", "-", s.lower()).strip("-")
    return s[:120] or "x"


def split_rationale(text):
    """Split a decision bullet into (decision, rationale) on an em/double dash."""
    for sep in (" — ", " -- ", ": "):
        if sep in text:
            a, b = text.split(sep, 1)
            if len(a) >= 8:
                return a.strip(), b.strip()
    return text.strip(), ""


class Migrator:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()
        self._facts = set()        # (subject,attribute,value) dedup
        self._decisions = set()    # decision-text dedup
        self.review = []           # (lineno, reason, text)
        self.counts = dict(facts=0, devices=0, decisions=0, episodes=0,
                           ip_facts=0, review=0)
        self.source_id = None

    # ---- source provenance --------------------------------------------
    def add_source(self, source_type, uri, text):
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.cur.execute(
            """INSERT INTO memory_sources (source_type, source_uri, content_hash)
               VALUES (%s,%s,%s) RETURNING id""", (source_type, uri, h))
        sid = self.cur.fetchone()[0]
        return sid

    # ---- inserts (with event provenance + dedup) ----------------------
    def fact(self, subject, attribute, value, line, vtype="text"):
        value = (value or "").strip()
        subject = subject.strip()[:200]
        attribute = attribute.strip()[:200]
        if not subject or not attribute:
            return None
        key = (subject.lower(), attribute.lower(), value)
        if key in self._facts:
            return None
        self._facts.add(key)
        src = "claude_memory.md:%s" % line if line else "live_fleet_state"
        self.cur.execute(
            """INSERT INTO fleet_facts (subject,attribute,value,value_type,source,created_by)
               VALUES (%s,%s,%s,%s,%s,'migration') RETURNING id""",
            (subject, attribute, value, vtype, src))
        fid = self.cur.fetchone()[0]
        fm_db.log_event(self.cur, "migration", "insert", "fleet_fact", fid,
                        None, {"subject": subject, "attribute": attribute,
                               "value": value}, "migrated from " + src)
        self.counts["facts"] += 1
        if vtype.startswith("ip"):
            self.counts["ip_facts"] += 1
        return fid

    def decision(self, decision, rationale, scope, line):
        decision = decision.strip()
        if not decision:
            return None
        key = decision.lower()[:160]
        if key in self._decisions:
            return None
        self._decisions.add(key)
        if not rationale:
            rationale = "migrated from claude_memory.md §%s (line %s)" % (scope, line)
        self.cur.execute(
            """INSERT INTO fleet_decisions (decision,rationale,scope,status)
               VALUES (%s,%s,%s,'active') RETURNING id""",
            (decision, rationale, scope))
        did = self.cur.fetchone()[0]
        fm_db.log_event(self.cur, "migration", "insert", "fleet_decision", did,
                        None, {"decision": decision, "scope": scope},
                        "migrated from claude_memory.md:%s" % line)
        self.counts["decisions"] += 1
        return did

    def device(self, hostname, **kw):
        hostname = hostname.strip().lower()[:120]
        if not hostname:
            return None
        cols = ("role", "architecture", "ip_tailscale", "ip_lan", "os",
                "status", "location", "power_profile", "notes")
        vals = {c: (kw.get(c) or None) for c in cols}
        self.cur.execute("SELECT id FROM fleet_devices WHERE hostname=%s", (hostname,))
        row = self.cur.fetchone()
        if row:
            did = row[0]
            sets, params = [], []
            for c in cols:
                if vals[c]:
                    sets.append("%s=COALESCE(%s,%s)" % (c, "%s", c))
                    params.append(vals[c])
            if sets:
                params.append(did)
                self.cur.execute("UPDATE fleet_devices SET " + ",".join(sets) +
                                 " WHERE id=%s", params)
                fm_db.log_event(self.cur, "migration", "update", "fleet_device",
                                did, None, vals, "roster enrich")
        else:
            self.cur.execute(
                """INSERT INTO fleet_devices
                     (hostname,role,architecture,ip_tailscale,ip_lan,os,status,location,power_profile,notes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (hostname, vals["role"], vals["architecture"], vals["ip_tailscale"],
                 vals["ip_lan"], vals["os"], vals["status"], vals["location"],
                 vals["power_profile"], vals["notes"]))
            did = self.cur.fetchone()[0]
            fm_db.log_event(self.cur, "migration", "insert", "fleet_device",
                            did, None, {"hostname": hostname, **vals},
                            "roster")
            self.counts["devices"] += 1
        return did

    def episode(self, ticket_id, agent, started_at, result, summary, line):
        summary = (summary or "").strip()[:4000]
        self.cur.execute(
            """INSERT INTO agent_episodes (ticket_id,agent,started_at,result,summary)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (ticket_id) DO UPDATE SET
                 summary=EXCLUDED.summary, agent=COALESCE(EXCLUDED.agent, agent_episodes.agent)
               RETURNING id""",
            (ticket_id, agent, started_at, result, summary))
        eid = self.cur.fetchone()[0]
        fm_db.log_event(self.cur, "migration", "insert", "agent_episode", eid,
                        None, {"ticket_id": ticket_id, "agent": agent},
                        "migrated from claude_memory.md:%s" % line)
        self.counts["episodes"] += 1
        return eid

    def to_review(self, line, reason, text):
        self.review.append((line, reason, text.strip()[:300]))
        self.counts["review"] += 1

    # ---- claude_memory.md parsing -------------------------------------
    def parse_memory(self, text):
        lines = text.splitlines()
        # split into level-2 sections, keeping 1-based line numbers
        sections = []       # (title, start_line, [(lineno, text), ...])
        cur = None
        for i, ln in enumerate(lines, 1):
            if ln.startswith("## ") and not ln.startswith("### "):
                cur = [ln[3:].strip(), i, []]
                sections.append(cur)
            elif cur is not None:
                cur[2].append((i, ln))
            # lines before the first '## ' (file header) are ignored intentionally
        for title, start, body in sections:
            self._dispatch_section(title, start, body)

    def _subsections(self, body):
        """Yield (subtitle_or_None, [(lineno, text)...]) grouped by '### '."""
        groups, sub, buf = [], None, []
        for lineno, ln in body:
            if ln.startswith("### "):
                groups.append((sub, buf))
                sub, buf = ln[4:].strip(), []
            else:
                buf.append((lineno, ln))
        groups.append((sub, buf))
        return groups

    @staticmethod
    def _bullets(buf):
        for lineno, ln in buf:
            s = ln.strip()
            if s.startswith("- ") or s.startswith("* "):
                yield lineno, s[2:].strip()
            elif re.match(r"^\d+\.\s", s):
                yield lineno, re.sub(r"^\d+\.\s", "", s).strip()

    @staticmethod
    def _prose(buf):
        return " ".join(ln.strip() for _, ln in buf if ln.strip()
                        and not ln.strip().startswith("#")).strip()

    def _dispatch_section(self, title, start, body):
        t = title.upper()
        if t.startswith("PERMANENT DECISIONS"):
            for lineno, b in self._bullets(body):
                d, r = split_rationale(b)
                self.decision(d, r, "permanent", lineno)
        elif t.startswith("STANDING RULES"):
            for sub, buf in self._subsections(body):
                scope = slug(sub) if sub else "standing"
                for lineno, b in self._bullets(buf):
                    d, r = split_rationale(b)
                    self.decision(d, r, scope, lineno)
        elif t.startswith("FRANK CORRECTIONS"):
            for lineno, b in self._bullets(body):
                d, r = split_rationale(b)
                self.decision(d, r, "frank-correction", lineno)
        elif t.startswith("FLEET HW QUICK-REF"):
            self._parse_quickref(body)
        elif t.startswith("FLEET ARCHITECTURE"):
            self._parse_subject_block(body, "architecture")
        elif t.startswith("NETWORK + INFRASTRUCTURE") or t.startswith("NETWORK"):
            self._parse_subject_block(body, "network")
        elif t.startswith("PROJECTS"):
            self._parse_projects(body)
        elif t.startswith("TOOL GOTCHAS"):
            self._parse_gotchas(body)
        elif t.startswith("PEOPLE + ACCOUNTS") or t.startswith("PEOPLE"):
            self._parse_people(body)
        elif t.startswith("DEVICE RE REFERENCE"):
            self._parse_subject_block(body, "re")
        elif t.startswith("HARDWARE PLAN"):
            for lineno, b in self._bullets(body):
                self.fact("hardware-plan", "note", b, lineno)
                if RULE_WORDS.search(b):
                    d, r = split_rationale(b)
                    self.decision(d, r, "derived-rule", lineno)
        elif RE_SESSION.match(title) or t.startswith("SESSION"):
            self._parse_session(title, start, body)
        elif t.startswith("MINERVA AUTO-BRIEF") or t.startswith("MINERVA DAILY BRIEF") \
                or t.startswith("OVERVIEW") or t.startswith("PROPOSALS") \
                or t.startswith("TOP SELF-RELEVANT") or t.startswith("ACTIONS SUGGESTED"):
            self.to_review(start, "transient daily-brief section (not canonical)", title)
        else:
            # unknown but non-transient section: capture its content as misc facts
            # (+ rule detection) so canonical reference is never lost on regen.
            subject = "misc:" + slug(title)
            got = False
            for lineno, b in self._bullets(body):
                self.fact(subject, "note", b, lineno)
                self._ip_facts(subject, b, lineno)
                if RULE_WORDS.search(b):
                    d, r = split_rationale(b)
                    self.decision(d, r, "derived-rule", lineno)
                got = True
            if not got:
                prose = self._prose(body)
                if prose:
                    self.fact(subject, "description", prose, start)
                else:
                    self.to_review(start, "unrecognised empty section", title)

    # ---- detail parsers ----------------------------------------------
    def _ip_facts(self, subject, text, line):
        for ip in IPV4.findall(text or ""):
            # crude v4 sanity: each octet 0-255
            if all(0 <= int(o) <= 255 for o in ip.split(".")):
                attr = "ip_tailscale" if ip.startswith("100.") else "ip_lan"
                self.fact(subject, attr, ip, line, vtype="ipv4")

    def _parse_quickref(self, body):
        for lineno, b in self._bullets(body):
            m = re.match(r"(.+?)\s*(?:\(([^)]*)\))?\s*:\s*(.*)$", b)
            if not m:
                self.to_review(lineno, "quickref bullet not parsed", b)
                continue
            name, chassis, rest = m.group(1), m.group(2), m.group(3)
            ts = re.search(r"\bTS\s+((?:\d{1,3}\.){3}\d{1,3})", rest)
            lan = re.search(r"\bLAN\s+((?:\d{1,3}\.){3}\d{1,3})", rest)
            role = rest.split(",")[0].strip()
            # one bullet may list several hosts sharing a note (e.g. "Apep, Gameboy: offline")
            for part in name.split(","):
                host = slug(part)
                if not host:
                    continue
                self.device(host, role=role, architecture=(chassis or None),
                            ip_tailscale=(ts.group(1) if ts else None),
                            ip_lan=(lan.group(1) if lan else None),
                            notes=rest.strip())
                if ts:
                    self.fact(host, "ip_tailscale", ts.group(1), lineno, vtype="ipv4")
                if lan:
                    self.fact(host, "ip_lan", lan.group(1), lineno, vtype="ipv4")

    def _parse_subject_block(self, body, prefix):
        for sub, buf in self._subsections(body):
            subject = "%s:%s" % (prefix, slug(sub)) if sub else prefix
            bl = list(self._bullets(buf))
            if bl:
                for lineno, b in bl:
                    self.fact(subject, "note", b, lineno)
                    self._ip_facts(subject, b, lineno)
                    if RULE_WORDS.search(b):
                        d, r = split_rationale(b)
                        self.decision(d, r, "derived-rule", lineno)
            else:
                prose = self._prose(buf)
                if prose:
                    first = buf[0][0] if buf else None
                    self.fact(subject, "description", prose, first)
                    self._ip_facts(subject, prose, first)

    def _parse_projects(self, body):
        for sub, buf in self._subsections(body):
            if not sub:
                continue
            subject = "project:" + slug(sub)
            bl = list(self._bullets(buf))
            if bl:
                for lineno, b in bl:
                    self.fact(subject, "note", b, lineno)
            prose = self._prose(buf)
            if prose and not bl:
                self.fact(subject, "description", prose, buf[0][0] if buf else None)

    def _parse_gotchas(self, body):
        for sub, buf in self._subsections(body):
            subject = "gotcha:" + slug(sub) if sub else "gotcha"
            for lineno, b in self._bullets(buf):
                self.fact(subject, "note", b, lineno)
                if RULE_WORDS.search(b):
                    d, r = split_rationale(b)
                    self.decision(d, r, "derived-rule", lineno)

    def _parse_people(self, body):
        for lineno, b in self._bullets(body):
            m = re.match(r"([A-Za-z0-9 ./_-]+?)\s*(?:\(([^)]*)\))?\s*:\s*(.*)$", b)
            if not m:
                self.to_review(lineno, "people bullet not parsed", b)
                continue
            name, _alias, rest = m.group(1), m.group(2), m.group(3)
            subject = "person:" + slug(name)
            self.fact(subject, "note", rest, lineno)
            self._ip_facts(subject, rest, lineno)

    def _parse_session(self, title, start, body):
        tid = WQID.search(title)
        if not tid:
            for _, ln in body:
                tid = WQID.search(ln)
                if tid:
                    break
        ticket_id = ("wq#" + tid.group(1)) if tid else None
        agent = None
        pm = re.search(r"\(([^)]*)\)", title)
        if pm:
            agent = pm.group(1).split(",")[0].strip()[:120]
        dm = DATE.search(title)
        started = None
        if dm:
            try:
                started = dt.datetime.strptime(dm.group(1), "%Y-%m-%d")
            except ValueError:
                started = None
        result = None
        blob = title + " " + " ".join(ln for _, ln in body[:6])
        for kw in ("KILLED", "BLOCKED", "SHIPPED", "DONE", "COMPLETE", "RESOLVED"):
            if kw in blob.upper():
                result = kw.lower()
                break
        firstbullets = [b for _, b in list(self._bullets(body))[:3]]
        summary = title
        if firstbullets:
            summary = title + " — " + " ".join(firstbullets)
        self.episode(ticket_id, agent, started, result, summary, start)

    # ---- LIVE_FLEET_STATE roster --------------------------------------
    KV = re.compile(r"\*\*([a-z0-9_]+)\*\*=(.*?)(?=,?\s*\*\*[a-z0-9_]+\*\*=|$)")

    def parse_live_state(self, text):
        for i, ln in enumerate(text.splitlines(), 1):
            s = ln.strip()
            m = re.match(r"^-\s+\*\*([a-z0-9_.-]+)\*\*\s*—?\s*(.*)$", s)
            if not m:
                continue
            subject = m.group(1).strip().lower()
            rest = m.group(2)
            attrs = {}
            for am in self.KV.finditer(rest):
                attrs[am.group(1)] = am.group(2).strip().rstrip(",").strip()
            if not attrs:
                continue
            is_device = any(k in attrs for k in DEVICE_HINT)
            if is_device:
                hw = attrs.get("hardware", "")
                arch = attrs.get("arch") or (hw.split(";")[0].strip()[:200] if hw else None)
                self.device(
                    subject,
                    role=attrs.get("role") or attrs.get("status"),
                    architecture=arch,
                    ip_tailscale=attrs.get("tailscale_ip"),
                    ip_lan=attrs.get("lan_ip"),
                    os=attrs.get("kernel") or attrs.get("os"),
                    status=attrs.get("status") or attrs.get("availability"),
                    notes=(hw[:500] or None))
            # every roster attr becomes a fact too (authoritative source)
            for k, v in attrs.items():
                vtype = "ipv4" if k.endswith("_ip") else "text"
                self.fact(subject, k, v, None, vtype=vtype)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true",
                    help="truncate the 6 tables before migrating (clean rebuild)")
    ap.add_argument("--memory", default=MEMORY_DEFAULT)
    ap.add_argument("--live", default=LIVE_DEFAULT)
    ap.add_argument("--review", default=REVIEW_DEFAULT)
    args = ap.parse_args()

    conn = fm_db.connect()
    cur = conn.cursor()
    if args.reset:
        cur.execute("""TRUNCATE fleet_facts, fleet_devices, fleet_decisions,
                       agent_episodes, memory_sources, memory_events
                       RESTART IDENTITY CASCADE""")

    m = Migrator(conn)
    with open(args.memory, encoding="utf-8", errors="replace") as fh:
        mem_text = fh.read()
    m.add_source("file", args.memory, mem_text)
    m.parse_memory(mem_text)

    try:
        with open(args.live, encoding="utf-8", errors="replace") as fh:
            live_text = fh.read()
        m.add_source("file", args.live, live_text)
        m.parse_live_state(live_text)
    except FileNotFoundError:
        m.to_review(0, "LIVE_FLEET_STATE.md not found", args.live)

    # review queue file
    with open(args.review, "w", encoding="utf-8") as fh:
        fh.write("# Migration review queue (wq#3647) — %s\n" %
                 dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        fh.write("# Lines/sections the migrator did not map to a structured row.\n\n")
        for line, reason, text in m.review:
            fh.write("- [L%s] %s: %s\n" % (line, reason, text))

    conn.commit()

    # ---- verification summary ----
    print("=== migration counts ===")
    for k, v in m.counts.items():
        print("  %-10s %d" % (k, v))
    cur.execute("SELECT count(*) FROM memory_events")
    print("  events     %d" % cur.fetchone()[0])
    cur.execute("SELECT hostname FROM fleet_devices ORDER BY hostname")
    hosts = [r[0] for r in cur.fetchall()]
    print("=== %d devices ===\n  %s" % (len(hosts), ", ".join(hosts)))
    cur.execute("SELECT scope, count(*) FROM fleet_decisions GROUP BY scope ORDER BY scope")
    print("=== decisions by scope ===")
    for scope, n in cur.fetchall():
        print("  %-20s %d" % (scope, n))
    conn.close()


if __name__ == "__main__":
    main()

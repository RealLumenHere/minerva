#!/usr/bin/env python3
"""state_admission.py: the six-point admission gate for `lumen_state.py ingest`
(wq#5006, pattern from the brain.md extraction, wq#4970).

The question a fact must pass before it enters the bi-temporal store:
"will this still matter in six months, AND is it hard to rebuild from the code?"
Six criteria, each turned into a mechanical check. Each check returns findings at
one of two levels:

  REFUSE  nothing is written; the CLI exits 3
  WARN    the fact is written; the finding is printed to stderr and logged

  1. Durable             WARN   transient readings (load, uptime, PIDs, "right now")
                                unless --half-life marks the fact as decaying
  2. Not reconstructable WARN   code, diffs, or file dumps (the repo already holds them)
  3. Decision-level      WARN   values over 400 chars or multi-line step logs.
                                The detail goes in --note or claude_memory.md.
  4. Traceable           REFUSE no --source, or a --source that cites nothing
                                (no wq#, PR, path, URL, episode, Frank or probe)
  5. Working language    REFUSE secret-shaped material, or a placeholder value
                                (TODO/TBD/<...>/???/unknown)
  6. Explicit reversal   WARN   (REFUSE under STATE_ADMISSION_STRICT=1) when a
                                different current value is superseded with no
                                --why/--note to say why

Scope: the CLI path only. Library callers (probes, reconcilers: `ingest(...,
probe=...)`) skip this gate, so no automated writer changes behaviour. A REFUSE can be
overridden on the record with --admit-override "<reason>". Every decision that
is not a clean pass is appended to ADMISSION_LOG.
"""
from __future__ import annotations

import json
import os
import re
import time

ADMISSION_LOG = os.environ.get("STATE_ADMISSION_LOG", "/home/lumen/logs/state-admission.jsonl")
MAX_VALUE_CHARS = 400

_TRANSIENT = re.compile(
    r"(\bload average\b|\bload avg\b|\bup \d+\s*(d|day|days|h|hr|hours|min|minutes)\b|"
    r"\bup \d+d\d+h\b|\bpid \d+\b|\bright now\b|\bat the moment\b|\bcurrently running\b|"
    r"\b\d{1,3}% (cpu|mem|memory|full|used)\b|\bfail[_ ]streak \d+\b)", re.I)
_CODE = re.compile(r"(```|^diff --git|^@@ |^\+\+\+ |^--- a/|^\s*def \w+\(|^\s*import \w+|#!/)", re.M)
_SECRET = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(password|passwd|api[_-]?key|secret|token)\s*[=:]\s*\S{6,})", re.I)
_PLACEHOLDER = re.compile(r"^(todo|tbd|tbc|fixme|xxx|\?+|unknown|n/?a|none yet|placeholder|<[^>]*>)$", re.I)
_PLACEHOLDER_INLINE = re.compile(r"(<placeholder>|<fill[^>]*>|\bTODO\b|\bTBD\b|\?\?\?)")
_CITES = re.compile(
    r"(wq#?\d+|pr\s*#?\d+|pulls/\d+|#\d{2,}|https?://|/[\w.-]+/[\w./-]+|episode|frank|probe|"
    r"\bssh\b|verified|observed|audit|inspect|runbook|issue-log|aq#\d+|commit [0-9a-f]{7,}|\b[0-9a-f]{8,}\b)",
    re.I)


def check(subject, predicate, value, *, source=None, note=None, why=None,
          half_life=None, prior_value=None, source_kind=None, strict=None):
    """Return a list of (level, criterion, message). level is 'REFUSE' or 'WARN'."""
    if strict is None:
        strict = os.environ.get("STATE_ADMISSION_STRICT") == "1"
    out = []
    v = (value or "").strip()
    src = (source or "").strip()

    # 4. Traceable
    if not src:
        out.append(("REFUSE", "4-traceable", "no --source: every fact must say where it came from"))
    elif not _CITES.search(src):
        out.append(("REFUSE", "4-traceable",
                    f"--source {src!r} cites nothing checkable (want a wq#, PR, path, URL, episode, "
                    "probe, or 'Frank ...')"))

    # 5. Working language: no secrets, no placeholders
    for label, text in (("value", v), ("note", note or ""), ("source", src), ("why", why or "")):
        if text and _SECRET.search(text):
            out.append(("REFUSE", "5-working-language",
                        f"{label} looks like it holds secret material; store a pointer to the secret, never the secret"))
    if _PLACEHOLDER.match(v) or _PLACEHOLDER_INLINE.search(v):
        out.append(("REFUSE", "5-working-language", f"value {v[:40]!r} is a placeholder, not a fact"))

    # 1. Durable
    if _TRANSIENT.search(v) and half_life is None:
        out.append(("WARN", "1-durable",
                    "value carries a transient reading (load/uptime/pid/percent/'right now'); "
                    "keep the durable part, or pass --half-life if the fact really decays"))

    # 2. Not reconstructable from code
    if _CODE.search(v):
        out.append(("WARN", "2-not-reconstructable",
                    "value looks like code or a diff; the repo already holds it, so record the decision and a path"))

    # 3. Decision-level scope
    if len(v) > MAX_VALUE_CHARS or v.count("\n") >= 3:
        out.append(("WARN", "3-decision-level",
                    f"value is {len(v)} chars / {v.count(chr(10)) + 1} lines; keep the decision in the value "
                    "and move the step-by-step into --note or claude_memory.md"))

    # 6. Explicit reversals
    if (prior_value is not None and prior_value.strip().lower() != v.lower()
            and source_kind != "observed" and not (why or note)):
        out.append(("REFUSE" if strict else "WARN", "6-explicit-reversal",
                    f"supersedes current value {prior_value[:60]!r} with no --why/--note; say what changed and why"))
    return out


def log(decision, subject, predicate, findings, override=None):
    try:
        os.makedirs(os.path.dirname(ADMISSION_LOG), exist_ok=True)
        with open(ADMISSION_LOG, "a") as fh:
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "decision": decision, "subject": subject, "predicate": predicate,
                "findings": [f"{lvl} {crit}: {msg}" for lvl, crit, msg in findings],
                "override": override,
            }) + "\n")
    except OSError:
        pass  # the log must never be a new way for an ingest to die

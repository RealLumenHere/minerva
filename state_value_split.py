#!/usr/bin/env python3
"""wq#1629 — split a fleet fact's machine-checkable token out of its prose.

`lumen_state.py ingest` stored the whole sentence in `value`:

    root_disk = '227G/466G 51% used, 220G free after 2026-08-09 offload (was 365G/82%)'

so a token scanner (`lumen_contradiction_scan.is_structured`: fact_class !=
narrative, len <= 60, `TOKEN_RE.fullmatch`) could not read the value at all.
184 of 316 closed facts were unmatchable for exactly this reason — the coverage
ceiling was set at ingest, not at scan. MINERVA_STALENESS_REVIEW.md §3.5 forbids
string-matching prose, and rightly: the fix is to stop writing prose into the
column the scanner reads.

This module is the splitter. `split_value(predicate, value)` returns
`(token, note, rule)` — the token goes to `state_facts.value`, the prose to
`state_facts.note` (wq#1629 migration), and `rule` names which rule fired.
It is deterministic, has no model in it, and refuses to guess: when no rule
matches the value is returned unchanged with `rule=None`, which keeps a prose
fact prose rather than inventing a token for it.

CLI:
  state_value_split.py split <predicate> <value>        # what would it do
  state_value_split.py backfill [--apply] [-p PRED ...] # rewrite existing rows
  state_value_split.py report [--limit N]               # unsplittable leftovers
"""
import argparse
import json
import os
import re
import sqlite3
import sys

DB = os.environ.get('MINERVA_DB', '/home/lumen/minerva/minerva.db')

# Mirrors lumen_contradiction_scan.{TOKEN_RE,MAX_VALUE_LEN} — a token this module
# emits has to be one the scanner will actually index, so the two definitions are
# the same definition. Tested against the real scanner in
# test_state_value_split.py::test_emitted_tokens_are_scanner_structured.
TOKEN_RE = re.compile(r'^[A-Za-z0-9._:/@-]+$')
MAX_TOKEN_LEN = 60

# Predicates whose value is a state word ("OFFLINE", "IN SERVICE") rather than a
# measurement or an identifier.
STATE_PREDICATES = {'status', 'availability', 'lifecycle', 'state', 'health',
                    'multiplayer-state', 'reachability'}
_STATE_PRED_RE = re.compile(r'(^|[_-])(status|state|availability|health)([_-]|$)')

# Predicates measured as "used of total".
_MEASURE_PRED_RE = re.compile(r'(disk|storage|space|memory|ram|capacity)')

# The state word is usually first and shouted. Two trailing caps words are kept
# so "IN SERVICE" and "DEAD ON ARRIVAL" survive as one token.
_ANCHORED_STATE = re.compile(
    r'^([A-Z][A-Z0-9]*(?:[+/_.\-][A-Za-z0-9]+)*(?:\s+[A-Z][A-Z0-9]+){0,2})'
    r'(?=$|[\s:;,.()\[\]—–])')

# Fallback for a state word that is not first: "tailnet OFFLINE since 2026-07-26".
# Deliberately a closed vocabulary — any all-caps word would also catch "AIO",
# "NOT" and "PT", which are not states.
STATE_WORDS = {
    'ACTIVE', 'ALIVE', 'BLOCKED', 'BROKEN', 'BUILT', 'DEAD', 'DECOMMISSIONED',
    'DEGRADED', 'DEPLOYED', 'DONE', 'DOWN', 'DISABLED', 'ENABLED', 'FAILED',
    'FIXED', 'GREEN', 'HEALTHY', 'IDLE', 'LIVE', 'MISSING', 'OFFLINE', 'OK',
    'ONLINE', 'PAUSED', 'PENDING', 'PLANNED', 'PROVISIONING', 'REACHABLE',
    'RED', 'REMOVED', 'REPLACED', 'RETIRED', 'RESTORED', 'RUNNING', 'SHIPPED',
    'STOPPED', 'SUPERSEDED', 'UNREACHABLE', 'UP', 'VERIFIED',
}
_STATE_WORD_SCAN = 6  # words from the start to look in; a state word arrives early

_SIZE_PAIR = re.compile(r'\b(\d+(?:\.\d+)?[KMGTP])/(\d+(?:\.\d+)?[KMGTP])\b',
                        re.IGNORECASE)

# "P3 (relabelled 2026-08-11)", "6.14.0-33-generic — since the 09-02 reboot".
_PREFIX_TOKEN = re.compile(
    r'^([A-Za-z0-9._:/@-]{2,60})\s*(?:[—–]|--|-\s|[:;,]|\()\s*(\S)')

# Words that are prose, not values, however they are punctuated. Without this
# "Frank confirmed: ..." would split to the token "Frank".
_PREFIX_STOPWORDS = {'the', 'a', 'an', 'and', 'but', 'per', 'via', 'see', 'note',
                     'was', 'were', 'is', 'it', 'this', 'that', 'frank', 'lumen',
                     'not', 'todo', 'fixme', 'after', 'before', 'as', 'so'}

_NOTE_STRIP = ' \t—–-:;,.'


def is_structured(value):
    """Whether a token scanner can already read this value as-is."""
    return len(value) <= MAX_TOKEN_LEN and bool(TOKEN_RE.fullmatch(value))


def _normalise(token):
    """Multi-word caps run -> one token. 'IN SERVICE' -> 'IN-SERVICE'."""
    return re.sub(r'\s+', '-', token.strip())


def _note_from(value, start, end):
    """The prose that goes with the token.

    A token taken off the front leaves a clean remainder, so the note is that
    remainder ('OFFLINE since 2026-07-26' -> 'since 2026-07-26'). A token found
    mid-sentence cannot be cut out without mangling the sentence, so the note is
    the whole original value — redundant with the token by one word, lossless
    everywhere else.
    """
    if start == 0:
        note = value[end:].strip(_NOTE_STRIP)
    else:
        note = value.strip()
    return note or None


def _state_token(value):
    m = _ANCHORED_STATE.match(value)
    if m and len(_normalise(m.group(1))) >= 2:
        return m.start(1), m.end(1), _normalise(m.group(1)), 'state_anchored'
    for word in re.finditer(r'[A-Za-z][A-Za-z0-9+/_.-]*', value[:200]):
        if word.start() and len(value[:word.start()].split()) >= _STATE_WORD_SCAN:
            break
        tok = word.group(0)
        if tok.isupper() and tok in STATE_WORDS:
            return word.start(), word.end(), tok, 'state_vocabulary'
    return None


def _measure_token(value):
    m = _SIZE_PAIR.search(value)
    if m:
        return m.start(), m.end(), m.group(0).upper(), 'measure_size_pair'
    return None


def _prefix_token(value):
    m = _PREFIX_TOKEN.match(value)
    if m and m.group(1).lower() not in _PREFIX_STOPWORDS:
        return 0, m.end(1), m.group(1), 'prefix_token'
    return None


def split_value(predicate, value, note=None):
    """`(value, note, rule)` for a fact about to be written or backfilled.

    An explicit `note` from the caller is authoritative: the value is theirs to
    choose and nothing is auto-split under it. Otherwise the value is left alone
    unless it is unreadable to a token scanner AND one of the rules produces a
    token that is readable — no rule, no split.
    """
    value = str(value).strip()
    predicate = (predicate or '').strip().lower()
    if note is not None:
        return value, (str(note).strip() or None), 'explicit'
    if is_structured(value):
        return value, None, None

    rules = []
    if predicate in STATE_PREDICATES or _STATE_PRED_RE.search(predicate):
        rules.append(_state_token)
    if _MEASURE_PRED_RE.search(predicate):
        rules.append(_measure_token)
    rules.append(_prefix_token)

    for rule in rules:
        hit = rule(value)
        if hit is None:
            continue
        start, end, token, name = hit
        if not is_structured(token):
            continue
        new_note = _note_from(value, start, end)
        if new_note is None:
            continue  # the whole value was the token; nothing was prose
        return token, new_note, name
    return value, None, None


# ── backfill ────────────────────────────────────────────────────────────────
DEFAULT_PREDICATES = ('status', 'root_disk', 'availability')


def _rows(conn, predicates):
    q = ('SELECT id, subject, predicate, value, note, valid_to FROM state_facts '
         'WHERE note IS NULL')
    params = []
    if predicates:
        q += ' AND predicate IN (%s)' % ','.join('?' * len(predicates))
        params += list(predicates)
    return conn.execute(q + ' ORDER BY subject, predicate, valid_from', params).fetchall()


def plan(conn, predicates=DEFAULT_PREDICATES):
    """What backfill would change. Read-only."""
    changes, skipped = [], []
    for row in _rows(conn, predicates):
        token, note, rule = split_value(row['predicate'], row['value'])
        if rule is None or token == row['value']:
            if not is_structured(row['value']):
                skipped.append(dict(row))
            continue
        changes.append({'id': row['id'], 'subject': row['subject'],
                        'predicate': row['predicate'], 'open': row['valid_to'] is None,
                        'old': row['value'], 'value': token, 'note': note,
                        'rule': rule})
    return changes, skipped


MINERVA_DIR = os.environ.get('MINERVA_DIR', '/home/lumen/minerva')


def _minerva():
    """The live store's own modules. This file is deployed beside them, so the
    import works from /home/lumen/minerva without the path insert; the insert is
    for running it out of the repo checkout."""
    if MINERVA_DIR not in sys.path:
        sys.path.insert(0, MINERVA_DIR)
    import lumen_state
    import minerva_audit
    import projection_state
    return lumen_state, minerva_audit, projection_state


def apply(conn, changes, source_ref='wq#1629'):
    """Rewrite value/note in place. Windows (valid_from/valid_to) are never
    touched: this is the same fact, re-shaped, not a supersession."""
    lumen_state, minerva_audit, projection_state = _minerva()
    if changes:
        # wq#1633: LIVE_FLEET_STATE* and the proposal digest render these values,
        # so a backfill leaves them stale until the next regen.
        projection_state.mark_state_projections_dirty(conn)
    for ch in changes:
        # fact_class is recomputed: it was 'narrative' because the value was a
        # sentence, and a token is not narrative — that reclassification is the
        # point (it is one of the scanner's skip reasons). render_tier is NOT:
        # a split renders the same characters, so it must not move a fact into or
        # out of the injected core block. Stored tier wins; the recomputed one is
        # only a fallback for a row that never had one.
        fact_class, tier = lumen_state._classify_fact(ch['predicate'], ch['value'],
                                                      ch['note'])
        conn.execute('UPDATE state_facts SET value=?, note=?, fact_class=?, '
                     'render_tier=COALESCE(render_tier, ?) WHERE id=?',
                     (ch['value'], ch['note'], fact_class, tier, ch['id']))
        minerva_audit.audit(conn, minerva_audit.WRITE, 'state_facts', ch['id'],
                            source_ref=source_ref,
                            detail=f"wq#1629 value/note split ({ch['rule']}): "
                                   f"{ch['old'][:80]} -> {ch['value']}")
    conn.commit()
    return len(changes)


def _conn(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--db', default=DB)
    sub = ap.add_subparsers(dest='cmd', required=True)
    ps = sub.add_parser('split', help='show the split for one value')
    ps.add_argument('predicate')
    ps.add_argument('value')
    pb = sub.add_parser('backfill', help='rewrite stored prose values (dry-run by default)')
    pb.add_argument('--apply', action='store_true')
    pb.add_argument('-p', '--predicate', action='append', dest='predicates',
                    help='repeatable; default status/root_disk/availability, '
                         '"--predicate ALL" for every predicate')
    pr = sub.add_parser('report', help='values no rule can split')
    pr.add_argument('--limit', type=int, default=20)
    a = ap.parse_args(argv)

    if a.cmd == 'split':
        token, note, rule = split_value(a.predicate, a.value)
        print(json.dumps({'value': token, 'note': note, 'rule': rule}, indent=2))
        return 0

    preds = a.predicates if getattr(a, 'predicates', None) else list(DEFAULT_PREDICATES)
    if preds == ['ALL']:
        preds = None
    conn = _conn(a.db)
    lumen_state = _minerva()[0]
    lumen_state._migrate(conn)
    changes, skipped = plan(conn, preds)

    if a.cmd == 'report':
        for row in skipped[:a.limit]:
            print(f"{row['subject']}.{row['predicate']}: {row['value'][:100]}")
        print(f"{len(skipped)} value(s) no rule splits")
        return 0

    for ch in changes:
        flag = 'OPEN ' if ch['open'] else 'closed'
        print(f"[{flag}] {ch['subject']}.{ch['predicate']}: {ch['value']}  ({ch['rule']})")
        print(f"         note: {(ch['note'] or '')[:110]}")
    print(f"{len(changes)} row(s) to split, {len(skipped)} left as prose"
          f"{'' if a.apply else '  (dry run; pass --apply)'}")
    if a.apply:
        apply(conn, changes)
        print(f"applied {len(changes)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

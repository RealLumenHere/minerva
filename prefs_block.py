"""wq#1640 -- Frank-side hardware quick-ref, rendered from state_facts.

MINERVA_PIPELINE_MAP.md (wq#1626) s3.3#6 / s5#15: Frank's <userPreferences> and
the three Anthropic-side skills (lumen-session-start, lumen-change-safety,
lumen-fleet-provisioning) each carry a hand-maintained hardware quick-ref
(Lumen/Thor/Thoth/Mirror/PBox users + Tailscale IPs + LAN IPs) that no fleet
tool could regenerate -- when a node moves, Frank edits four copies by hand
and the REPEAT instinct fires. This module is the regenerator:

  render_prefs_block()   -- the exact block, sourced from lumen_state.current()
                             (state_facts, MINERVA_PIPELINE_MAP.md s1.1) for the
                             five hosts, self-expiring retired/decommissioned
                             hosts the same way state_facts already does (no
                             hand-maintained override table).
  check_and_file_ticket() -- the daily drift check: render, diff against the
                             last handed-out copy (minerva/context/prefs_block.last),
                             and on drift ONLY, file one user-lane ticket with the
                             new block pasted inline, superseding any prior open
                             one of the same kind. No-op when nothing changed.

Wired into `lumen_state.py render --prefs-block` (lumen_state_render.diff, this
directory) for the on-demand regenerate path; `check_and_file_ticket()` is meant
to run off a daily timer (prefs-block-check.service/.timer, this directory) for
the drift-only ticketing path -- two different triggers, one render function.
"""
import argparse
import difflib
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/home/lumen/minerva')
import lumen_state as ls  # noqa: E402

WQ_BIN = '/home/lumen/bin/wq'
WQ_DB = '/home/lumen/agents/work-queue/queue.db'
LAST_PATH = '/home/lumen/minerva/context/prefs_block.last'

TITLE_PREFIX = '[fleet hw quick-ref]'

HOSTS = ('lumen', 'thor', 'thoth', 'mirror', 'pbox')
DISPLAY_NAME = {'lumen': 'Lumen', 'thor': 'Thor', 'thoth': 'Thoth',
                'mirror': 'Mirror', 'pbox': 'PBox'}

# wq#1640 ticket text: the two lines Frank's CURRENT hand-maintained copy is
# known to have wrong. Not derived from the store (state_facts already shows
# both hosts DECOMMISSIONED, see render below) -- this is what differs in the
# copy this ticket exists to replace, so a drift ticket says so explicitly
# instead of leaving Frank to spot it by re-reading five lines.
KNOWN_STALE_FLAGS = (
    'PBox listed as an active host in the current hand-maintained copy -- '
    'state_facts has it DECOMMISSIONED 2026-08-28.',
    'Mirror listed as merely "retired" per the 05-18 table -- state_facts now '
    'shows DECOMMISSIONED 2026-08-28, same closure as PBox, not a softer state.',
)

RETIRED_LIFECYCLE = ('decommissioned', 'retired')


def _host_facts(subject):
    return {r['predicate']: r for r in ls.current(subject)}


def _retirement(by_pred):
    """(label, fact) if this host is closed (lifecycle or status says so),
    else (None, None). Mirrors state_facts' own closing enum (state_registry.json)
    plus the STATUS convention observed in the store ('DECOMMISSIONED -- ...')."""
    lc = by_pred.get('lifecycle')
    if lc and lc['value'] in RETIRED_LIFECYCLE:
        return lc['value'].upper(), lc
    st = by_pred.get('status')
    if st:
        head = st['value'].strip().split(' ', 1)[0].rstrip(' -—').upper()
        if head in ('DECOMMISSIONED', 'RETIRED'):
            return head, st
    return None, None


def render_prefs_block(now=None):
    """The exact quick-ref block: one line per host, sourced live from
    state_facts. Retired/decommissioned hosts render as a closure line, not
    stale IPs -- the same self-expiry lumen_state.py's own docstring describes
    for the store itself, extended to this projection."""
    now = now or datetime.now(timezone.utc)
    ts = now.strftime('%Y-%m-%d %H:%M UTC')
    lines = [
        '## Fleet hardware quick-ref (auto-rendered)',
        f'_Generated {ts} by `lumen_state.py render --prefs-block` from '
        'minerva.db state_facts (current slice). Never hand-edit -- re-render, '
        'or wait for the next daily drift ticket._',
        '',
    ]
    for subject in HOSTS:
        by_pred = _host_facts(subject)
        name = DISPLAY_NAME[subject]
        label, fact = _retirement(by_pred)
        if label:
            since = fact['valid_from'][:10]
            lines.append(f'- **{name}** -- {label} (since {since})')
            continue
        user = by_pred.get('ssh_user', {}).get('value_display', 'unset in state_facts')
        ts_ip = by_pred.get('tailscale_ip', {}).get('value_display', 'unset in state_facts')
        lan_ip = by_pred.get('lan_ip', {}).get('value_display', 'unset in state_facts')
        lines.append(f'- **{name}** -- user: {user}, tailscale: {ts_ip}, lan: {lan_ip}')
    lines.append('')
    lines.append('Known-stale in the last hand-maintained copy:')
    for note in KNOWN_STALE_FLAGS:
        lines.append(f'- {note}')
    return '\n'.join(lines) + '\n'


def load_last(path=LAST_PATH):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def save_last(text, path=LAST_PATH):
    with open(path, 'w') as f:
        f.write(text)


def diff_against_last(new_text, path=LAST_PATH):
    """Unified diff of new_text against the last handed-out copy, or None when
    nothing changed (no prior copy counts as a change: the first render is
    itself drift worth a ticket)."""
    old_text = load_last(path)
    if old_text == new_text:
        return None
    old_lines = (old_text or '').splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    return ''.join(difflib.unified_diff(
        old_lines, new_lines, fromfile='prefs_block.last', tofile='rendered'))


def _prior_open_tickets(db_path=WQ_DB):
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE lane='user' AND status='open' AND title LIKE ?",
            (TITLE_PREFIX + '%',)).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _wq_add(title, detail, wq_bin=WQ_BIN):
    # wq#2572: `wq add` (wq#1676) hard-refuses (exit 6) a user-lane ticket with no
    # `Liveness:` line as an untagged Frank-gate. This drift-notice ticket has no
    # deterministic completion probe -- it is superseded by the NEXT daily check
    # (check_and_file_ticket's own diff-against-last logic), not resolved by a
    # machine-checkable condition -- so it takes the --no-probe escape hatch. This
    # was the actual root cause of prefs-block-check.service's failed-unit state
    # (ISSUE-LOG wq2572): a real crash, not a finding, unrelated to any exit-code
    # convention -- `wq add` started refusing the call and this call site was
    # never updated.
    out = subprocess.run(
        [wq_bin, 'add', '--lane', 'user', '--pri', '2', '--title', title,
         '--detail', detail, '--no-probe',
         'drift-notice ticket, superseded by next daily check, no deterministic probe',
         # wq#3504: wq add's duplicate-title guard refused this call whenever the previous
         # drift ticket was still open (exactly the case _wq_supersede exists for, but it
         # only runs AFTER a successful add) -> crash -> failed unit -> sentinel ticket that
         # could never clear. --force is safe: the prior open ticket of this title is
         # superseded immediately after in check_and_file_ticket.
         '--force'],
        capture_output=True, text=True, check=True).stdout
    m = re.search(r'added task (\d+)', out)
    if not m:
        raise RuntimeError(f'unexpected `wq add` output: {out!r}')
    return int(m.group(1))


def _wq_supersede(old_id, new_id, wq_bin=WQ_BIN):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    subprocess.run(
        [wq_bin, 'edit', str(old_id), '--status', 'done', '--detail',
         f'SUPERSEDED by wq#{new_id} (fleet hardware quick-ref drift check, '
         f'{today}) -- that ticket carries the current block.'],
        capture_output=True, text=True, check=True)


def check_and_file_ticket(now=None, wq_bin=WQ_BIN, last_path=LAST_PATH,
                           db_path=WQ_DB):
    """Daily drift check. Renders, diffs against the last handed-out copy, and
    on drift ONLY: files one user-lane ticket with the new block pasted inline,
    supersedes any prior open ticket of the same kind, and updates the
    handed-out copy. Returns the new ticket id, or None when nothing drifted."""
    rendered = render_prefs_block(now=now)
    diff = diff_against_last(rendered, path=last_path)
    if diff is None:
        return None
    detail = rendered + '\n---\ndiff vs. last handed-out copy:\n\n' + diff
    new_id = _wq_add(f'{TITLE_PREFIX} hand-maintained copy is stale -- paste this in',
                      detail, wq_bin=wq_bin)
    for old_id in _prior_open_tickets(db_path=db_path):
        if old_id != new_id:
            _wq_supersede(old_id, new_id, wq_bin=wq_bin)
    save_last(rendered, path=last_path)
    return new_id


def _main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('render')
    sub.add_parser('check')
    a = ap.parse_args()
    if a.cmd == 'render':
        print(render_prefs_block())
    elif a.cmd == 'check':
        new_id = check_and_file_ticket()
        print(f'wq#{new_id} filed (drift)' if new_id else 'no drift, no ticket')


if __name__ == '__main__':
    _main()

#!/usr/bin/env python3
"""minerva_defer_review — make 'defer' a state that ends, not a dead letter.

WHY (audited 2026-08-14): minerva_eval consumes status='accept' only. Nothing has
ever read status='defer'. 495 defers had decided_at NULL and the last human sweep
was 2026-08-10, so ~50 items a day were being written to a table nobody reads.
Worse, the single most common defer reason is an INFORMATION GAP ("uncertain
applicability", "needs a benchmark first") - which is not a verdict, it is a
research question that Lumen already has four idle research workers able to answer.

Four outcomes, no fifth:
  PILOT     (wq#1559) defers whose reason is an EMPIRICAL question - "needs a
            benchmark", "A/B it first", "measure against what we have" - and whose
            impact is H or M become an experiment on idle fleet hardware: an
            experiment card at foreman/pilots/<id>/CARD.md plus a ticket in the
            'pilot' wq lane. Status 'piloting'. Desk research cannot answer a
            question whose answer is a number; before wq#1559 these either got a
            literature review or aged out unjudged.
  RESEARCH  information-gap defers worth resolving become real research prompts in
            /home/lumen/scratch/research_prompts/ (the queue the fleet workers
            already drain) and move to status='researching'.
  REAP      when that prompt lands in research_prompts/done/ - or a pilot writes its
            REPORT.md - the proposal is re-scored with the note attached -> accept /
            defer / reject, for real. One verdict function, two producers.
  RETIRE    settled defers ("overlaps what we already have", "paper not a tool")
            and anything older than STALE_DAYS become 'rejected_aged' with a reason,
            so the pile drains instead of growing.

FORM FAILURE IS NOT A VERDICT (wq#1559). A note with no 'RECOMMENDATION:' line used
to retire the proposal - 19 of 72 researched defers died that way, never judged. It
now buys exactly one enforced re-run (deepseek-v4 rung; the format is fail-closed at
lumen_fleet_graph.write_output, so a formless note is never written at all) and
retires only if the second attempt also comes back formless.

Read-only against everything except the proposals table and the prompt queue.
Usage: minerva_defer_review.py [review|reap|status] [--dry] [--limit N]
"""
import json, os, re, sqlite3, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

DB = '/home/lumen/minerva/minerva.db'
PROMPTS = Path('/home/lumen/scratch/research_prompts')
DONE = PROMPTS / 'done'
MIN_AGE_DAYS = int(os.getenv('DEFER_MIN_AGE_DAYS', '7'))
STALE_DAYS = int(os.getenv('DEFER_STALE_DAYS', '45'))
MAX_PROMPTS = int(os.getenv('DEFER_MAX_PROMPTS', '5'))
PREFIX = 'minerva_defer_'
# wq#2489: the verdict line is a contract (foreman harness/output_contract.py). Loaded by
# path so a missing or broken foreman tree degrades to the inline regex instead of taking
# the whole daily review down. A note is read in FULL: the verdict is written last.
NOTE_READ_CAP = 200_000
_CONTRACT_PATH = os.getenv('DEFER_OUTPUT_CONTRACT', '/home/lumen/foreman/harness/output_contract.py')


def _load_contract():
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('output_contract', _CONTRACT_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['output_contract'] = mod   # dataclasses resolve annotations through it
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        sys.modules.pop('output_contract', None)
        print(f'output_contract unavailable ({str(e)[:120]}); using the inline regex', file=sys.stderr)
        return None


_CONTRACT = _load_contract()

# wq#1559 — pilot lane.
PILOTS = Path(os.getenv('DEFER_PILOT_ROOT', '/home/lumen/foreman/pilots'))
CARD_TEMPLATE = PILOTS / 'EXPERIMENT_CARD.template.md'
MAX_PILOTS = int(os.getenv('DEFER_MAX_PILOTS', '3'))
WQ_BIN = os.getenv('DEFER_WQ_BIN', '/home/lumen/bin/wq')
PILOT_LANE = os.getenv('DEFER_PILOT_LANE', 'pilot')
PILOT_NODE = os.getenv('DEFER_PILOT_NODE', 'hathor')
# Budgets are per-pilot defaults written into the card; a card may be edited before
# the run, but never by the runner itself.
PILOT_WALLCLOCK_MIN = int(os.getenv('DEFER_PILOT_WALLCLOCK_MIN', '90'))
PILOT_TOKEN_BUDGET = int(os.getenv('DEFER_PILOT_TOKEN_BUDGET', '150000'))

# wq#1559 — the enforced re-run. Pinned rung, issued DIRECTLY at the LiteLLM gateway
# rather than re-queued to the fleet worker pool.
#
# WHY NOT THE POOL: a prompt in scratch/research_prompts/ is claimed by whichever of
# the four fleet workers gets there first, and call_inference picks its model from
# PROVIDER_TO_MODEL[FLEET_PROVIDER] — a per-WORKER env var. There is no per-prompt
# rung selection anywhere in the worker or the graph. Adding one would send a
# deepseek call while record_call()/can_run() still charge the claiming worker's
# provider bucket, so cerebras quota would be spent on deepseek traffic and the
# token log would attribute it to the wrong provider. That is a real accounting bug
# to buy one retry with. A direct, single, synchronous call spends the rung it names
# and logs it as such.
RERUN_RUNG = os.getenv('DEFER_RERUN_RUNG', 'deepseek-v4-flash')
RERUN_MAX_PER_PASS = int(os.getenv('DEFER_RERUN_MAX_PER_PASS', '3'))
LITELLM_URL = os.getenv('LITELLM_URL', 'http://localhost:4000/v1/chat/completions')
RERUN_SUFFIX = """

--- RE-RUN (wq#1559). The previous answer to this exact question was discarded
UNREAD because it did not carry the required verdict line. Nothing about the
research changed; only the format was wrong. Answer the question again and end the
report with, on its own line and spelled exactly this way:

RECOMMENDATION: ADOPT

(or WATCH, or SKIP). A report without that line is discarded again and the proposal
is retired unjudged, so the line is not optional."""

INFO_GAP = re.compile(
    r'uncertain|unclear|no evidence|not proven|requires (a )?(benchmark|validation|investigation|evaluation)'
    r'|pending (a )?(benchmark|test|evaluation)|needs? (a )?(benchmark|measurement|proof)|unverified', re.I)
# Extended 2026-08-14 after sampling the real pile: the first cut left 241 of 495
# defers unclassified, and most of them were plainly settled - "academic paper, not
# a tool", "impractical on this hardware", "would require re-architecting". Those
# are verdicts already, so they retire instead of aging for 45 more days.
SETTLED = re.compile(
    r'overlap|already (has|have|built|exists|provides)|duplicat|off-mission|not applicable'
    r'|superseded|no clear (immediate )?use case|is a benchmark, not|theoretical paper|lacks a ready'
    r'|not (a )?(directly )?(ready|actionable|applicable)|academic (paper|technique|framework)'
    r'|no ready-to-use|impractical|requires? (a )?gpu|no gpu|beyond (the )?scope'
    r'|re-?architect|not directly proven|large new subsystem|speculative', re.I)

# wq#1559 — the empirical shape. These reasons do not name a missing FACT (which
# desk research can fetch); they name a missing MEASUREMENT, which only a run on
# real hardware produces. Checked BEFORE INFO_GAP in cmd_review on purpose: a reason
# like "requires a benchmark first" matches both, and the right answer is to run the
# benchmark, not to read about one.
PILOT_SHAPED = re.compile(
    r'\bpilot\b|\bbenchmark|\bA/B\b|\ba-b test|\bab test|\btrial\b|\bmeasure'
    r'|\bcompare[ds]?\b|\bcomparison\b|head-to-head|small-scale', re.I)
PILOT_IMPACT = ('H', 'M')


def log(m):
    print(f'[{datetime.now(timezone.utc).strftime("%H:%M:%S UTC")}] {m}', flush=True)


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _value_ok(r):
    """Worth spending a research worker on."""
    return (r['impact'] in ('L', 'XL')) or (r['effort'] in ('S', 'M') and r['risk'] in ('S', 'M'))


def build_prompt(r):
    """The prompt names the exact unknown that caused the defer, so the research
    worker resolves THAT rather than re-summarising the source article."""
    return f"""RESEARCH PROMPT: resolve the open question blocking proposal {r['id']}

Proposal: {r['title']}
Deferred on: {(r['created_at'] or '')[:10]}
Why it was deferred: {r['decision_reason']}

What Lumen does today in this area:
{(r['current_state'] or '')[:800]}

What was proposed:
{(r['proposed_change'] or '')[:1000]}

OBJECTIVE — answer only the question that caused the deferral, with evidence:
1. Is the claimed capability real and currently maintained? Cite sources with dates.
2. Does it actually fit a CPU-only Dell T5810 (Xeon E5-2699 v4, no AVX-512/AMX, no
   inference GPU, 64GB RAM, all model traffic through a local LiteLLM gateway)?
   If it needs a GPU, AVX-512, AMX or a hosted API with per-token billing, say so
   plainly and recommend SKIP.
3. Where does it overlap what Lumen already runs, and what would it replace?
4. Give measured or vendor-published numbers where they exist; label which is which.

END WITH: RECOMMENDATION: ADOPT | WATCH | SKIP, one paragraph of reasoning, and — if
ADOPT — the smallest concrete first change (one file or one new service, named).
No paid APIs, no Anthropic spend."""


def build_card(r):
    """The experiment card. Every field a runner needs to execute the pilot without
    asking a question, and the kill criteria that let it stop early without asking
    permission. Rendered from the template when one exists so the card format is
    edited in one place (foreman/pilots/EXPERIMENT_CARD.template.md)."""
    report = PILOTS / r['id'] / 'REPORT.md'
    fields = {
        'PID': r['id'],
        'TITLE': r['title'] or '',
        'DEFERRED_ON': (r['created_at'] or '')[:10],
        'DEFER_REASON': r['decision_reason'] or '',
        'CURRENT_STATE': (r['current_state'] or '')[:800],
        'PROPOSED_CHANGE': (r['proposed_change'] or '')[:1200],
        'IMPACT': r['impact'] or '?',
        'EFFORT': r['effort'] or '?',
        'RISK': r['risk'] or '?',
        'NODE': PILOT_NODE,
        'WALLCLOCK_MIN': str(PILOT_WALLCLOCK_MIN),
        'TOKEN_BUDGET': str(PILOT_TOKEN_BUDGET),
        'REPORT_PATH': str(report),
        'GENERATED': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    try:
        tpl = CARD_TEMPLATE.read_text()
    except Exception:
        tpl = _CARD_FALLBACK
    for k, v in fields.items():
        tpl = tpl.replace('{{' + k + '}}', v)
    sys.path.insert(0, '/home/lumen/foreman/harness')
    import pilot_spec_author
    tpl, spec = pilot_spec_author.fill_card(tpl, dict(r), pilot_spec_author.gateway_call())
    return tpl


# Used only if the template file is missing. Kept minimal on purpose: a card that
# renders from a fallback should look obviously unstyled, not silently authoritative.
_CARD_FALLBACK = """# PILOT {{PID}} — {{TITLE}}

Generated {{GENERATED}} by minerva_defer_review (wq#1559). Template missing.

Deferred {{DEFERRED_ON}} because: {{DEFER_REASON}}

Impact {{IMPACT}} / effort {{EFFORT}} / risk {{RISK}}
Node: {{NODE}} · wall-clock budget {{WALLCLOCK_MIN}} min · token budget {{TOKEN_BUDGET}}
Report to: {{REPORT_PATH}} (must end with RECOMMENDATION: ADOPT | WATCH | SKIP)

## Current state
{{CURRENT_STATE}}

## Proposed change
{{PROPOSED_CHANGE}}
"""


def _pilot_ticket_detail(r, card_path):
    return (f"PILOT (wq#1559 defer pipeline) for proposal {r['id']}: {r['title']}\n\n"
            f"Experiment card (read it FIRST, it is the spec): {card_path}\n"
            f"Report to: {PILOTS / r['id'] / 'REPORT.md'} — must end with a line\n"
            f"'RECOMMENDATION: ADOPT | WATCH | SKIP' plus the measured numbers.\n\n"
            f"Run on {PILOT_NODE}. Baseline first, then treatment, same fixed sample.\n"
            f"Do NOT wire anything into prod from the pilot: an ADOPT feeds PLAN like\n"
            f"any other accept and is built through the normal path.\n\n"
            f"Why it was deferred: {(r['decision_reason'] or '')[:600]}")


def _queue_pilot(r):
    """Write the card, open the ticket, move the proposal to 'piloting'. The ticket
    is created BEFORE the status flips: if wq refuses (mis-lane, duplicate title,
    queue locked) the proposal stays 'defer' and the next pass retries, rather than
    stranding it in a status with no ticket behind it."""
    d = PILOTS / r['id']
    d.mkdir(parents=True, exist_ok=True)
    card = d / 'CARD.md'
    card.write_text(build_card(r))
    title = f"[pilot] {(r['title'] or r['id'])[:90]}"
    try:
        out = subprocess.run(
            [WQ_BIN, 'add', '--lane', PILOT_LANE, '--pri', '2', '--title', title,
             '--detail', _pilot_ticket_detail(r, card),
             # ISSUE-LOG #257/#265/#337/#369/#397/#430/#663/#666/#778 (wq#2688 session,
             # 2026-09-25): every pilot names its node in the card ("Run on {{NODE}}")
             # but the ticket carried no needs: tag, so fworker.resolve_exec_grant
             # never minted a FOREMAN_GRANT_FILE and every `ssh <node>`/`fleet-exec run
             # <node>` from the worker was denied (grant_missing_env / requires
             # approval) -- documented as "the fix" at least 9 times and never wired in.
             '--needs', f'ssh({PILOT_NODE})'],
            capture_output=True, text=True, timeout=60)
    except Exception as e:
        log(f'  PILOT {r["id"]} wq add failed ({e}) — left as defer, will retry')
        return None
    if out.returncode != 0:
        log(f'  PILOT {r["id"]} wq add rc={out.returncode}: {(out.stderr or out.stdout)[:200]}')
        return None
    m = re.search(r'#?(\d+)', out.stdout or '')
    tid = m.group(1) if m else '?'
    c = db()
    c.execute("UPDATE proposals SET status='piloting', decision_reason=? WHERE id=?",
              (f'Deferred for a measurement; pilot wq#{tid}, card {card}. '
               f'Original: {(r["decision_reason"] or "")[:260]}', r['id']))
    c.commit(); c.close()
    return tid


def cmd_review(limit=MAX_PROMPTS, dry=False):
    PROMPTS.mkdir(parents=True, exist_ok=True)
    c = db()
    rows = c.execute(
        "SELECT * FROM proposals WHERE status='defer' "
        "AND created_at <= datetime('now', ?) ORDER BY created_at ASC",
        (f'-{MIN_AGE_DAYS} days',)).fetchall()
    log(f'defers older than {MIN_AGE_DAYS}d: {len(rows)}')
    sent = retired = piloted = 0
    for r in rows:
        reason = r['decision_reason'] or ''
        stale = c.execute("SELECT 1 FROM proposals WHERE id=? AND created_at <= datetime('now', ?)",
                          (r['id'], f'-{STALE_DAYS} days')).fetchone() is not None
        if SETTLED.search(reason) and not INFO_GAP.search(reason):
            if not dry:
                _retire(r, 'settled at generation time: ' + reason[:200])
            retired += 1
            continue
        # wq#1559: PILOT before RESEARCH. An empirical question ("needs a benchmark")
        # matches INFO_GAP too, and desk research answers it with a literature review
        # instead of a number. Impact-gated so the lane runs the ones worth a node.
        if (PILOT_SHAPED.search(reason) and (r['impact'] or '') in PILOT_IMPACT
                and piloted < MAX_PILOTS):
            if _already_piloted(r['id']):
                continue
            if dry:
                log(f'  PILOT {r["id"]} {r["title"][:60]}')
                piloted += 1
                continue
            tid = _queue_pilot(r)
            if tid is not None:
                log(f'  PILOT {r["id"]} -> wq#{tid} {r["title"][:60]}')
                piloted += 1
            continue
        if INFO_GAP.search(reason) and _value_ok(r) and sent < limit:
            if _already_queued(r['id']):
                continue
            if not dry:
                _queue_research(r)
            log(f'  RESEARCH {r["id"]} {r["title"][:60]}')
            sent += 1
            continue
        if stale:
            if not dry:
                _retire(r, f'no path to a decision after {STALE_DAYS} days: ' + reason[:180])
            retired += 1
    c.close()
    log(f'review done: pilot={piloted} research={sent} retired={retired} dry={dry}')
    return sent, retired, piloted


def _already_piloted(pid):
    """A card on disk means this proposal already has (or had) a pilot. Cheap and
    idempotent — a re-run of review never opens a second ticket for the same id."""
    return (PILOTS / pid / 'CARD.md').exists()


def _already_queued(pid):
    for d in (PROMPTS, DONE, PROMPTS / 'stuck'):
        try:
            if any(f.name.startswith(PREFIX + pid) for f in d.iterdir()):
                return True
        except Exception:
            pass
    return False


def _queue_research(r):
    path = PROMPTS / f'{PREFIX}{r["id"]}.txt'
    path.write_text(build_prompt(r))
    c = db()
    c.execute("UPDATE proposals SET status='researching', decision_reason=? WHERE id=?",
              (f'Deferred for an information gap; research queued at {path.name}. '
               f'Original: {(r["decision_reason"] or "")[:280]}', r['id']))
    c.commit(); c.close()


def _retire(r, why):
    c = db()
    c.execute("UPDATE proposals SET status='rejected_aged', decided_at=?, decision_reason=? WHERE id=?",
              (datetime.now(timezone.utc).isoformat(), f'defer-review: {why}'[:500], r['id']))
    c.commit(); c.close()


def cmd_reap(dry=False):
    """A researcher or a pilot came back -> re-score the proposal with its note.

    Two producers, one verdict function. 'researching' reads the fleet research note;
    'piloting' reads foreman/pilots/<id>/REPORT.md. Both are scored by _verdict_from,
    so a pilot needs no special case downstream and an ADOPT reaches PLAN exactly the
    way a research ADOPT does."""
    c = db()
    rows = c.execute("SELECT * FROM proposals WHERE status IN ('researching','piloting')").fetchall()
    c.close()
    done = 0
    reruns = 0
    for r in rows:
        piloting = r['status'] == 'piloting'
        note = _find_report(r['id']) if piloting else _find_note(r['id'])
        if not note:
            continue
        text = ''
        try:
            text = note.read_text(errors='replace')[:NOTE_READ_CAP]
        except Exception:
            continue
        kind = 'pilot' if piloting else 'research'
        log(f'  REAP {r["id"]} <- {note.name} ({len(text)} chars, {kind})')
        if dry:
            done += 1
            continue
        verdict, why = _verdict_from(text)
        if verdict == 'rerun':
            # wq#1559: form failure is not a verdict. One enforced re-run, then retire.
            if _rerun_count(r) >= 1:
                _set(r['id'], 'rejected_aged',
                     f'{kind} returned no RECOMMENDATION line after one enforced re-run')
                log(f'    -> RETIRE {r["id"]} (second formless {kind} note)')
            elif reruns >= RERUN_MAX_PER_PASS:
                log(f'    -> DEFER RERUN {r["id"]} (pass cap {RERUN_MAX_PER_PASS} hit)')
                continue
            else:
                _requeue_formless(r, note, kind)
                reruns += 1
            done += 1
            continue
        _set(r['id'], verdict, f'defer-review reaped {kind} {note.name}: {why}')
        done += 1
    log(f'reap done: {done}')
    return done


def _set(pid, status, why):
    c = db()
    c.execute('UPDATE proposals SET status=?, decided_at=?, decision_reason=? WHERE id=?',
              (status, datetime.now(timezone.utc).isoformat(), why[:500], pid))
    c.commit(); c.close()


def _rerun_count(r):
    return 1 if 'wq1559-rerun=1' in (r['decision_reason'] or '') else 0


def _requeue_formless(r, note, kind):
    """Move the formless note aside so _find_note/_find_report cannot re-read it, then
    re-queue the SAME question once. The re-run is marked in decision_reason (not a new
    column) so the bound survives a crash between the two writes."""
    try:
        note.rename(note.with_suffix(note.suffix + '.formfail'))
    except Exception as e:
        log(f'    formfail rename failed ({e}) — retiring instead of looping')
        _set(r['id'], 'rejected_aged', f'{kind} note formless and unmovable: {e}')
        return
    if kind == 'pilot':
        # The pilot ticket is still the artifact of record; re-running means the pilot
        # writes REPORT.md again with the line. Status stays 'piloting'.
        new_reason = f'wq1559-rerun=1 pilot REPORT had no RECOMMENDATION line. ' \
                     f'{(r["decision_reason"] or "")[:300]}'
        _mark(r['id'], 'piloting', new_reason)
        return
    text = _rerun_direct(build_prompt(r) + RERUN_SUFFIX)
    if text is None:
        # Gateway unreachable or the rung refused. Do NOT retire on an infrastructure
        # failure — that is the exact conflation this ticket exists to remove. Leave
        # it researching, unmarked, so the next pass tries the re-run again.
        log(f'    re-run call failed — left researching, not retired')
        return
    if _verdict_from(text)[0] == 'rerun':
        # Fail-closed at the producer: a formless answer is never written to the
        # note queue, so the reaper cannot mis-score what does not exist.
        _set(r['id'], 'rejected_aged',
             f'no RECOMMENDATION line after one enforced re-run on {RERUN_RUNG}')
        log(f'    -> RETIRE {r["id"]} (re-run on {RERUN_RUNG} also formless)')
        return
    try:
        (DONE).mkdir(parents=True, exist_ok=True)
        (DONE / f'{PREFIX}{r["id"]}_rerun.md').write_text(text)
    except Exception as e:
        log(f'    re-run note write failed ({e}) — left researching')
        return
    _mark(r['id'], 'researching',
          f'wq1559-rerun=1 first note had no RECOMMENDATION line; re-ran on '
          f'{RERUN_RUNG}. {(r["decision_reason"] or "")[:260]}')
    log(f'    re-ran {r["id"]} on {RERUN_RUNG}, verdict line present')


def _rerun_direct(prompt):
    """One synchronous call on the pinned rung. Returns text or None (never raises).
    None means infrastructure failed, which is NOT a verdict and never retires."""
    import urllib.request
    key = ''
    try:
        for line in open('/home/lumen/agent/.env'):
            if line.startswith('LITELLM_MASTER_KEY='):
                key = line.split('=', 1)[1].strip().strip('"\'')
                break
    except Exception:
        pass
    body = json.dumps({
        'model': RERUN_RUNG,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 2500,
        'metadata': {'trace_name': 'minerva:defer_rerun',
                     'trace_metadata': {'service': 'minerva_defer_review',
                                        'stage': 'rerun', 'ticket': 'wq1559'}},
    }).encode()
    hdr = {'Content-Type': 'application/json'}
    if key:
        hdr['Authorization'] = f'Bearer {key}'
    try:
        req = urllib.request.Request(LITELLM_URL, data=body, headers=hdr)
        with urllib.request.urlopen(req, timeout=240) as resp:
            d = json.load(resp)
        return d['choices'][0]['message']['content']
    except Exception as e:
        log(f'    rerun gateway error: {str(e)[:160]}')
        return None


def _mark(pid, status, why):
    """Status + reason without stamping decided_at — the item is still in flight."""
    c = db()
    c.execute('UPDATE proposals SET status=?, decision_reason=? WHERE id=?',
              (status, why[:500], pid))
    c.commit(); c.close()


def _find_report(pid):
    p = PILOTS / pid / 'REPORT.md'
    return p if p.exists() else None


def _find_note(pid):
    """The fleet claims a prompt by renaming it, and moves it to done/ when the
    research note is written - so any file carrying the id under done/ counts.

    NEVER returns a .formfail file. _requeue_formless renames a note that came back
    without its verdict line to <name>.formfail, and that name still carries the id
    prefix this function matches on. Today the re-run note happens to win because
    done/ is scanned before scratch/research/, but that is accidental: iterdir()
    order is arbitrary, so a .formfail sibling in the SAME directory could be handed
    back instead. The reaper would then re-read the note it already rejected, spend
    the proposal's one re-run allowance on it, and retire it unjudged - reviving the
    exact defect wq#1559 exists to remove."""
    for d in (DONE, PROMPTS):
        try:
            for f in d.iterdir():
                if PREFIX + pid in f.name and f.is_file() and not f.name.endswith('.formfail'):
                    return f
        except Exception:
            pass
    res = Path('/home/lumen/scratch/research')
    try:
        for f in res.glob(f'*{pid}*'):
            if not f.name.endswith('.formfail'):
                return f
    except Exception:
        pass
    return None


def _read_recommendation(text):
    """(ADOPT|WATCH|SKIP, tail) or (None, ''). Contract module first, inline regex if it
    could not be loaded."""
    if _CONTRACT is not None:
        res = _CONTRACT.parse_or_reask(text, _CONTRACT.Recommendation)
        if res.status == 'ok':
            return res.value.recommendation, res.tail
        return None, ''
    m = re.search(r'RECOMMENDATION:\s*(ADOPT|WATCH|SKIP)', text, re.I)
    return (m.group(1).upper(), text[m.end():m.end() + 300]) if m else (None, '')


def _verdict_from(text):
    """Deterministic read of the required RECOMMENDATION line.

    wq#1559: a missing line is NOT a verdict. It used to return rejected_aged, which
    retired 19 of 72 researched defers for a formatting miss the proposal's author
    never made. It now returns 'rerun' and the caller buys exactly one enforced
    re-run before retiring.

    wq#2489: the misses were not formatting either. cmd_reap sliced the note to its
    first 6000 chars and the line sits at the end of a 6-10k note, so 23 notes with a
    valid verdict were scored formless. The caller now passes the whole note."""
    v, tail = _read_recommendation(text)
    if v is None:
        return 'rerun', 'no RECOMMENDATION line'
    tail = tail.strip().replace(chr(10), ' ')
    if v == 'ADOPT':
        return 'accept', 'research says ADOPT: ' + tail[:200]
    if v == 'WATCH':
        return 'defer', 'research says WATCH: ' + tail[:200]
    return 'rejected_aged', 'research says SKIP: ' + tail[:200]


def cmd_status():
    c = db()
    for row in c.execute("SELECT status, count(*) n, min(date(created_at)) oldest "
                         "FROM proposals GROUP BY status ORDER BY n DESC"):
        print(f"{row['status']:<16} {row['n']:>5}  oldest {row['oldest']}")
    q = c.execute("SELECT count(*) n FROM proposals WHERE status='defer' "
                  "AND created_at <= datetime('now', ?)", (f'-{MIN_AGE_DAYS} days',)).fetchone()
    print(f'\ndefers eligible for review (>{MIN_AGE_DAYS}d): {q["n"]}')
    c.close()


if __name__ == '__main__':
    a = sys.argv[1:]
    dry = '--dry' in a
    lim = MAX_PROMPTS
    if '--limit' in a:
        lim = int(a[a.index('--limit') + 1])
    cmd = a[0] if a and not a[0].startswith('-') else 'status'
    if cmd == 'review':
        cmd_reap(dry); cmd_review(lim, dry)
    elif cmd == 'reap':
        cmd_reap(dry)
    else:
        cmd_status()

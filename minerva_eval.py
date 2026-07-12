#!/usr/bin/env python3
"""
minerva_eval.py - Self-improvement eval loop.

For each accepted proposal:
  1. Create a sandbox workspace (/home/lumen/minerva/eval/<proposal_id>/)
  2. Run pre-deploy safety checks:
     - No rogue scripts running
     - All currently-running Lumen services still healthy
     - Proposed change doesn't touch permanently-off-limits paths (Surface Pro biz, Lumen LAN MAC config, etc.)
  3. If ALL checks pass, mark proposal 'eval_passed=1' and 'status=deploy_ready'
  4. If any check fails, mark 'status=blocked' with the reason
  5. Never actually executes the proposed change - that requires Frank or explicit Claude action

Output: updates the proposals table, writes a decision record to /home/lumen/minerva/eval/<pid>/decision.json
"""
import json, sqlite3, sys, os
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '/home/lumen/minerva')
from minerva_skills import run_skill

DB = Path('/home/lumen/minerva/minerva.db')
EVAL_DIR = Path('/home/lumen/minerva/eval')
LOG = Path('/home/lumen/minerva/logs/eval.log')

# Permanently off-limits paths/keywords (standing rules)
OFFLIMITS_KEYWORDS = [
    'surface pro',           # Frank's refurbishment biz (rule: strictly off-limits)
    '/etc/netplan',          # MAC spoofing risk (rule: never spoof LAN MAC)
    'enp0s25',               # same
    '00:11:22:33:44:55',     # known-bad spoofed MAC
]

# Skill IDs of pre-deploy safety checks (from seed)
SAFETY_CHECKS = [
    ('6e30cf941b0c', {}, 'rogue_script_check'),                        # scan for rogue scripts
    ('ffe2be7bd03b', {'service_name': 'lumen-intel.service', 'minutes': 20}, 'intel_healthy'),
    ('ffe2be7bd03b', {'service_name': 'lumen-fleet-cerebras.service', 'minutes': 60}, 'fleet_cerebras_healthy'),
    ('ffe2be7bd03b', {'service_name': 'lumen-fleet-sambanova.service', 'minutes': 60}, 'fleet_sambanova_healthy'),
    ('ffe2be7bd03b', {'service_name': 'lumen-fleet-mistral.service', 'minutes': 60}, 'fleet_mistral_healthy'),
    ('ffe2be7bd03b', {'service_name': 'lumen-fleet-openrouter.service', 'minutes': 60}, 'fleet_openrouter_healthy'),
]
# NOTE 2026-06-10: ghost_backup check REMOVED (Ghost permanently deleted 2026-05).
# fleet_pbox_healthy REMOVED (PBox permanently retired 2026-06-07).
# fleet_openrouter_healthy ADDED (openrouter is an active fleet worker since 2026-06-07).


def log(m):
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
    line = f'[{ts}] {m}'
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def check_offlimits(proposal):
    """Return (safe: bool, reason: str). False if proposal touches forbidden territory."""
    text = (
        (proposal.get('title') or '') + ' ' +
        (proposal.get('current_state') or '') + ' ' +
        (proposal.get('proposed_change') or '') + ' ' +
        (proposal.get('rollback') or '')
    ).lower()
    for kw in OFFLIMITS_KEYWORDS:
        if kw.lower() in text:
            return False, f'touches_offlimits:{kw}'
    return True, 'no_offlimits_match'


def get_pending_accepted(limit=10):
    """Find proposals the LLM marked as 'accept' but that haven't been eval'd yet."""
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        '''SELECT id, title, current_state, proposed_change, rollback, status, eval_passed
           FROM proposals
           WHERE status = 'accept' AND (eval_passed IS NULL OR eval_passed = 0)
           ORDER BY created_at DESC LIMIT ?''', (limit,)
    ).fetchall()
    conn.close()
    return rows


def update_status(pid, status, eval_result):
    conn = sqlite3.connect(DB)
    conn.execute(
        '''UPDATE proposals
           SET status=?, eval_result=?, eval_passed=?, decided_at=?
           WHERE id=?''',
        (status, json.dumps(eval_result), 1 if eval_result.get('all_passed') else 0,
         datetime.now(timezone.utc).isoformat(), pid))
    conn.commit()
    conn.close()


def eval_proposal(pid, title, current_state, proposed_change, rollback):
    """Run the full eval pipeline for one proposal."""
    log(f'--- Eval [{pid}] {title[:60]} ---')
    workspace = EVAL_DIR / pid
    workspace.mkdir(parents=True, exist_ok=True)

    result = {
        'proposal_id': pid,
        'checks': {},
        'all_passed': False,
        'blocking_reason': None,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

    # Check 1: off-limits keywords
    safe, reason = check_offlimits({
        'title': title, 'current_state': current_state,
        'proposed_change': proposed_change, 'rollback': rollback,
    })
    result['checks']['offlimits'] = {'passed': safe, 'reason': reason}
    if not safe:
        result['blocking_reason'] = f'offlimits: {reason}'
        log(f'  [BLOCK] {pid}: {result["blocking_reason"]}')
        (workspace / 'decision.json').write_text(json.dumps(result, indent=2))
        return result

    # Check 2: safety skills (rogue scripts + active-service health)
    # (ghost_backup check removed 2026-06-10 — Ghost permanently deleted)
    for skill_id, ctx, label in SAFETY_CHECKS:
        r = run_skill(skill_id, context=ctx, timeout_sec=20)
        try:
            payload = json.loads(r.get('stdout', '{}').strip().split('\n')[-1])
        except Exception:
            payload = {'ok': False, 'error': 'parse_fail'}
        passed = payload.get('ok', False)
        result['checks'][label] = {'passed': passed, 'detail': payload}

    # Check 3 (wq#111): cross-model agreement scoring, flag-gated via
    # XAGREE_ENABLED (drop-in minerva-eval.service.d/xagree.conf).
    # Flag OFF => check never added (byte-identical eval behavior).
    # Flag ON  => FAIL-CLOSED: provider error / missing context blocks.
    try:
        import cross_agreement as _xa
        _xa_on = _xa.enabled()
    except Exception:
        _xa_on = False
    if _xa_on:
        # Use the same freshness-aware loader as the proposal pipeline
        # (lumen_state.txt has NOT been regenerated since 2026-06-25 —
        # lumen_state_gen.py no longer writes it — so the raw file is stale;
        # minerva_proposal.LUMEN_STATE falls back to the maintained string).
        try:
            from minerva_proposal import LUMEN_STATE as _ctx
        except Exception:
            _ctx = ''
        if len(_ctx) < 200:
            xr = {'passed': False, 'score': None, 'reason': 'no_lumen_state_context'}
        else:
            try:
                xr = _xa.agreement_check(pid, current_state, _ctx)
            except Exception as _xe:
                xr = {'passed': False, 'score': None, 'reason': f'xagree_error:{_xe}'}
        result['checks']['cross_agreement'] = {'passed': bool(xr.get('passed')), 'detail': xr}
        log(f'  xagree: passed={xr.get("passed")} score={xr.get("score")} reason={xr.get("reason")}')

    # Tally
    all_passed = all(c.get('passed') for c in result['checks'].values())
    result['all_passed'] = all_passed
    if not all_passed:
        failing = [k for k, v in result['checks'].items() if not v.get('passed')]
        result['blocking_reason'] = f'failed_checks: {",".join(failing)}'
    else:
        result['blocking_reason'] = None

    (workspace / 'decision.json').write_text(json.dumps(result, indent=2))
    log(f'  {"[PASS]" if all_passed else "[BLOCK]"} {pid}: all_passed={all_passed}')
    if result['blocking_reason']:
        log(f'  blocking_reason: {result["blocking_reason"]}')
    return result


def run(limit=10):
    log(f'=== Eval loop (limit={limit}) ===')
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    pending = get_pending_accepted(limit)
    log(f'Proposals to eval: {len(pending)}')

    for pid, title, cur, prop, rb, status, eval_passed in pending:
        result = eval_proposal(pid, title, cur, prop, rb)
        new_status = 'deploy_ready' if result['all_passed'] else 'blocked'
        update_status(pid, new_status, result)

    # Summary
    conn = sqlite3.connect(DB)
    ready = conn.execute("SELECT COUNT(*) FROM proposals WHERE status='deploy_ready'").fetchone()[0]
    blocked = conn.execute("SELECT COUNT(*) FROM proposals WHERE status='blocked'").fetchone()[0]
    conn.close()
    log(f'=== Eval done. Ready-to-deploy: {ready} | Blocked: {blocked} ===')
    return {'ready': ready, 'blocked': blocked}


if __name__ == '__main__':
    run()

#!/usr/bin/env python3
"""service_health_check v2: aware of oneshot (timer-triggered) services."""
import json, os, sys, subprocess

ctx = json.loads(os.environ.get('MINERVA_SKILL_CONTEXT', '{}'))
svc = ctx.get('service_name')
if not svc:
    print(json.dumps({'ok': False, 'error': 'missing service_name'}))
    sys.exit(1)
minutes = int(ctx.get('minutes', 15))

# Service state
try:
    active = subprocess.check_output(['systemctl', 'is-active', svc], text=True).strip()
except subprocess.CalledProcessError as e:
    active = e.output.strip() if e.output else 'unknown'

# Service type
try:
    svc_type = subprocess.check_output(['systemctl', 'show', svc, '--property=Type', '--value'], text=True).strip()
except Exception:
    svc_type = 'unknown'

# Last exit status for oneshot
try:
    last_exit = subprocess.check_output(['systemctl', 'show', svc, '--property=ExecMainStatus', '--value'], text=True).strip()
except Exception:
    last_exit = 'unknown'

# Recent errors
try:
    logs = subprocess.check_output(['journalctl', '-u', svc, f'--since={minutes} minutes ago', '--no-pager'], text=True, stderr=subprocess.STDOUT)
    # Only flag real service errors. Ignore:
    #   - WARNING-level lines (transient: rate limits, upstream HTTP 4xx/5xx, retries)
    #   - any line mentioning HTTP status codes (upstream API is not our service)
    #   - 'server disconnected' / 'timeout' style network blips
    IGNORE_PATTERNS = ['warning', 'http 4', 'http 5', 'rate limit', 'retry', 'server disconnected', 'connection reset', 'timeout']
    errors = []
    for ln in logs.splitlines():
        lnl = ln.lower()
        if any(p in lnl for p in IGNORE_PATTERNS):
            continue
        if ' error' in lnl or ' failed' in lnl or 'traceback' in lnl:
            errors.append(ln)
    errors = errors[-10:]
except Exception as e:
    errors = [f'log_fetch_err:{e}']

# Oneshot is healthy if: last exit 0 AND no recent errors
if svc_type == 'oneshot':
    healthy = last_exit == '0' and len(errors) == 0
    reason = 'oneshot_last_exit_ok' if healthy else f'oneshot_exit={last_exit}, errors={len(errors)}'
else:
    healthy = active in ('active', 'activating') and len(errors) == 0
    reason = 'active_no_errors' if healthy else f'state={active}, errors={len(errors)}'

print(json.dumps({
    'ok': healthy, 'service': svc, 'state': active, 'type': svc_type,
    'last_exit': last_exit, 'reason': reason, 'recent_errors': errors,
}))
sys.exit(0 if healthy else 2)

#!/usr/bin/env python3
"""service_backup_and_restart: safe service unit modification helper."""
import json, os, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ctx = json.loads(os.environ.get('MINERVA_SKILL_CONTEXT', '{}'))
svc = ctx.get('service_name')
if not svc:
    print(json.dumps({'ok': False, 'error': 'missing service_name'}))
    sys.exit(1)
unit_path = Path(f'/etc/systemd/system/{svc}.service' if not svc.endswith('.service') else f'/etc/systemd/system/{svc}')
if not unit_path.exists():
    print(json.dumps({'ok': False, 'error': f'unit not found: {unit_path}'}))
    sys.exit(1)
ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
bak = unit_path.with_suffix(f'.service.bak.{ts}')
try:
    shutil.copy2(unit_path, bak)
except PermissionError:
    # Try sudo -n (passwordless) fall-back
    r = subprocess.run(['sudo', '-n', 'cp', str(unit_path), str(bak)], capture_output=True, text=True)
    if r.returncode != 0:
        print(json.dumps({'ok': False, 'error': f'backup failed: {r.stderr}'}))
        sys.exit(1)
# daemon-reload + restart
r1 = subprocess.run(['sudo', '-n', 'systemctl', 'daemon-reload'], capture_output=True, text=True)
r2 = subprocess.run(['sudo', '-n', 'systemctl', 'restart', svc], capture_output=True, text=True)
print(json.dumps({
    'ok': r1.returncode == 0 and r2.returncode == 0,
    'backup': str(bak),
    'daemon_reload_rc': r1.returncode,
    'restart_rc': r2.returncode,
    'restart_stderr': r2.stderr[:400],
}))
sys.exit(0 if (r1.returncode == 0 and r2.returncode == 0) else 2)

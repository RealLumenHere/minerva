#!/usr/bin/env python3
"""rogue_script_check: scan for leaked robot / fanoff scripts."""
import json, subprocess, sys

try:
    out = subprocess.check_output(
        ['pgrep', '-af', 'robot/|fanoff|explore|goto|monitor.sh'],
        text=True, stderr=subprocess.STDOUT,
    )
except subprocess.CalledProcessError:
    out = ''
lines = [ln for ln in out.splitlines() if ln.strip()]
print(json.dumps({'ok': len(lines) == 0, 'rogue_count': len(lines), 'processes': lines}))
sys.exit(0 if len(lines) == 0 else 2)

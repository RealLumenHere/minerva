#!/usr/bin/env python3
"""verify_fleet_output: hallucination guard for fleet outputs."""
import json, os, sys
sys.path.insert(0, '/home/lumen/minerva')
from minerva_verify import verify

ctx = json.loads(os.environ.get('MINERVA_SKILL_CONTEXT', '{}'))
answer = ctx.get('answer', '')
context = ctx.get('context', '')
subject = ctx.get('subject', 'fleet_output')
if not answer or not context:
    print(json.dumps({'ok': False, 'error': 'missing answer or context'}))
    sys.exit(1)
result = verify(answer, context, subject=subject, mode='semantic')
print(json.dumps(result))
sys.exit(0 if result['passed'] else 2)

#!/usr/bin/env python3
"""wq#249: unit test for cross_agreement strict entailment parsing.
Runs against the deployed cross_agreement.py. Before the wq#249 fix the
quoted-boolean cases FAIL (bool("false") coerces True = false accept).
Exit nonzero on any miss. No network, no LLM."""
import sys, json, re, importlib.util
from importlib.machinery import SourceFileLoader

TARGET = sys.argv[1] if len(sys.argv) > 1 else '/home/lumen/minerva/cross_agreement.py'
loader = SourceFileLoader('xa_under_test', TARGET)
spec = importlib.util.spec_from_loader('xa_under_test', loader)
xa = importlib.util.module_from_spec(spec)
sys.modules['xa_under_test'] = xa
loader.exec_module(xa)

# drive the full _judge parse path with a fake transport
def judge_of(content):
    class FakeResp:
        status_code = 200
        def json(self): return {'choices': [{'message': {'content': content}}]}
    xa.requests.post = lambda *a, **k: FakeResp()
    return xa._judge('m', 'claim', 'context', 'key', retries=1)

CASES = [
    ('{"entailed": true}', True, 'plain true'),
    ('{"entailed": false}', False, 'plain false'),
    ('{"entailed": "true"}', True, 'quoted true'),
    ('{"entailed": "false"}', False, 'quoted false  <-- the wq#240 false-accept'),
    ('{"entailed": "TRUE "}', True, 'case/space'),
    ('{"entailed": "yes"}', False, 'non-boolean string -> fail-closed'),
    ('{"entailed": 1}', False, 'int -> fail-closed'),
    ('{"entailed": null}', False, 'null -> fail-closed'),
    ('no json here', None, 'no JSON -> provider failure (None)'),
]
bad = 0
for content, want, label in CASES:
    got = judge_of(content)
    ok = got is want if want is None else got == want and isinstance(got, bool)
    print(f'{"OK  " if ok else "FAIL"} {label}: got {got!r}, want {want!r}')
    bad += 0 if ok else 1
print(f'\n{len(CASES)-bad}/{len(CASES)} passed on {TARGET}')
sys.exit(1 if bad else 0)

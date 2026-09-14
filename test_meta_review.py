"""wq#1927: offline checks for meta_review_synthesis (network mocked)."""
import importlib.util, json, os
from unittest import mock

os.environ['META_REVIEW_ENABLED'] = '1'
spec = importlib.util.spec_from_file_location('minerva_verify', os.path.join(os.path.dirname(__file__), 'minerva_verify.py'))
mv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mv)
assert mv.META_REVIEW_ENABLED is True
assert mv.META_REVIEW_MODEL == os.getenv('META_REVIEW_MODEL', mv.JUDGE_MODEL)


def fake_resp(payload):
    r = mock.Mock()
    r.status_code = 200
    r.json.return_value = {'choices': [{'message': {'content': json.dumps(payload)}}]}
    return r


res = {'citation_score': 0.2, 'judge_score': 0.3, 'final_score': 0.27, 'passed': False,
       'reason': 'citation_low(0.20)', 'claim_labels': ['NOT_IN_CONTEXT'],
       'mean_span_confidence': 0.9, 'failing_claims': ['x']}
mv.LITELLM_KEY = 'test'

with mock.patch.object(mv.requests, 'post', return_value=fake_resp(
        {'verdict': 'reject', 'reject_reason': 'stale-premise', 'rationale': 'premises ungrounded'})):
    out = mv.meta_review_synthesis(res)
assert out == {'verdict': 'reject', 'reject_reason': 'stale-premise',
               'rationale': 'premises ungrounded'}, out

with mock.patch.object(mv.requests, 'post', return_value=fake_resp({'verdict': 'maybe'})):
    assert mv.meta_review_synthesis(res) is None

with mock.patch.object(mv.requests, 'post', return_value=fake_resp(
        {'verdict': 'accept', 'reject_reason': 'bad-idea'})):
    assert mv.meta_review_synthesis(res) == {'verdict': 'accept', 'reject_reason': '', 'rationale': ''}

with mock.patch.object(mv.requests, 'post', return_value=fake_resp(
        {'verdict': 'reject', 'reject_reason': 'something-else'})):
    assert mv.meta_review_synthesis(res) == {'verdict': 'reject', 'reject_reason': '', 'rationale': ''}

with mock.patch.object(mv.requests, 'post', side_effect=RuntimeError('boom')):
    assert mv.meta_review_synthesis(res) is None

bad_http = mock.Mock()
bad_http.status_code = 500
with mock.patch.object(mv.requests, 'post', return_value=bad_http):
    assert mv.meta_review_synthesis(res) is None

mv.LITELLM_KEY = ''
assert mv.meta_review_synthesis(res) is None

# Wiring: verify() consults the meta-review when the flag is on, appends the
# classification to the reject reason, and never flips `passed`.
mv.LITELLM_KEY = 'test'
with mock.patch.object(mv, 'semantic_citation_score', return_value=(0.2, [('c', 0.2, 'low')], None)), \
     mock.patch.object(mv, 'judge_entailment_uq', return_value=(0.0, ['NOT_IN_CONTEXT'], 'r', None, [1.0])), \
     mock.patch.object(mv, 'meta_review_synthesis', return_value={'verdict': 'reject', 'reject_reason': 'stale-premise', 'rationale': 'x'}) as mmeta, \
     mock.patch.object(mv, 'sqlite3') as msql:
    out = mv.verify('Some claim.', 'context', subject='selftest_meta', mode='semantic')
assert mmeta.called
assert out['passed'] is False
assert out['meta_review']['reject_reason'] == 'stale-premise'
assert out['reason'].endswith(';meta:stale-premise'), out['reason']

with mock.patch.object(mv, 'semantic_citation_score', return_value=(0.2, [], None)), \
     mock.patch.object(mv, 'judge_entailment_uq', return_value=(0.0, ['NOT_IN_CONTEXT'], 'r', None, [1.0])), \
     mock.patch.object(mv, 'meta_review_synthesis', return_value={'verdict': 'accept', 'reject_reason': '', 'rationale': 'x'}), \
     mock.patch.object(mv, 'sqlite3'):
    out = mv.verify('Some claim.', 'context', subject='selftest_meta', mode='semantic')
assert out['passed'] is False  # advisory: meta accept cannot flip a primary reject

mv.META_REVIEW_ENABLED = False
with mock.patch.object(mv, 'semantic_citation_score', return_value=(0.9, [], None)), \
     mock.patch.object(mv, 'judge_entailment_uq', return_value=(1.0, ['SUPPORTED'], 'r', None, [1.0])), \
     mock.patch.object(mv, 'meta_review_synthesis') as mmeta, \
     mock.patch.object(mv, 'sqlite3'):
    out = mv.verify('Some claim.', 'context', subject='selftest_meta', mode='semantic')
assert not mmeta.called and 'meta_review' not in out  # flag off: path untouched

print('meta_review_synthesis unit checks OK')

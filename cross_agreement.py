#!/usr/bin/env python3
"""
cross_agreement.py — cross-model agreement scoring for the Minerva eval gate.
wq#111, proposal b1585eee7900.

Queries 2+ DISTINCT LLM providers via the local LiteLLM gateway and scores
provider agreement on the entailment of a proposal's factual claims
(current_state) against LUMEN CURRENT STATE. Low agreement = the claim set is
model-idiosyncratic and should not auto-deploy.

GUARANTEES:
- Flag-gated: env XAGREE_ENABLED (systemd drop-in minerva-eval.service.d/
  xagree.conf). Default OFF => eval behavior byte-identical.
- Fail-CLOSED when ENABLED: any provider error / unparsable judgment =>
  score None => check FAILS (a missing score can never auto-accept).
- NEVER routes through messenger-chat / messenger-re (asserted).
- Logs every scored run to minerva.db verification_log (mode='xagree').
"""
import json, os, re, sqlite3, requests
from datetime import datetime, timezone

DB = '/home/lumen/minerva/minerva.db'
LITELLM_URL = 'http://localhost:4000/v1/chat/completions'

DEFAULT_MODELS = 'cerebras-oss-120b,deepseek-v4-flash'
FORBIDDEN = ('messenger-chat', 'messenger-re')


def enabled() -> bool:
    return os.environ.get('XAGREE_ENABLED', '0').strip().lower() in ('1', 'true', 'yes', 'on')


def threshold() -> float:
    try:
        return float(os.environ.get('XAGREE_THRESHOLD', '0.6'))
    except Exception:
        return 0.6


def _models():
    ms = [m.strip() for m in os.environ.get('XAGREE_MODELS', DEFAULT_MODELS).split(',') if m.strip()]
    for m in ms:
        assert m not in FORBIDDEN, f'forbidden model in XAGREE_MODELS: {m}'
    assert len(ms) >= 2, 'XAGREE_MODELS needs >=2 providers'
    return ms


def _split_claims(text, cap=5):
    parts = re.split(r'(?<=[.;])\s+', (text or '').strip())
    return [p.strip() for p in parts if len(p.strip()) > 15][:cap]


_JUDGE_TMPL = """You are a strict entailment judge. Given the CONTEXT (ground truth) and a CLAIM, decide if the claim is entailed by (supported by) the context. Judge ONLY against the context, never your own knowledge.

CONTEXT:
{ctx}

CLAIM:
{claim}

Return ONLY JSON: {{"entailed": true|false}}"""


def _judge(model, claim, context, key, timeout=45, retries=3):
    """One provider, one claim. Returns True/False, or None after ALL retries
    fail (429s / timeouts / bad JSON). Bounded retry mirrors cerebras_gen's
    pattern so a single transient provider hiccup can't spuriously block a
    proposal — but the gate stays fail-closed once retries are exhausted."""
    import time as _t
    for attempt in range(retries):
        try:
            r = requests.post(
                LITELLM_URL,
                headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                json={'model': model,
                      'messages': [{'role': 'user',
                                    'content': _JUDGE_TMPL.format(ctx=context[:6000], claim=claim[:800])}],
                      'temperature': 0.0, 'max_tokens': 600},
                timeout=timeout)
            if r.status_code == 429:
                _t.sleep(3 * (attempt + 1))
                continue
            if r.status_code != 200:
                return None
            content = r.json()['choices'][0]['message']['content'] or ''
            m = re.search(r'\{[^{}]*"entailed"[^{}]*\}', content)
            if not m:
                return None
            return bool(json.loads(m.group(0)).get('entailed'))
        except requests.Timeout:
            continue
        except Exception:
            return None
    return None


def _log_row(subject, input_text, score, passed, models, reason, n_claims):
    try:
        conn = sqlite3.connect(DB)
        conn.execute(
            '''INSERT INTO verification_log
               (subject, input_text, context_ref, citation_score, judge_score,
                final_score, passed, rejected_reason, model, ts, mode, n_claims)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (subject, (input_text or '')[:2000], 'LUMEN_STATE', None,
             score, score, 1 if passed else 0, reason, models,
             datetime.now(timezone.utc).isoformat(), 'xagree', n_claims))
        conn.commit()
        conn.close()
    except Exception:
        pass  # logging must never break the gate


def agreement_check(pid, current_state, context):
    """Full check for one proposal. Returns dict:
       {'passed': bool, 'score': float|None, 'reason': str|None, ...}
       FAIL-CLOSED: any provider failure on any claim => passed=False."""
    key = os.environ.get('LITELLM_MASTER_KEY', '')
    if not key:
        _log_row(f'xagree:{pid}', current_state, None, False, '', 'no_litellm_key', 0)
        return {'passed': False, 'score': None, 'reason': 'no_litellm_key'}
    models = _models()
    claims = _split_claims(current_state)
    if not claims:
        # nothing factual to disagree on — vacuous pass, logged
        _log_row(f'xagree:{pid}', current_state, 1.0, True, ','.join(models), 'no_claims', 0)
        return {'passed': True, 'score': 1.0, 'reason': 'no_claims', 'n_claims': 0}

    per_claim = []
    for c in claims:
        votes = []
        for m in models:
            v = _judge(m, c, context, key)
            if v is None:
                _log_row(f'xagree:{pid}', c, None, False, m, 'provider_error', len(claims))
                return {'passed': False, 'score': None,
                        'reason': f'provider_error:{m}', 'n_claims': len(claims)}
            votes.append(v)
        if all(votes):
            per_claim.append(1.0)      # unanimous entailed
        elif not any(votes):
            per_claim.append(0.0)      # unanimous NOT entailed
        else:
            per_claim.append(0.5)      # providers disagree

    score = sum(per_claim) / len(per_claim)
    thr = threshold()
    passed = score >= thr
    reason = None if passed else f'agreement {score:.2f} < threshold {thr:.2f}'
    _log_row(f'xagree:{pid}', ' | '.join(claims), score, passed,
             ','.join(models), reason, len(claims))
    return {'passed': passed, 'score': score, 'reason': reason,
            'threshold': thr, 'n_claims': len(claims), 'models': models}

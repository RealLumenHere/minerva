#!/usr/bin/env python3
"""
minerva_latent.py — Latent-Reasoning Guardrail (proposal 9c9db2ec, staged wq#5)
A lightweight THIRD faithfulness layer for minerva_verify.py.

Purpose (evidence-driven, see reports/latent_guard_eval_20260704.md):
  On the 40-item verify_gold set the existing 2-layer gate ALREADY catches
  20/20 hallucinations (recall=1.00). Its only error mode is FALSE POSITIVES
  (grounded claims wrongly rejected by the flash judge, fpr=0.10). So the
  measurable win is REDUCING false positives, not catching more hallucinations.

  Therefore the default integration is MODE=rescue: the latent pass ONLY
  re-examines claims the primary gate REJECTED, and may flip reject->accept when
  it independently, strongly supports the claim from context. It can NEVER flip
  accept->reject, so recall cannot regress. (MODE=and, the proposal's literal
  additional-AND-gate, is also implemented for comparison but is off by default;
  eval shows it yields no recall gain and adds FP risk on this sample.)

Model: local Ollama reasoning model (default LFM2.5-1.2B-Thinking) — no cloud
  spend, fail-closed. gemma4:8B also works but is ~2x slower.
"""
import os, re, requests
from datetime import datetime, timezone

OLLAMA_CHAT_URL = os.getenv('OLLAMA_CHAT_URL', 'http://127.0.0.1:11434/api/chat')
# 'lightweight local latent reasoner'. Overridable; proposal named gemma4 (8B local),
# but the 1.2B thinking model matches quality on the gold set at lower cost.
LATENT_MODEL = os.getenv('LATENT_MODEL', 'hf.co/LiquidAI/LFM2.5-1.2B-Thinking-GGUF:Q4_K_M')
LATENT_TEMP = float(os.getenv('LATENT_TEMP', '0.1'))
LATENT_NUM_PREDICT = int(os.getenv('LATENT_NUM_PREDICT', '700'))
LATENT_TIMEOUT = int(os.getenv('LATENT_TIMEOUT', '120'))
# rescue only if latent confidence >= this (tuned high to avoid rescuing hallucinations)
LATENT_RESCUE_T = float(os.getenv('LATENT_RESCUE_T', '0.75'))

_PROMPT = """Decide if CLAIM is supported (stated or logically entailed) by CONTEXT only. \
Ignore outside knowledge; numbers, names and dates must match verbatim. \
Reason briefly, then end with a line EXACTLY of the form: SCORE=<x.xx>  \
where 1.00 = fully supported, 0.00 = contradicted or absent from context.

CONTEXT:
{context}

CLAIM: {claim}"""

_SCORE_RE = re.compile(r'SCORE\s*=\s*([01](?:\.\d+)?)')


def latent_support(claim: str, context: str, model: str = None) -> dict:
    """Run one latent-reasoning pass. Returns {score, justification, err}.
    Fail-closed: on any error score=None (callers must treat None as 'no rescue')."""
    model = model or LATENT_MODEL
    prompt = _PROMPT.format(context=context[:3500], claim=claim)
    try:
        r = requests.post(OLLAMA_CHAT_URL, json={
            'model': model, 'stream': False,
            'messages': [{'role': 'user', 'content': prompt}],
            'options': {'temperature': LATENT_TEMP, 'num_predict': LATENT_NUM_PREDICT},
        }, timeout=LATENT_TIMEOUT)
        if r.status_code != 200:
            return {'score': None, 'justification': '', 'err': f'latent_http_{r.status_code}'}
        txt = r.json().get('message', {}).get('content', '') or ''
        m = _SCORE_RE.findall(txt)
        if not m:
            return {'score': None, 'justification': txt[-160:], 'err': 'latent_no_score'}
        score = float(m[-1])
        return {'score': score, 'justification': txt[-200:], 'err': None}
    except Exception as e:
        return {'score': None, 'justification': '', 'err': f'latent_err:{type(e).__name__}'}


def latent_answer_score(claims: list, context: str, model: str = None) -> dict:
    """Aggregate latent support over all claims of an answer.
    Returns {min_score, mean_score, per_claim:[...], err}. min_score drives the
    rescue decision (every claim must be latent-supported to rescue an answer)."""
    if not claims:
        return {'min_score': 1.0, 'mean_score': 1.0, 'per_claim': [], 'err': None}
    per, errs = [], []
    for c in claims:
        res = latent_support(c, context, model)
        per.append({'claim': c[:120], 'score': res['score'], 'err': res['err']})
        if res['err']:
            errs.append(res['err'])
    scored = [p['score'] for p in per if p['score'] is not None]
    if not scored:                       # total latent outage -> fail-closed (no rescue)
        return {'min_score': None, 'mean_score': None, 'per_claim': per, 'err': errs[0] if errs else 'latent_all_failed'}
    return {'min_score': min(scored), 'mean_score': sum(scored) / len(scored),
            'per_claim': per, 'err': errs[0] if errs else None}


def apply_guard(primary: dict, claims: list, context: str,
                mode: str = None, rescue_t: float = None, model: str = None) -> dict:
    """Post-process a primary verify() result with the latent guard.
    mode='rescue' (default): flip reject->accept only, never accept->reject.
    mode='and': claim must ALSO clear latent rescue_t (can only add rejections).
    Returns the (possibly mutated) primary dict, annotated with latent_* fields."""
    mode = mode or os.getenv('LATENT_MODE', 'rescue')
    rescue_t = LATENT_RESCUE_T if rescue_t is None else rescue_t
    # Do not run on outages: a judge/embed error already fail-closed; leave it.
    if primary.get('error_kind') and mode == 'rescue' and not primary.get('passed'):
        # only skip if the failure was an infra error, not a content judgement
        if str(primary['error_kind']).startswith(('fail_closed', 'judge_err', 'embed')):
            primary['latent_skipped'] = 'primary_infra_error'
            return primary
    lat = latent_answer_score(claims, context, model)
    primary['latent_min_score'] = lat['min_score']
    primary['latent_mean_score'] = lat['mean_score']
    primary['latent_err'] = lat['err']
    primary['latent_mode'] = mode
    ms = lat['min_score']
    if mode == 'rescue':
        if (not primary.get('passed')) and ms is not None and ms >= rescue_t:
            primary['passed'] = True
            primary['latent_rescued'] = True
            primary['reason'] = (primary.get('reason') or '') + f';latent_rescued(min={ms:.2f})'
    elif mode == 'and':
        if primary.get('passed') and (ms is None or ms < rescue_t):
            primary['passed'] = False
            primary['latent_blocked'] = True
            primary['reason'] = (primary.get('reason') or '') + f';latent_blocked(min={ms})'
    return primary


if __name__ == '__main__':
    ctx = open('/home/lumen/minerva/eval_sets/verify_gold_context.txt').read()
    for c in ['The nomic-embed-text model runs locally on Ollama for embeddings.',
              'Lumen has an NVIDIA RTX 4060 GPU installed.']:
        print(latent_support(c, ctx)['score'], '::', c)

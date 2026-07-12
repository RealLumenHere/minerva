#!/usr/bin/env python3
"""
minerva_proposal.py - Convert high self-relevance intel into structured proposals.
DGM-inspired: each proposal is a candidate self-modification.
Uses Cerebras qwen-3-235b for generation + verify.py for grounding check.
"""
import json, re, sqlite3, time, requests, os, sys, hashlib
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv('/home/lumen/agent/.env')
sys.path.insert(0, '/home/lumen/minerva')
from minerva_verify import verify

DB = Path('/home/lumen/minerva/minerva.db')
PROPOSALS_DIR = Path('/home/lumen/minerva/proposals/pending')
LOG = Path('/home/lumen/minerva/logs/proposal.log')
CEREBRAS_KEY = os.getenv('CEREBRAS_API_KEY', '')   # legacy, unused since 2026-06-07
# 2026-06-07: generation via local LiteLLM gateway. Old direct-Cerebras
# 'qwen-3-235b-a22b-instruct-2507' was removed upstream ~2026-05-28 (silent 404).
LITELLM_URL = 'http://localhost:4000/v1/chat/completions'
LITELLM_KEY = os.getenv('LITELLM_MASTER_KEY', '')
MODEL = os.getenv('MINERVA_MODEL', 'cerebras-oss-120b')

# 2026-06-10: LUMEN_STATE is now AUTO-DERIVED from the live system by
# lumen_state_gen.py (writes lumen_state.txt). The hand-maintained string below
# is only a FALLBACK if the file is missing/stale, because a stale ground-truth
# silently corrupts the hallucination guard. minerva-proposal.service has an
# ExecStartPre that regenerates lumen_state.txt before each run.
def _load_lumen_state():
    from pathlib import Path
    import time as _t
    f = Path('/home/lumen/minerva/lumen_state.txt')
    try:
        if f.exists() and (_t.time() - f.stat().st_mtime) < 6 * 3600:
            txt = f.read_text().strip()
            if len(txt) > 200 and 'PROBE FAILED' not in txt:
                return txt
    except Exception:
        pass
    return LUMEN_STATE_FALLBACK

LUMEN_STATE_FALLBACK = """LUMEN CURRENT STATE (verified 2026-06-07):

Hardware: Dell T5810 (Precision Tower 5810), Ubuntu 26.04, 64GB RAM, no dedicated GPU (AMD RX550 display only), Tailscale 100.117.66.102. Storage: RAID5 477G at /data/lumen, RAID0 1.9T at /scratch.

LLM access: ALL inference via the local LiteLLM gateway (docker, 127.0.0.1:4000). Providers: Cerebras (cerebras-llama), SambaNova, Mistral, DeepSeek, Gemini, OpenRouter-free, plus local Ollama (gemma4 12B, LFM2.5-1.2B, nomic-embed-text). PBox GPU node RETIRED 2026-06-07. NO Anthropic/OpenAI spend.

Pipelines (all systemd services on Lumen):
- lumen-intel: every ~4h, arXiv/Semantic Scholar/HF/GitHub/newsletter feeds -> intel.db seen_items (keyword pre-scoring).
- minerva-classifier (every 4h): two-axis LLM scoring -> classified_intel.
- minerva-proposal (every 4h): THIS module. minerva-eval (every 4h): 7 safety checks -> deploy_ready or blocked.
- minerva-brief (daily 13:00 UTC): morning brief appended to claude_memory.md.
- lumen-scout + lumen-signal-engine (daily 06:00 UTC): GitHub trending + Reddit/HN signals -> VIP prompts.
- lumen-fleet-cerebras/sambanova/mistral/openrouter: research workers consuming /home/lumen/scratch/research_prompts/, writing research notes (NOT blog posts) to /home/lumen/scratch/research/.
- lumen-prompt-engine + lumen-persona-engine: keep the prompt queue full from rotating topic banks.
- lumen-research-index: embeds the research corpus into pgvector (research_docs) via local Ollama nomic-embed-text.
- lumen-pause (13:00 UTC) / lumen-resume (16:00 UTC): daily quiet window for fleet writers.

Minerva layer (DEPLOYED, operational): minerva.db (classified_intel, proposals, skills, archival_notes, verification_log, recall_fts), minerva_verify.py hallucination guard (citation check + LLM judge, fail-closed), skills table with Voyager-style entries, archival memory + recall search (minerva_memory.py).

Real current GAPS (the truth — do not invent others):
- Skills/archival creation has been stalled since 2026-04-22 (tables exist, rarely written to).
- Intel feed keyword pre-scoring is shallow; feed health is not monitored.
- No experiment/eval harness to measure whether adopted changes actually help.
- Prompt-engine topic banks are static, no auto-adaptation.
- No automated deployer for deploy_ready proposals; they require a human/Claude session.

Content policy: NO publishing of any kind. Ghost CMS was permanently deleted 2026-05 for legal reasons. Research outputs MUST become integration proposals, never published content."""

# Resolve the live ground-truth at import (auto-derived if fresh, else fallback).
LUMEN_STATE = _load_lumen_state()

# Scope extension 2026-06-10: the hallucination guard only checks current_state.
# This adds a feasibility gate on the PROPOSED CHANGE so a proposal can't pass by
# proposing to build on retired infra (PBox), deleted infra (Ghost), or off-mission
# work (publishing/SEO/affiliate) or off-limits territory (Surface Pro / MAC spoof).
_OFFLIMITS = ['surface pro', '/etc/netplan', 'enp0s25', 'mac spoof', 'spoof mac', '64:00:6a']
_RETIRED_OR_FORBIDDEN = {
    'pbox': 'targets retired PBox GPU node (gone 2026-06-07)',
    'ghost': 'targets deleted Ghost CMS (gone 2026-05)',
    'publish': 'proposes publishing (off-mission, no CMS)',
    'blog post': 'proposes publishing (off-mission)',
    'seo': 'proposes SEO content (off-mission)',
    'affiliate': 'proposes affiliate monetization (off-mission)',
    'wordpress': 'targets a CMS (no publishing)',
}

def feasibility_check(text: str):
    """Return (ok: bool, reason: str|None) for a proposed change."""
    t = (text or '').lower()
    for kw in _OFFLIMITS:
        if kw in t:
            return False, f'offlimits:{kw}'
    for kw, why in _RETIRED_OR_FORBIDDEN.items():
        if kw in t:
            return False, f'infeasible:{why}'
    return True, None


def _recall_advisory(text, top_k=4):
    """ADVISORY ONLY: surface related past episodes/lessons for the human reviewer.
    Never gates acceptance; fail-open (returns '' on ANY error) so it can never
    break the proposal pipeline. Wired 2026-06-26 (Memory V2 optional hook)."""
    try:
        import lumen_episode
        hits = lumen_episode.recall((text or '')[:600], top_k=top_k) or []
        if not hits:
            return ''
        fail_kinds = {'failure', 'lesson', 'bug', 'pathology', 'outage'}
        lines = []
        for h in hits:
            kind = (h.get('kind') or '').lower()
            flag = 'FAILURE/LESSON' if kind in fail_kinds else 'context'
            title = (h.get('title') or h.get('content') or '')[:120]
            try:
                score = float(h.get('score') or 0.0)
            except Exception:
                score = 0.0
            lines.append(f'- [{flag}] ({kind}) {title}  _(score {score:.2f})_')
        return ('_Episodic recall surfaced related past episodes. Advisory only - not a gate; '
                'weigh before promoting._\n\n' + '\n'.join(lines))
    except Exception as e:
        try:
            log(f'  recall advisory skipped ({e})')
        except Exception:
            pass
        return ''


PROMPT_TMPL = """You are Minerva, Lumen's self-improvement engine. You evaluate whether a new AI tool/technique/framework should be adopted into Lumen's own architecture.

CRITICAL RULES:
- Base every claim about Lumen on the provided LUMEN CURRENT STATE only. Do not invent services, files, or capabilities.
- If the external tool's applicability to Lumen is uncertain, say so. Do NOT fabricate integration details.
- Keep the proposal concrete and falsifiable.

LUMEN CURRENT STATE:
__LUMEN_STATE__

NEW INTEL ITEM TO EVALUATE:
Title: __TITLE__
Source: __SOURCE__
Category: __CATEGORY__
Adoption hint from classifier: __HINT__
Content:
__CONTENT__

Return ONLY a JSON object (no prose):
{
  "title": "<short proposal title, imperative voice>",
  "current_state": "<what Lumen does now in this area - cite the specific service/file>",
  "proposed_change": "<specific concrete change - new service? library? config edit? which file?>",
  "effort": "S|M|L|XL",
  "impact": "S|M|L|XL",
  "risk": "S|M|L|XL",
  "rollback": "<one sentence on how to revert>",
  "decision": "accept|reject|defer",
  "decision_reason": "<1-2 sentences>",
  "integration_steps": ["<step 1>", "<step 2>", "<step 3-5>"]
}

SCORING RUBRIC — score against these ANCHORS, do not default to M. Most items are
NOT M; force yourself to the nearest anchor. If torn between two, pick the one whose
example is closer, never the middle.

EFFORT (implementation cost for Lumen specifically):
- S  = config edit / add one glob / pip install + 10 lines. e.g. "add sqlite-utils dep and wrap one query".
- M  = one new small script + a systemd unit/timer, no schema change. e.g. today's model-watchdog.
- L  = new service touching existing data model, migration, or multi-file refactor. e.g. Memory V2 bi-temporal state.
- XL = re-architecture across several services or a new subsystem. e.g. replacing the whole eval pipeline.

IMPACT (value to Lumen if adopted):
- S  = marginal polish, one edge case, or nice-to-have with a narrow blast radius.
- M  = a real but localized improvement to one pipeline. e.g. a better dedup gate.
- L  = removes a recurring failure class or unblocks multiple downstream items. e.g. endpoint watchdog killing silent-fallback incidents.
- XL = step-change in autonomy/reliability/cost across the whole stack. rare.

RISK (chance/severity of breaking Lumen):
- S  = additive, isolated, trivially revertible.
- M  = touches a live pipeline but fail-open / behind a flag.
- L  = touches instruction-plane, routing, or shared DB; needs a drill.
- XL = could brick Lumen or corrupt state; almost never adopt without staged rollout.

Two calibration examples (do not copy, match the SHAPE):
- "wrap query-db helper for research metadata" -> effort S, impact S, risk S.
- "replace absolute proposal scoring with pairwise-comparative" -> effort M, impact L, risk S."""


def log(m):
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
    line = f'[{ts}] {m}'
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def cerebras_gen(prompt, retries=3):
    if not LITELLM_KEY:
        return ''
    for attempt in range(retries):
        try:
            r = requests.post(
                LITELLM_URL,
                headers={'Authorization': f'Bearer {LITELLM_KEY}', 'Content-Type': 'application/json'},
                json={'model': MODEL, 'messages': [{'role': 'user', 'content': prompt}],
                      'temperature': 0.25, 'max_tokens': 1800},
                timeout=60,
            )
            if r.status_code == 429:
                wait = 3 * (attempt + 1)
                log(f'  cerebras 429, retry in {wait}s (attempt {attempt+1}/{retries})')
                time.sleep(wait)
                continue
            if r.status_code != 200:
                log(f'  cerebras http {r.status_code}: {r.text[:200]}')
                return ''
            return r.json()['choices'][0]['message']['content']
        except Exception as e:
            log(f'  cerebras err: {e}')
            time.sleep(2)
    return ''


def get_candidates(min_self=0.6, limit=10):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        '''SELECT id, source, title, reasoning, category, self_relevance, audience_relevance
           FROM classified_intel
           WHERE self_relevance >= ? AND processed = 0
           ORDER BY self_relevance DESC, audience_relevance DESC
           LIMIT ?''', (min_self, limit)).fetchall()
    conn.close()
    return rows


def mark_processed(intel_id):
    conn = sqlite3.connect(DB)
    conn.execute('UPDATE classified_intel SET processed=1 WHERE id=?', (intel_id,))
    conn.commit()
    conn.close()


def save_proposal(proposal, intel_id, verify_result, recall_md=''):
    pid = hashlib.md5(f'{intel_id}:{proposal.get("title","")}'.encode()).hexdigest()[:12]
    conn = sqlite3.connect(DB)
    conn.execute(
        '''INSERT OR REPLACE INTO proposals
           (id, parent_id, title, source_intel_id, current_state, proposed_change,
            effort, impact, risk, rollback, status, eval_result, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (pid, None, proposal.get('title', '')[:200], intel_id,
         proposal.get('current_state', '')[:1000],
         proposal.get('proposed_change', '')[:2000],
         proposal.get('effort', 'M'),
         proposal.get('impact', 'M'),
         proposal.get('risk', 'M'),
         proposal.get('rollback', '')[:500],
         proposal.get('decision', 'pending'),
         json.dumps(verify_result)[:4000],
         datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

    # Build markdown with plain string concatenation (no f-string braces in frontmatter)
    steps = proposal.get('integration_steps', []) or []
    steps_md = '\n'.join(f'{i + 1}. {s}' for i, s in enumerate(steps))

    md_parts = [
        '---',
        f'id: {pid}',
        f'title: {proposal.get("title","")}',
        f'source_intel_id: {intel_id}',
        f'effort: {proposal.get("effort","M")}',
        f'impact: {proposal.get("impact","M")}',
        f'risk: {proposal.get("risk","M")}',
        f'decision: {proposal.get("decision","pending")}',
        f'verify_passed: {verify_result.get("passed", False)}',
        f'verify_final: {verify_result.get("final_score", 0):.2f}',
        f'created_at: {datetime.now(timezone.utc).isoformat()}',
        '---',
        '',
        f'# {proposal.get("title","")}',
        '',
        f'**Decision:** {proposal.get("decision","pending")} - {proposal.get("decision_reason","")}',
        '',
        '## Current state',
        proposal.get('current_state', ''),
        '',
        '## Proposed change',
        proposal.get('proposed_change', ''),
        '',
        '## Integration steps',
        steps_md,
        '',
        '## Effort / Impact / Risk',
        f'- Effort: {proposal.get("effort","M")}',
        f'- Impact: {proposal.get("impact","M")}',
        f'- Risk: {proposal.get("risk","M")}',
        '',
        '## Rollback',
        proposal.get('rollback', ''),
        '',
        '## Prior experience (advisory)',
        recall_md or '_No related prior episodes surfaced._',
        '',
        '## Verification',
        f'- Passed: {verify_result.get("passed")}',
        f'- Citation score: {verify_result.get("citation_score",0):.2f}',
        f'- Judge score: {verify_result.get("judge_score",0):.2f}',
        f'- Final: {verify_result.get("final_score",0):.2f}',
        f'- Judge rationale: {verify_result.get("judge_rationale","")}',
        '',
    ]
    md = '\n'.join(md_parts)
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = PROPOSALS_DIR / f'{datetime.now(timezone.utc).strftime("%Y%m%d")}_{pid}.md'
    path.write_text(md)
    return pid


def run(min_self=0.6, limit=10):
    log(f'=== Proposal generator (min_self={min_self} limit={limit}) ===')
    candidates = get_candidates(min_self, limit)
    log(f'Candidates: {len(candidates)}')
    generated = 0
    rejected = 0
    for intel_id, source, title, reasoning, category, sr, ar in candidates:
        hint = reasoning.split('HINT:', 1)[1].strip() if 'HINT:' in reasoning else ''
        content = reasoning
        prompt = (PROMPT_TMPL
                  .replace('__LUMEN_STATE__', LUMEN_STATE)
                  .replace('__TITLE__', str(title)[:300])
                  .replace('__SOURCE__', str(source)[:80])
                  .replace('__CATEGORY__', str(category)[:30])
                  .replace('__HINT__', str(hint)[:400])
                  .replace('__CONTENT__', str(content)[:800]))
        # wq#112: CPP formatter (arXiv:2607.08018), flag-gated via
        # /etc/lumen/prompt_engine.cfg cpp_enabled; fail-open at both layers.
        try:
            from cpp_formatter import cpp_format
            prompt = cpp_format(prompt)
        except Exception as _cpp_e:
            log(f'  cpp formatter skipped ({_cpp_e})')
        out = cerebras_gen(prompt)
        if not out:
            log(f'  SKIP [{intel_id[:8]}] {str(title)[:50]} - no output')
            continue
        m = re.search(r'\{[\s\S]*\}', out)
        if not m:
            log(f'  SKIP [{intel_id[:8]}] {str(title)[:50]} - no JSON')
            continue
        try:
            obj = json.loads(m.group(0))
        except Exception as e:
            log(f'  SKIP [{intel_id[:8]}] JSON parse fail: {e}')
            continue

        # Hallucination guard: verify ONLY the current_state claims against LUMEN_STATE
        # (proposed_change is future speculation, not verifiable against current state)
        answer_to_verify = obj.get('current_state', '')[:1500]
        v = verify(answer_to_verify, LUMEN_STATE,
                   subject=f'proposal:{str(title)[:40]}', model=MODEL, mode='semantic')
        if not v['passed']:
            log(f'  REJECT [{intel_id[:8]}] {str(title)[:50]} - hallucination guard failed ({v.get("reason")})')
            mark_processed(intel_id)
            rejected += 1
            continue
        # Scope extension 2026-06-10: feasibility gate on the proposed change.
        feas_ok, feas_reason = feasibility_check(
            f"{obj.get('proposed_change','')} {obj.get('title','')} {obj.get('current_state','')}")
        if not feas_ok:
            log(f'  REJECT [{intel_id[:8]}] {str(title)[:50]} - feasibility gate ({feas_reason})')
            mark_processed(intel_id)
            rejected += 1
            continue
        recall_md = _recall_advisory(f"{obj.get('proposed_change','')} {obj.get('title','')}")
        if recall_md:
            log(f'  RECALL [{intel_id[:8]}] prior-experience advisory attached')
        pid = save_proposal(obj, intel_id, v, recall_md=recall_md)
        mark_processed(intel_id)
        generated += 1
        log(f'  GEN    [{intel_id[:8]}] -> proposal {pid} decision={obj.get("decision")} {str(title)[:50]}')
        time.sleep(0.5)
    log(f'Done. Generated={generated} Rejected={rejected}')
    return generated


if __name__ == '__main__':
    run()

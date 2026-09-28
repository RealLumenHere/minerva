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
                # Live probe + invariants. The probe alone is facts without judgement.
                return txt + chr(10) + chr(10) + INVARIANTS
    except Exception:
        pass
    return LUMEN_STATE_FALLBACK


# ---------------------------------------------------------------------------
# INVARIANTS — appended to the LIVE digest, never replaced by it.
#
# ROOT CAUSE THIS FIXES (audited 2026-08-14): wq#127 restored the auto-derived
# lumen_state.txt on 2026-07-11. _load_lumen_state() then returned the fresh file
# INSTEAD of the hand-maintained fallback — and the generator only emits probed
# facts (hardware, models, units, fleet roles). The judgement guidance that lived
# only in the fallback string vanished from the scorer's context: the GAPS list,
# the ALREADY-BUILT list, and the content policy. The measurable consequence is
# all over the defer pile — dozens of defers reading "Lumen lacks an evaluation
# harness" when eight of them exist and are listed below. Probed facts go stale;
# these do not, so they are appended to whatever the probe produced.
# ---------------------------------------------------------------------------
INVARIANTS = """
Real current GAPS (the truth — do not invent others):
- Skills/archival creation has been stalled since 2026-04-22 (tables exist, rarely written to).
- Intel feed keyword pre-scoring is shallow; feed health is not monitored.
- Research-to-action conversion is noisy: many proposals are rejected at build
  for stale/duplicate premises rather than for being bad ideas.

ALREADY BUILT — do NOT propose these again, and do NOT justify a defer by claiming
they are missing (verified 2026-08-14):
- Eval/experiment harnesses EXIST: DGM v2 paired_eval + paired-bootstrap gate,
  skillhex, pelican_bench, model_audit, predicate_audit, judgearena_bench,
  memory_audit, inflation_check, recall_bench. "Lumen has no eval harness" is FALSE
  and is not a valid reason to defer — name which harness you would measure with.
- Automated deployer EXISTS: minerva_factory (PLAN/GATE/CANARY APPLY/RECORD,
  GREEN auto-apply, YELLOW approval file, RED never-auto, auto-revert). It also
  stages whole new services inert under /home/lumen/factory/staged/.
- Agent prompts DO auto-adapt via DGM v2 + skillhex (human-gated promotion).
- Cross-model agreement (cross_agreement.py, XAGREE_ENABLED) and SpanUQ-lite
  voting in minerva_verify EXIST. Entity novelty: familiarity_scorer.py.
- Memory V3 is deployed: bi-temporal state (lumen_state.py), episodic recall
  (lumen_episode.py), proposal-only consolidation (lumen_dream.py), BGE-M3 hybrid
  retrieval with RRF fusion.
- Near-duplicate suppression EXISTS: minerva_dedup.py (local embedding + entity
  overlap) runs ahead of the verify judge.
- Deferred proposals are re-reviewed: minerva_defer_review.py turns information-gap
  defers into research prompts and retires settled ones.

Hardware truth for feasibility calls: Xeon E5-2699 v4 (Broadwell) — NO AVX-512 and
NO AMX. Anything requiring AVX-512, AMX, or a CUDA GPU is infeasible here; say that
plainly and reject rather than defer.

DECISION DISCIPLINE: "defer" means a specific, answerable question is genuinely open
and worth answering. If the item is infeasible on this hardware, off-mission, or
already covered by the ALREADY BUILT list, REJECT it and say which. Do not use defer
as a way to avoid committing.

Content policy: NO publishing of any kind. Ghost CMS was permanently deleted 2026-05
for legal reasons. Research outputs MUST become integration proposals, never
published content."""


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
- Research-to-action conversion is noisy: many proposals are rejected at build
  for stale/duplicate premises rather than for being bad ideas.

ALREADY BUILT — do NOT propose these again (verified 2026-08-10):
- Eval/experiment harnesses EXIST: DGM v2 paired_eval + paired-bootstrap gate,
  skillhex, pelican_bench, model_audit, predicate_audit, judgearena_bench,
  memory_audit, inflation_check.
- Automated deployer EXISTS: minerva_factory (PLAN/GATE/CANARY APPLY/RECORD,
  GREEN auto-apply, YELLOW approval file, RED never-auto, auto-revert).
- Agent prompts DO auto-adapt via DGM v2 + skillhex (human-gated promotion).
  No /data/lumen/prompt_engine/topics.yaml exists — do not reference it.
- Cross-model agreement (cross_agreement.py, XAGREE_ENABLED) and SpanUQ-lite
  voting in minerva_verify EXIST. Entity novelty: familiarity_scorer.py.

Before proposing a NEW capability, check the ALREADY BUILT list first.

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
URL: __URL__
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
    body = {'model': MODEL, 'messages': [{'role': 'user', 'content': prompt}],
            # aq#353-class fix (2026-08-07): MODEL=deepseek-v4-flash is a
            # REASONING model; 1800 was exhausted entirely by hidden
            # reasoning on complex proposal prompts (Langfuse: output=1800
            # exactly, content="") -> '' -> "SKIP ... no output" (7-8/10
            # candidates per run). Same class already fixed in
            # minerva_classifier (400->1200, 07-27) and minerva_verify
            # judge (800->3000, today).
            'temperature': 0.25, 'max_tokens': 4000}
    if 'deepseek' in str(MODEL):
        # Measured 2026-08-07 on a failing candidate (6806fd88): without JSON
        # mode reasoning ran 3694-6157 tokens (exhausts any sane cap); WITH
        # response_format json_object it dropped to 1852 and produced clean
        # content. JSON mode structurally curbs reasoning sprawl on this
        # prompt class. Gated to deepseek — cerebras rungs may reject it.
        body['response_format'] = {'type': 'json_object'}
    for attempt in range(retries):
        try:
            r = requests.post(
                LITELLM_URL,
                headers={'Authorization': f'Bearer {LITELLM_KEY}', 'Content-Type': 'application/json'},
                json=body,
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


# wq#2027: proposals ff80d5e94515 / 37cb4ce26a9b were generated from a repo
# TITLE only — classified_intel.url and .summary were both empty, so the
# "orchestration patterns" / "memory + skills" claims the judge scored 1.00
# were the classifier's own HINT text, never a fact about the repo. A row
# with no fetched source cannot ground a claim about its subject, so it must
# not be eligible to seed a proposal at all — source-grounding (verify(),
# source_grounding_check()) has nothing to check a claim against otherwise.
def _ensure_intel_source_columns(conn):
    """Additive migration: `summary` may not exist yet on older DBs (mirrors
    minerva_verify._ensure_judge_ms_column). `url` already exists (wq#1500
    schema)."""
    cols = {r[1] for r in conn.execute('PRAGMA table_info(classified_intel)')}
    if 'summary' not in cols:
        conn.execute('ALTER TABLE classified_intel ADD COLUMN summary TEXT')
        conn.commit()


def get_candidates(min_self=0.6, limit=10):
    conn = sqlite3.connect(DB)
    _ensure_intel_source_columns(conn)
    n_ineligible = conn.execute(
        '''SELECT count(*) FROM classified_intel
           WHERE self_relevance >= ? AND processed = 0
             AND (url IS NULL OR trim(url) = '' OR summary IS NULL OR trim(summary) = '')''',
        (min_self,)).fetchone()[0]
    if n_ineligible:
        log(f'  {n_ineligible} eligible-by-score row(s) skipped: no fetched url/summary '
            f'(wq#2027 source-grounding gate — classifier must fetch source before these seed a proposal)')
    rows = conn.execute(
        '''SELECT id, source, title, reasoning, category, self_relevance, audience_relevance, summary, url
           FROM classified_intel
           WHERE self_relevance >= ? AND processed = 0
             AND url IS NOT NULL AND trim(url) != ''
             AND summary IS NOT NULL AND trim(summary) != ''
           ORDER BY self_relevance DESC, audience_relevance DESC
           LIMIT ?''', (min_self, limit)).fetchall()
    conn.close()
    return rows


def mark_processed(intel_id):
    conn = sqlite3.connect(DB)
    conn.execute('UPDATE classified_intel SET processed=1 WHERE id=?', (intel_id,))
    conn.commit()
    conn.close()



def _save_duplicate(proposal, intel_id, match, score):
    """Record a suppressed duplicate with DGM-style lineage (parent_id) so the pile
    stays auditable: what it duplicated, how close, and why it stopped here."""
    pid = hashlib.md5(f'{intel_id}:{proposal.get("title","")}'.encode()).hexdigest()[:12]
    conn = sqlite3.connect(DB)
    conn.execute(
        'INSERT OR REPLACE INTO proposals '
        '(id, parent_id, title, source_intel_id, current_state, proposed_change, '
        'effort, impact, risk, rollback, status, eval_result, created_at, decision_reason) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (pid, match.get('proposal_id'), proposal.get('title', '')[:200], intel_id,
         proposal.get('current_state', '')[:1000], proposal.get('proposed_change', '')[:2000],
         proposal.get('effort', 'M'), proposal.get('impact', 'M'), proposal.get('risk', 'M'),
         proposal.get('rollback', '')[:500], 'duplicate',
         json.dumps({'dedup': {'cosine': round(score, 4),
                               'entity_share': match.get('entity_share'),
                               'of': match.get('proposal_id')}}),
         datetime.now(timezone.utc).isoformat(),
         (f'Near-duplicate of {match.get("proposal_id")} '
          f'"{str(match.get("title",""))[:80]}" (cosine {score:.3f}). '
          f'Suppressed before the verify judge.')[:500]))
    conn.commit(); conn.close()
    return pid


def save_proposal(proposal, intel_id, verify_result, recall_md=''):
    pid = hashlib.md5(f'{intel_id}:{proposal.get("title","")}'.encode()).hexdigest()[:12]
    conn = sqlite3.connect(DB)
    conn.execute(
        '''INSERT OR REPLACE INTO proposals
           (id, parent_id, title, source_intel_id, current_state, proposed_change,
            effort, impact, risk, rollback, status, eval_result, created_at, decision_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (pid, None, proposal.get('title', '')[:200], intel_id,
         proposal.get('current_state', '')[:1000],
         proposal.get('proposed_change', '')[:2000],
         proposal.get('effort', 'M'),
         proposal.get('impact', 'M'),
         proposal.get('risk', 'M'),
         proposal.get('rollback', '')[:500],
         proposal.get('decision', 'pending'),
         json.dumps(verify_result)[:4000],
         datetime.now(timezone.utc).isoformat(),
         proposal.get('decision_reason', '')[:500]))
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
    for intel_id, source, title, reasoning, category, sr, ar, summary, url in candidates:
        hint = reasoning.split('HINT:', 1)[1].strip() if 'HINT:' in reasoning else ''
        # Ground the proposal in the SOURCE text, not in the classifier's own
        # reasoning about it. Proposing from reasoning was self-referential:
        # there was no external claim for the hallucination guard to entail
        # against, which is why judge scores sat at 0.00. Pre-migration rows
        # have no summary, so they keep the old behaviour.
        content = summary or reasoning
        prompt = (PROMPT_TMPL
                  .replace('__LUMEN_STATE__', LUMEN_STATE)
                  .replace('__TITLE__', str(title)[:300])
                  .replace('__SOURCE__', str(source)[:80])
                  .replace('__URL__', str(url or 'n/a')[:300])
                  .replace('__CATEGORY__', str(category)[:30])
                  .replace('__HINT__', str(hint)[:400])
                  .replace('__CONTENT__', str(content)[:3000]))
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

        # DEDUP GATE (2026-08-14). 33 of 266 defers in four days were the same
        # item re-derived from different feeds. Runs before the (expensive) verify
        # judge so a duplicate costs one local embedding, not a judge call.
        # Fail-open: any error inside minerva_dedup returns "not a duplicate".
        try:
            import minerva_dedup
            _dtext = f"{obj.get('title','')}\n{obj.get('proposed_change','')[:600]}"
            _conflict, _route = minerva_dedup.route_conflict(
                f"{obj.get('title','')} {obj.get('proposed_change','')}")
            if _conflict:
                obj['decision'] = 'reject'
                obj['decision_reason'] = (f'Duplicate: LiteLLM route "{_route}" already exists in '
                                          f'litellm_config.yaml. ' + (obj.get('decision_reason') or ''))[:500]
                log(f'  DUP-ROUTE [{intel_id[:8]}] route {_route} already configured -> reject')
                save_proposal(obj, intel_id, {'passed': False, 'reason': 'duplicate_route'})
                mark_processed(intel_id); rejected += 1
                continue
            _isdup, _match, _score = minerva_dedup.check(_dtext)
            if _isdup:
                log(f'  DUP    [{intel_id[:8]}] {str(title)[:44]} ~ {_match["title"][:44]} '
                    f'(cos {_score:.3f}, entity {_match.get("entity_share")})')
                _save_duplicate(obj, intel_id, _match, _score)
                mark_processed(intel_id); rejected += 1
                continue
        except Exception as _de:
            log(f'  dedup skipped (fail-open): {str(_de)[:120]}')

        # Hallucination guard: verify ONLY the current_state claims against
        # per-claim retrieved ground truth (proposed_change is future
        # speculation, not verifiable against current state).
        # 2026-07-27: switched judge context from the static LUMEN_STATE digest
        # to minerva_factpack.build_context() — hybrid of digest + live-probed
        # facts under a closed-world rule. Measured on verify_gold_v2 (hand-
        # audited production sample): judge recall 0.36 -> 0.54 at precision
        # 1.00 (both known-false claims still rejected). Retrieval-only had
        # REGRESSED to 0.21 (negation claims lost the digest's GAPS section),
        # hence hybrid. build_context falls back to the digest on any error,
        # so this path can never see less context than before. The generator
        # prompt (line ~333) intentionally still uses LUMEN_STATE.
        answer_to_verify = obj.get('current_state', '')[:1500]
        try:
            import minerva_factpack
            _verify_ctx = minerva_factpack.build_context(answer_to_verify)
        except Exception as _fpe:
            log(f'  factpack unavailable ({_fpe}) — falling back to static digest')
            _verify_ctx = LUMEN_STATE
        # wq#2027: subject-grounding — checks the proposal's claims about the
        # EXTERNAL subject (title/proposed_change) against the classifier's
        # fetched url/summary, not against Lumen CONTEXT (which is all the
        # checks above this one can see). get_candidates() already guarantees
        # url/summary are non-empty for every row reaching this point.
        v = verify(answer_to_verify, _verify_ctx,
                   subject=f'proposal:{str(title)[:40]}', model=MODEL, mode='semantic',
                   source_url=url, source_summary=summary,
                   subject_claim_text=f"{obj.get('title','')} {obj.get('proposed_change','')}"[:1500])
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
        try:
            import minerva_dedup
            minerva_dedup.record(f"{obj.get('title','')}\n{obj.get('proposed_change','')[:600]}",
                                 intel_id, pid, obj.get('title', ''))
        except Exception:
            pass
        mark_processed(intel_id)
        generated += 1
        log(f'  GEN    [{intel_id[:8]}] -> proposal {pid} decision={obj.get("decision")} {str(title)[:50]}')
        time.sleep(0.5)
    log(f'Done. Generated={generated} Rejected={rejected}')
    return generated


if __name__ == '__main__':
    # wq#412 (2026-08-08): env overrides so the timer can raise throughput
    # without code edits. PROPOSAL_LIMIT=25 set in the service drop-in to
    # drain the 813-item eligible backlog (~7 days at inflow ~30/day);
    # revert = remove the env line. Defaults unchanged.
    run(min_self=float(os.getenv('PROPOSAL_MIN_SELF', '0.6')),
        limit=int(os.getenv('PROPOSAL_LIMIT', '10')))

#!/usr/bin/env python3
"""
minerva_brief.py - Daily morning brief generator.
Synthesizes: new proposals, high-value intel, verification stats, skill library changes.
Auto-appends a summary block to /home/lumen/claude_memory.md for next session.
Runs daily at 13:00 UTC (06:00 PT).
"""
import json, sqlite3, os, requests, re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv('/home/lumen/agent/.env')

DB = Path('/home/lumen/minerva/minerva.db')
BRIEFINGS_DIR = Path('/home/lumen/minerva/briefings')
MEMORY_FILE = Path('/home/lumen/claude_memory.md')
LOG = Path('/home/lumen/minerva/logs/brief.log')
CEREBRAS_KEY = os.getenv('CEREBRAS_API_KEY', '')
MODEL = os.getenv('MINERVA_MODEL', 'cerebras-oss-120b')  # 2026-06-07: vestigial (brief makes no LLM calls); kept in sync with fleet default
PT_TZ = 'America/Los_Angeles'


def log(m):
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
    line = f'[{ts}] {m}'
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def to_pt(utc_iso):
    """Convert UTC ISO timestamp to Pacific for Frank's display."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(utc_iso.replace('Z', '+00:00'))
        return dt.astimezone(ZoneInfo(PT_TZ)).strftime('%Y-%m-%d %H:%M PT')
    except Exception:
        return utc_iso


def gather_data():
    conn = sqlite3.connect(DB)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # New proposals (last 24h)
    proposals = conn.execute(
        '''SELECT id, title, effort, impact, risk, status, created_at, current_state, proposed_change
           FROM proposals WHERE created_at > ? ORDER BY created_at DESC''', (cutoff,)
    ).fetchall()

    # High self-relevance intel (last 24h)
    high_intel = conn.execute(
        '''SELECT source, title, category, self_relevance, audience_relevance, reasoning, ts
           FROM classified_intel
           WHERE ts > ? AND self_relevance >= 0.5
           ORDER BY self_relevance DESC LIMIT 10''', (cutoff,)
    ).fetchall()

    # Verification stats (last 24h)
    vstats = conn.execute(
        '''SELECT COUNT(*) total,
                  SUM(CASE WHEN passed=1 THEN 1 ELSE 0 END) passed,
                  SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END) failed,
                  ROUND(AVG(final_score),2) avg_score
           FROM verification_log WHERE ts > ?''', (cutoff,)
    ).fetchone()

    # Total intel processed (last 24h)
    intel_total = conn.execute(
        'SELECT COUNT(*) FROM classified_intel WHERE ts > ?', (cutoff,)
    ).fetchone()[0]

    # Skills added (lifetime, but flag recent)
    skill_total = conn.execute('SELECT COUNT(*) FROM skills').fetchone()[0]
    skills_new = conn.execute(
        'SELECT COUNT(*) FROM skills WHERE created_at > ?', (cutoff,)
    ).fetchone()[0]

    # Pending proposals backlog
    pending = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status IN ('pending','accept','defer')"
    ).fetchone()[0]

    conn.close()
    return {
        'proposals': proposals,
        'high_intel': high_intel,
        'vstats': vstats,
        'intel_total': intel_total,
        'skill_total': skill_total,
        'skills_new': skills_new,
        'pending': pending,
    }


def build_brief(data):
    now_pt = datetime.now(timezone.utc).astimezone(
        __import__('zoneinfo').ZoneInfo(PT_TZ)
    ).strftime('%Y-%m-%d %H:%M PT')
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    vt, vp, vf, vavg = data['vstats'] if data['vstats'] else (0, 0, 0, 0)
    # 2026-05-18 driftfix: SQL aggregates over an empty filtered set return NULL,
    # not 0. The fallback above only catches the rows-empty case, not the
    # all-NULL-fields case. Guard each numeric field defensively.
    if vt is None: vt = 0
    if vp is None: vp = 0
    if vf is None:
        log('minerva_brief: verification_failures count was NULL; defaulting to 0. Upstream vstats query returned NULL aggregate — likely empty verification_log table for the window.')
        vf = 0
    if vavg is None: vavg = 0.0

    parts = [
        f'# MINERVA DAILY BRIEF - {today}',
        f'Generated: {now_pt}',
        '',
        '## Overview (last 24h)',
        f'- Intel items classified: {data["intel_total"]}',
        f'- High self-relevance hits (adoptable): {len(data["high_intel"])}',
        f'- Proposals generated: {len(data["proposals"])}',
        f'- Proposals pending decision: {data["pending"]}',
        f'- Skill library size: {data["skill_total"]} (new: {data["skills_new"]})',
        f'- Hallucination guard: {vp}/{vt} passed (avg score {vavg or 0:.2f})',
        '',
    ]

    if data['proposals']:
        parts.append('## Proposals for your review')
        parts.append('')
        for pid, title, eff, imp, risk, status, created, cur, prop in data['proposals']:
            parts.append(f'### [{status.upper()}] {title}')
            parts.append(f'- ID: `{pid}` | Effort: {eff} | Impact: {imp} | Risk: {risk}')
            parts.append(f'- Created: {to_pt(created)}')
            parts.append(f'- Current: {(cur or "")[:300]}')
            parts.append(f'- Proposed: {(prop or "")[:400]}')
            parts.append(f'- File: `/home/lumen/minerva/proposals/pending/{today.replace("-","")}_{pid}.md`')
            parts.append('')
    else:
        parts.append('## Proposals')
        parts.append('_No new proposals in the last 24h._')
        parts.append('')

    if data['high_intel']:
        parts.append('## Top self-relevant intel (potential adoption candidates)')
        parts.append('')
        for source, title, cat, sr, ar, reasoning, ts in data['high_intel']:
            # NULL-safe scores (2026-07-19 fix: rows with unscored self/audience broke .2f)
            sr = sr if sr is not None else 0.0
            ar = ar if ar is not None else 0.0
            hint = ''
            if reasoning and 'HINT:' in reasoning:
                hint = reasoning.split('HINT:', 1)[1].strip()[:200]
            parts.append(f'- **[{cat}]** {title[:100]}')
            parts.append(f'  - self={sr:.2f} audience={ar:.2f} | {source}')
            if hint:
                parts.append(f'  - Hint: {hint}')
        parts.append('')
    else:
        parts.append('## Top self-relevant intel')
        parts.append('_No items scored >= 0.5 self-relevance in the last 24h._')
        parts.append('')

    parts.append('## Actions suggested for Claude next session')
    if data['pending'] > 0:
        parts.append(f'- Review and decide on {data["pending"]} pending proposals in `/home/lumen/minerva/proposals/pending/`')
    if len(data['proposals']) > 0:
        parts.append('- For each accepted proposal: run eval sandbox, then deploy if passing')
    if vf > 0:
        parts.append(f'- Investigate {vf} hallucination rejections (check `verification_log` table)')
    if data['intel_total'] < 20:
        parts.append(f'- Intel intake was low ({data["intel_total"]} items). Check lumen-intel.service status.')
    parts.append('')

    return '\n'.join(parts)


def append_to_memory(brief_summary):
    """Append a compact summary to claude_memory.md for the next session to pick up."""
    if not MEMORY_FILE.exists():
        log(f'memory file not found: {MEMORY_FILE}')
        return
    marker_start = '<!-- MINERVA_BRIEF_START -->'
    marker_end = '<!-- MINERVA_BRIEF_END -->'
    block = f'{marker_start}\n{brief_summary}\n{marker_end}'

    content = MEMORY_FILE.read_text()
    if marker_start in content and marker_end in content:
        # Replace existing block
        pattern = re.compile(re.escape(marker_start) + r'.*?' + re.escape(marker_end), re.DOTALL)
        new_content = pattern.sub(block, content)
    else:
        # Prepend after first section header or at top
        new_content = block + '\n\n' + content
    MEMORY_FILE.write_text(new_content)
    log(f'memory updated with brief block')


def run():
    log('=== Brief generator ===')
    data = gather_data()
    brief = build_brief(data)

    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    brief_path = BRIEFINGS_DIR / f'{today}.md'
    brief_path.write_text(brief)
    log(f'brief written: {brief_path}')

    # Build compact memory block (first 40 lines of brief)
    lines = brief.split('\n')
    summary_header = [
        '# MINERVA AUTO-BRIEF (most recent daily summary)',
        f'_Read this for morning status. Full brief: /home/lumen/minerva/briefings/{today}.md_',
        '',
    ]
    summary = '\n'.join(summary_header + lines[:50])
    append_to_memory(summary)
    log(f'=== Brief done. Proposals: {len(data["proposals"])} | High intel: {len(data["high_intel"])} | Pending: {data["pending"]} ===')
    return brief_path


if __name__ == '__main__':
    run()

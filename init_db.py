#!/usr/bin/env python3
"""minerva.db — central DB for self-improvement, skills, archival memory."""
import sqlite3
from pathlib import Path
DB = Path('/home/lumen/minerva/minerva.db')

SCHEMA = [
    # Intel items classified for self-relevance
    '''CREATE TABLE IF NOT EXISTS classified_intel (
        id TEXT PRIMARY KEY,
        source TEXT, title TEXT, url TEXT,
        audience_relevance REAL,     -- blog-worthiness 0-1
        self_relevance REAL,          -- Lumen-adoptability 0-1
        category TEXT,                -- tool|framework|paper|technique|news
        reasoning TEXT,               -- why this score
        ts TEXT, processed INTEGER DEFAULT 0
    )''',
    # Proposals: research -> action candidates
    '''CREATE TABLE IF NOT EXISTS proposals (
        id TEXT PRIMARY KEY,
        parent_id TEXT,               -- DGM-style archive lineage
        title TEXT, source_intel_id TEXT,
        current_state TEXT, proposed_change TEXT,
        effort TEXT, impact TEXT, risk TEXT, rollback TEXT,
        status TEXT DEFAULT 'pending',  -- pending|accepted|rejected|deployed|reverted
        eval_result TEXT, eval_passed INTEGER DEFAULT 0,
        created_at TEXT, decided_at TEXT, deployed_at TEXT
    )''',
    # Skill library (Voyager pattern)
    '''CREATE TABLE IF NOT EXISTS skills (
        id TEXT PRIMARY KEY,
        name TEXT UNIQUE, description TEXT,
        preconditions TEXT,           -- JSON
        code_path TEXT,               -- /home/lumen/minerva/skills/<name>.py
        embedding BLOB,
        times_used INTEGER DEFAULT 0,
        success_rate REAL DEFAULT 0,
        parent_skill_id TEXT,
        created_at TEXT, last_used_at TEXT
    )''',
    # Archival memory (cold storage, searchable)
    '''CREATE TABLE IF NOT EXISTS archival_notes (
        id TEXT PRIMARY KEY,
        kind TEXT,                    -- decision|failure|success|insight
        title TEXT, content TEXT,
        embedding BLOB,
        tags TEXT, source_ref TEXT,
        created_at TEXT
    )''',
    # Hallucination guard log — track every verification
    '''CREATE TABLE IF NOT EXISTS verification_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT,                 -- what was verified
        input_text TEXT,              -- claim being checked
        context_ref TEXT,             -- RAG source
        citation_score REAL,
        judge_score REAL,
        final_score REAL,
        passed INTEGER,
        rejected_reason TEXT,
        model TEXT,
        ts TEXT
    )''',
    # Changelog: every modification Minerva makes to Lumen
    '''CREATE TABLE IF NOT EXISTS changelog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT,
        change_type TEXT,             -- config|code|schedule|sources
        target_path TEXT,
        diff TEXT,
        applied_by TEXT DEFAULT 'minerva',
        applied_at TEXT,
        reverted INTEGER DEFAULT 0,
        revert_reason TEXT
    )''',
]

INDEXES = [
    'CREATE INDEX IF NOT EXISTS idx_intel_processed ON classified_intel(processed, self_relevance)',
    'CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_archival_kind ON archival_notes(kind, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_verif_ts ON verification_log(ts)',
]

conn = sqlite3.connect(DB)
for s in SCHEMA:
    conn.execute(s)
for i in INDEXES:
    conn.execute(i)
conn.commit()
print('minerva.db created ->', DB)
print('Tables:', [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()])
conn.close()

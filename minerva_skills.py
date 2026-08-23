#!/usr/bin/env python3
"""
minerva_skills.py - Voyager-pattern skill library for Lumen.

A skill is a reusable code module with metadata:
  - name, description, preconditions (JSON), code (Python),
    embedding (for similarity retrieval), usage stats.

Usage patterns:
  - add_skill(name, description, code, preconditions=None) -> skill_id
  - find_skills(query, top_k=5) -> list of matching skills
  - run_skill(skill_id, context) -> result (sandboxed)
  - promote_from_proposal(proposal_id, code) -> create skill from accepted proposal

Retrieval uses PBox Ollama nomic-embed-text embeddings (already available).
"""
import json, sqlite3, hashlib, os, requests, subprocess, tempfile, sys
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
from dotenv import load_dotenv
load_dotenv('/home/lumen/agent/.env')

DB = Path('/home/lumen/minerva/minerva.db')
SKILLS_DIR = Path('/home/lumen/minerva/skills')
LOG = Path('/home/lumen/minerva/logs/skills.log')
EMBED_URL = os.environ.get('MINERVA_EMBED_URL', 'http://127.0.0.1:11434')  # wq#567: unified env pin
EMBED_MODEL = os.environ.get('MINERVA_EMBED_MODEL', 'nomic-embed-text')


def log(m):
    ts = datetime.now(timezone.utc).strftime('%H:%M:%S UTC')
    line = f'[{ts}] {m}'
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def embed(text):
    """Get embedding via local Ollama. timeout=90: nomic cold-load measured
    44.3s (MAX_LOADED_MODELS=1 evicts it constantly); 15s silently failed."""
    try:
        r = requests.post(
            f'{EMBED_URL}/api/embed',
            json={'model': EMBED_MODEL, 'input': text[:4000], 'keep_alive': '30m'},
            timeout=90,
        )
        if r.status_code != 200:
            log(f'  embed http {r.status_code}')
            return None
        vec = r.json().get('embeddings', [[]])[0]
        return np.array(vec, dtype=np.float32) if vec else None
    except Exception as e:
        log(f'  embed err: {e}')
        return None


def cosine(a, b):
    if a is None or b is None:
        return 0.0
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1e-9
    return float(np.dot(a, b) / denom)


def add_skill(name, description, code, preconditions=None, parent_skill_id=None):
    """Register a new skill. Returns skill_id."""
    sid = hashlib.md5(f'{name}:{code[:200]}'.encode()).hexdigest()[:12]
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    code_path = SKILLS_DIR / f'{sid}_{name.replace(" ", "_").lower()[:40]}.py'
    code_path.write_text(code)

    # Build embedding from name + description + code summary
    embed_text = f'{name}. {description}. Code: {code[:1500]}'
    emb = embed(embed_text)
    emb_bytes = emb.tobytes() if emb is not None else b''

    conn = sqlite3.connect(DB)
    conn.execute(
        '''INSERT OR REPLACE INTO skills
           (id, name, description, preconditions, code_path, embedding,
            times_used, success_rate, parent_skill_id, created_at, last_used_at)
           VALUES (?,?,?,?,?,?,0,0,?,?,?)''',
        (sid, name[:200], description[:1000],
         json.dumps(preconditions or {}),
         str(code_path), emb_bytes,
         parent_skill_id,
         datetime.now(timezone.utc).isoformat(),
         None))
    conn.commit()
    conn.close()
    log(f'  add_skill: {sid} "{name}" -> {code_path}')
    return sid


def find_skills(query, top_k=5, min_similarity=0.3):
    """Retrieve skills by semantic similarity to query."""
    q_emb = embed(query)
    if q_emb is None:
        return []
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        'SELECT id, name, description, code_path, embedding, times_used, success_rate FROM skills'
    ).fetchall()
    conn.close()
    scored = []
    for sid, name, desc, path, emb_b, tu, sr in rows:
        if not emb_b:
            continue
        skill_emb = np.frombuffer(emb_b, dtype=np.float32)
        sim = cosine(q_emb, skill_emb)
        if sim >= min_similarity:
            scored.append({
                'id': sid, 'name': name, 'description': desc,
                'code_path': path, 'similarity': sim,
                'times_used': tu, 'success_rate': sr,
            })
    scored.sort(key=lambda x: -x['similarity'])
    return scored[:top_k]


def run_skill(skill_id, context=None, timeout_sec=30):
    """Execute a skill in a subprocess sandbox. Returns dict with result/stderr/returncode."""
    conn = sqlite3.connect(DB)
    row = conn.execute(
        'SELECT name, code_path FROM skills WHERE id=?', (skill_id,)
    ).fetchone()
    conn.close()
    if not row:
        return {'ok': False, 'error': 'skill_not_found'}
    name, code_path = row
    if not Path(code_path).exists():
        return {'ok': False, 'error': f'code_path_missing:{code_path}'}

    ctx_json = json.dumps(context or {})
    env = os.environ.copy()
    env['MINERVA_SKILL_CONTEXT'] = ctx_json

    try:
        result = subprocess.run(
            ['python3', code_path],
            capture_output=True, text=True, timeout=timeout_sec, env=env,
        )
        success = result.returncode == 0
        # Update stats
        conn = sqlite3.connect(DB)
        row = conn.execute(
            'SELECT times_used, success_rate FROM skills WHERE id=?', (skill_id,)
        ).fetchone()
        tu, sr = (row[0] or 0), (row[1] or 0.0)
        new_tu = tu + 1
        # Rolling success rate
        new_sr = ((sr * tu) + (1.0 if success else 0.0)) / new_tu
        conn.execute(
            'UPDATE skills SET times_used=?, success_rate=?, last_used_at=? WHERE id=?',
            (new_tu, new_sr, datetime.now(timezone.utc).isoformat(), skill_id)
        )
        conn.commit()
        conn.close()
        return {
            'ok': success,
            'returncode': result.returncode,
            'stdout': result.stdout[:8000],
            'stderr': result.stderr[:4000],
            'new_stats': {'times_used': new_tu, 'success_rate': new_sr},
        }
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'timeout', 'timeout_sec': timeout_sec}
    except Exception as e:
        return {'ok': False, 'error': f'run_err:{e}'}


def list_skills():
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        '''SELECT id, name, description, times_used, success_rate, created_at
           FROM skills ORDER BY times_used DESC, created_at DESC'''
    ).fetchall()
    conn.close()
    return rows


# ---------- seed skills (core utilities Lumen will reuse) ----------

SEED_SKILLS = [
    {
        'name': 'verify_fleet_output',
        'description': 'Wrap any fleet researcher response with hallucination verification against its source context. Rejects ungrounded claims before they reach Ghost.',
        'preconditions': {'has': ['answer', 'context']},
        'code': '''#!/usr/bin/env python3
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
''',
    },
    {
        'name': 'service_health_check',
        'description': 'Check if a named systemd service is active and has not errored in the last N minutes.',
        'preconditions': {'has': ['service_name']},
        'code': '''#!/usr/bin/env python3
"""service_health_check: verify systemd service is healthy."""
import json, os, sys, subprocess

ctx = json.loads(os.environ.get('MINERVA_SKILL_CONTEXT', '{}'))
svc = ctx.get('service_name')
if not svc:
    print(json.dumps({'ok': False, 'error': 'missing service_name'}))
    sys.exit(1)
minutes = int(ctx.get('minutes', 15))
try:
    active = subprocess.check_output(['systemctl', 'is-active', svc], text=True).strip()
except subprocess.CalledProcessError as e:
    active = e.output.strip() if e.output else 'unknown'
try:
    logs = subprocess.check_output(['journalctl', '-u', svc, f'--since={minutes} minutes ago', '--no-pager'], text=True, stderr=subprocess.STDOUT)
    errors = [ln for ln in logs.splitlines() if 'error' in ln.lower() or 'failed' in ln.lower()][-10:]
except Exception as e:
    errors = [f'log_fetch_err:{e}']
healthy = active in ('active', 'activating') and len(errors) == 0
print(json.dumps({'ok': healthy, 'service': svc, 'state': active, 'recent_errors': errors}))
sys.exit(0 if healthy else 2)
''',
    },
    {
        'name': 'backup_ghost_db',
        'description': 'Timestamped backup of Ghost CMS SQLite database per standing rule (required before any container/config change).',
        'preconditions': {},
        'code': '''#!/usr/bin/env python3
"""backup_ghost_db: mandatory Ghost DB backup per standing rule."""
import json, shutil, sys
from datetime import datetime, timezone
from pathlib import Path

src = Path('/home/lumen/ghost-content/data/ghost.db')
if not src.exists():
    print(json.dumps({'ok': False, 'error': f'ghost.db missing at {src}'}))
    sys.exit(1)
ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
dst = src.with_suffix(f'.db.bak.{ts}')
shutil.copy2(src, dst)
print(json.dumps({'ok': True, 'backup': str(dst), 'size_bytes': dst.stat().st_size}))
sys.exit(0)
''',
    },
    {
        'name': 'rogue_script_check',
        'description': 'Scan for rogue Roborock S5 / exploration / fanoff scripts that previous sessions may have left running. Per standing rule: run before any S5 action.',
        'preconditions': {},
        'code': '''#!/usr/bin/env python3
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
''',
    },
    {
        'name': 'service_backup_and_restart',
        'description': 'Backup a systemd service unit file (.service) with timestamped .bak, then systemctl daemon-reload + restart. Use before editing service files.',
        'preconditions': {'has': ['service_name']},
        'code': '''#!/usr/bin/env python3
"""service_backup_and_restart: safe service unit modification helper."""
import json, os, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ctx = json.loads(os.environ.get('MINERVA_SKILL_CONTEXT', '{}'))
svc = ctx.get('service_name')
if not svc:
    print(json.dumps({'ok': False, 'error': 'missing service_name'}))
    sys.exit(1)
unit_path = Path(f'/etc/systemd/system/{svc}.service' if not svc.endswith('.service') else f'/etc/systemd/system/{svc}')
if not unit_path.exists():
    print(json.dumps({'ok': False, 'error': f'unit not found: {unit_path}'}))
    sys.exit(1)
ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
bak = unit_path.with_suffix(f'.service.bak.{ts}')
try:
    shutil.copy2(unit_path, bak)
except PermissionError:
    # Try sudo -n (passwordless) fall-back
    r = subprocess.run(['sudo', '-n', 'cp', str(unit_path), str(bak)], capture_output=True, text=True)
    if r.returncode != 0:
        print(json.dumps({'ok': False, 'error': f'backup failed: {r.stderr}'}))
        sys.exit(1)
# daemon-reload + restart
r1 = subprocess.run(['sudo', '-n', 'systemctl', 'daemon-reload'], capture_output=True, text=True)
r2 = subprocess.run(['sudo', '-n', 'systemctl', 'restart', svc], capture_output=True, text=True)
print(json.dumps({
    'ok': r1.returncode == 0 and r2.returncode == 0,
    'backup': str(bak),
    'daemon_reload_rc': r1.returncode,
    'restart_rc': r2.returncode,
    'restart_stderr': r2.stderr[:400],
}))
sys.exit(0 if (r1.returncode == 0 and r2.returncode == 0) else 2)
''',
    },
]


def seed():
    """Populate the skill library with Lumen-native core skills."""
    log('=== Seeding skill library ===')
    added = 0
    for s in SEED_SKILLS:
        sid = add_skill(s['name'], s['description'], s['code'], preconditions=s.get('preconditions'))
        if sid:
            added += 1
    log(f'Seeded {added} skills')
    return added


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'seed':
        seed()
    elif len(sys.argv) > 1 and sys.argv[1] == 'list':
        for row in list_skills():
            print(row)
    elif len(sys.argv) > 2 and sys.argv[1] == 'find':
        for r in find_skills(' '.join(sys.argv[2:])):
            print(f'{r["similarity"]:.3f}  {r["id"]}  {r["name"]}')
    else:
        print('usage: minerva_skills.py [seed|list|find <query>]')

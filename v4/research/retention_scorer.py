#!/usr/bin/env python3
"""
retention_scorer.py - wq#597 Retention-brief scorecard.

Scores Frank's four axes (availability / correctness / retention / context economy)
against live Lumen state. READ-ONLY: opens every DB in file:...?mode=ro, never writes
outside the --json artifact path.

  python3 retention_scorer.py --json /home/lumen/minerva/v4/reports/597_scorecard.json

Axis instruments:
  A availability   - fq curated catch-latency + source-type dependency vs what the
                     session-start injection (claude_context.sh --core) actually carries.
  B correctness    - live conflict census over state_facts (dup-open, cross-predicate
                     numeric contradiction, config-fact vs source-file mtime, confirm
                     coverage) + golden v4 headline/trap@1 read from the #595 instrument
                     (NOT forked; --bench re-runs it).
  C retention      - fq F7/F12 labelled seeds + re-derivation detector over the episode
                     corpus (title-token Jaccard clusters spanning >=2 days) + LiteLLM
                     SpendLogs cost frame.
  D context economy- tiktoken cost of --core vs a topic query vs the full claude_memory.md,
                     plus a bulk-read lower bound mined from the episode corpus.
"""
import os, re, sys, json, time, sqlite3, subprocess, argparse, itertools
from datetime import datetime, timezone

MIN_DB = '/home/lumen/minerva/minerva.db'
FQ_DB  = '/home/lumen/agents/failure-queue/failures.db'
MEM_MD = '/home/lumen/claude_memory.md'
IDX_MD = '/home/lumen/minerva/context/claude_memory_index.md'
CTX_SH = '/home/lumen/minerva/context/claude_context.sh'
EVAL_D = '/home/lumen/minerva/eval'
TOPIC_Q = 'ollama memory policy on lumen'

def ro(path):
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)

def toks(text):
    import tiktoken
    return len(tiktoken.get_encoding('cl100k_base').encode(text))

def sh(cmd, timeout=900):
    t0 = time.time()
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.stdout, p.returncode, round(time.time() - t0, 1)

# ---------------------------------------------------------------- AXIS A
# Which on-box source would have prevented each catalogued failure.
SOURCE_PATTERNS = [
    ('plug_telemetry',   r'plug|watt|\bW\b|kasa|tapo|power[- ]?(dash|snapshot)'),
    ('systemd_state',    r'systemctl|journalctl|failed-units|is-active|systemd-analyze'),
    ('live_config_file', r'/etc/|/var/|\.conf|drop-in|dropin|cat /|read /|stat |mtime|ls -l'),
    ('hw_probe',         r'lsblk|lscpu|smartctl|free -|dmidecode|nvme |df -'),
    ('spendlogs',        r'spendlog|litellm|cache_hit|spend|token'),
    ('state_store',      r'lumen_state|state_facts|current '),
    ('episode_store',    r'lumen_episode|episode|precheck|recall'),
    ('queue_state',      r'\bwq \b|wq#|\bfq \b|\baq \b|work-queue'),
    ('repo_git',         r'git |github|gh api|restic|snapshot'),
    ('runtime_probe',    r'curl |http|ps aux|rss|psi|redis-cli|sqlite3|live dom|cdp'),
]
# A discipline rule, not a lookup: no amount of memory availability prevents these, they
# belong to instincts.yaml. Scored separately so the availability denominator stays honest.
PROCESS_RULE = re.compile(r'(host guard|guard-of-guards|lock |atomic claim|begin immediate|'
                          r'known positive|plant a known|cite the directive|ask instead|'
                          r'dedicated test|rule shipped|subject lock|before acting|escalat)', re.I)
# What the session-start injection demonstrably carries AS A LIVE VALUE.
# Probed against the "VERIFY NOW — live state" block only: prose elsewhere in the
# injection is historical narrative, not current state, and treating a tool's NAME
# appearing in a changelog as "the fact was available" is exactly the fq F1 error.
LIVE_PROBES = {
    'plug_telemetry':   [r'\d+(?:\.\d+)?\s*W\b', r'watt'],
    'systemd_state':    [r'(?:active|inactive|failed)\s*\(systemd\)'],
    'live_config_file': [r'MAX_LOADED_MODELS', r'\.conf\b[^\n]*=', r'mtime'],
    'hw_probe':         [r'\b\d+(?:\.\d+)?\s?(?:GB|GiB|TB)\b', r'lsblk', r'\d+%\s*(?:used|full)'],
    'spendlogs':        [r'\$\s?\d', r'spend'],
    'state_store':      [r'state_facts', r'lumen_state\.py'],
    'episode_store':    [r'recent episodes'],
    'queue_state':      [r'triage queue', r'\bwq\b[^\n]*open', r'\bfq\b[^\n]*(?:open|stats)'],
    'repo_git':         [r'restic', r'snapshot'],
    'runtime_probe':    [r'\b(?:200|302|404|503)\b', r'in_recovery'],
}
# GRADE: 'full' = the live value an agent would need is in the block. 'partial' = the block
# carries a NARROWER thing that shares the name (a 4-unit allowlist is not failed-unit
# enumeration; an aq COUNT is not wq/fq state; episodes retrieved for a fixed generic query
# are not episodes for the work at hand). Scoring partial as full is the falsely-passing
# audit (fq#26-29) applied to my own instrument, so both ends are reported.
GRADE = {'systemd_state': 'partial', 'episode_store': 'partial',
         'queue_state': 'partial', 'runtime_probe': 'partial'}
CAVEATS = {
    'systemd_state': ('named-unit allowlist only: the block lists 4 named services; it does not '
                      'enumerate failed units (the fq F9 SILENT_GAP mechanism)'),
    'queue_state':   ('aq open COUNT only; no wq lane state, no fq open patterns'),
    'runtime_probe': ('vault HA topology + one public HTTP code only; no arbitrary service probe'),
    'episode_store': ('top-6 episodes retrieved for the fixed query "session start core context", '
                      'not for the work the session is about to do'),
}

def _verify_block(core_text):
    m = re.search(r'=====\s*VERIFY NOW(.*?)=====\s*END VERIFY\s*=====', core_text, re.S | re.I)
    return m.group(1) if m else ''

def axis_a(core_text):
    verify = _verify_block(core_text)
    con = ro(FQ_DB)
    rows = con.execute("""select id, class, occurred, caught_by, summary,
                          coalesce(prevented_by,''), coalesce(evidence,'')
                          from failures where curated=1""").fetchall()
    con.close()
    catch = {}
    need = {}
    per_record = []
    for fid, cls, occ, caught, summ, prev, ev in rows:
        catch[caught] = catch.get(caught, 0) + 1
        blob = f'{prev} {summ}'.lower()
        srcs = [name for name, pat in SOURCE_PATTERNS if re.search(pat, blob, re.I)]
        if not srcs:
            srcs = ['process_rule'] if PROCESS_RULE.search(prev) else ['unclassified']
        for s in srcs:
            need[s] = need.get(s, 0) + 1
        per_record.append({'fq': fid, 'class': cls, 'occurred': occ,
                           'caught_by': caught, 'sources': srcs})
    carried = {}
    for src, probes in LIVE_PROBES.items():
        hits = [p for p in probes if re.search(p, verify, re.I)]
        prose = bool(re.search(dict(SOURCE_PATTERNS)[src], core_text, re.I))
        carried[src] = {'grade': (GRADE.get(src, 'full') if hits else 'absent'),
                        'live_value_in_verify_block': bool(hits),
                        'matched_probes': hits,
                        'merely_mentioned_in_prose': prose}
        if src in CAVEATS:
            carried[src]['caveat'] = CAVEATS[src]
    n = len(rows)
    lookup = [r for r in per_record if r['sources'] not in (['process_rule'], ['unclassified'])]
    def grade_of(r):
        gs = [carried.get(s, {}).get('grade', 'absent') for s in r['sources']]
        return 'full' if 'full' in gs else ('partial' if 'partial' in gs else 'absent')
    strict = sum(1 for r in lookup if grade_of(r) == 'full')
    generous = sum(1 for r in lookup if grade_of(r) in ('full', 'partial'))
    for r in per_record:
        r['boot_grade'] = grade_of(r)
    return {
        'curated_n': n,
        'catch_latency': {
            'surfaced_by_mechanism (monitor)': catch.get('monitor', 0),
            'discovered_late (agent)': catch.get('agent', 0),
            'reached_frank': catch.get('frank', 0),
            'never_caught': catch.get('never', 0),
            'unknown': catch.get('unknown', 0),
        },
        'source_dependency': dict(sorted(need.items(), key=lambda kv: -kv[1])),
        'core_injection_carries': carried,
        'availability_at_boot': {
            'scope': 'records preventable by an ON-BOX DATA LOOKUP (process-discipline rules '
                     'and unclassifiable rows excluded from the denominator — no memory system '
                     'prevents "run the host guard")',
            'data_lookup_records': len(lookup),
            'strict_full_grade': strict,
            'strict_rate': round(strict / len(lookup), 3) if lookup else None,
            'generous_full_or_partial': generous,
            'generous_rate': round(generous / len(lookup), 3) if lookup else None,
            'process_rule_records': sum(1 for r in per_record if r['sources'] == ['process_rule']),
            'unclassified_records': sum(1 for r in per_record if r['sources'] == ['unclassified']),
            'of_curated_total': n,
        },
        'core_bytes': len(core_text),
        'verify_block_bytes': len(verify),
        'per_record': per_record,
    }

# ---------------------------------------------------------------- AXIS B
RAM_RE  = re.compile(r'(\d{1,4})\s?GB', re.I)
RAMCTX  = re.compile(r'(ram|memory|ddr|ecc|dimm)', re.I)
IP_RE   = re.compile(r'\b(?:192\.168|10|100)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')
PATH_RE = re.compile(r'(/(?:etc|home|usr|var|opt|scratch|srv)/[A-Za-z0-9_./\-]+)')

HW_PREDS = ('ram', 'hw', 'hardware', 'memory', 'os', 'spec', 'specs')
PLAN_RE  = re.compile(r'(option|candidate|plan|proposal|vs\b|compare|ceiling|profile|quote|'
                      r'if we|would|could|budget|kit|incoming|on hand|target)', re.I)

PRED_VETO = re.compile(r'(ceiling|plan|option|profile|lead|candidate|proposal|vs_)', re.I)

def _hw_identity(pred, val):
    if PRED_VETO.search(pred):
        return False
    # Predicate gate only. Value-level filtering happens per-match in _ram_claims: a fact
    # can legitimately mention a planned kit AND assert the installed size (khepri.hw does),
    # and vetoing the whole fact loses the known positive.
    return any(pred == h or pred.startswith(h + '_') or pred.endswith('_' + h) for h in HW_PREDS)

def _ram_claims(text):
    """GB values asserting INSTALLED system memory. Window sizes are deliberate:
    context/storage judged over +-45 chars, availability language ('64GB kit ON HAND')
    over a TIGHT +-25 so a neighbouring clause cannot veto the real claim, historical
    language ('supersedes the ... 24GB') over a wider left window."""
    out = set()
    for m in RAM_RE.finditer(text):
        pre, post = text[max(0, m.start() - 60):m.start()], text[m.end():m.end() + 45]
        wide = pre[-45:] + post
        if not re.search(r'(ram|memory|ddr|ecc|dimm|sodimm)', wide, re.I):
            continue
        near = pre[-22:] + post[:22]   # storage nouns only veto when ADJACENT (planted-control
        if re.search(r'(emmc|ssd|nvme|disk|blade|storage|swap|vram|m\.2|/dev/)', near, re.I):
            continue                     # fix: a 512GB NVMe later in the sentence is not this claim
        if re.search(r'\d\s*[x\u00d7]\s*$', pre):            # "4x16GB" = module size
            continue
        tight = pre[-25:] + post[:25]
        if re.search(r'(kit|incoming|on hand|not installed|ordered|planned|target|ceiling|'
                     r'option|quote|budget)', tight, re.I):
            continue
        if re.search(r'(supersed|earlier|previous|stale|corrects|was |formerly)', pre, re.I):
            continue
        out.add(int(m.group(1)))
    return out

def axis_b(bench_json, state_db=MIN_DB):
    con = ro(state_db)
    q = con.execute
    dup = q("""select subject, predicate, count(*) c from state_facts
               where valid_to is null group by 1,2 having c>1""").fetchall()
    open_rows = q("""select id, subject, predicate, value, valid_from,
                     last_confirmed_at, fact_class, coalesce(source_ref,'')
                     from state_facts where valid_to is null""").fetchall()
    con.close()

    by_subj = {}
    for r in open_rows:
        by_subj.setdefault(r[1], []).append(r)

    ram_conflicts, ip_conflicts = [], []
    for subj, rows in by_subj.items():
        claims = {}
        for r in rows:
            if not _hw_identity(r[2], r[3]):
                continue
            for gb in _ram_claims(r[3]):
                claims.setdefault(gb, []).append(r[2])
        if len(claims) > 1:
            ram_conflicts.append({'subject': subj,
                                  'values_gb': {str(k): sorted(set(v)) for k, v in claims.items()}})
        ips = {}
        for r in rows:
            if 'planned' in r[2] or 'plan' in r[2]:
                continue
            fam = 'tailscale' if 'tailscale' in r[2] or 'ts_' in r[2] else ('lan' if 'lan' in r[2] else None)
            if not fam:
                continue
            for ip in set(IP_RE.findall(r[3])):
                if fam == 'tailscale' and not ip.startswith('100.'):
                    continue
                if fam == 'lan' and ip.startswith('100.'):
                    continue
                ips.setdefault(fam, {}).setdefault(ip, []).append(r[2])
        for fam, d in ips.items():
            if len(d) > 1:
                ip_conflicts.append({'subject': subj, 'family': fam,
                                     'values': {k: sorted(set(v)) for k, v in d.items()}})

    stale, checkable, missing_path = [], 0, 0
    for fid, subj, pred, val, vfrom, lconf, fclass, sref in open_rows:
        paths = set(PATH_RE.findall(val)) | set(PATH_RE.findall(sref))
        local = [p for p in paths if os.path.exists(p)]
        if not paths:
            continue
        if not local:
            missing_path += 1
            continue
        checkable += 1
        ref = (lconf or vfrom or '')[:10]
        try:
            ref_ts = datetime.strptime(ref, '%Y-%m-%d').timestamp() + 86399
        except Exception:
            continue
        for p in local:
            mt = os.path.getmtime(p)
            if mt > ref_ts:
                stale.append({'subject': subj, 'predicate': pred, 'path': p,
                              'path_is_dir': os.path.isdir(p),
                              'file_mtime': datetime.fromtimestamp(mt).strftime('%Y-%m-%d %H:%M'),
                              'fact_asserted': ref,
                              'days_behind': round((mt - ref_ts) / 86400, 1)})
                break

    conf = {}
    for r in open_rows:
        k = r[6] or 'none'
        d = conf.setdefault(k, {'n': 0, 'never_confirmed': 0})
        d['n'] += 1
        if not r[5]:
            d['never_confirmed'] += 1

    out = {
        'open_facts': len(open_rows),
        'dup_open_subject_predicate': {'n': len(dup),
                                       'note': 'structurally 0: idx_sf_one_open is a UNIQUE index on '
                                               '(subject,predicate) WHERE valid_to IS NULL. Intra-predicate '
                                               'conflict cannot exist; the real conflict surface is '
                                               'cross-predicate and store-vs-file.'},
        'cross_predicate_ram_conflicts': ram_conflicts,
        'cross_predicate_ip_conflicts': ip_conflicts,
        'config_fact_vs_source_file_mtime': {
            'stale_regular_file_only': sum(1 for x in stale if not x['path_is_dir']),
            'facts_naming_a_local_path': checkable,
            'facts_naming_a_path_not_on_this_host': missing_path,
            'stale_n': len(stale), 'stale': stale},
        'confirm_coverage': conf,
    }
    if bench_json and os.path.exists(bench_json):
        with open(bench_json) as f:
            b = json.load(f)
        s = b.get('summary', b)
        rc = s.get('runtime_config', {})
        out['golden_v4'] = {
            'artifact': bench_json,
            'label': s.get('label'), 'ts': s.get('ts'),
            'scoring_semantics': s.get('scoring_semantics'),
            'headline_scope': s.get('headline_scope'),
            'headline': s.get('headline'),
            'trap': s.get('trap'),
            'abstention': s.get('abstention'),
            'federated_coverage': s.get('federated_coverage'),
            'orig_193': s.get('orig_193'),
            'control_ok': s.get('control_ok'),
            'control_results': s.get('control_results'),
            'golden_sha256': rc.get('golden_sha256'),
            'golden_is_frozen_snapshot': rc.get('golden_is_frozen_snapshot'),
            'effective_engine': rc.get('effective_engine'),
            'owner': 'wq#572/#595 instrument, invoked not forked',
        }
    return out

# ---------------------------------------------------------------- AXIS C
STOP = set('''a an the and or of for to in on at with from by is are was were be been this that
these those it its as into via over under after before during not no yes new now then than
session sessions claude lumen wq fq aq shipped ship done fix fixed add added run ran build built
update updated verify verified live full all one two three'''.split())

def _key(title):
    t = re.sub(r'[^a-z0-9 ]', ' ', (title or '').lower())
    t = re.sub(r'\b(19|20)\d{2}[- ]?\d{0,2}[- ]?\d{0,2}\b', ' ', t)
    return {w for w in t.split() if w not in STOP and len(w) > 2 and not w.isdigit()}

# Self-reported re-derivation markers in episode bodies. Deliberately literal:
# the episode says, in its own words, that the work had already been done or
# already been proven a dead end.
REDERIVE_RE = re.compile(
    r'(re-?deriv\w*|rediscover\w*|duplicate (?:work|effort|pages|tickets?)|'
    r'already (?:shipped|done|fixed|answered|known|investigated|decided|solved)|'
    r'dead end (?:re-?walked|again)|re-?walked|second time|'
    r'previously (?:answered|established|decided) (?:and|but)|'
    r'had already been (?:done|shipped|fixed|answered)|redid|re-did)', re.I)

def rederivation_markers(limit=15):
    con = ro(MIN_DB)
    rows = con.execute("""select id, title, created_at, content from archival_notes
                         where content like '%## What changed%'""").fetchall()
    con.close()
    hits = []
    for i, t, c, body in rows:
        m = REDERIVE_RE.search(body or '')
        if m:
            j = max(0, m.start() - 90)
            hits.append({'id': i, 'date': (c or '')[:10], 'title': (t or '')[:100],
                         'marker': m.group(0), 'context': re.sub(r'\s+', ' ', body[j:m.end() + 110])})
    hits.sort(key=lambda h: h['date'])
    return {'episodes_scanned': len(rows), 'episodes_with_marker': len(hits),
            'marker_rate': round(len(hits) / len(rows), 3) if rows else None,
            'samples': hits[-limit:],
            'note': 'SELF-REPORTED lower bound: only counts re-derivation an agent noticed and '
                    'wrote down. Silent re-derivation is invisible to this measure by construction.'}

def axis_c(jaccard=0.5, min_days=1):
    con = ro(MIN_DB)
    eps = con.execute("""select id, title, created_at, kind from archival_notes
                         where content like '%## What changed%' and created_at is not null
                         order by created_at""").fetchall()
    con.close()
    items = [(i, t, c[:10], k, _key(t)) for i, t, c, k in eps if len(_key(t)) >= 3]
    parent = {i[0]: i[0] for i in items}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[rb] = ra
    pairs = 0
    for (ia, ta, da, ka, sa), (ib, tb, db, kb, sb) in itertools.combinations(items, 2):
        if da == db:
            continue
        inter = len(sa & sb)
        if not inter:
            continue
        j = inter / len(sa | sb)
        if j >= jaccard:
            d0 = datetime.strptime(da, '%Y-%m-%d'); d1 = datetime.strptime(db, '%Y-%m-%d')
            if abs((d1 - d0).days) >= min_days:
                union(ia, ib); pairs += 1
    groups = {}
    meta = {i[0]: i for i in items}
    for i in parent:
        groups.setdefault(find(i), []).append(i)
    clusters = []
    for root, members in groups.items():
        if len(members) < 2:
            continue
        ms = sorted((meta[m] for m in members), key=lambda x: x[2])
        days = sorted({m[2] for m in ms})
        if len(days) < 2:
            continue
        clusters.append({'n_episodes': len(ms), 'n_distinct_days': len(days),
                         'span_days': (datetime.strptime(days[-1], '%Y-%m-%d') -
                                       datetime.strptime(days[0], '%Y-%m-%d')).days,
                         'first': days[0], 'last': days[-1],
                         'titles': [m[1][:110] for m in ms[:6]]})
    clusters.sort(key=lambda c: (-c['n_episodes'], -c['span_days']))
    con = ro(FQ_DB)
    lab = con.execute("""select id, class, occurred, caught_by, summary from failures
                         where curated=1 and class in ('F7','F12') order by occurred""").fetchall()
    con.close()
    reworked = sum(c['n_episodes'] - 1 for c in clusters)
    return {
        'title_jaccard_experiment': {
            'verdict': 'REJECTED as a re-derivation instrument — see scorecard C. It clusters '
                       'TEMPLATED SERIAL WORK (per-util uplift ships, corrector auto-fixes, '
                       'factory route-adds), which is legitimate throughput, and it misses every '
                       'labelled F7/F12 case, whose titles share no vocabulary. Reported as a '
                       'negative control, not as a number.',
            'clusters': len(clusters), 'episodes_in_clusters': sum(c['n_episodes'] for c in clusters)},
        'episode_corpus_n': len(eps),
        'episodes_scored': len(items),
        'jaccard_threshold': jaccard,
        'repeat_clusters': len(clusters),
        'episodes_in_repeat_clusters': sum(c['n_episodes'] for c in clusters),
        'redundant_episode_sessions': reworked,
        'redundancy_rate': round(reworked / len(items), 3) if items else None,
        'top_clusters': clusters[:12],
        'labelled_F7_F12': [{'fq': r[0], 'class': r[1], 'occurred': r[2],
                             'caught_by': r[3], 'summary': r[4][:120]} for r in lab],
    }

def spend_frame(days=35):
    sql = ("select count(*), round(sum(spend)::numeric,4), round(avg(spend)::numeric,6), "
           "count(distinct date(\\\"startTime\\\")) from \\\"LiteLLM_SpendLogs\\\"")
    out, rc, _ = sh(f'docker exec lumen-pgvector psql -U lumen -d litellm -tA -F"|" -c "{sql}"')
    if rc != 0 or '|' not in out:
        return {'available': False, 'raw': out[:200]}
    n, tot, avg, d = out.strip().split('\n')[0].split('|')
    return {'available': True, 'calls': int(n), 'total_spend_usd': float(tot),
            'mean_spend_per_call_usd': float(avg), 'days_covered': int(d),
            'mean_spend_per_day_usd': round(float(tot) / max(int(d), 1), 4),
            'note': 'fleet LLM spend only (LiteLLM-routed). Claude-Code work rides the Max '
                    'subscription and is NOT priced here, so dollar attribution of re-derivation '
                    'is a floor, not a total.'}

# ---------------------------------------------------------------- AXIS D
BULK_RE = re.compile(r'(cat\s+\S*claude_memory\.md|read\s+\S*claude_memory\.md|'
                     r'claude_memory\.md\s+WHOLE|full[- ]file\s+dump|full read of claude_memory)', re.I)

def axis_d(core_text, core_secs):
    with open(MEM_MD, encoding='utf-8', errors='replace') as f:
        mem = f.read()
    with open(IDX_MD, encoding='utf-8', errors='replace') as f:
        idx = f.read()
    topic, rc, tsec = sh(f'bash {CTX_SH} "{TOPIC_Q}"', timeout=900)
    t_mem, t_core, t_topic, t_idx = toks(mem), toks(core_text), toks(topic), toks(idx)
    con = ro(MIN_DB)
    hits = con.execute("select count(*) from archival_notes where content like '%claude_memory.md%'").fetchone()[0]
    rows = con.execute("select content from archival_notes where content like '%claude_memory.md%'").fetchall()
    con.close()
    bulk = sum(1 for (c,) in rows if BULK_RE.search(c or ''))
    fcon = ro(FQ_DB)
    f8 = fcon.execute("select count(*) from failures where class='F8'").fetchone()[0]
    fcon.close()
    return {
        'tokenizer': 'tiktoken cl100k_base',
        'claude_memory_md': {'bytes': len(mem.encode()), 'tokens': t_mem},
        'index_md': {'bytes': len(idx.encode()), 'tokens': t_idx},
        'core_injection': {'tokens': t_core, 'seconds': core_secs},
        'topic_query': {'query': TOPIC_Q, 'tokens': t_topic, 'seconds': tsec, 'rc': rc},
        'multiplier_full_over_core': round(t_mem / t_core, 1) if t_core else None,
        'multiplier_full_over_topic': round(t_mem / t_topic, 1) if t_topic else None,
        'bulk_read_lower_bound': {
            'episodes_mentioning_claude_memory_md': hits,
            'episodes_matching_bulk_read_pattern': bulk,
            'fq_F8_records': f8,
            'note': 'LOWER BOUND ONLY. Nothing logs reads of claude_memory.md; this counts '
                    'self-reported mentions in the episode corpus. See scorecard D2 for the '
                    'instrumentation that would make this a real rate.'},
    }

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', default='/home/lumen/minerva/v4/reports/597_scorecard.json')
    ap.add_argument('--bench', default='', help='golden v4 results JSON from the #595 instrument')
    ap.add_argument('--core-cache', default='', help='reuse a captured --core output instead of re-running')
    ap.add_argument('--jaccard', type=float, default=0.5)
    ap.add_argument('--control-db', default='',
                    help='COPY of minerva.db carrying a planted conflict; scores the positive control')
    ap.add_argument('--state-db', default=MIN_DB,
                    help='state_facts source; point at a COPY to run planted-positive controls')
    args = ap.parse_args()

    if args.core_cache and os.path.exists(args.core_cache):
        core, csec = open(args.core_cache, encoding='utf-8', errors='replace').read(), 0.0
    else:
        core, _, csec = sh(f'bash {CTX_SH} --core', timeout=900)

    bench = args.bench
    if not bench:
        cands = sorted(p for p in os.listdir(EVAL_D) if p.startswith('results_') and p.endswith('.json'))
        bench = os.path.join(EVAL_D, cands[-1]) if cands else ''

    res = {'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'host': os.uname().nodename,
           'ticket': 'wq#597',
           'A_availability': axis_a(core),
           'B_correctness': axis_b(bench, args.state_db),
           'C_retention': axis_c(args.jaccard),
           'D_context_economy': axis_d(core, csec)}
    if args.control_db and os.path.exists(args.control_db):
        cb = axis_b('', args.control_db)
        planted = [c for c in cb['cross_predicate_ram_conflicts'] if 'test' in c['subject']]
        res['B_correctness']['planted_positive_control'] = {
            'control_db': args.control_db, 'went_red': bool(planted), 'planted_detected': planted,
            'note': 'no clean sweep is trusted until it has gone red on a plant (fq F1 x4)'}
    res['C_retention']['self_reported_rederivation'] = rederivation_markers()
    res['C_retention']['spend_frame'] = spend_frame()
    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    with open(args.json, 'w') as f:
        json.dump(res, f, indent=2)
    a, b, c, d = res['A_availability'], res['B_correctness'], res['C_retention'], res['D_context_economy']
    ab = a['availability_at_boot']
    print(f"A availability@boot strict {ab['strict_full_grade']}/{ab['data_lookup_records']}"
          f" = {ab['strict_rate']} | generous {ab['generous_full_or_partial']}"
          f"/{ab['data_lookup_records']} = {ab['generous_rate']} "
          f"(process-rule {ab['process_rule_records']}, unclassified {ab['unclassified_records']})")
    print(f"  catch: {a['catch_latency']}")
    print(f"B open_facts {b['open_facts']} dup_open {b['dup_open_subject_predicate']['n']} "
          f"ram_conflicts {len(b['cross_predicate_ram_conflicts'])} "
          f"ip_conflicts {len(b['cross_predicate_ip_conflicts'])} "
          f"mtime_stale {b['config_fact_vs_source_file_mtime']['stale_n']}"
          f"/{b['config_fact_vs_source_file_mtime']['facts_naming_a_local_path']}")
    sr = c['self_reported_rederivation']
    print(f"C episodes {c['episodes_scored']} labelled_F7F12 {len(c['labelled_F7_F12'])} "
          f"self-reported-rederivation {sr['episodes_with_marker']}/{sr['episodes_scanned']} "
          f"= {sr['marker_rate']} (title-jaccard experiment REJECTED, see JSON)")
    print(f"D full {d['claude_memory_md']['tokens']}tok core {d['core_injection']['tokens']}tok "
          f"topic {d['topic_query']['tokens']}tok x{d['multiplier_full_over_core']} over core")
    print(f"wrote {args.json}")

if __name__ == '__main__':
    main()

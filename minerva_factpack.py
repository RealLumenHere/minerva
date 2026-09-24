#!/usr/bin/env python3
"""minerva_factpack — per-claim retrieval context for the verify judge.

WHY (verify_gold_v2, 2026-07-27): the NLI judge is precise but starved.
Against the one static digest it achieved precision 1.00 but recall 0.36 —
it rejected 64% of TRUE claims because a 13k-char digest cannot describe
every subsystem a proposal touches. The latent rescue layer was compensating
(88% of accepts) at a measured ~5% false-accept cost. Root fix: retrieve the
relevant slice of live ground truth per verification instead of hoping the
digest happens to cover it.

Design:
  corpus  — atomic single-line facts, each tagged with its source, probed from:
              [service]  systemd lumen-*/minerva-* units (desc + run mode)
              [model]    litellm_config.yaml model list (+ api_base)
              [fact]     lumen_state.py current (canonical fleet facts)
              [artifact] minerva.db tables/rowcounts, eval harnesses, skills
              [hw]       GPU/CPU/RAM probes
            cached in-process + on disk (FACTPACK_TTL, default 600s) because
            proposal runs verify in batches.
  retrieve — score corpus lines against the answer text; cross-encoder rerank
            via lumen-rerank :8140 when healthy, keyword-overlap fallback
            otherwise (fail-open to keyword, NEVER to empty context).
  context — invariant header (identity/policy, always present) + top-K
            retrieved lines, capped to fit JUDGE_CTX_CHARS.

The static digest path stays as fallback: build_context() degrades to
lumen_state.txt on any corpus failure, so the guard can never see LESS than
it did before this module existed.
"""
import os, re, json, time, sqlite3, subprocess, hashlib
from datetime import datetime, timezone
from pathlib import Path

CACHE = Path('/tmp/minerva_factpack_cache.json')
TTL = int(os.getenv('FACTPACK_TTL', '600'))
TOP_K = int(os.getenv('FACTPACK_TOP_K', '25'))
# wq#1528 (design §2c/§2e): the facts budget is RESERVED before the digest.
# TOP_K=25 lines x ~160 chars each ≈ 4000; sized so the full retrieval survives,
# not whatever happens to be left after the digest (at CAP 20000 the compact
# digest 17535 + closed-world 693 left facts only 1772 chars — 11 of 25 lines).
FACTS_RESERVE = int(os.getenv('FACTPACK_FACTS_RESERVE', '4000'))
# 15500 -> 20000 (wq#1501) -> 22500 (wq#1528): must be >= compact judge digest
# (17535 on 2026-09-13) + CLOSED_WORLD (693) + FACTS_RESERVE, and
# <= VERIFY_JUDGE_CTX_CHARS; minerva_verify asserts that ordering at import,
# bounded above by the qwen36-local rung window (n_ctx 8192 tokens).
CAP = int(os.getenv('FACTPACK_CAP', '22500'))
# wq#2430: provenance-tagged evidence. Each retrieved chunk carries its origin
# URI, ingest timestamp, and a staleness flag; minerva_verify rejects claims
# whose ONLY support is a stale chunk. A chunk is stale when it was ingested
# longer ago than EVIDENCE_TTL seconds (0 disables the flag entirely).
EVIDENCE_TTL = int(os.getenv('FACTPACK_EVIDENCE_TTL', str(7 * 86400)))
RERANK_URL = 'http://127.0.0.1:8140/rerank'
DIGEST = Path('/home/lumen/minerva/lumen_state.txt')

HEADER = (
    "LUMEN GROUND TRUTH (live-probed fact pack; each line is one verified fact)\n"
    "Identity: Lumen = Dell T5810 orchestration server, Ubuntu 26.04, sole admin Frank.\n"
    "Policy: all LLM inference routes via LiteLLM gateway localhost:4000; "
    "NO Anthropic/OpenAI API spend; verify/critic gates fail closed.\n")


def _run(cmd, timeout=20):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.stdout if r.returncode == 0 else ''


def _probe_services():
    out, timed = [], set()
    t = _run("systemctl list-timers --all --no-legend --no-pager 2>/dev/null")
    for line in t.splitlines():
        if line.split():
            timed.add(line.split()[-1])
    for line in _run("systemctl list-units 'lumen-*' 'minerva-*' --all --no-legend --plain").splitlines():
        c = line.split()
        if len(c) < 5 or c[0].endswith(('.timer', '.path')):
            continue
        base, active, desc = c[0].replace('.service', ''), c[2], ' '.join(c[4:])[:70]
        mode = (f'timer-driven by {base}.timer' if c[0] in timed
                else 'running now' if active == 'active' else 'inactive')
        out.append(f'[service] {base} exists ({mode}): {desc}')
    return out


def _probe_models():
    out, name = [], None
    try:
        for line in open('/home/lumen/agent/litellm_config.yaml'):
            m = re.match(r'\s*-\s*model_name:\s*(\S+)', line)
            if m:
                name = m.group(1)
                out.append(f'[model] {name} is registered in the LiteLLM gateway model list')
            b = re.match(r'\s*api_base:\s*(\S+)', line)
            if b and name:
                out.append(f'[model] {name} is served from {b.group(1)}')
    except Exception:
        pass
    return out


def _probe_state():
    out = []
    txt = _run('python3 /home/lumen/minerva/lumen_state.py current 2>/dev/null', 45)
    for line in txt.splitlines():
        line = line.strip()
        if line and not line.startswith(('#', '=')) and len(line) > 12:
            out.append(f'[fact] {line[:160]}')
    return out[:250]


def _probe_artifacts():
    out = []
    try:
        db = sqlite3.connect('/home/lumen/minerva/minerva.db')
        for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if t.startswith('recall_fts'):
                continue
            n = db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            out.append(f'[artifact] minerva.db table {t} exists with {n} rows')
        for sid, nm, desc in db.execute('SELECT id,name,description FROM skills'):
            out.append(f'[artifact] skill "{nm}" exists in the skills table: {str(desc)[:90]}')
        db.close()
    except Exception:
        pass
    for f, what in [('minerva_verify_eval.py', 'hallucination-guard gold eval v1'),
                    ('minerva_verify_eval_v2.py', 'production ground-truth guard eval v2'),
                    ('minerva_eval.py', 'pre-deploy proposal safety sandbox (NOT a performance harness)'),
                    ('minerva_verify.py', 'citation + NLI verification service used by proposal and fleet verify gates')]:
        if Path(f'/home/lumen/minerva/{f}').exists():
            out.append(f'[artifact] {f} exists: {what}')
    return out


CARDS_PATH = Path(os.getenv('FACTPACK_CARDS_PATH',
                                    '/home/lumen/minerva/factpack_cards.json'))


def _probe_cards():
    """[capability] lines from per-module cards (factpack_cards_gen.py).

    Phase-1 extension (2026-07-28): mechanism-absence claims cannot be entailed
    by unit/model/table enumeration — the evidence lives inside module source.
    Cards are generated FROM the source, hash-keyed, and gated by
    FACTPACK_CARDS=1 until a verify_gold_v2 rescore holds precision at 1.00.
    "does NOT implement" lines are explicit negative ground truth, dated so the
    judge can see freshness.
    """
    if os.getenv('FACTPACK_CARDS', '0') != '1' or not CARDS_PATH.exists():
        return []
    out = []
    try:
        cards = json.loads(CARDS_PATH.read_text())
    except Exception:
        return []
    for path, c in cards.items():
        mod = path.rsplit('/', 1)[-1]
        for line in c.get('present', []):
            out.append(f'[capability] {mod}: implements {line}')
        for line in c.get('absent', []):
            out.append(f'[capability] {mod}: does NOT implement {line} '
                       f'(verified from source, {c.get("generated", "?")})')
    return out


def _probe_hw():
    out = []
    gpu = _run("lspci 2>/dev/null | grep -iE 'vga|display'").strip()
    if gpu:
        out.append(f'[hw] only GPU present: {gpu.split(": ",1)[-1][:100]} (display output only, no inference GPU)')
    cpu = _run("lscpu 2>/dev/null | grep 'Model name' | head -1").strip()
    if cpu:
        out.append(f'[hw] CPU: {cpu.split(":",1)[-1].strip()[:80]}, all local inference is CPU-based')
    return out


# wq#2430: epoch seconds of the corpus build the current lines came from —
# the ingest timestamp for every probe-derived chunk's provenance record.
_CORPUS_META = {'generated_at': None}


def corpus(force=False):
    if not force and CACHE.exists() and time.time() - CACHE.stat().st_mtime < TTL:
        try:
            data = json.loads(CACHE.read_text())
            # wq#2430 cache v2 = {'generated_at': epoch, 'lines': [...]};
            # v1 was a bare list — mtime is the best ingest time we have for it.
            if isinstance(data, dict):
                _CORPUS_META['generated_at'] = data.get('generated_at') or CACHE.stat().st_mtime
                return data.get('lines') or []
            _CORPUS_META['generated_at'] = CACHE.stat().st_mtime
            return data
        except Exception:
            pass
    lines = []
    for probe in (_probe_services, _probe_models, _probe_state, _probe_artifacts, _probe_cards, _probe_hw):
        try:
            lines += probe()
        except Exception:
            continue
    lines = list(dict.fromkeys(lines))
    _CORPUS_META['generated_at'] = time.time()
    if lines:
        try:
            CACHE.write_text(json.dumps({'generated_at': _CORPUS_META['generated_at'],
                                         'lines': lines}))
        except Exception:
            pass
    return lines


_STOP = set('the a an of to in for is are was were with and or not no on by from that this it as'.split())


def _kw_score(query, line):
    q = set(re.findall(r'[a-z0-9_.-]{3,}', query.lower())) - _STOP
    l = set(re.findall(r'[a-z0-9_.-]{3,}', line.lower())) - _STOP
    return len(q & l) / (len(q) ** 0.5 + 1) if q else 0.0


def _rerank(query, lines):
    import requests
    r = requests.post(RERANK_URL, json={'query': query[:1000], 'texts': lines},
                      timeout=25)
    r.raise_for_status()
    d = r.json()
    scores = d.get('scores') or d.get('results') or d
    if isinstance(scores, list) and scores and isinstance(scores[0], dict):
        scores = [s.get('score', 0.0) for s in scores]
    if not (isinstance(scores, list) and len(scores) == len(lines)):
        raise ValueError('rerank shape mismatch')
    return scores


def retrieve(answer_text, k=TOP_K):
    lines = corpus()
    if not lines:
        return []
    kw = [(_kw_score(answer_text, l), l) for l in lines]
    kw.sort(reverse=True)
    shortlist = [l for s, l in kw[:min(len(kw), max(k * 3, 120))] if s > 0]
    if not shortlist:
        return [l for _, l in kw[:k]]
    try:
        scores = _rerank(answer_text, shortlist)
        ranked = sorted(zip(scores, shortlist), reverse=True)
        return [l for _, l in ranked[:k]]
    except Exception:
        return shortlist[:k]      # keyword fallback — never empty


# wq#2430: provenance schema. Every retrieved chunk maps its source tag to the
# URI it was probed from; [capability] lines override the corpus build time
# with the card's own generation date (the one probe whose evidence genuinely
# ages — everything else is re-probed each corpus build, <= FACTPACK_TTL old).
_SOURCE_REFS = {
    'service': 'cmd://systemctl list-units lumen-* minerva-*',
    'model': 'file:///home/lumen/agent/litellm_config.yaml',
    'fact': 'cmd:///home/lumen/minerva/lumen_state.py current',
    'artifact': 'sqlite:///home/lumen/minerva/minerva.db',
    'capability': f'file://{CARDS_PATH}',
    'hw': 'cmd://lspci+lscpu',
}
_CARD_DATE_RE = re.compile(r'verified from source, (\d{4}-\d{2}-\d{2})')


def tag_provenance(lines, generated_at=None):
    """wq#2430: wrap retrieved chunk strings in the provenance contract:
    {'text', 'source', 'source_ref', 'ingested_at' (UTC ISO), 'stale'}.
    stale = ingested more than EVIDENCE_TTL seconds ago (0 disables)."""
    now = time.time()
    gen = generated_at or _CORPUS_META.get('generated_at') or now
    out = []
    for line in lines:
        tag = line[1:line.find(']')] if line.startswith('[') and ']' in line else 'unknown'
        ingested = gen
        m = _CARD_DATE_RE.search(line)
        if m:
            try:
                ingested = datetime.strptime(m.group(1), '%Y-%m-%d').replace(
                    tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
        out.append({
            'text': line,
            'source': tag,
            'source_ref': _SOURCE_REFS.get(tag, 'probe://unknown'),
            'ingested_at': datetime.fromtimestamp(ingested, timezone.utc).isoformat(timespec='seconds'),
            'stale': bool(EVIDENCE_TTL) and (now - ingested) > EVIDENCE_TTL,
        })
    return out


def retrieve_evidence(answer_text, k=TOP_K):
    """wq#2430: retrieve() with the provenance contract attached."""
    return tag_provenance(retrieve(answer_text, k))


def _evidence_key(text):
    return hashlib.sha1(text.encode()).hexdigest()


# wq#2430: provenance of the facts the most recent build_context() actually
# included, keyed by the answer text's hash. minerva_verify picks this up when
# its caller hands it a context built here but no explicit evidence list (the
# live proposal path); the key check makes cross-answer leakage impossible.
LAST_EVIDENCE = {'key': None, 'chunks': []}


CLOSED_WORLD = (
    "\nLIVE-PROBED FACTS (retrieved for this specific verification).\n"
    "CLOSED-WORLD RULE for tagged enumerations: the [service], [model] and\n"
    "[artifact] probes enumerate the COMPLETE live system. If a claim says a\n"
    "service/model/table/mechanism does NOT exist and no probed line shows it,\n"
    "treat the absence as SUPPORTED by these probes — absence from a complete\n"
    "enumeration is positive evidence, not missing context.\n"
    "[capability] lines are per-module mechanism inventories generated from\n"
    "the module source itself. A [capability] line saying a module does NOT\n"
    "implement a mechanism is direct evidence for absence claims about that\n"
    "subsystem; a mechanism absent from every relevant module card is absent.\n")


_SVC_HEAD = 'Lumen/Minerva systemd units'
_MODE_RE = re.compile(r'^- (?P<name>\S+) \[(?P<mode>[^\]]+)\]: (?P<desc>.*)$')


def judge_digest(digest: str) -> str:
    """wq#1501: the digest the JUDGE sees. Same file as the generator's, but the
    systemd section (13K of the 25K) is compacted to name + run-mode so the whole
    inventory still fits an 8K-token local rung alongside the retrieved facts.
    Absence claims keep their closed-world basis (every unit is still named);
    what a unit DOES arrives through the per-claim [service] retrieval instead.
    Deterministic string transform — no probes, so it costs nothing."""
    start = digest.find(_SVC_HEAD)
    if start < 0:
        return digest
    end = digest.find('\n\n', start)
    if end < 0:
        end = len(digest)
    block = digest[start:end].splitlines()
    out = [block[0].split(' — ')[0] + ' — name [run mode] (descriptions: see [service] facts below).']
    for ln in block[1:]:
        m = _MODE_RE.match(ln)
        if not m:
            continue
        mode = m.group('mode')
        if mode.startswith('timer-driven by '):
            mode = 'timer ' + mode[len('timer-driven by '):]
        out.append(f'- {m.group("name")} [{mode}]')
    return digest[:start] + '\n'.join(out) + digest[end:]


def judge_digest_len() -> int:
    try:
        return len(judge_digest(DIGEST.read_text()))
    except Exception:
        return 0


def build_context(answer_text):
    """Hybrid context: static digest (narrative + documented GAPS, which entail
    negative claims) + per-claim retrieved facts under a closed-world rule
    (which entail both existence AND absence claims from live probes).

    v1 of this module returned retrieval ONLY — verify_gold_v2 rescoring showed
    recall REGRESSING 0.36 -> 0.21: existence claims improved but gap/negation
    claims collapsed, because a list of positive facts cannot entail "no X
    exists" and the digest's GAPS section had been carrying those. Hybrid +
    closed-world fixes both directions. Falls back to digest alone on any error
    so the guard never sees less than the pre-factpack baseline.

    wq#2430: on success, LAST_EVIDENCE records the provenance of exactly the
    fact lines included in the returned context (keyed by answer hash) so the
    verify path can apply its stale-evidence TTL gate without a caller change.
    On the fallback path the key is left cleared — no facts, no gate.
    """
    LAST_EVIDENCE['key'], LAST_EVIDENCE['chunks'] = None, []
    try:
        digest = judge_digest(DIGEST.read_text())
    except Exception:
        digest = HEADER
    try:
        facts = retrieve(answer_text)
        if len(facts) < 5:
            raise ValueError('thin retrieval')
        # wq#1501: the digest is NEVER truncated here — it was (CAP 15500 < 17096
        # digest), which dropped the digest tail and every retrieved fact. Facts
        # fill whatever budget remains under CAP; the judge window (VERIFY_JUDGE_CTX_
        # CHARS) is the outer bound and minerva_verify asserts the ordering at import.
        head = digest + CLOSED_WORLD
        # wq#1528: facts keep at least FACTS_RESERVE even if the digest grows past
        # its share of CAP. The import-time assertion keeps CAP >= head + reserve;
        # if the digest still outgrows it between restarts, the overflow spills
        # into the judge window's slack (verify slices context[:JUDGE_CTX_CHARS],
        # facts sit at the END) — so a digest spurt costs the facts TAIL, never
        # every fact, which is exactly the wq#1383-class failure this prevents.
        room = max(FACTS_RESERVE, CAP - len(head))
        body, used = [], 0
        for f in facts:
            if used + len(f) + 1 > room:
                break
            body.append(f); used += len(f) + 1
        LAST_EVIDENCE['chunks'] = tag_provenance(body)
        LAST_EVIDENCE['key'] = _evidence_key(answer_text)
        return head + '\n'.join(body)
    except Exception:
        return digest


if __name__ == '__main__':
    import sys
    c = corpus(force='--force' in sys.argv)
    from collections import Counter
    print(f'corpus: {len(c)} lines', dict(Counter(l.split("]")[0] + "]" for l in c)))
    q = ' '.join(a for a in sys.argv[1:] if not a.startswith('--')) or 'skills table workflow capture'
    print(f'\ntop-8 for: {q}')
    for l in retrieve(q, 8):
        print(' ', l[:130])

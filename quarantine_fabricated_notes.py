#!/usr/bin/env python3
"""wq#2027 — quarantine fleet research notes that cite a repo tree/paths which
do not resolve against the real repo.

Reference defect (foreman/research/AGENTFORGE_EVAL.md section 0/7):
scratch/research/intel_new_github_repo__roshanrameshhub_agentforge_01a87c.md
claims the repo's tree is `agents/ routing/ rag/ memory/ mcp/ workflow/
human/`; the real tree (fetched via the GitHub trees API) is `agent/ client/
docker/ docs/ k8s/ media/ monitoring/ schema/ scripts/ service/` — zero
overlap. That note fed a proposal (37cb4ce26a9b) that the verify gate scored
1.00, because nothing checked the note's claimed tree against the actual
repo. This script is the general form of that one finding: any note under
`scratch/research/` that names a github.com repo AND lists what reads as a
top-level directory tree for it is a quarantine candidate if none of its
cited top-level entries exist in the real tree.

A note that names no tree at all is not a candidate (most research notes
don't enumerate a repo's layout) — only "cited a tree, tree doesn't resolve"
is flagged, matching the ticket's own quarantine criterion.

Fail-closed on lookup failure: a repo the GitHub API could not be asked about
(network error, private repo, rate limit) is left alone, not quarantined —
this script proves fabrication, it does not guess at it.

Dry-run by default; --apply moves matched files into
`<research-dir>/_quarantine/` and prepends a banner recording what didn't
resolve, so `lumen-research-index` (which globs scratch/research/*.md) stops
picking the file up.

    python3 quarantine_fabricated_notes.py [--research-dir DIR] [--apply]
"""
import argparse
import os
import re
import sys
from datetime import datetime, timezone

import requests

RESEARCH_DIR_DEFAULT = '/home/lumen/scratch/research'
GH_TREE_API = 'https://api.github.com/repos/{repo}/git/trees/{ref}'
GH_REPO_API = 'https://api.github.com/repos/{repo}'
TIMEOUT = 15

_GH_URL_RE = re.compile(r'github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})', re.I)
# A "tree line" looks like a directory listing entry: a bare identifier ending
# in `/`, optionally inside backticks/bullets/table cells. Matches both prose
# ("claims tree `agents/ routing/ rag/`") and a fenced listing.
_TREE_DIR_RE = re.compile(r'\b([a-zA-Z][a-zA-Z0-9_.-]{0,40})/(?!\S)')


def _gh_headers():
    h = {'Accept': 'application/vnd.github+json', 'User-Agent': 'minerva-quarantine'}
    tok = os.getenv('GITHUB_TOKEN') or os.getenv('GH_TOKEN')
    if tok:
        h['Authorization'] = f'Bearer {tok}'
    return h


def extract_repo_slug(text: str):
    m = _GH_URL_RE.search(text)
    if not m:
        return None
    return f'{m.group(1)}/{m.group(2)}'.rstrip('/')


def extract_cited_dirs(text: str) -> set:
    """Top-level directory names the note claims the repo contains. Heuristic,
    not a parser: pulls every `word/` token, which is how both a fenced tree
    listing and inline prose ("real tree `agent/ client/ docker/`") render
    top-level dirs in these notes."""
    return {m.group(1).lower() for m in _TREE_DIR_RE.finditer(text)}


def fetch_real_top_level_dirs(repo_slug: str):
    """Returns (dirs: set|None, error: str|None). None dirs = could not be
    confirmed (fail-closed: caller must not treat that as fabrication)."""
    try:
        r = requests.get(GH_REPO_API.format(repo=repo_slug), timeout=TIMEOUT, headers=_gh_headers())
        if r.status_code != 200:
            return None, f'repo_lookup_{r.status_code}'
        default_branch = r.json().get('default_branch', 'main')
        r2 = requests.get(GH_TREE_API.format(repo=repo_slug, ref=default_branch),
                           params={'recursive': '0'}, timeout=TIMEOUT, headers=_gh_headers())
        if r2.status_code != 200:
            return None, f'tree_lookup_{r2.status_code}'
        tree = r2.json().get('tree', [])
        return {e['path'].lower() for e in tree if e.get('type') == 'tree'}, None
    except Exception as e:
        return None, f'error:{e}'


def check_note(path: str):
    """Returns a dict describing the verdict, never raises."""
    text = open(path, encoding='utf-8', errors='replace').read()
    slug = extract_repo_slug(text)
    if not slug:
        return {'path': path, 'verdict': 'skip', 'reason': 'no_github_repo_cited'}
    cited = extract_cited_dirs(text)
    if not cited:
        return {'path': path, 'verdict': 'skip', 'reason': 'no_tree_cited', 'repo': slug}
    real, err = fetch_real_top_level_dirs(slug)
    if real is None:
        return {'path': path, 'verdict': 'inconclusive', 'reason': err, 'repo': slug, 'cited': sorted(cited)}
    overlap = cited & real
    if overlap:
        return {'path': path, 'verdict': 'ok', 'repo': slug,
                'cited': sorted(cited), 'real': sorted(real), 'overlap': sorted(overlap)}
    return {'path': path, 'verdict': 'quarantine', 'repo': slug,
            'cited': sorted(cited), 'real': sorted(real)}


def quarantine(path: str, verdict: dict, research_dir: str):
    qdir = os.path.join(research_dir, '_quarantine')
    os.makedirs(qdir, exist_ok=True)
    dest = os.path.join(qdir, os.path.basename(path))
    banner = (
        f'<!-- wq#2027 QUARANTINED {datetime.now(timezone.utc).isoformat()}: '
        f'cited repo tree for {verdict["repo"]} does not resolve. '
        f'cited={verdict["cited"]} real={verdict["real"]} -->\n\n'
    )
    with open(path, encoding='utf-8', errors='replace') as f:
        body = f.read()
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(banner + body)
    os.remove(path)
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--research-dir', default=RESEARCH_DIR_DEFAULT)
    ap.add_argument('--file', help='check a single file instead of scanning research-dir')
    ap.add_argument('--apply', action='store_true', help='move matches to _quarantine/; default is dry-run')
    a = ap.parse_args()

    if a.file:
        paths = [a.file]
    else:
        if not os.path.isdir(a.research_dir):
            print(f'no such directory: {a.research_dir}', file=sys.stderr)
            return 1
        paths = [os.path.join(a.research_dir, f) for f in sorted(os.listdir(a.research_dir))
                 if f.endswith('.md') and os.path.isfile(os.path.join(a.research_dir, f))]

    quarantine_candidates = []
    for p in paths:
        v = check_note(p)
        if v['verdict'] == 'quarantine':
            quarantine_candidates.append(v)
            print(f'[QUARANTINE] {p}  repo={v["repo"]}  cited={v["cited"]}  real={v["real"]}')
        elif v['verdict'] == 'inconclusive':
            print(f'[SKIP-inconclusive] {p}  repo={v.get("repo")}  reason={v["reason"]}')
        elif v['verdict'] == 'ok':
            print(f'[OK] {p}  repo={v["repo"]}  overlap={v["overlap"]}')
        # 'skip' (no repo / no tree cited) is silent — the overwhelming majority of notes.

    print(f'\n{len(quarantine_candidates)} quarantine candidate(s) of {len(paths)} note(s) scanned')
    if not a.apply:
        print('dry-run: nothing moved (pass --apply to quarantine)')
        return 0
    for v in quarantine_candidates:
        dest = quarantine(v['path'], v, a.research_dir)
        print(f'  moved {v["path"]} -> {dest}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

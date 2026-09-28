#!/usr/bin/env python3
"""wq#2027 — fetch+store the source metadata a classified_intel row needs
before it is eligible to seed a proposal (minerva_proposal.get_candidates()
now requires a non-empty url AND summary — see that file's wq#2027 comment).

Defect this closes (foreman/research/AGENTFORGE_EVAL.md section 0/1):
classified_intel rows for GitHub-repo intel were classified from the repo
TITLE alone — url and summary both empty — so a proposal's "orchestration
patterns" / "memory + skills" claims were the classifier's own HINT text,
never a fact about the repo.

fetch_repo_source(url) does the four checks AGENTFORGE_EVAL.md's own manual
research used to catch both fabrications: README head (what the repo says
about itself), license (real vs./none), commit count + pushed_at (demo/
abandoned vs. maintained — e.g. Roshanrameshhub/AgentForge: 6 commits,
created+pushed same day). Returns a flat dict; `summary` is a single text
blob sized for the classifier prompt and for source_grounding_check()'s
citation scoring in minerva_verify.py.

Integration point (minerva_classifier.py lives at /home/lumen/minerva,
outside this repo — not reachable from this worktree/session; wire this in
from a session with that path in scope):
    from fetch_source import fetch_repo_source
    ...
    meta = fetch_repo_source(item_url)
    if meta:
        cur.execute('UPDATE classified_intel SET url=?, summary=? WHERE id=?',
                     (meta['url'], meta['summary'], intel_id))
Fail-open at the classifier layer (a fetch failure just leaves url/summary
empty, same as today) — the gate that matters is downstream and already
fail-closed: get_candidates() will not surface the row until this succeeds.
"""
import os
import re
import requests
from datetime import datetime, timezone

TIMEOUT = 15
GH_REPO_API = 'https://api.github.com/repos/{repo}'
GH_README_API = 'https://api.github.com/repos/{repo}/readme'
GH_COMMITS_API = 'https://api.github.com/repos/{repo}/commits'
README_HEAD_CHARS = 600
SUMMARY_MAX_CHARS = 1200

_GH_URL_RE = re.compile(r'github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})', re.I)


def _gh_headers():
    h = {'Accept': 'application/vnd.github+json', 'User-Agent': 'minerva-classifier'}
    tok = os.getenv('GITHUB_TOKEN') or os.getenv('GH_TOKEN')
    if tok:
        h['Authorization'] = f'Bearer {tok}'
    return h


def _repo_slug_from_url(url: str):
    m = _GH_URL_RE.search(url or '')
    if not m:
        return None
    return f'{m.group(1)}/{m.group(2)}'.rstrip('/')


def _readme_head(repo_slug: str) -> str:
    try:
        r = requests.get(GH_README_API.format(repo=repo_slug), timeout=TIMEOUT,
                          headers={**_gh_headers(), 'Accept': 'application/vnd.github.raw+json'})
        if r.status_code != 200:
            return ''
        return r.text[:README_HEAD_CHARS]
    except Exception:
        return ''


def _commit_count_estimate(repo_slug: str):
    """GitHub has no direct commit-count endpoint; page 1 with per_page=1 and
    reading the `last` Link-header page number is the standard estimate. None
    on any failure — caller must not read that as zero commits."""
    try:
        r = requests.get(GH_COMMITS_API.format(repo=repo_slug), timeout=TIMEOUT,
                          params={'per_page': 1}, headers=_gh_headers())
        if r.status_code != 200:
            return None
        link = r.headers.get('Link', '')
        m = re.search(r'[?&]page=(\d+)>;\s*rel="last"', link)
        if m:
            return int(m.group(1))
        body = r.json()
        return len(body) if isinstance(body, list) else None
    except Exception:
        return None


def fetch_repo_source(url: str):
    """Returns a dict with url/summary/license/commit_count/pushed_at/
    fetched_at, or None if the url isn't a GitHub repo or the repo lookup
    itself fails (404, rate-limited, network error) — the classifier leaves
    url/summary empty in that case, same as the pre-wq#2027 behaviour, and
    the row simply stays ineligible until a later classifier run succeeds."""
    slug = _repo_slug_from_url(url)
    if not slug:
        return None
    try:
        r = requests.get(GH_REPO_API.format(repo=slug), timeout=TIMEOUT, headers=_gh_headers())
        if r.status_code != 200:
            return None
        repo = r.json()
    except Exception:
        return None

    license_name = ((repo.get('license') or {}).get('spdx_id')) or 'NOASSERTION'
    pushed_at = repo.get('pushed_at') or ''
    created_at = repo.get('created_at') or ''
    stars = repo.get('stargazers_count')
    commit_count = _commit_count_estimate(slug)
    readme = _readme_head(slug)

    summary_parts = [
        f'{slug}: {repo.get("description") or "(no description)"}',
        f'License: {license_name}. Stars: {stars if stars is not None else "unknown"}.',
        f'Commits: {commit_count if commit_count is not None else "unknown"}. '
        f'Created: {created_at or "unknown"}. Last pushed: {pushed_at or "unknown"}.',
    ]
    if readme:
        summary_parts.append(f'README head: {readme}')
    summary = ' '.join(summary_parts)[:SUMMARY_MAX_CHARS]

    return {
        'url': f'https://github.com/{slug}',
        'summary': summary,
        'license': license_name,
        'commit_count': commit_count,
        'created_at': created_at,
        'pushed_at': pushed_at,
        'stars': stars,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
    }


if __name__ == '__main__':
    import sys, json
    if len(sys.argv) != 2:
        print('usage: fetch_source.py <github-url-or-owner/repo>', file=sys.stderr)
        raise SystemExit(2)
    arg = sys.argv[1]
    url = arg if arg.startswith('http') else f'https://github.com/{arg}'
    meta = fetch_repo_source(url)
    print(json.dumps(meta, indent=2) if meta else 'null')

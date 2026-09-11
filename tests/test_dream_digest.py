"""lumen_dream_digest (wq#1638) — the outbox digest and the one auto-apply.

The rule this file exists to hold: exact-duplicate episodic merges (cosine >= 0.98)
apply themselves, and NOTHING else does. Prose demotion plans, instinct candidates
and detector proposals are listed, aged and given an apply command — never applied.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import lumen_dream_digest as dd  # noqa: E402
import lumen_dream as ld  # noqa: E402

NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / 'minerva.db'
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE note_merges (absorbed_id TEXT PRIMARY KEY, keep_id TEXT NOT NULL,
                                  sim REAL, method TEXT, ts TEXT);
        CREATE TABLE prose_demotions (section_id TEXT NOT NULL, title TEXT,
                                      demoted_at TEXT NOT NULL, restored_at TEXT,
                                      PRIMARY KEY (section_id, demoted_at));
    """)
    c.commit(); c.close()
    return str(p)


def merge_plan(d, date, pairs):
    p = d / f'merge_plan_{date}.json'
    p.write_text(json.dumps({'generated': date, 'merges': [
        {'keep': k, 'absorb': a, 'sim': s, 'keep_title': k, 'absorb_title': a}
        for k, a, s in pairs]}))
    return p


def prose_plan(d, date, ids):
    p = d / f'prose_demotion_plan_{date}.json'
    p.write_text(json.dumps({'generated': date, 'count': len(ids),
                             'candidates': [{'id': i, 'strong': True} for i in ids]}))
    return p


@pytest.fixture
def dream(tmp_path):
    d = tmp_path / 'dream'
    d.mkdir()
    return d


# ---------------------------------------------------------------- age


def test_age_comes_from_the_dated_filename_not_the_mtime(dream):
    p = merge_plan(dream, '2026-08-09', [('a', 'b', 0.99)])
    # written seconds ago; the backlog it represents is 35 days old
    assert dd._age_days(p, NOW) == (35, 'name')


def test_a_file_rewritten_in_place_falls_back_to_mtime_and_says_so(dream):
    p = dream / 'instincts_candidates.yaml'
    p.write_text('candidates:\n')
    age, src = dd._age_days(p, NOW)
    assert src == 'mtime' and age >= 0


# ---------------------------------------------------------------- auto-apply


def test_only_exact_pairs_apply(dream, db):
    plan = merge_plan(dream, '2026-09-13',
                      [('keep1', 'dup1', 0.9996), ('keep2', 'near2', 0.94)])
    applied = dd.auto_apply(plan_path=plan, plan_dir=dream, db=db)
    assert [m['absorb'] for m in applied] == ['dup1']
    rows = sqlite3.connect(db).execute(
        'SELECT absorbed_id, keep_id, method FROM note_merges').fetchall()
    assert rows == [('dup1', 'keep1', 'exact-auto')]


def test_re_running_the_same_plan_applies_nothing_new(dream, db):
    plan = merge_plan(dream, '2026-09-13', [('keep1', 'dup1', 0.99)])
    assert len(dd.auto_apply(plan_path=plan, plan_dir=dream, db=db)) == 1
    assert dd.auto_apply(plan_path=plan, plan_dir=dream, db=db) == []


def test_auto_apply_picks_the_newest_plan_when_none_is_named(dream, db):
    merge_plan(dream, '2026-08-09', [('old', 'olddup', 0.99)])
    merge_plan(dream, '2026-09-13', [('new', 'newdup', 0.99)])
    assert [m['absorb'] for m in dd.auto_apply(plan_dir=dream, db=db)] == ['newdup']


def test_a_chain_of_absorbed_notes_resolves_to_one_survivor(dream, db):
    # the shape a real plan has: five near-identical notes proposed pairwise, so
    # absorb-targets are themselves absorbed a link or two later
    plan = merge_plan(dream, '2026-09-13', [
        ('keep', 'n1', 0.9996), ('n1', 'n2', 0.9995), ('n2', 'n3', 0.999),
        ('n3', 'n4', 0.9989)])
    dd.auto_apply(plan_path=plan, plan_dir=dream, db=db)
    rows = dict(sqlite3.connect(db).execute('SELECT absorbed_id, keep_id FROM note_merges'))
    assert rows == {'n1': 'keep', 'n2': 'keep', 'n3': 'keep', 'n4': 'keep'}


def test_a_dry_run_applies_nothing(dream, db):
    plan = merge_plan(dream, '2026-09-13', [('keep1', 'dup1', 0.99)])
    assert ld.apply_merge_plan(plan, confirm=False, db=db) == []
    assert sqlite3.connect(db).execute('SELECT count(*) FROM note_merges').fetchone()[0] == 0


def test_auto_apply_uses_the_lumen_dream_it_ships_with(dream, db, monkeypatch):
    """The live install is on sys.path under MINERVA_HOME; a checkout must still
    apply through its own lumen_dream.py, or a worktree run tests the wrong code."""
    monkeypatch.delitem(sys.modules, 'lumen_dream', raising=False)
    monkeypatch.setattr(dd, 'MINERVA', '/nonexistent/minerva')
    plan = merge_plan(dream, '2026-09-13', [('keep1', 'dup1', 0.99)])
    assert [m['absorb'] for m in dd.auto_apply(plan_path=plan, plan_dir=dream, db=db)] == ['dup1']


# ---------------------------------------------------------------- scan


def test_an_applied_plan_drops_off_the_queue(dream, db):
    plan = merge_plan(dream, '2026-09-06', [('keep1', 'dup1', 0.99)])
    dd.auto_apply(plan_path=plan, plan_dir=dream, db=db)
    assert dd.scan(plan_dir=dream, db=db, now=NOW) == []


def test_a_partly_applied_plan_stays_with_its_remainder(dream, db):
    merge_plan(dream, '2026-09-06', [('k1', 'dup1', 0.99), ('k2', 'near2', 0.94)])
    dd.auto_apply(plan_dir=dream, db=db)
    item, = dd.scan(plan_dir=dream, db=db, now=NOW)
    assert (item['pending'], item['total']) == (1, 2)
    assert 'manual review' in item['note']


def test_prose_plans_are_listed_but_never_applied(dream, db):
    prose_plan(dream, '2026-08-15', ['sec-a', 'sec-b'])
    item, = dd.scan(plan_dir=dream, db=db, now=NOW)
    assert item['kind'] == 'prose demotion plan'
    assert item['pending'] == 2 and item['age_days'] == 29
    assert item['apply'].startswith('python3 /home/lumen/minerva/lumen_prose_lifecycle.py apply')
    assert sqlite3.connect(db).execute(
        'SELECT count(*) FROM prose_demotions').fetchone()[0] == 0


def test_a_demoted_section_is_not_still_pending(dream, db):
    prose_plan(dream, '2026-08-15', ['sec-a', 'sec-b'])
    c = sqlite3.connect(db)
    c.execute("INSERT INTO prose_demotions(section_id,title,demoted_at) VALUES('sec-a','x','t')")
    c.commit(); c.close()
    item, = dd.scan(plan_dir=dream, db=db, now=NOW)
    assert item['pending'] == 1


def test_review_only_artifacts_are_listed_with_no_apply_command(dream, db):
    (dream / 'instincts_candidates.yaml').write_text('candidates:\n')
    (dream / 'convo-fact-candidates.md').write_text('- fact\n')
    (dream / 'detector_proposals_2026-08-30.json').write_text('{}')
    kinds = {i['kind']: i for i in dd.scan(plan_dir=dream, db=db, now=NOW)}
    assert set(kinds) == {'instinct candidates', 'convo fact candidates',
                          'governance detector proposal'}
    assert all(i['apply'] is None for i in kinds.values())
    assert 'instincts.yaml' in kinds['instinct candidates']['hint']


def test_reflections_in_the_subdirectory_are_counted(dream, db):
    (dream / 'reflections').mkdir()
    (dream / 'reflections' / '2026-08-09.md').write_text('lesson')
    item, = dd.scan(plan_dir=dream, db=db, now=NOW)
    assert item['kind'] == 'reflection'


def test_an_unknown_artifact_kind_is_still_surfaced(dream, db):
    (dream / 'brand_new_proposal_2026-09-06.json').write_text('{}')
    item, = dd.scan(plan_dir=dream, db=db, now=NOW)
    assert item['kind'] == 'unclassified artifact'


def test_the_runs_own_outputs_are_not_proposals(dream, db):
    (dream / 'dream-2026-09-06.md').write_text('# report')
    (dream / 'digest-2026-09-06.md').write_text('# digest')
    (dream / dd.STATUS_JSON).write_text('{}')
    assert dd.scan(plan_dir=dream, db=db, now=NOW) == []


def test_the_oldest_artifact_is_listed_first(dream, db):
    merge_plan(dream, '2026-09-06', [('k', 'a', 0.94)])
    prose_plan(dream, '2026-08-15', ['sec-a'])
    assert [i['age_days'] for i in dd.scan(plan_dir=dream, db=db, now=NOW)] == [29, 7]


def test_what_a_command_can_drain_is_listed_above_what_only_a_human_can(dream, db):
    (dream / 'reflections').mkdir()
    (dream / 'reflections' / 'reflection-2026-06-25.md').write_text('lesson')  # 80d
    merge_plan(dream, '2026-09-06', [('k', 'a', 0.94)])                        # 7d
    kinds = [i['kind'] for i in dd.scan(plan_dir=dream, db=db, now=NOW)]
    assert kinds == ['episodic merge plan', 'reflection']


def test_an_older_plan_the_newest_one_covers_is_not_a_second_command(dream, db):
    merge_plan(dream, '2026-08-09', [('k1', 'a1', 0.94)])
    merge_plan(dream, '2026-09-06', [('k1', 'a1', 0.94), ('k2', 'a2', 0.94)])
    old, new = sorted(dd.scan(plan_dir=dream, db=db, now=NOW), key=lambda i: -i['age_days'])
    assert old['superseded_by'] == 'merge_plan_2026-09-06.json'
    assert old['apply'] is None and 'apply that one' in old['note']
    assert new['apply'] is not None
    # the shared pair is one job, not two
    assert dd.headline([old, new], [])['pending_items'] == 2


def test_supersession_is_per_kind_not_across_kinds(dream, db):
    prose_plan(dream, '2026-08-15', ['sec-a'])
    prose_plan(dream, '2026-09-06', ['sec-a', 'sec-b'])
    merge_plan(dream, '2026-09-06', [('k1', 'a1', 0.94)])
    items = {(i['kind'], i['age_days']): i for i in dd.scan(plan_dir=dream, db=db, now=NOW)}
    assert items[('prose demotion plan', 29)]['superseded_by'] == \
        'prose_demotion_plan_2026-09-06.json'
    assert items[('prose demotion plan', 7)]['apply'] is not None
    assert items[('episodic merge plan', 7)]['apply'] is not None


def test_an_older_plan_with_pairs_of_its_own_keeps_its_command(dream, db):
    merge_plan(dream, '2026-08-09', [('k1', 'a1', 0.94), ('kx', 'ax', 0.94)])
    merge_plan(dream, '2026-09-06', [('k1', 'a1', 0.94)])
    old = max(dd.scan(plan_dir=dream, db=db, now=NOW), key=lambda i: i['age_days'])
    assert 'superseded_by' not in old and old['apply'] is not None


# ---------------------------------------------------------------- render


def test_the_digest_carries_age_and_the_apply_command_for_every_plan(dream, db):
    merge_plan(dream, '2026-08-09', [('k', 'a', 0.94)])
    prose_plan(dream, '2026-08-15', ['sec-a'])
    text = dd.render(dd.scan(plan_dir=dream, db=db, now=NOW), [], now=NOW)
    assert '35d' in text and '29d' in text
    assert 'lumen_dream.py apply --plan' in text
    assert 'lumen_prose_lifecycle.py apply --plan' in text


def test_the_digest_logs_the_ids_it_applied(dream, db):
    plan = merge_plan(dream, '2026-09-13', [('keep1', 'dup1', 0.9996)])
    applied = dd.auto_apply(plan_path=plan, plan_dir=dream, db=db)
    text = dd.render([], applied, now=NOW)
    assert 'dup1' in text and 'keep1' in text and '0.9996' in text


def test_an_empty_outbox_says_drained(dream, db):
    assert dd.DRAINED in dd.render([], [], now=NOW)


# ---------------------------------------------------------------- briefing


def _status(dream, **kw):
    base = dict(generated=NOW.isoformat(), digest='/x/digest.md', ticket='1800',
                pending_plans=2, pending_items=9, oldest_days=35, applied=3,
                status='OUTBOX PENDING: 2', applied_ids=[],
                top=[dict(kind='episodic merge plan', age_days=35, path='/x/p.json',
                          apply='python3 apply-me')])
    base.update(kw)
    (dream / dd.STATUS_JSON).write_text(json.dumps(base))


def test_the_briefing_names_the_backlog_the_ticket_and_one_apply_command(dream):
    _status(dream)
    s = dd.briefing_section(plan_dir=dream, now=NOW)
    assert '2 unapplied' in s and 'oldest 35d' in s
    assert 'python3 apply-me' in s and 'wq#1800' in s


def test_the_briefing_reports_a_drained_outbox_instead_of_a_list(dream):
    _status(dream, status=dd.DRAINED, pending_plans=0)
    assert 'drained' in dd.briefing_section(plan_dir=dream, now=NOW)


def test_a_stopped_weekly_run_is_louder_than_an_empty_queue(dream):
    _status(dream, generated=(NOW - timedelta(days=30)).isoformat())
    s = dd.briefing_section(plan_dir=dream, now=NOW)
    assert 'has stopped' in s and '30d' in s


def test_no_status_file_means_no_briefing_noise(dream):
    assert dd.briefing_section(plan_dir=dream, now=NOW) == ''


# ---------------------------------------------------------------- the ticket


class FakeWq:
    """A wq that records its argv and answers `add` the way the real one does."""

    def __init__(self, tmp_path):
        self.log = tmp_path / 'wq.log'
        self.path = tmp_path / 'wq'
        self.path.write_text(
            '#!/usr/bin/env python3\n'
            'import sys, json\n'
            f'open({str(self.log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")\n'
            'print("added task 1801 [user]") if sys.argv[1] == "add" else print("done")\n')
        self.path.chmod(0o755)

    def __str__(self):
        return str(self.path)

    @property
    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]


@pytest.fixture
def fake_wq(tmp_path):
    return FakeWq(tmp_path)


@pytest.fixture
def queue_db(tmp_path):
    p = tmp_path / 'queue.db'
    c = sqlite3.connect(p)
    c.execute('CREATE TABLE tasks (id INTEGER PRIMARY KEY, lane TEXT, status TEXT, title TEXT)')
    c.execute("INSERT INTO tasks VALUES (1700,'user','open','[dream-digest 2026-W36] 7 unapplied')")
    c.execute("INSERT INTO tasks VALUES (1701,'user','done','[dream-digest 2026-W35] 6 unapplied')")
    c.execute("INSERT INTO tasks VALUES (1702,'user','open','Nest UART recon')")
    c.commit(); c.close()
    return str(p)


def test_one_ticket_is_opened_and_last_weeks_is_superseded(dream, db, fake_wq, queue_db):
    merge_plan(dream, '2026-08-09', [('k', 'a', 0.94)])
    items = dd.scan(plan_dir=dream, db=db, now=NOW)
    tid, superseded = dd.open_ticket(items, [], '/x/digest.md', '/x/status.json',
                                     now=NOW, wq_bin=str(fake_wq), queue_db=queue_db)
    assert tid == '1801' and superseded == [1700]
    calls = fake_wq.calls
    assert [c[0] for c in calls] == ['add', 'done']
    assert calls[1][1] == '1700' and 'superseded by wq#1801' in calls[1][3]


def test_the_ticket_is_user_lane_week_stamped_and_liveness_tagged(dream, db, fake_wq, queue_db):
    merge_plan(dream, '2026-08-09', [('k', 'a', 0.94)])
    items = dd.scan(plan_dir=dream, db=db, now=NOW)
    dd.open_ticket(items, [], '/x/digest.md', '/x/status.json', now=NOW,
                   wq_bin=str(fake_wq), queue_db=queue_db)
    add = fake_wq.calls[0]
    assert add[1:3] == ['--lane', 'user']
    title = add[add.index('--title') + 1]
    detail = add[add.index('--detail') + 1]
    assert title.startswith('[dream-digest 2026-W37]')      # unique per week
    assert 'Liveness: file_contains(/x/status.json, OUTBOX DRAINED)' in detail
    assert 'lumen_dream.py apply --plan' in detail


def test_an_empty_outbox_opens_no_ticket_at_all(dream, db, fake_wq, queue_db):
    assert dd.open_ticket([], [], '/x/d.md', '/x/s.json', now=NOW,
                          wq_bin=str(fake_wq), queue_db=queue_db) == (None, [])
    assert not fake_wq.log.exists()


def test_a_refused_wq_add_leaves_last_weeks_ticket_open(dream, db, tmp_path, queue_db):
    bad = tmp_path / 'wq-refuses'
    bad.write_text('#!/bin/sh\necho "DUPLICATE — task 1700 already has this title" >&2\nexit 1\n')
    bad.chmod(0o755)
    merge_plan(dream, '2026-08-09', [('k', 'a', 0.94)])
    items = dd.scan(plan_dir=dream, db=db, now=NOW)
    assert dd.open_ticket(items, [], '/x/d.md', '/x/s.json', now=NOW,
                          wq_bin=str(bad), queue_db=queue_db) == (None, [])


# ---------------------------------------------------------------- end to end


def test_a_full_pass_applies_the_exact_pair_and_reports_the_rest(dream, db, fake_wq, queue_db):
    merge_plan(dream, '2026-09-13', [('keep1', 'dup1', 0.99), ('keep2', 'near2', 0.94)])
    prose_plan(dream, '2026-08-15', ['sec-a'])
    res = dd.digest(plan_dir=dream, db=db, now=NOW, report='/x/dream-2026-09-13.md',
                    wq_bin=str(fake_wq), queue_db=queue_db)

    assert [m['absorb'] for m in res['applied']] == ['dup1']
    assert res['ticket'] == '1801' and res['superseded'] == [1700]
    assert res['path'].name == 'digest-2026-09-13.md'
    text = res['path'].read_text()
    assert 'dup1' in text and 'near2' not in text          # applied ids, not skipped ones
    assert 'lumen_prose_lifecycle.py apply' in text

    status = json.loads((dream / dd.STATUS_JSON).read_text())
    assert status['pending_plans'] == 2 and status['applied_ids'] == ['dup1']
    assert status['ticket'] == '1801' and status['status'] == 'OUTBOX PENDING: 2'
    assert 'sec-a' not in str(status)                      # prose stayed a proposal
    assert sqlite3.connect(db).execute(
        'SELECT count(*) FROM prose_demotions').fetchone()[0] == 0


def test_a_drained_outbox_writes_the_needle_the_liveness_probe_looks_for(dream, db, fake_wq,
                                                                        queue_db):
    res = dd.digest(plan_dir=dream, db=db, now=NOW, wq_bin=str(fake_wq), queue_db=queue_db)
    assert res['ticket'] is None
    assert dd.DRAINED in (dream / dd.STATUS_JSON).read_text()


def test_the_weekly_report_section_survives_a_broken_digest():
    assert dd.report_section(None, 'OperationalError: locked') == [
        '- digest FAILED: OperationalError: locked']

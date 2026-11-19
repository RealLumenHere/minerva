"""wq#3144: lumen_federate.index() must never run mm.embed() with a write txn open.

Class: an implicit BEGIN from the first INSERT/DELETE stays open across every
following slow embed call, so every other minerva.db writer (lumen_episode commit,
memoryd, scouts) gets 'database is locked' for the whole index run. Seen live
2026-09-28 (fleet-sweep-b: >4 min at load 12) and the reason the re-index path unit
was held. This test fakes a slow embedder and hammers the same DB with a second
writer; any lock failure means the txn was held across an embed.
"""
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lumen_federate as f  # noqa: E402

N_SECTIONS = 8
EMBED_S = 0.15


def _make_source(tmp_path):
    md = tmp_path / 'mem.md'
    body = ''.join(
        f'## Section {i}\n' + (f'line {i} of prose about federated recall. ' * 12) + '\n\n'
        for i in range(N_SECTIONS))
    md.write_text('# Doc\n\n' + body)
    return md


def test_index_never_holds_write_txn_across_embed(tmp_path, monkeypatch):
    db = str(tmp_path / 'fed.db')
    seed = sqlite3.connect(db)
    seed.execute('PRAGMA journal_mode=WAL')
    seed.execute('CREATE TABLE side (t REAL)')
    seed.commit()
    seed.close()

    md = _make_source(tmp_path)
    monkeypatch.setattr(f, 'DB', db)
    monkeypatch.setattr(f, 'sources', lambda: [(md, 'prose-section', 'test doc')])

    embeds = []

    def slow_embed(_text):
        embeds.append(time.time())
        time.sleep(EMBED_S)
        return [0.1] * 8

    monkeypatch.setattr(f.mm, 'embed', slow_embed)

    stop = threading.Event()
    result = {'ok': 0, 'locked': 0}

    def other_writer():
        while not stop.is_set():
            c = sqlite3.connect(db, timeout=0.05)   # fail fast: a held txn shows up as 'locked'
            try:
                c.execute('BEGIN IMMEDIATE')
                c.execute('INSERT INTO side VALUES (?)', (time.time(),))
                c.commit()
                result['ok'] += 1
            except sqlite3.OperationalError as e:
                if 'locked' in str(e) or 'busy' in str(e):
                    result['locked'] += 1
            finally:
                c.close()
            time.sleep(0.02)

    t = threading.Thread(target=other_writer, daemon=True)
    t.start()
    try:
        stats = f.index(full=True, verbose=False)
    finally:
        stop.set()
        t.join(timeout=5)

    assert len(embeds) >= N_SECTIONS, f'embedder barely ran ({len(embeds)}); test is not exercising the loop'
    assert stats['new'] + stats['changed'] >= N_SECTIONS
    assert result['ok'] > 0, 'side writer never got in at all'
    assert result['locked'] == 0, (
        f"index() held a write txn across embed calls: side writer hit 'locked' "
        f"{result['locked']}x vs {result['ok']} ok while {len(embeds)} embeds ran")

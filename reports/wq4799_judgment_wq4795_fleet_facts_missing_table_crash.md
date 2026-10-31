# wq#4799 — judgment: verify-pass of wq#4795 (minerva PR#65) — CONTRADICT

Verifier: claude-sonnet-5@high (fresh instance). Author: claude-sonnet-5.
Escalating to opus-5@high per ticket instructions (contradiction found).

## Ticket under test

wq#4795 / PR http://100.121.17.5:3000/lumen/minerva/pulls/65 (merged as
3eafdaf / 5ffc546): adds `fleet_memory_writer.py` (expire / supersede-value
CLI for `fleet_facts`/`fleet_decisions` via the `minerva_audit` chokepoint,
one `BEGIN IMMEDIATE` transaction, refuses wrong-state rows, regenerates
`claude_memory.md`), and wires `lumen_state.py`'s lifecycle-closing path to
call `fleet_memory_writer.expire_facts_naming_subject()` in the same
transaction, so retiring a subject also expires every `fleet_facts` row that
names it.

## What was verified as correct

Tested directly against isolated scratch copies of `/home/lumen/minerva/minerva.db`
(never the live DB/live `claude_memory.md`):

- `fleet_memory_writer.py expire` / `supersede-value` CLI: correctly flips
  `fleet_facts.status`/`fleet_decisions.status`, writes one `memory_audit`
  row in the same transaction, refuses a row not in the expected
  `active_status` (tested: re-expiring an already-expired row, expiring a
  nonexistent id — both refused with the documented message, exit 1).
- `supersede-value` on `fleet_decisions` id=4: rewrites `decision` text in
  place, keeps `status='active'` (correct per docstring — this is "fix the
  prose in place", not the `supersedes`/`superseded_by` chain mechanism),
  audit row records `old_value=...`.
- End-to-end regen: ran the real `memory/context_builder.py --out` (not a
  stub) against an isolated DB/output path; the expired fact (id=2,
  `architecture:litellm-gateway`) correctly disappeared from the
  regenerated `claude_memory.md`-equivalent scratch file.
- `expire_facts_naming_subject(conn, 'horus', ...)` run directly against a
  copy of the live DB: expired exactly the 9 rows that mention "horus" as a
  whole word in `subject` or `value` (ids 5, 197, 198, 241-245, 293),
  matching the stated word-boundary regex design (`(?<!\w)...(?!\w)`,
  case-insensitive) and the wq#4789 incident precedent it's modeled on. No
  false positive/negative found in this matching logic itself.

## The break: unconditional `fleet_facts` query crashes any DB without that table

`fleet_memory_writer.expire_facts_naming_subject()` (fleet_memory_writer.py:136-138)
runs `SELECT id, subject, value FROM fleet_facts WHERE status='verified'`
with **no existence check** on the table. `lumen_state.py`'s lifecycle-closing
branch (`_ingest()`, lumen_state.py:826-833) now calls this **unconditionally**
inside the `predicate == reg['lifecycle'] and value in reg['closing']` branch,
mid-transaction, with no try/except around it.

`fleet_facts`/`fleet_decisions` are NOT part of `lumen_state.init_schema()` —
they were populated once by a separate Postgres-copy migration (wq#4554,
per fleet_memory_writer.py's own docstring: "Since the wq#4554 copy..."). Any
DB built via `lumen_state.init_schema()` alone — every throwaway test
fixture, and any fresh/rebuilt minerva.db before that one-time copy step
runs — has `state_facts` and friends, but no `fleet_facts` table at all.

Contrast with `minerva_audit.audit()`'s own `ensure_table(conn)`
(minerva_audit.py:78-84), which checks `sqlite_master` and creates
`memory_audit` if missing before every write — the exact defensive pattern
this PR's own chokepoint uses elsewhere, not applied here.

### Reproduction (falsifiable, re-run to confirm)

Pre-PR baseline (parent commit 80ac53e, materialized read-only into
`/tmp/fworker-wq4799/baseline`, no `.git`, not the live repo):

```
cd /tmp/fworker-wq4799/baseline
python3 -m pytest tests/test_lumen_state_single_owner_migrate.py \
  tests/test_lumen_state_report_observation.py \
  tests/test_lumen_state_staleness.py \
  tests/test_lumen_state_single_owner.py -q
# 55 passed in 10.46s
```

Post-PR (PR#65 / commit 3eafdaf, `/home/lumen/worktrees/wq4799-a1`, same
four test files, same command):

```
# 12 failed, 43 passed in 10.80s
```

All 12 failures are in tests that exercise the lifecycle-closing path
(decommission/retire/backfill: `test_decommission_closes_operational_...`,
`test_closure_never_ends_a_window_before_it_began`,
`test_operational_write_after_decommission_is_refused`,
`test_lifecycle_backfill_closes_operational_and_skips_what_was_not_reviewed`,
etc. across `test_lumen_state_single_owner.py` and
`test_lumen_state_single_owner_migrate.py`), and every one fails with the
same traceback root:

```
sqlite3.OperationalError: no such table: fleet_facts
  at fleet_memory_writer.py:137, in expire_facts_naming_subject
  called from lumen_state.py:831 (_ingest)
```

These test fixtures (`tests/test_lumen_state_single_owner.py:20-24`) build a
throwaway DB via `ls.init_schema()` on a `tmp_path` file — exactly the
"DB without `fleet_facts`" case above.

### Why this is a real break, not a test-only artifact

Any lifecycle-closing `ingest()` call — `lumen_state.py ingest <subject>
lifecycle retired/decommissioned` — against a minerva.db that lacks
`fleet_facts` now raises `sqlite3.OperationalError` **uncaught**, out of
`_ingest()`. Per `ingest()`'s own wrapper (lumen_state.py:590-600), an
exception on a connection it opened itself triggers `conn.rollback()` — so
the entire lifecycle-closing write (the `state_facts` lifecycle row, the
sibling operational closures, all of it) is lost, not just the new
`fleet_facts` cascade. This is strictly worse than the pre-PR behavior: a
retire/decommission ingest that used to succeed now fails outright on any
DB where the one-time `fleet_facts` copy hasn't been (re-)run — which is
every fresh schema, including the 12 tests above.

This directly contradicts the ticket's own acceptance bar ("Done means
demonstrated: tests run and pass") — the existing test suite catches this in
one `pytest` run; it appears the author did not run it after wiring the
`lumen_state.py` hook.

## Fix shape (not applied — verify-pass is not a re-implementation gate)

Guard the cascade the same way `minerva_audit.ensure_table()` guards its own
table: check `sqlite_master` for `fleet_facts` (or catch
`sqlite3.OperationalError` on "no such table") in
`expire_facts_naming_subject()` and no-op (return `[]`) when the table isn't
there yet, same as the module already tolerates a missing `context_builder`
regen as non-fatal.

## Verdict

**CONTRADICT.** Escalating to opus-5@high per ticket instructions.

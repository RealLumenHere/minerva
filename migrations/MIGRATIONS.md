# minerva.db migrations

Versioned, recorded, idempotent schema changes for `minerva.db`. Ticket #73.

## Why

Historically each module ran its own `CREATE TABLE IF NOT EXISTS` at import
(`init_db.py`, `lumen_state.py`, `minerva_memory.py`, `lumen_scout_expanded.py`).
That bootstraps a fresh db fine, but there was **no path for changing an existing
table** — adding a column, backfilling, adding an index — without hand-editing
prod. Migrations fill that gap.

## Model

- Files live in `migrations/` as `NNNN_slug.sql` — 4-digit zero-padded id,
  ascending, lowercase slug. Example: `0001_add_proposals_score_index.sql`.
- `0000_baseline.sql` is the anchor for the pre-migration schema; it does nothing
  structural (the per-module bootstrap already created those tables).
- The runner (`lumen_migrate.py`) applies each pending file **once**, inside a
  transaction, and records `id / slug / sha256 / applied_at` in the
  `schema_migrations` table.
- **Applied migrations are immutable.** Editing an already-applied file is
  detected by sha256 and refused — make a new `NNNN` file instead.
- The old per-module `CREATE TABLE IF NOT EXISTS` calls **stay** — they bootstrap
  a fresh db. Migrations own everything from `0001` onward.

## Commands

```
lumen_migrate.py status         # applied vs pending, with drift flags
lumen_migrate.py up             # apply all pending (backs the db up first)
lumen_migrate.py up --dry-run   # show what would run, change nothing
lumen_migrate.py new "add foo"  # scaffold the next NNNN_add_foo.sql
```

`up` copies `minerva.db` to `minerva.db.bak.<ts>.pre_migrate` before applying,
and rolls back the transaction on any failure (db left untouched, restore point
printed).

## Writing a migration

1. `lumen_migrate.py new "short description"` → creates the next-numbered stub.
2. Put plain SQL in it. Prefer additive + idempotent (`CREATE TABLE IF NOT
   EXISTS`, `CREATE INDEX IF NOT EXISTS`). SQLite has limited `ALTER TABLE`;
   for column drops/renames use the 12-step table-rebuild pattern within the
   single transaction.
3. `lumen_migrate.py up --dry-run`, then `up`.
4. Commit the `.sql` file. Never edit it again once applied anywhere.

## Fresh-db bootstrap

On a brand-new db, run the existing module bootstrap first (it creates the
tables), then `lumen_migrate.py up` — `0000_baseline` records the anchor and any
later migrations apply in order. Nothing double-creates because migrations from
`0001` on are the only ones doing structural change, and additive DDL is guarded
with `IF NOT EXISTS`.

## Wiring into automation (optional, not auto-enabled)

`lumen_migrate.py up` is safe to call at the top of `init_db.py` or a deploy hook
so schema changes ship with code. Left as an explicit step for now so a bad
migration can't wedge an import path — call it deliberately after pulling.

## Exceptions outside the runner

- `state_facts.closed_by` and `state_predicate_additions` (wq#1700) are created by
  `lumen_state._migrate()`, the same self-heal path as `last_confirmed_at` and
  `fact_class`, because every ingest must be able to rely on them before any
  runner call. The one-time data change is `lumen_state_single_owner_migrate.py`
  (dry-run by default, writes its own backup). No `NNNN` id was used, so the next
  runner id is still `0012`.

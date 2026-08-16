# Memory Service Interface + Access-Control Design (V4 P3 / SHARED)

_wq#570 [mv4-P0b] · session `mv4-w570-19cc6b` · 2026-08-14 PDT · READ-ONLY on production stores._
_Feeds #575 (lumen-memoryd). Design only — no code shipped, no schema applied, no service created._
_Primary inputs: SOTA_2026-08.md RQ3 + RQ5 · MEMORY_V4_PLAN.md §2-3 P3. Everything in §0 was
verified against live state THIS session (fq F1/F2); nothing below rests on a remembered fact._

---

## §0. AUDIT EVIDENCE — live state, this session

Every design decision downstream cites a line in this section. Where the ticket's premise and the
live store disagreed, the live store wins and the disagreement is called out.

### 0.1 Real schema (`sqlite3 -readonly minerva.db .schema <table>`)

```
archival_notes(id TEXT PK, kind TEXT, title TEXT, content TEXT, embedding BLOB,
               tags TEXT, source_ref TEXT, created_at TEXT, origin TEXT, trust TEXT,
               CHECK (embedding IS NULL OR (typeof(embedding)='blob'
                      AND length(embedding)>0 AND length(embedding)%4=0)))
  idx_archival_kind(kind, created_at) · idx_archival_origin(origin)

state_facts(id TEXT PK, subject, predicate, value, valid_from, valid_to, ingested_at,
            invalidated_at, source_ref, confidence REAL DEFAULT 1.0, episode_id,
            half_life_days REAL, last_confirmed_at TEXT, fact_class TEXT, render_tier TEXT)
  idx_sf_sp · idx_sf_open · idx_sf_valid · idx_state_facts_subject
  UNIQUE idx_sf_one_open(subject, predicate) WHERE valid_to IS NULL   <-- the bi-temporal invariant

memory_edges(src_type, src_id, rel, dst_type, dst_id, created_at, PK(all five))
note_merges(absorbed_id PK, keep_id, sim, method, ts)
verification_log(id, subject, input_text, context_ref, citation_score, judge_score,
                 final_score, passed, rejected_reason, model, ts, judge_rationale,
                 error_kind, mode, n_claims, claims_entailed, span_confidence)
changelog(id, proposal_id, change_type, target_path, diff, applied_by, applied_at,
          reverted, revert_reason)
schema_migrations(id PK, slug, sha256, applied_at)
```

Full table list (19): archival_notes, changelog, classified_intel, costbench_results,
costbench_runs, judgearena_results, memory_edges, model_error_budget, note_merges, plans,
proposals, recall_fts (+_config/_content/_data/_docsize/_idx), schema_migrations, skills,
skillhex_results, state_facts, verification_log.

### 0.2 Corpus size (the denominator for every claim below)

| Table | Rows |
|---|---|
| archival_notes | 1,161 |
| state_facts | 872 (650 current per plan §0; 872 = all versions incl. closed windows) |
| recall_fts | 1,132 |
| verification_log | 2,891 |
| proposals | 1,057 |
| classified_intel | 14,803 |

### 0.3 The provenance fields that already exist — actual values, not assumed

`origin` (archival_notes, n=1,161 — **no NULLs**):

| origin | count |
|---|---|
| claude-session | 1,078 |
| state-mirror | 71 |
| convo-learn | 6 |
| backfill | 6 |

`trust` (n=1,161 — **no NULLs**):

| trust | count |
|---|---|
| operator | 1,078 |
| derived | 77 |
| external | 6 |

`kind`: **58 distinct values over 1,161 rows.** Top: code-ship 272, session 258, infra 228,
fact-narrative 72, research 40, deployment 24, tooling 22. The tail is the finding — ~30 values
appear once or twice, and it contains near-duplicate pairs written by different sessions:
`diag`/`diagnosis`, `maint`/`maintenance`, `deploy`/`deployment`, `code`/`code-ship`,
`fix`/`bugfix`, `verify`/`audit`. **`kind` is an uncontrolled free-text field today.** That is
the single most concrete local argument for a validation kernel: no attacker was needed to
fragment it, just three sessions with different habits.

`state_facts` classification columns: fact_class = config 504 / identity 187 / narrative 130 /
status 51. render_tier = topic 520 / core 352. half_life_days = **NULL on 850 of 872 rows**
(7.0 on 11, 14.0 on 9, 10.0 on 2) — the decay machinery is wired but almost unpopulated.
(Detail is #571's job; recorded here only because §2 extends the same provenance surface.)

### 0.4 Who can write today (sweep + positive control)

Sweep: `grep -rln "INSERT INTO archival_notes|INSERT OR REPLACE INTO archival_notes|UPDATE
archival_notes" /home/lumen --include=*.py`, excluding `.bak` and `/backups/`.

Production writers to `archival_notes` — **3 files**:
- `/home/lumen/minerva/minerva_memory.py` (`write_note()`, the sanctioned path)
- `/home/lumen/minerva/memory_embed_guard.py` (embedding repair)
- `/home/lumen/minerva/lumen_dream.py` (`embed_retry()`, fail-closed re-embed)

Production writers to `state_facts` — **1 file**: `/home/lumen/minerva/lumen_state.py`.

**Positive control (AUDIT DISCIPLINE — a clean sweep is not trusted until it goes red):** planted
`/home/lumen/scratch/w570_canary/canary_writer.py` containing `INSERT INTO archival_notes`, re-ran
the identical sweep. The canary was found. Canary deleted after the run (`rm -rf`, confirmed).

**The control also caught a real miss in my own first pass.** My first sweep piped through
`grep -v scratch` and reported 3 files. The control run without that filter returned a 4th:
`/home/lumen/scratch/wq232/sandbox/minerva_memory.py` — a sandbox copy, not on any execution path,
but it is a live file containing a write statement against the production table name. It is
harmless only because nothing invokes it. Recorded because "3 writers" would have been a
slightly false statement of the attack surface, and #575 must enumerate writers by *capability*
(anything that can open the DB file), not by intention.

Corpus scanned: **14,408** `.py` files under `/home/lumen` excluding site-packages, node_modules,
venvs and backups (101,703 including them).

### 0.5 GAP-1 — trust and origin are declared by the caller, not derived by the store

`minerva_memory.py:140-144`:

```python
def write_note(kind, title, content, tags=None, source_ref=None, origin=None, trust=None):
    origin = origin or os.environ.get('LUMEN_MEM_ORIGIN', 'claude-session')
    trust  = trust  or os.environ.get('LUMEN_MEM_TRUST',  'operator')
```

Any process that can import the module — every agent lane, every worker, every script — sets its
own provenance, or inherits the default `claude-session` / **`operator`**, the highest trust level
in the store. `lumen_episode.py:39` calls `mm.write_note(...)` with no origin/trust argument, so
**every episode any lane commits is stamped `trust=operator`**. The 1,078/1,161 `operator` count
in §0.3 is therefore a floor on how much of the store is trusted-by-default rather than
trusted-by-evidence. This is the specific hole Collaborative Memory (2505.18279) closes with
server-side attribution, and it is the reason #575 exists as a *service* rather than a library.

### 0.6 GAP-2 — the write path is destructive and resets provenance

`minerva_memory.py:146` derives the primary key from content:

```python
nid = hashlib.md5(f'{kind}:{title}:{content[:200]}'.encode()).hexdigest()[:12]
```

and `:180-185` commits with **`INSERT OR REPLACE INTO archival_notes`**.

Consequences, all verifiable from the schema alone:
1. A second write with the same `kind` + `title` + first 200 chars **replaces** the row. SQLite's
   `INSERT OR REPLACE` deletes then re-inserts — `created_at`, `origin`, `trust` and `source_ref`
   of the original are gone, not merged.
2. There is no version lineage. Nothing in the schema records that a row was ever replaced.
   `note_merges` covers deliberate dedup merges only (`method` ∈ exact-auto | manual), not silent
   overwrites.
3. A lower-trust lane can overwrite a higher-trust note by reproducing three easily-observable
   fields — and the resulting row carries the *new* writer's provenance. That is a write-side
   poisoning primitive that needs no retrieval-stage attack at all (cf. MINJA 2503.03704, which
   needed queries only; here a plain import suffices).
4. Because the id is content-derived, an *edit* to a note's body creates a new row rather than a
   new version of the old one, so the store accumulates near-duplicates instead of history —
   which is exactly the pressure `lumen_dream.episodic_dupes_v2` exists to mop up.

### 0.7 GAP-3 — the "anomaly scan" is a counter, verified at the cited line

The plan cites `lumen_dream.py:242` as a live anomaly scan (V3 P5). Line 242 is the section
comment; the implementation is lines 243-248 and is, in full:

```python
# ---------- Memory V3 P5: write-origin anomaly scan ----------
def origin_stats(conn, days=7):
    return conn.execute(
        "SELECT COALESCE(origin,'?'), COALESCE(trust,'?'), COUNT(*) FROM archival_notes "
        "WHERE created_at > datetime('now', ?) GROUP BY 1,2 ORDER BY 3 DESC",
        (f'-{days} days',)).fetchall()
```

Called once from `run()` as `ostats = origin_stats(conn)` and rendered into the weekly report.
**It is a 7-day GROUP BY.** There is no baseline, no threshold, no comparison against prior
weeks, no alert, no quarantine, and no code path that acts on the result. `grep -n
"anomaly\|quarantine" lumen_dream.py` returns exactly one line — the comment above.

This is not a criticism of the V3 work; the report is genuinely useful to a human reading it. But
the design in §3 must supply the detector, not assume one exists. The ticket text "quarantine fed
by dream anomaly scan" describes a pipe whose upstream end is currently a printf.

### 0.8 GAP-4 — there is no write-audit table

Neither existing candidate is one:
- `verification_log` (2,891 rows) records *claim verification* — judge/citation scores for
  generated text. It has no row-id of the memory it protected and no write events.
- `changelog` records *applied proposals* — config/code/schedule/sources diffs, with a `reverted`
  flag. Its `applied_by` defaults to `'minerva'`. It has never carried memory writes.

So today: no record of who wrote what, when, from which lane, or what it displaced. A revocation
request (§4) currently has nothing to walk.

### 0.9 GAP-5 — the production agent hook does not use Memory V3 at all

This is the largest finding of the audit and it changes what §7 has to be.

`/home/lumen/agent/lumen_agent_v4.py:310-329` (`_memory_context`, the pre-call hook):

```python
if "/home/lumen/mem0" not in _sys.path:
    _sys.path.insert(0, "/home/lumen/mem0")
import lumen_memory_router as _r
route = _r.route(query)
ctx = _r.fetch_context(query, route["tier"], k=4) or ""
```

`/home/lumen/mem0/lumen_memory_router.py` (mtime **2026-06-14**) routes to three tiers:
- `facts` → `lumen_mem0.search_facts()` → **Mem0 over Qdrant** (container `mem0-qdrant`, verified
  `Up 6 days`, `http://127.0.0.1:6333/collections` → 200). A *separate substrate from minerva.db.*
- `state` → reads the flat file `/home/lumen/minerva/lumen_state.txt` (13,076 B, mtime today 18:45).
- `episodes` → `claude_memory.md` **tail, last 120 lines** (comment in-file: "full RAG over the
  6.6k-line file is a later optimization").

`/home/lumen/autonomy/agent5/a5_common.py:96-110` is the same wiring for the planner/generator/
evaluator split.

Therefore: `search_v3` — the fused dense+lex+graph+recency retriever measured at R@1 .725 / MRR
.795 — has **zero call sites in the agent execution path.** The V3 quality the plan reports is
available to interactive sessions and CLI probes, not to the lanes.

Related: `lumen_agent_v4.py:591` comments that startup "warm[s] the Memory V3 retrieval stack";
the code it guards is `_memory_context("warmup")`, which warms Mem0/Qdrant and a file read. The
comment is wrong. Flagging it because a future session grepping for "Memory V3" in the agent will
otherwise conclude the migration already happened.

**Consequence for #575:** the migration in §7 is not "swap an import." It is "move a lane off a
second, undocumented memory substrate." That has a Frank decision attached (§9, Q1) and must not
be guessed.

### 0.10 Retrieval contract that memoryd must preserve

`lumen_recall2.search_v3(query, top_k=10)` (204 lines, `/home/lumen/minerva/lumen_recall2.py`):
- candidate pool from graph anchors (subject 1.0 / ticket 1.2 / tag 0.9), dense entry ≥0.55 plus
  forced top-30, then expansion over shared subject/ticket anchors;
- fusion `0.50*dense + 0.28*lex + 0.16*graph + 0.06*recency`;
- excludes `note_merges.absorbed_id` on both the candidate scan and the fetch;
- **fails open to `minerva_memory.search()` (v2) when the candidate pool is empty**, and records
  that in `LAST_STATS['failopen_v2']` (wq#465);
- returns dicts carrying `trust` already — so a trust-aware read filter has a field to work with.

Embedding is an out-of-process HTTP call: `minerva_memory.embed()` → `POST
http://127.0.0.1:11434/api/embed`, model `nomic-embed-text`, `keep_alive='30m'`, 90s timeout,
2 attempts. **Note the tension for the shed directive:** the daemon holding no model is
satisfiable, but the existing embed call already pins nomic resident in Ollama for 30 minutes
after every write and every recall. If the 35W build intends "no resident embedder on Lumen",
that intent is currently defeated by `keep_alive`, not by any daemon. Raised in §9 Q3; not
changed here.

---

## §1. PatchBoard-style deterministic validation kernel over SQLite

PatchBoard (2605.29313) splits the problem three ways: an **Architect** declares the schema, the
**write contracts** per role, and the **runtime invariants**; a **deterministic kernel** validates
every proposed mutation before a transactional commit. Reported 84.6% task success vs 30.8%
LangGraph at 45.5k tok/success vs 368.3k (630 ALFWorld episodes, Qwen-plus, temp 0). We take the
structure, not the JSON-Patch wire format: our mutations are row-level and few in kind, so a patch
language buys nothing and costs a parser.

**The kernel is pure Python + SQL. No LLM anywhere on the write path.** Non-negotiable: an LLM
validator is itself injectable, and the whole point of RQ5's evidence is that the data layer is
where this gets fixed.

### 1.1 Schema declaration (frozen, versioned)

`/home/lumen/minerva/v4/schema/memory_contract_v1.yaml` — declarative, read at daemon start,
sha256 recorded in `schema_migrations`. Shape:

```yaml
version: 1
tables:
  archival_notes:
    writable_fields: [kind, title, content, tags, source_ref]
    server_owned:    [id, created_at, origin, trust, agent_id, content_sha256, schema_version]
    enums:
      kind: [session, code-ship, infra, research, incident, lesson, fix, deployment,
             tooling, planning, analysis, audit, decision, config, ops, pathology,
             prose-section, session-archive]     # closed set; see §1.5 migration
    limits: {title: 300, content: 8000, tags: 500, source_ref: 300}
  state_facts:
    writable_fields: [subject, predicate, value, valid_from, source_ref, confidence,
                      half_life_days, fact_class, render_tier]
    server_owned:    [id, ingested_at, valid_to, invalidated_at, episode_id, last_confirmed_at]
```

`server_owned` is the load-bearing word. A field in that list is **ignored if the client sends it**
— not rejected with an error the client can learn to avoid, ignored and overwritten. This is the
direct fix for GAP-1 (§0.5).

### 1.2 Per-lane write contracts

Lanes are the ones already in `wq`: `memory`, `pokemon`, `fleet`, `fable`, `research`, `auto`,
`github`, `user`, plus the daemon's own `dream` and `state-mirror` identities.

| Lane | archival_notes | state_facts | trust assigned | Notes |
|---|---|---|---|---|
| operator (Frank / interactive Claude session) | write | write | `operator` | Only identity that may write `trust=operator`. Authenticated by socket peer uid + token, §6.3. |
| `memory` | write | **propose only** | `derived` | May write research/audit notes; fact changes go to the proposal queue. |
| `fleet`, `pokemon`, `fable`, `research`, `auto`, `github` | write (own kinds) | **propose only** | `derived` | `source_ref` must resolve to a wq ticket id; enforced by regex, §1.3 I-7. |
| `dream` (consolidation) | **propose only** | **propose only** | n/a | Already proposal-only by V3 policy; the kernel makes it structural rather than conventional. |
| `state-mirror` | write (kind=fact-narrative) | write | `derived` | The existing 71-note mirror path. |
| `convo-learn` | write | none | `external` | Lowest trust; subject to write-path screening §3.4. |
| unknown / unauthenticated | **reject** | **reject** | n/a | Fail closed. |

"Propose only" means the write is accepted, validated, stored in the **proposal queue** — never in
the live table — and surfaced for operator apply. Nothing in this table auto-promotes (§5).

### 1.3 Runtime invariants (checked in-transaction, every write)

Deterministic, cheap, and each one traceable to something that has actually gone wrong here:

- **I-1 embedding sanity** — `embedding IS NULL OR (blob AND len>0 AND len%4==0)`. Already a
  table CHECK; the kernel additionally rejects a dimension mismatch against `EMBED_DIM=768`
  before insert. (wq#465: one zero-length blob silently dropped the whole fleet to v2 recall.)
- **I-2 single open fact window** — `state_facts` may not have two rows with the same
  `(subject, predicate)` and `valid_to IS NULL`. The unique index enforces it; the kernel checks
  first so the failure is a clean 409 instead of an IntegrityError traceback.
- **I-3 no destructive replace** — `INSERT OR REPLACE` is banned on `archival_notes`. A write
  whose `content_sha256` matches an existing live row is a no-op returning the existing id; a
  write that collides on the legacy content-derived id but differs in content becomes a **new
  version** (§2.3), never an overwrite. Directly closes GAP-2.
- **I-4 closed vocabularies** — `kind` ∈ enum, `trust` ∈ {operator, derived, external},
  `origin` matches `^(operator|state-mirror|convo-learn|backfill|agent-lane:[a-z0-9-]+(:wq#\d+)?)$`.
- **I-5 size limits** — per §1.1; over-limit is rejected, not silently truncated. (Today
  `write_note` truncates `content[:8000]` without telling the caller.)
- **I-6 monotonic time** — `valid_from <= now + 60s`; no future-dating facts. `created_at` is
  server-set, always UTC ISO-8601.
- **I-7 source_ref resolvable** — for agent lanes, `source_ref` must match `wq#\d+` or
  `session:<slug>` and, for `wq#N`, the ticket must exist in `queue.db`. An unattributable write
  from an autonomous lane is rejected.
- **I-8 no writes to superseded or revoked rows** — a revoked id is terminal (§4).
- **I-9 rate + volume** — §6.4.

Invariants are evaluated **before** any mutation and the whole set must pass. Result is one of
`accept` / `reject(reason)` / `quarantine(reason)` — never a partial write.

### 1.4 Transactional commit

```
BEGIN IMMEDIATE
  1. resolve identity        -> lane, agent_id, trust  (server-derived, §6.3)
  2. validate                -> schema + contract + invariants I-1..I-9
  3. on reject               -> write memory_audit(action='reject', reason) ; ROLLBACK-equivalent
  4. on quarantine           -> INSERT memory_quarantine ; audit ; COMMIT (live tables untouched)
  5. on accept               -> INSERT live row (+ supersede predecessor if versioning)
                             -> INSERT memory_audit(action='write', before_sha, after_sha)
COMMIT
```

`BEGIN IMMEDIATE` because that is what `wq` already learned the hard way for atomic claims — it
takes the write lock up front instead of discovering a conflict at COMMIT. Audit rows are written
**inside** the same transaction, so an accepted write with no audit row is impossible by
construction rather than by discipline.

WAL is already in use on `queue.db`; `minerva.db`'s journal mode must be confirmed and, if not
WAL, that is a separate gated change — **not** something #575 flips as a side effect.

### 1.5 Migrating the existing 58 `kind` values

The enum in §1.1 cannot be applied to history. Plan: (a) kernel enforces the closed set for **new**
writes only; (b) a mapping table folds the singleton tail onto canonical values
(`diag`→`diagnosis` etc.) as **dream proposals**, operator-applied; (c) existing rows keep their
`kind` until such a proposal is accepted. No backfill rewrite, no auto-normalisation. Retrieval is
unaffected either way — `kind` is not a fusion signal in `search_v3`.

---

## §2. Immutable provenance + scoped read/write policies

Collaborative Memory (2505.18279) contributes two things we lack: provenance attributes that are
**immutable once written** (agents, resources, timestamps) so permission checks can be applied
*retrospectively*, and read/write policies expressed as a scoped user–agent–resource relation that
may evolve over time without rewriting history.

We already have the two columns (`origin`, `trust`, §0.3). What is missing is that they are
client-declared (GAP-1), overwritable (GAP-2), and carry no agent identity, no version lineage,
and no integrity anchor.

### 2.1 Additive columns (no rewrites, no drops — V3 discipline)

All `ALTER TABLE ... ADD COLUMN`, all nullable, all backward-compatible with every current reader
including `search_v3` (which selects named columns, never `*`):

```sql
ALTER TABLE archival_notes ADD COLUMN agent_id        TEXT;   -- 'mv4-w570-19cc6b'
ALTER TABLE archival_notes ADD COLUMN lane            TEXT;   -- 'memory' | 'pokemon' | ...
ALTER TABLE archival_notes ADD COLUMN content_sha256  TEXT;   -- integrity anchor
ALTER TABLE archival_notes ADD COLUMN schema_version  INTEGER DEFAULT 1;
ALTER TABLE archival_notes ADD COLUMN supersedes      TEXT;   -- prior note id
ALTER TABLE archival_notes ADD COLUMN superseded_by   TEXT;   -- successor note id
ALTER TABLE archival_notes ADD COLUMN revoked_at      TEXT;   -- tombstone, §4
ALTER TABLE archival_notes ADD COLUMN revoked_reason  TEXT;
CREATE INDEX idx_archival_live ON archival_notes(revoked_at, superseded_by);
```

Existing 1,161 rows get NULLs and are treated as `schema_version=0, lane='legacy'` — honest about
the fact that their provenance was never enforced. **We do not backfill a trust level we cannot
evidence.** Retrospective permission checks (the Collaborative-Memory property) therefore start
from the cutover date and say so, rather than pretending the history was governed.

### 2.2 Immutability, enforced not promised

SQLite triggers, because a policy that lives only in `memoryd` is defeated by anyone who opens the
DB file directly — and §0.4 showed a stray sandbox copy of the writer already exists on disk:

```sql
CREATE TRIGGER trg_archival_provenance_immutable
BEFORE UPDATE OF origin, trust, agent_id, lane, created_at, content_sha256, supersedes
ON archival_notes
BEGIN
  SELECT RAISE(ABORT, 'provenance is immutable; supersede instead');
END;
```

Mutable after write: `superseded_by`, `revoked_at`, `revoked_reason`, `embedding` (repair path
must keep working — `memory_embed_guard.py` and `lumen_dream.embed_retry()`, §0.4). Everything
else is write-once.

### 2.3 Version lineage replaces overwrite

New id scheme for kernel-written notes: **random 12-hex** (`secrets.token_hex(6)`), not
`md5(kind:title:content[:200])`. Content-addressing was the mechanism of GAP-2 — it made
"same-ish note" mean "same row". Instead:

- `content_sha256` carries the dedup signal (exact-duplicate detection stays cheap, I-3);
- an edit writes a **new row** with `supersedes = <old_id>`, and the kernel sets
  `superseded_by = <new_id>` on the old row in the same transaction;
- readers filter `WHERE superseded_by IS NULL AND revoked_at IS NULL` for the live view; history
  is one join away and never disappears.

Legacy content-derived ids keep working unchanged — the scheme applies to new writes only.

### 2.4 Trust is derived, never declared

Server-side lattice, computed from the authenticated identity (§6.3) and never read from the
request body:

```
operator            : socket peer uid == lumen AND operator token          -> trust=operator
agent lane, wq-attributed, all invariants pass                             -> trust=derived
agent lane, content ingested from outside the fleet (convo-learn, scouts)  -> trust=external
anything else                                                              -> reject
```

`origin` is likewise server-composed as `agent-lane:<lane>:wq#<id>` from the claim the daemon
verified in `queue.db` — the lane cannot spell its own name. This retires the two environment
variables at `minerva_memory.py:142-143` as an *authorization* mechanism; they may survive as a
hint for the operator path only, and the kernel must ignore them for lane traffic.

### 2.5 Scoped read policies

Reads are less dangerous than writes but not harmless: the Always-On survey's failure mode
(2606.30306) is stale/corrupt state crossing *between* agents, which is a read event.

| Reader | Default read scope |
|---|---|
| operator | everything, including quarantined and revoked (with a banner) |
| agent lane | live rows only: `revoked_at IS NULL AND superseded_by IS NULL`, `trust ∈ {operator, derived}` |
| agent lane, explicit `include_external=true` | adds `trust=external`, results tagged `[ext]` |
| any | **never** quarantined rows |

`search_v3` already returns `trust` per hit (§0.10) and `lumen_episode.py:165` already prefixes
`[ext]` on external hits — so the display convention exists; the daemon makes it a filter instead
of a decoration.

**Every read is logged** (§3.1). That is not surveillance for its own sake: without a read log,
§4's propagation control has nothing to walk, and #576's usage salience has no input. One table
serves audit, revocation, and EARNED.

---

## §3. Append-only audit log + quarantine

### 3.1 `memory_audit` — the table that does not exist today (GAP-4)

```sql
CREATE TABLE memory_audit (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,          -- UTC ISO-8601, server clock
  action       TEXT NOT NULL,          -- write | supersede | revoke | read | reject | quarantine
  table_name   TEXT NOT NULL,
  row_id       TEXT,
  lane         TEXT,
  agent_id     TEXT,
  trust        TEXT,
  source_ref   TEXT,                   -- wq#N / session:slug
  before_sha   TEXT,                   -- NULL on create
  after_sha    TEXT,                   -- NULL on read/revoke
  detail       TEXT,                   -- reason on reject/quarantine; query on read
  invariant    TEXT                    -- which I-n fired, when applicable
);
CREATE INDEX idx_audit_row  ON memory_audit(row_id, ts);
CREATE INDEX idx_audit_lane ON memory_audit(lane, ts);
CREATE INDEX idx_audit_act  ON memory_audit(action, ts);

CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON memory_audit
BEGIN SELECT RAISE(ABORT, 'memory_audit is append-only'); END;
CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON memory_audit
BEGIN SELECT RAISE(ABORT, 'memory_audit is append-only'); END;
```

Append-only is enforced by trigger, not by convention — same reasoning as §2.2.

**Read-volume estimate, so this is costed rather than hand-waved.** Reads are the high-frequency
action: `_memory_context` fires once per agent research prompt. At the current shape of the fleet
that is order 10²–10³ rows/day, each ~200 B → single-digit MB/year. A `read` row per *hit*
(top-k=4..10) rather than per *query* multiplies that by k; recommendation for #575 is **one audit
row per query plus the hit id list in `detail`** — cheaper, and still sufficient for both
revocation walking and #576's hit counting. Retention: never delete; if size ever matters, roll to
`memory_audit_archive` by year, which is a superseding operation, not a delete.

Growth guard: a `PRAGMA page_count` check in the weekly dream run, reported alongside the existing
growth metrics, so the table's cost is visible before it is a problem.

### 3.2 `memory_quarantine`

```sql
CREATE TABLE memory_quarantine (
  id           TEXT PRIMARY KEY,       -- would-be row id
  ts           TEXT NOT NULL,
  table_name   TEXT NOT NULL,
  payload      TEXT NOT NULL,          -- full rejected write, JSON
  lane         TEXT, agent_id TEXT, trust TEXT, source_ref TEXT,
  reason       TEXT NOT NULL,          -- invariant / detector that fired
  status       TEXT NOT NULL DEFAULT 'held',   -- held | released | discarded
  reviewed_by  TEXT, reviewed_at TEXT, review_note TEXT
);
```

Quarantined content is **never retrievable** by any lane (§2.5) and never embedded. Release is an
operator action that re-runs the full kernel — a released payload is not trusted because a human
looked at it once; it must still pass I-1..I-9.

### 3.3 The detector that §0.7 showed is missing

`origin_stats()` gives distribution; a detector needs a baseline and a threshold. Minimal,
deterministic, no LLM, computed nightly by dream and consumed by the daemon:

- **D-1 origin-mix shift** — per-lane 7-day write counts vs the trailing 8-week median. Flag at
  >3σ or >5× median, whichever is looser. With n=1,161 over ~2 years this is a coarse instrument
  at first; it gets sharper as the lane dimension populates, and it is honest to say so.
- **D-2 trust escalation** — any write claiming a trust level above what §2.4 derives for that
  lane. Should be structurally impossible post-cutover; if it fires, the kernel was bypassed
  (someone opened the DB directly — see the sandbox writer in §0.4). **Quarantine + ntfy, loud.**
- **D-3 novel-writer** — first write ever seen from a `(lane, agent_id)` pair. Not suspicious by
  itself (every new session is novel); logged, and counted into D-1.
- **D-4 near-duplicate flood** — >N writes in a window with cosine ≥0.95 to an existing note.
  Reuses `lumen_dream.episodic_dupes_v2`, which already computes exactly this.
- **D-5 unresolvable attribution** — I-7 failures, which under the kernel are rejects; the
  *count* of them is the interesting signal.

Actions: D-2 → quarantine + alert. D-1/D-4 → dream proposal for operator review. D-3/D-5 →
report line only. Nothing auto-deletes; nothing auto-promotes.

### 3.4 Write-path screening for `external` trust

Per RQ5's verdict, screening applies to derived/external paths only — operator writes are not
screened, because a screen the operator has to fight is a screen that gets disabled:

1. size/shape limits (I-5);
2. deterministic pattern screen for prompt-injection markers in stored *content*
   ("ignore previous instructions", tool-call syntax, bare URLs in fact values). Redact-and-flag,
   never silent drop;
3. `content_sha256` recorded so tampering after the fact is detectable (OWASP ASI06 tooling
   pattern);
4. no LLM in this path — same reason as §1.

Honest limitation: a pattern screen catches careless injection, not careful injection. It is a
speed bump. The actual defense is §2.4 (external content cannot acquire operator trust) and §5
(nothing gets promoted without Frank).

---

## §4. Explicit revocation + propagation control

The Always-On Agents survey (2606.30306) names this the under-addressed gap: corrupt or stale
state crosses agents through the shared substrate, and systems have no way to *call it back*. Our
current position is worse than the literature's — with no read log and no audit table, a poisoned
note discovered today cannot even be traced to the lanes that consumed it.

### 4.1 Supersede-not-delete, revoke-not-erase

Three distinct operations, deliberately not collapsed:

| Operation | Meaning | Row effect | Retrievable after? |
|---|---|---|---|
| **supersede** | this is now wrong/outdated; here is the replacement | `superseded_by` set on old, new row written | no (live view), yes (history) |
| **revoke** | this should never have been written (poisoned, mis-attributed, leaked secret) | `revoked_at` + `revoked_reason` set | no, to anyone but operator |
| **merge** | two rows are the same thing | existing `note_merges` path, unchanged | absorbed id excluded by `search_v3` |

No operation deletes a row. This matches the bi-temporal discipline `state_facts` already
implements (`valid_to` / `invalidated_at`, invalidate-not-delete) — §4 simply gives the note layer
the same property the fact layer has had since Memory V2.

**Secret-leak caveat, stated plainly:** revoke hides a row from retrieval; it does not erase the
string. If a credential lands in a note, revocation is step one and a *hard scrub* — operator-run,
backed up first, recorded in `changelog` — is step two. #575 must ship the scrub path as an
explicit operator command rather than leaving the impression that revoke is sufficient. (Cf. the
2026-08-13 Gitea-token episode: masking rules exist because a leaked credential in a store is a
rotation event, not a filtering event.)

### 4.2 Propagation — walking the read log

```
memoryd revoke <note_id> --reason "<why>"
  1. verify operator identity                              (lane cannot revoke)
  2. BEGIN IMMEDIATE
       set revoked_at / revoked_reason on the row
       audit(action='revoke')
     COMMIT
  3. SELECT DISTINCT lane, agent_id, source_ref, ts
       FROM memory_audit
      WHERE action='read' AND detail LIKE '%'||<note_id>||'%'
      ORDER BY ts DESC                                     -- the consumer set
  4. for each consumer: emit a RECALL NOTICE
       - open wq tickets  -> append note to the ticket (visible on next claim)
       - closed tickets   -> listed in the revocation report only
       - live sessions    -> ntfy lumen-fleet
  5. write revocation report to v4/reports/revocation-<id>-<date>.md
```

Step 3 is the whole reason §3.1 logs reads. Step 4 is deliberately **notification, not rollback**:
the daemon cannot un-say something an agent already wrote into a report. What it can do is make
the contamination *legible* — which ticket, which session, which downstream artifact. Claiming
anything stronger would be dishonest about what a memory service can enforce.

### 4.3 Cascade

If a revoked note was cited as `source_ref` by other notes (an `agent-lane` write attributing to
it), those are surfaced as **candidates**, not auto-revoked — a note quoting a bad note may still
be independently correct. The cascade set goes into the revocation report for operator decision.
Depth-1 only; deeper transitive walks generate more noise than signal at 1,161 notes.

### 4.4 Quarantine ↔ revocation symmetry

Quarantine catches bad content *before* it lands; revocation catches it *after*. Same detectors
(§3.3) feed both. A D-2 hit on an existing row is a revocation candidate, not an automatic
revocation — because the alternative is a detector that can silently delete the fleet's memory,
which is a worse failure than the one it prevents.

---

## §5. No auto-promote paths — anywhere

GovMem (2607.02579) on real traces: **0 of 133** automatic promotions were safe, and 11/11 gate
positives were correctly rejected by human review. Correlated traces make vote-counting look like
consensus. This is the single most directly load-bearing external result for our design, because
it says the thing that is tempting — "promote what many lanes agree on" — is exactly the thing
that fails.

Every promotion-shaped operation in the fleet, and its gate:

| Promotion | Today | Under memoryd |
|---|---|---|
| trust `external`/`derived` → `operator` | possible via env var (GAP-1) | **impossible**; trust is derived (§2.4). Only an operator write produces `operator`. |
| note → instinct (`instincts.yaml`) | dream writes candidates; Frank merges by hand (last merge 2026-08-13) | unchanged, still Frank-gated; daemon exposes candidates read-only |
| note → `claude_memory.md` core section | `minerva_memory.core_append()` exists | proposal only; daemon never edits the prose file |
| fact ingest by an agent lane | any lane may call `lumen_state.py ingest` | **propose only** (§1.2); operator applies |
| dedup merge (`note_merges`) | proposal + operator apply | unchanged |
| quarantine release | n/a | operator only, re-validated (§3.2) |
| revocation | n/a | operator only (§4.2) |
| forgetting / archival (P4, #576) | n/a | proposal only, per #571's design |

The daemon therefore has **no code path that raises trust, edits prose, or accepts an instinct.**
That is a property #575 should assert with a test, not merely document: a test that enumerates
every `INSERT`/`UPDATE` statement in the daemon and fails if any targets `instincts.yaml`,
`claude_memory.md`, or sets `trust='operator'` outside the operator handler.

Proposals surface through the machinery that already exists: the proposals table (1,057 rows) and
the operator UI at `propose.lumennode.uk` (127.0.0.1:8110, CF-Access, Frank-only). No new review
surface is built — a second inbox is a second thing to forget to read.

---

## §6. CLI + localhost HTTP API sketch (the #575 build target)

### 6.1 Shape

`lumen-memoryd` — single Python process, stdlib `http.server` + `ThreadingHTTPServer` or a thin
FastAPI/uvicorn if a dependency is acceptable. Binds **127.0.0.1 only**. No TLS (loopback), no
external route, **not** exposed through cloudflared.

**Port: 8139.** Verified free this session; the 813x block is the local-service cluster
(8130, 8131=Scribe, 8135, 8136, 8140 occupied; 8137/8138/8139/8141-8144 free). #575 must re-check
at build time rather than trusting this line.

Unit `/etc/systemd/system/lumen-memoryd.service`: `User=lumen`, `Restart=always`,
`RestartSec=5`, `MemoryHigh=512M`, `MemoryMax=1G`, `Environment=MEMORYD_DB=/home/lumen/minerva/minerva.db`.
Memory caps are part of the shed intent (§6.6) and also make a leak visible as a restart rather
than as fleet-wide pressure — the failure mode wq#380 already cost a session.

CLI `/home/lumen/bin/memoryd` is a thin client over the same HTTP surface, so there is exactly one
enforcement path. A CLI that talks to SQLite directly would reintroduce every gap in §0.

### 6.2 Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | liveness + schema sha + db mtime + counts. Used by the restart drill (§8) and Gatus. |
| POST | `/recall` | `{query, top_k=10, tiers?, include_external=false}` → `search_v3` results, trust-filtered per §2.5, audit `read` row written. |
| POST | `/commit` | `{kind, title, content, tags[], source_ref}` → kernel validate → accept/propose/reject/quarantine. Provenance is **not** accepted from the body. |
| POST | `/precheck` | `{plan}` → `lumen_episode.precheck` equivalent: related prior episodes + recorded failures. Read-only, no side effects beyond an audit row. |
| POST | `/propose` | explicit proposal submission (fact changes, merges, demotions) → proposals table. |
| POST | `/revoke` | operator only. `{note_id, reason}` → §4.2 including the consumer walk. |
| GET | `/audit` | operator only. Filters on lane/row_id/action/date. |
| GET | `/quarantine` | operator only. List + release/discard. |

Responses always carry `{status, id?, reason?, invariant?, audit_seq}`. A rejected write returns
**which invariant fired** — an agent that cannot see why it was rejected will retry forever, which
is how a guard becomes a load generator.

### 6.3 Identity and authorization

Three-factor, cheapest first:

1. **Peer credentials** — `SO_PEERCRED` on a Unix socket, or uid check if HTTP-on-loopback.
   Establishes "this is the `lumen` user", nothing more.
2. **Lane token** — `X-Memoryd-Agent: <WQ_AGENT>`, the session-scoped id from
   `wq whoami --register` (wq#502). The daemon resolves it against `queue.db`: the agent must be
   registered, and for a write with `source_ref=wq#N` the agent must **hold the live claim** on
   ticket N. This reuses the coordination primitive the fleet already trusts rather than inventing
   a parallel identity system.
3. **Operator token** — a 0600 file `/home/lumen/.secrets/memoryd-operator.token`, required for
   `/revoke`, `/audit`, `/quarantine`, and any `trust=operator` write. Rotation follows the
   one-file/one-rotation-point pattern established for the Gitea credential (wq#529).

Unknown or unresolvable identity → **reject**, audit row, no data returned. Fail closed.

Caveat worth stating: on a single-user box any process running as `lumen` can forge a header or
open the DB directly. This design raises the floor from "no boundary at all" to "a boundary you
must deliberately step around, and stepping around it is detectable (D-2)". It is not a security
boundary against a compromised host and #575 must not claim it is.

### 6.4 Rate + size guards (I-9)

Per lane, sliding window, in-memory (no persistence needed — a restart resetting counters is
acceptable):

- writes: 60/hour, 500/day. Operator: unlimited.
- reads: 600/hour.
- request body: 64 KB hard cap (content limit is 8 KB per I-5; the rest is headroom for tags).
- `top_k` clamped to 50.
- concurrent requests: 8, queue depth 32, then 503 with `Retry-After`.

Over-limit is a `429` **plus an audit row plus a dream report line** — a lane hammering the store
is itself a signal (D-1), not just a thing to throttle.

### 6.5 Latency budget

`search_v3` measures p50 ~180 ms today, dominated by the embed round-trip and a brute-force cosine
over ~1,100 rows. The daemon adds identity resolution (one indexed `queue.db` lookup) and one audit
insert — target overhead **< 15 ms p50**, and #575 must measure it rather than assume. Per decision
C (wq#567) the judgement is on p50 and accuracy; p95 is environment noise on this box.

A long-lived process is also the natural home for the `_SUBJ_CACHE` / `_TAG_CACHE` 600 s caches
that today are rebuilt per CLI invocation — a real win, but one that must be validated against
golden v4 (#572) like everything else, because a stale subject cache changes recall.

### 6.6 NO models in the daemon

The daemon holds no weights, loads no embedder, imports no torch. Embedding stays an out-of-process
HTTP call to Ollama (`127.0.0.1:11434`, `nomic-embed-text`) exactly as `minerva_memory.embed()`
does today (§0.10). `MemoryMax=1G` makes a violation of this fail loudly instead of quietly
consuming the RAM the 35W build is trying to free.

Open item for Frank, not decided here: the existing embed call passes `keep_alive='30m'`, which
pins nomic resident in Ollama after every write and recall. If the shed directive means "no
resident embedder on Lumen", that is a change to `minerva_memory.py`, affects V3 recall latency,
and is out of #575's scope. Raised as §9 Q3.

---

## §7. Migrating the lumen-agent memory hook

### 7.1 What is actually there (cited, §0.9)

- `/home/lumen/agent/lumen_agent_v4.py:310-329` — `_memory_context(query)`, fail-open, 6,000-char
  cap (wq#138). Called at `:351-358` via `run_in_executor` (wq#467, off the event loop) and once at
  `:591-602` as a startup warm-up.
- Import path: `sys.path.insert(0, "/home/lumen/mem0")` → `lumen_memory_router.route()` +
  `fetch_context()`.
- `/home/lumen/mem0/lumen_memory_router.py` (2026-06-14) → `facts` = **Mem0/Qdrant**
  (`mem0-qdrant` container, up 6 days), `state` = `lumen_state.txt` flat file,
  `episodes` = last 120 lines of `claude_memory.md`.
- `/home/lumen/autonomy/agent5/a5_common.py:96-110` — identical wiring for planner/generator/
  evaluator.
- `search_v3` call sites in the agent path: **zero**.

### 7.2 The decision that must precede the migration

Two substrates hold "what Lumen knows": minerva.db (1,161 notes, 872 facts, V3 retrieval, all the
governance in this document) and Mem0/Qdrant (contents unenumerated by this audit — read-only
scope, and enumerating it means running Mem0 queries). Options:

- **(a) Retire Mem0.** Lanes recall through memoryd → `search_v3` + `lumen_state.py current`.
  One substrate, one governance model. Requires deciding what, if anything, in Qdrant is not
  already in minerva.db, and frees a container.
- **(b) Keep Mem0 as the `facts` tier**, memoryd fronts both. Two write paths to govern; §2-§4
  apply to only one of them. Weakest option — it preserves the exact "isolated memories +
  message passing" fragmentation the MAS literature (2512.13564 §7.5) says fails at scale.
- **(c) Freeze Mem0 read-only**, migrate lanes to memoryd, keep Qdrant as a cold archive until a
  later session proves nothing unique is in it.

Recommendation: **(c) then (a)** — reversible, and it does not require enumerating Qdrant before
the cutover. **This is Frank's call, not mine** (§9 Q1). #575 must not start until it is answered,
because the answer determines whether `/recall` is a facade over two stores or one.

### 7.3 Migration sequence (once Q1 is answered)

1. **Client module** `/home/lumen/agent/memoryd_client.py` — `recall()`, `commit()`, `precheck()`
   over HTTP, 2 s connect / 5 s read timeout, **fail-open returning `""`**. The fail-open property
   is not negotiable: `_memory_context`'s docstring says a memory miss must never block research,
   and that has been true since Day 6.
2. **Flag** `MEMORY_V4_MEMORYD` in `/home/lumen/agent/.env`, **default off**. Off = today's code
   path, byte-identical.
3. **Shadow mode** — flag on, both paths run, memoryd result logged and *discarded*. Compare
   coverage and latency on organic prompts for one soak period. This is how the Day-6 fleet
   cutover was validated (A5_TEE_RATE tee) and it caught things canaries did not.
4. **Golden v4 gate (#572)** — adopt only on measured win: p50 and accuracy, per-class, no class
   down >2 pts. If the entity/tail work (#574) has landed, re-baseline first.
5. **Cutover** — `_memory_context` body replaced by the client call; the 6,000-char cap and the
   `run_in_executor` offload stay exactly as they are. `a5_common.memory_context` follows in the
   same change, same flag.
6. **Backups before any of it** — `.bak` siblings for both files, `sqlite3 .backup` of minerva.db
   to `/home/lumen/backups/`.
7. **Fix the stale comment** at `lumen_agent_v4.py:591` in the same commit (§0.9).

Rollback at every step is "set the flag off and restart lumen-agent" — and, because step 2 keeps
the old path intact, rollback never depends on the daemon being healthy.

---

## §8. Restart drill + paired-restore requirements for #575

Standing fleet rule: any service stopped gets restarted and verified before session end, or
flagged as an explicit open issue. A new unit inherits that, and must prove it.

**#575 does not ship until all of these pass:**

1. **Cold start** — `systemctl start lumen-memoryd` → `/healthz` 200 within 5 s, schema sha
   matches `memory_contract_v1.yaml`, counts non-zero.
2. **Kill -9 mid-write** — kill the daemon during a `/commit`; on restart the DB has either the
   complete row *and* its audit row, or neither. Never one without the other (§1.4).
3. **Paired restore** — stop the unit, confirm `_memory_context` still returns (fail-open,
   flag on, daemon down) and the agent keeps working; restart; confirm recall resumes. **An agent
   lane must never be blocked by memoryd being down.**
4. **Reboot survival** — `systemctl is-enabled` true, symlink present. (wq#393 found `lumen-agent`
   WantedBy-set but unsymlinked — enabled-looking and dead after a reboot. Assert the symlink, not
   the unit state.)
5. **DB locked** — hold a write lock from another process; `/commit` must return 503 with
   `Retry-After` rather than hanging the request or corrupting state. `#572`'s bench and `#576`'s
   hit logging will both be writing concurrently.
6. **Append-only triggers** — attempt an `UPDATE` and a `DELETE` on `memory_audit` directly via
   sqlite3; both must ABORT.
7. **Immutability trigger** — attempt an `UPDATE` of `origin`/`trust` on a live note; must ABORT.
8. **Trust forgery** — `/commit` with `trust: operator` in the body from a lane identity; must be
   ignored (row lands `derived`) *and* produce a D-2 audit row.
9. **Unknown identity** — `/commit` with no/garbage agent header; reject, audit, no row.
10. **Rate limit** — exceed the write budget; 429 + audit + no partial writes.
11. **Golden v4 parity** — `/recall` results identical to direct `search_v3` for the golden set
    with `include_external=true` and filters disabled. The daemon must not change retrieval while
    it is only supposed to be governing it.
12. **Restic** — minerva.db backed up before the first schema change, restore-acceptance verified.

A drill that has not gone red is not a drill: #575 should plant a deliberate failure for at least
#2, #6 and #8 and show the red result in its report, exactly as this document's sweep did in §0.4.

---

## §9. Open questions for Frank — do not guess these

**Q1. Mem0/Qdrant: retire, front, or freeze?** (§7.2) The agent lanes' `facts` tier is a second
memory substrate outside every control in this document. Recommendation (c)→(a): freeze read-only,
migrate lanes to memoryd, retire once nothing unique is shown to be in it. #575 is blocked on this
answer because it determines whether `/recall` fronts one store or two.

**Q2. Schema-change window for the §2.1 additive columns.** Eight `ALTER TABLE ADD COLUMN` plus
two triggers on minerva.db. Additive and backward-compatible, but it is the store every timer,
the dream run, and every session reads. Wants a quiet window, a `sqlite3 .backup` first, and
confirmation that nothing else holds the write lock — note that wq subsystem `minerva-db` is a
contended lock (#571 could not claim it this session, §10).

**Q3. `keep_alive='30m'` on the embed call.** (§6.6) The daemon holds no model, but the existing
embed path pins nomic resident in Ollama after every write and recall. If "no warm models on
Lumen" is meant literally for the 35W build, that is a separate change to `minerva_memory.py` with
a recall-latency cost. Not touched here.

**Q4. Read-audit granularity.** One audit row per query (recommended, §3.1) vs per hit. Per-hit is
strictly better input for #576's usage salience; per-query is ~k× cheaper. If #576 wants per-hit,
say so now — the schema should not change twice.

**Q5. Does the operator path get screened?** §3.4 exempts operator writes on the grounds that a
screen the operator fights is a screen that gets disabled. If you would rather have the size and
pattern checks apply to everything including your own writes, that is a one-line change.

---

## §10. Session record

**Claim.** wq#570 claimed by `mv4-w570-19cc6b`, provisional 60 m lease promoted to 240 m
(expiry 2026-08-15 00:54:15Z). #570 declares no subsystems, so the `minerva-db` slock held by
`mv4-w572-432b5b` (claimed 19:34:16Z, lease 23:34:16Z) did not block it. All database access this
session was `sqlite3 -readonly`; no write, no schema change, no WAL interaction.

**Sibling ticket status.** wq#571 could not be claimed earlier in this session — `minerva-db` held
by the live #572 lease, and `wq block` refuses on a ticket the caller does not own (`_finish` →
`owner_ok`). That collision is unresolved and sits with Frank; it is noted here because #571's
output (usage salience) and this document share the `memory_audit` read log as a dependency (Q4).

**Read-only verification performed:** schema dumps (7 tables), 6 row counts, 3 value-distribution
queries, 2 code sweeps with a planted positive control, 9 targeted file reads, 1 port scan, 1
container/HTTP liveness check. Corpus for the write-path sweep: 14,408 production `.py` files.

**Deliberately not done:** no Mem0/Qdrant enumeration (would require running Mem0 queries — write-
adjacent, and Q1 is unanswered); no `PRAGMA journal_mode` change; no schema applied; no service
created; nothing stopped, so nothing needs restoring.

**Corrections to the plan's own §0 table** (`MEMORY_V4_PLAN.md`), all evidenced above:
- "Multi-agent access | ad-hoc module imports | no service boundary" — accurate, and understates
  it: the lanes are on a *different substrate entirely* (§0.9), not merely an unguarded import.
- "Dream/consolidation | anomaly scan LIVE (V3 P5, lumen_dream.py:242)" — the line is correct and
  the function exists, but it is a 7-day `GROUP BY` with no baseline, threshold, or action
  (§0.7). "Live" overstates it; the detector still has to be built.

---

## Appendix — build order for #575

1. Schema additions (§2.1) + triggers (§2.2, §3.1) behind a backup. **Gated on Q2.**
2. `memory_audit` + `memory_quarantine` tables; nothing reads them yet.
3. Kernel module (§1.3) as a pure library with unit tests — validated against the live store in
   dry-run mode, writing nothing, proving I-1..I-9 fire on real rows before any daemon exists.
4. Daemon skeleton: `/healthz`, `/recall` (read-only, audit rows only). Lowest-risk half.
5. Restart drill items 1-5 (§8) at this point, before any write path is exposed.
6. `/commit`, `/propose`, `/precheck` behind the kernel. Drill items 6-10.
7. Detectors D-1..D-5 into the dream run (§3.3); quarantine wired.
8. `/revoke` + propagation walk (§4.2). Drill on a planted test note, end to end.
9. Agent migration (§7.3), flag off → shadow → golden-gated cutover.

Steps 1-8 change nothing about what the lanes see. Step 9 is the only one that alters agent
behaviour, and it is flag-gated, shadow-validated, and golden-gated. That ordering is the point:
the governance lands before the dependency does, so a rollback at step 9 never leaves the store
half-governed.

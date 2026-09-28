# wq#2901 — lane attribution for fleet workers (memoryd write path)

Status: design, then built in the same ticket. Changes `v4/schema/memory_contract_v1.yaml`,
whose sha256 is reported by `/healthz` — so the contract edit is stated here first.

Companion to `memory_service_design.md` (#575). Section numbers below refer to that doc.

## 1. The failure

Every fleet-worker episode commit is refused by lumen-memoryd and lands through the
direct-write fallback instead. Nothing is lost — but nothing is *attributed*, which is the
whole point of the governed path (§1.2, I-7).

Evidence, `minerva.db memory_audit`, read 2026-09-28:

```
reject / archival_notes / "identity: agent 'fworker-wqN' is not in the wq session registry"
09-20 2603   09-21 405   09-24 401   09-25 1358   09-26 408   09-28 426
```

5594 of 5637 registry rejects name an `fworker-wq<N>` identity. Zero accepted fleet-worker
commits in the window.

## 2. Five independent causes

Each one alone is sufficient to refuse the write; all five must go.

**C1 — identity.** `fworker` sets `WQ_AGENT=fworker-wq<N>` (`foreman/harness/fworker.py:678`
`wq_identity`, exported at `:2373`). That name is deliberately *not* in `queue.db
agent_sessions`: `wq`'s `holder_is_dead` (`/home/lumen/bin/wq:800-812`) distinguishes
"idle between attempts" from "dead" precisely by the absence of a session row plus the
presence of a `lumen-job-<holder>-<ts>` unit. Registering fworkers would break
`wq release --dead-worker`. `daemon.py:106` calls `K.agent_registered()` and refuses.

**C2 — source_ref.** `episodic.digest_events` sets `ref` to the episode file path
(`foreman/memory/episodic.py:148`), e.g. `/home/lumen/foreman/_runs/wq2901/episode.jsonl`.
The kernel's `SOURCE_REF_RE` (`kernel.py:22`) accepts only `wq#\d+` or `session:<slug>`, and
lane derivation needs the ticket id (`daemon.py:110-118`). A path can never yield a lane.
(Latent second bug at the same line: `episode_path(ticket_id)` is called without
`runs_root`, so the recorded path is wrong whenever `FOREMAN_RUNS_ROOT` is overridden.)

**C3 — contract lanes.** The contract lists 12 lanes; `queue.db` has 28. The biggest fleet
lanes are all missing — `build` (1468 tickets), `fleet-fires` (66), `pilot` (65),
`mailbot` (48), `ops` (35). Unknown lane is a fail-closed reject (`kernel.py:172`, I-4), so
even a correctly identified write with a correct `wq#N` ref would still be refused.

**C4 — kind.** `lumen_episode.py commit --kind` takes any string; the contract enum is
closed for new writes (I-4). Observed rejected kinds: `verify`, `verification`,
`investigation`, `bugfix`. The CLI validates nothing, so the caller learns nothing.

**C5 — the test suite writes to production.** 319 of today's 426 rejects are a single
identity, `fworker-wq1`, from `foreman/harness/tests/test_shim_*.py`: the suite runs
fworker end to end, `commit_episode_to_archive` fires for real, and the fallback has put
**67 notes and 430 audit write rows** into the live `minerva.db` with
`source_ref=/tmp/pytest-of-lumen/...`. Same class as wq#2784. Without this, the acceptance
criterion "registry rejects drop to ~0" is unreachable no matter what the daemon does.

**Also asked: is the claim still held when `commit_to_archive` runs?** Often no, and that
matters. `fworker.py:3969` commits the episode immediately after `attempt_end`, but the
*worker agent itself* runs `wq done <id>` from inside its session, so by then the ticket is
usually `status='done'`. `K.agent_claims()` filters `status='claimed'` (`kernel.py:307`),
so a live-claim requirement would reject exactly the runs that succeeded. `claimed_by` does
survive the close — verified live: tickets 2912/2904/2903/2899 are `done` with
`claimed_by='fworker-wq<N>'` intact.

## 3. Prior art

`fleet-search web "ephemeral worker identity attribution audit log claim ticket workload
identity pattern"` — the non-human-identity literature (CSA *Defining Non-Human Identity*,
2026-07) lands on the same shape: an ephemeral agent instance gets a unique identity **at
spawn**, bound to the thing that spawned it, rather than a shared account or a human-session
record. `fworker-wq<N>` already *is* that identity; the defect is that the store only
recognises human-session identities. The fix is to teach the store the spawn record, not to
forge a session row.

## 4. Design

### D1 — a third identity class: per-ticket fleet worker

New branch in `daemon.py resolve_identity`, after the service-token branch and before the
session branch. An agent is a fleet worker when it matches `^fworker-wq(\d+)$`
(`MEMORYD_FWORKER_IDENTITY_RE`, mirroring `wq`'s `WQ_FWORKER_IDENTITY_RE` so the shape is
env-overridable in one style, not two). It is accepted when **all** of:

1. `source_ref == "wq#<N>"` with the *same* N as the identity. A worker identity can only
   ever speak about its own ticket — this is what makes the class safe to add.
2. `tasks.claimed_by == agent_id` for ticket N, **at any status**. The claim *receipt*, not
   the live lease (§2, above). It is written only by `wq claim`; a writer cannot set it for
   itself, and cannot set it at all for a ticket another holder claimed.
3. The lane comes from `tasks.lane` for N — unchanged §2.4 derivation. The worker still
   cannot spell its own lane.

Trust stays `derived` from the lane's contract entry. Anything else is rejected with the
existing reason strings, so drill D9 (`agent="not-a-registered-session"`) stays red.

Threat delta: none of substance. §6.3 already concedes that any process running as `lumen`
can forge `X-Memoryd-Agent`; what it cannot forge is a `claimed_by` row for a ticket it does
not hold, and the lane is never taken from the request. The new class is *narrower* than the
session class beside it — one agent, one ticket, one lane.

Rejected alternative: register fworkers in `agent_sessions`. It would make one liveness
signal serve two contradictory purposes and break `wq release --dead-worker` (C1).

### D2 — `source_ref = wq#N`, path into the content

`digest_events` returns `ref = "wq#<id>"`, and the episode file path moves into the note
body as an `## Evidence` line, so the raw-evidence pointer survives. `digest_events` gains
`runs_root` so that path is the real one (C2's latent bug).

The fallback *mechanism* is untouched: still tried on any memoryd refusal, still one
transaction with its own audit row, still fail-open. Only the `source_ref` it records
changes shape — from a path to `wq#N`, which is the shape I-7 wanted from the start.

### D3 — all 28 live lanes in the contract, plus a drift check

Explicit entries for every lane present in `queue.db`, standard agent-lane policy
(`archival_notes: write, state_facts: propose, trust: derived`). `user` keeps its existing
`reject/reject` — that policy is unrelated to this ticket and stays as found.

Fail-closed on unknown lanes stays (§1.2). What bit us is not the policy but its *silence*:
a new wq lane starts rejecting every episode with no signal. So `/healthz` gains
`lane_drift` — lanes live in `queue.db` and absent from the contract — and the drill battery
asserts it is empty. The next new lane is then a visible warning instead of 400 silent
403s a day.

### D4 — validate `--kind` at the CLI

`lumen_episode.py` reads the contract's `kind` enum (the same file the daemon pins) and, for
`commit`:

- a known synonym is mapped: `verify`/`verification`/`audit-trail` → `audit`,
  `investigation`/`triage` → `analysis`, `bugfix`/`bug-fix`/`bug` → `fix`,
  `postmortem`/`post-mortem` → `lesson`.
- anything else exits non-zero with the allowed list. Failing loud at the caller beats a 422
  the caller never sees.

Fail-open on a missing or unparseable contract: the CLI must not become a new way for an
episode commit to die.

### D5 — the fallback stops being silent

`commit()` counts a memoryd refusal that *should* have been an accept — we had an agent id
and a `wq#N` ref — as a breadcrumb at `/home/lumen/minerva/logs/memoryd_reject.json` (same
shape and guard-timer pattern as `V3_FAIL_MARKER`), and stamps the fallback audit row with
invariant `wq2901-memoryd-reject`. One `SELECT` then answers "how often is the governed path
refusing legitimate writes", which is the question nobody could ask for eight days.

### D6 — test isolation

`foreman/harness/tests/conftest.py` gains an autouse fixture pointing `LUMEN_EPISODE_BIN` at
a no-op stub in `tmp_path`. The commit path is still exercised
(`subprocess.run(check=True)` still runs a real binary); it just cannot reach the live
store. Matches the existing `_no_live_aq` / `_no_refill_kick` pattern in that file.

## 5. Acceptance

- An fworker commit lands via memoryd (`accept`) with the lane read from the ticket.
- `memory_audit` registry rejects → ~0.
- `drills_575` still pass; D9/D9b still red-on-purpose.
- Fallback mechanism unchanged; `source_ref` shape changed as stated in D2.

Probe must be able to fail (wq#2630): the accept probe runs against a ticket whose lane
(`build`) is one of the *newly added* contract entries, and the paired negative probe uses a
well-formed `fworker-wq<N>` identity for a ticket it does **not** hold — an input from
outside the population D1 was designed around — and must still be refused.

# wq#4538 S2 boot half — session claim anchor in `claude_context.sh --core`

Implements §3 of `design/wq4538_orch_episode_identity.md` (foreman, PR#2397): every
orchestrator session files/claims one synthetic `session`-lane ticket at `--core`
boot, immediately after the wq#1127 REGISTERED check, and the banner (plus
`WQ_SESSION_TICKET`) hands the ticket id to the session. `lumen_episode.py commit()`
(this repo, wq#4559) then folds any source_ref I-7 cannot verify — ranges, lists,
other agents' tickets — into the episode's content/tags and commits under this
anchor instead, so the write is attributable without widening the gate.

The design doc places this archive in foreman `ops/`; it lives here because the
file it patches is `/home/lumen/minerva/context/` and this minerva PR is the
implementation vehicle (the foreman branch was docs + the disabled `session` lane
entry only).

## Why a diff and not a landed file

`claude_context.sh` is an untracked live file (same situation as
`ops/wq1127-core-boot-identity-guard/` in foreman, which archived its diff the
same way). Per the design's rollout order this boot half is step 3 — applied via
the attended boot-script path AFTER the QW3+S2-client PR (this branch) is deployed
and the deployed foreman checkout carries the `session` lane in
`dispatcher/config.json` (`wq add` reads that config at committed git HEAD,
wq#772). Applying it earlier makes `wq add --lane session` fail at boot.

## Apply

```sh
patch /home/lumen/minerva/context/claude_context.sh \
    < /home/lumen/minerva/ops/wq4538-session-claim/claude_context_session_claim.diff
bash -n /home/lumen/minerva/context/claude_context.sh   # syntax gate
```

Validated on 2026-10-23 against the live file (sha256 f3ed939673…): `patch
--dry-run` clean, `bash -n` clean on the patched result.

## Notes

- `--core` runs as a subprocess of the session, so the `export WQ_SESSION_TICKET`
  reaches only the script's own children; the `===== SESSION TICKET: wq#N =====`
  banner is the real transport — the session exports it itself, exactly as it
  already does with the `REGISTERED SESSION` banner / `WQ_AGENT`.
- Reuse-before-add: a re-booted session re-uses its own open `session`-lane claim
  (read-only queue.db lookup) instead of leaking one ticket per boot.
- Shapes verified against `bin/wq` (argparse at bin/wq:3981-3982): `wq add
  --lane/--title/--detail/--no-probe`, `wq claim --id N --lane session`; queue.db
  `tasks` has `id/lane/title/status/claimed_by`.
- Lifecycle: the session closes its own ticket at handoff (`wq done` with the
  session summary as notes) or leaves it to the stale-claim sweep — the claim
  RECEIPT (`claimed_by`) survives either, so late spool replays still
  authenticate (wq#3352).

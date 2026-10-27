# wq#4724 — `--require-exit` as the build-job default in `fleet_tools.yaml`

Follow-up to wq#4711 (lumen-bin PR #279, merged `5e84ad41e4`): `watchdog --require-exit`
closes the bug where a stable artifact got reported OK while the producer (e.g.
stage11b/stage12 after xorriso) was still running and could still fail. wq#4711 itself
could not update `fleet_tools.yaml` or wq#4635's ticket text because `/home/lumen/minerva`
was not among that session's allowed working directories — see
`/home/lumen/foreman/wq4711_cross_repo_evidence.md` ("fleet_tools.yaml / wq#4635 text (not
updated by this ticket)").

## wq#4635 (done, not part of this bundle)

`wq edit 4635` already appended a `--require-exit` pointer (PR #279 / wq#4711) to the
ticket's own `detail` text and added `--require-exit` to its example invocation. That's a
`wq` database edit, not a file change, so there's nothing to land for it.

## `fleet_tools.yaml` (this bundle)

`/home/lumen/minerva` is a registered PRIMARY tree for a BUILD worker's Edit/Write tool
calls (`guard.sh`, wq#1719/wq#900) — confirmed by testing, not assumed: a direct `Edit` on
`/home/lumen/minerva/context/fleet_tools.yaml` from this session was refused with "Write/Edit
inside PRIMARY tree /home/lumen/minerva denied (wq#1719/wq#900) ... for anything else make
the change in your own worktree and land it with lumen-pr". That remedy doesn't reach this
file: `context/` is `.gitignore`'d in the minerva repo (`git check-ignore -v
context/fleet_tools.yaml` → `.gitignore:18:context/`), so there is no tracked copy in any
worktree (including this ticket's own `wq4724-a1`, which is itself a minerva worktree) and
no PR path — same shape as `ops/wq4538-session-claim/` (`claude_context.sh`, also untracked
live) and `ops/wq4537-claude-memory-hand-prose/` (`minerva/memory/context_builder.py`,
`install.sh`: "Run as lumen from a session with minerva write access: BUILD workers are
walled out of /home/lumen/minerva"). Staging a diff + apply instructions, per that
precedent, instead of guessing at a bypass.

## What changes

The `watchdog` entry's `use` and `invoke` fields (live file lines 120-124, `fleet_tools.yaml`
sha256 `9939162da1516b2b320386b87120f5f1a42db8136d9bf42bf4a51269d8d3c246` as of 2026-10-27):
adds a sentence to `use` pointing `-a` jobs at `--require-exit` (wq#4711/PR #279) and adds
`--require-exit` to the `invoke` example command.

## Apply

```sh
patch -p1 -d /home/lumen/minerva --dry-run < /home/lumen/worktrees/wq4724-a1/ops/wq4724-watchdog-require-exit/fleet_tools_require_exit.diff
patch -p1 -d /home/lumen/minerva < /home/lumen/worktrees/wq4724-a1/ops/wq4724-watchdog-require-exit/fleet_tools_require_exit.diff
```

Validated by exact-string comparison, not `patch --dry-run` — `patch` itself is not on this
BUILD worker's Bash allowlist ("This command requires approval"), the same per-command gap
class as wq#2856/wq#1107 (unlisted command, not a retryable failure). `grep -n ^
/home/lumen/minerva/context/fleet_tools.yaml | sed -n '120,124p'` on 2026-10-27 reproduced
the diff's four `-` lines and the one unchanged context line byte for byte, so the hunk's
old-side matches the live file exactly at the anchored line numbers.

## Not done here

- Re-rendering `FLEET_TOOLS.md`/`FLEET_TOOLS_BOOT.md` from the patched YAML
  (`/home/lumen/bin/fleet-tools-render`, also not on this worker's Bash allowlist) — run it
  after applying the patch so the rendered docs don't drift from the YAML source.

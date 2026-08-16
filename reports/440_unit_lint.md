# wq#440 — systemd unit lint (prop fd50d5642dc3, Scribe #301)
Date: 2026-08-10 · Session: claude-web-0809b

## Scope decision
Cited paper (Harness Handbook) is about agent-harness CODE readability;
the proposal misapplied it as "refactor ALL live systemd units" — rejected:
touching 230 units incl. crown services for cosmetic uniformity is maximum
blast radius for zero functional gain, against fleet change-safety doctrine.
Salvageable delta shipped: the lint script, strictly READ-ONLY.

## Shipped
/home/lumen/bin/unit-lint (+x, --json mode) — flags: missing Description,
service with no [Install] and no timer, timer without service, oneshot
without TimeoutStartSec, missing memory limits (advisory), ExecStart binary
missing on disk; counts .bak/.disabled clutter. Never edits; exit 0 always.

## Baseline findings (2026-08-10)
230 units scanned; 141 flagged but nearly all advisory (memory limits /
TimeoutStartSec); only 2 structural findings; 50 .bak/.disabled files in
/etc/systemd/system (cleanup candidate for a future gated pass — NOT done
now). No broken ExecStart paths.

## Rollback
rm /home/lumen/bin/unit-lint. Nothing else touched.

# wq#591 — Plug awareness: retention + unprompted findings

Agent `mv4-w591-803a2d` · lane `memory` · 2026-08-14 · authority Frank → mv4 orch → this agent → Lumen
Origin: `FAILURE_ARCHAEOLOGY.md` §2 failure #1 and §7. Frank: *"We are collecting this data for a
reason and it bothers me that no one was aware."*

## Headline: the brief's premise was half wrong, and that matters

The brief said the fleet measures wall wattage every 60s and throws it away. Verified live this
session: **it does not.** The samples are retained, at higher resolution than the brief assumed,
and have been for weeks.

| claim in brief | verified state (2026-08-14) |
|---|---|
| plug-snapshot overwrites `plugs.json` every 60s, no history | TRUE — but that path is a Glance *publish*, not the corpus |
| `power.db` has 6 rows, last 2026-07-16 | TRUE — legacy A/B scratch, `power-ab-poll.service.new` never installed |
| "no on-box wattage history exists" | **FALSE** — VictoriaMetrics on Bes holds it |

**The corpus:** VM on Bes (`100.110.156.91`), `-retentionPeriod=3y`, metric
`homeassistant_sensor_power_w`, **13 live series at 15s cadence** with a `node` label,
**1,582,489 samples in the last 30d**, continuous since 2026-07-20 (26 days). Reachable from
Lumen at `127.0.0.1:18428` through `lumen-powerdash-vmtunnel.service`.

So per the brief's own instruction — *"CHECK whether VM already retains plug series before building
a second store. If it does, use it and say so; do not duplicate a corpus"* — **no second store was
built.** `power.db` was not touched, `plug-snapshot` was not modified, the Glance publish path is
byte-for-byte unchanged. Deliverable 1 was already satisfied by infrastructure shipped in wq#341/#342.

This sharpens the finding rather than softening it. The Nephthys recommendation was not made in the
absence of data. It was made **on top of 26 days of 15-second measurements that nobody queried.**
The disease is not retention. It is that querying was never anybody's job. That is what got built.

### Series hygiene resolved while verifying
- 25 series names exist; only 13 are live. `node=""` (45 samples/30d) and `node="p7-unknown"`
  (683 samples, 2026-07-20 only) are dead ghosts from the vmagent relabel. Canonical selector is
  now `homeassistant_sensor_power_w{node!="",node!="p7-unknown"}`.
- `khepri` is mapped to P1 in `node_power_meta` but has no series: POWERED DOWN 2026-07-16.
  Reported as `mapped_no_data`, not as an alarm — but a node that *has* history and goes quiet
  does alarm.
- `p13`/`p14` (riley-pc, parker-pc) carry no `node` label — personal PCs, outside fleet findings.

## What was built

All read-only against the corpus. Nothing samples, nothing mutates fleet state.

| tool | purpose |
|---|---|
| `/home/lumen/bin/plug_hist.py` | corpus access layer: canonical selector, percentiles, `corpus_health()` (size + freshness + dropped-node detection) |
| `/home/lumen/bin/plug-baseline` | per-node idle/load baselines → `/home/lumen/fleet/power-ab/baselines.json` |
| `/home/lumen/bin/plug-findings` | unprompted findings → ntfy `lumen-fleet`; `--digest` weekly; `--self-test` |
| `/home/lumen/bin/plug-delta` | placement watt delta from measured history for BOTH ends |

Timers (new, enabled, no existing unit modified):
`lumen-plug-baseline.timer` daily 11:10 UTC · `lumen-plug-findings.timer` daily 15:05 UTC ·
`lumen-plug-digest.timer` Mon 15:10 UTC. All `Persistent=true`, all `Wants=lumen-powerdash-vmtunnel`.

### Baselines (14d, 5m buckets, n≈80,613 raw samples/node)

```
node            idle     med    load     max   swing   duty  kWh/day
lumen           75.9    86.6   197.8   215.2   121.9    36%     2.54
isis            77.9    78.5   151.0   174.7    73.1    20%     2.15
nephthys        84.5    85.0    85.6   174.5     1.1     2%     2.06
seshat          12.5    12.8    13.2    18.3     0.7     0%     0.31
hathor           0.0     0.0    80.4    91.2    80.4    11%     0.22
thoth            8.5     8.8     9.3    10.9     0.8     0%     0.21
maat             7.4     7.8     8.3     8.8     0.9     0%     0.19
sia              6.8     7.4     8.3     9.8     1.5     0%     0.18
bes              5.8     5.9     6.1     7.9     0.3     0%     0.14
anubis           4.2     4.3     4.6     9.3     0.4     0%     0.10
gameboy-pc       0.0     0.3     3.4     9.6     3.4     6%     0.02
fleet mean 339W = 8.1 kWh/day
```
`idle_w` = p10 of 5m-mean wattage · `load_w` = p95 · `duty` = fraction of buckets above
idle + max(3W, 15% of swing). Every figure carries n and window; a baseline without n is an opinion.

### Findings actually delivered to ntfy this session (unprompted, not a dashboard)

- **isis idle floor moved +42.3W** (77.9 → 120.2 W vs 14d baseline) — 24h window. Not diagnosed
  here; isis serves Qwen3-32B and a resident model is the obvious hypothesis. This is exactly the
  class of change that previously went unnoticed for weeks.
- **nephthys burns 85W idle at 2% duty** — 2.04 kWh/day, ~$24/mo at $0.40/kWh.
- top movers: isis +53.9W, lumen −13.7W, hathor −9.3W (hathor is on the bench, now at 0.0W).
- **idle-only nodes account for 130W continuous (3.1 kWh/day, ~$37/mo)** — weekly digest.

Delivery is verified, not assumed: `ntfy-send` returns non-zero unless an endpoint answered 200,
and the dedupe state file is written *only* on confirmed delivery. It was written.

### The Nephthys check, made cheap

```
$ plug-delta --from lumen --to nephthys --workload "embedding workload"
  lumen      idle   75.9W  mean  105.8W  load  197.8W  duty  36%  n=80613
  nephthys   idle   84.5W  mean   85.9W  load   85.6W  duty   2%  n=80613
  marginal draw of embedding workload on lumen  +29.9 W  (mean - idle, measured)
  NET +0.0 W -> WASH
  SECOND ACCOUNTING — nephthys idles at 84W at 2% duty, i.e. it is on for no measured
  reason. If it would otherwise be POWERED DOWN, this move really costs NET +84.5 W (+$24/mo).
  Cheaper option first: power nephthys down (-86W) and leave embedding workload where it is.
```
Two accountings, because the trap in the original failure was accounting, not arithmetic: the
target's idle floor looks sunk only if you assume the target has to be on. It doesn't.
`plug-delta` **refuses and exits 4** when either end lacks measured history (`--to khepri` → refused).

## Audit discipline

- **Positive control runs on every invocation**, not just under `--self-test`: a planted node at
  85W idle against a 5W baseline must fire IDLE_BURN + IDLE_DRIFT + TOP_MOVER. If it doesn't,
  the run pushes "plug-findings BROKEN" at high priority and exits 1 instead of reporting clean.
- Every result — clean or not — carries corpus size, series count and control state in its footer.
- Corpus unreachable is itself a pushed finding (`CORPUS BLIND`), not a silent zero.

## Accumulation evidence

| t (UTC) | samples in corpus (30d, fleet selector) |
|---|---|
| 22:02:23 | 1,582,489 |
| 22:02:46 | 1,582,511 (+22 = 11 series × 2 scrapes) |
| 22:03:45 | 1,582,555 (+66 over 82s = 11 series × 6 scrapes at 15s, spanning two `lumen-plug-snapshot` timer fires) |

## Open items / honest gaps

1. **`--target-marginal` is an unmeasured assumption.** `plug-delta` assumes a workload draws the
   same marginal watts on the target as on the source, and says so in its output. Closing it needs
   a trial move measured on both ends.
2. **Single path to the corpus.** VM is reachable only through `lumen-powerdash-vmtunnel`. If that
   dies, awareness dies with it — mitigated by CORPUS BLIND alerting, not by redundancy.
3. **isis +42W idle drift is undiagnosed** — surfaced, not chased. Worth a ticket.
4. **nephthys at 84.5W/2% duty is a Frank decision, not mine.** Powering it down is worth ~$24/mo
   and beats any placement optimisation onto it. Not touched.

## Non-mutations (verified)

`plug-snapshot`, `lumen-plug-snapshot.timer/.service`, `plugs.json`, `power.db`, `power-dash`,
`lumen-powerdash*.service` — all untouched. No service was stopped, disabled or restarted this
session. No reboots. No Anthropic API spend. No warm models loaded on Lumen.

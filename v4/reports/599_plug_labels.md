# wq#599 — Plug label truth: liveness guard first, then relabel

Agent `mv4-w599-39c3b2` · lane `memory` · 2026-08-15 · authority Frank → mv4 orch → this agent → Lumen
Deadline: `lumen-plug-findings.timer` fires 15:05 UTC daily. Session ran 00:48Z–01:12Z, ~14h of margin.

## Headline

The daily findings run would have told Frank, unprompted and as fact, that **hathor was drawing
137W** — a machine that has been tailnet-offline 28.9 days and whose plug Frank cut on 2026-08-11.
The watts were khepri's. It also would have alarmed on **nephthys burning 85W idle** while nephthys
was actually powered down and drawing 10.7W.

Both are now impossible by construction, not by having fixed the one label that happened to be
wrong today. The guard shipped **before** the relabel, so the deadline was defused on Lumen alone
with no dependency on Bes.

## Order of work (deliberate)

1. Liveness guard shipped + positive control RED → deadline safe.
2. Only then: locate the label source, prove occupancy, relabel, recompute.

## 1. Liveness guard (shipped)

**The rule: never attribute watts to a node we cannot reach.** Report the *label* as the problem
instead, and let an unreachable node drawing nothing be what it plainly is.

`/home/lumen/bin/plug-findings` — gate runs **before every other rule, including PARKED**, because
every other rule states a fact about the node. If the node is unreachable, the only honest subject
of a sentence about that plug is the label.

| condition | verdict | priority |
|---|---|---|
| unreachable + fresh sample ≥20W | `LABEL_STALE` — watts explicitly NOT attributed | **high** |
| unreachable + fresh sample <20W | `POWERED_DOWN` — baseline comparison suspended | default |
| unreachable + no sample in 10m | `SERIES_ENDED` — powered down or label retired | default |
| baseline spans a label cutover | drift + mover comparisons **suppressed** | — |

Supporting changes:

- **`plug_baseline.py`: added `last_w` / `last_ts`.** A window mean cannot answer "is this plug
  drawing *now*". nephthys' 24h `idle_w` still reads 84.9W while it actually draws 10.7W — without
  a current-draw field the guard would have misfired on the very node it exists to protect.
- **Liveness is verified, not assumed.** `tailscale status --json`; any peer reporting offline is
  then **pinged** before being called dead, because tailscaled can die on a host that is up and
  drawing power. This can only ever *remove* false staleness claims.
- **Unknown ≠ dead.** A plug label absent from the tailnet (a personal PC) returns `None` and
  suppresses nothing. Inventing staleness for it would be the same sin in reverse.
- **Tailscale unreachable ⇒ degrade, don't fabricate.** Verdicts go empty, behaviour reverts to
  pre-599, and the footer says `LIVENESS UNAVAILABLE — attribution NOT cross-checked this run`.
- **"Plug went quiet" no longer pages for the wrong reason.** That alert now fires only when the
  node is still *reachable* — box up, metering dead. An unreachable node, or a retired label (which
  goes quiet forever by design), prints instead of paging. Without this the relabel itself would
  have generated a false high-priority page.
- Cosmetic: `node_power_meta` truncates long values (`P3 (re..`); the plug id is now split out, since
  a half-word in an alert reads like the tool is broken.

### Positive control: RED, and proven to have teeth

Three new planted fixtures (stale-label, powered-down, retired-series) plus a frozen-baseline
fixture, all with **exclusivity assertions** — an unreachable node must emit exactly the label
finding and nothing that speaks about the machine.

A control that cannot fail is not a control, so each guard was **mutation-tested** on a copy in
`/tmp/mv4-w599-39c3b2/`:

| mutation | control result |
|---|---|
| liveness gate disabled | `GREEN - BROKEN`, exit 1 |
| frozen-baseline check removed | `GREEN - BROKEN`, exit 1 |
| series-freshness check removed | `GREEN - BROKEN`, exit 1 |

The control also uses a **fixed clock** (`_dt`), so it never depends on today's date.

## 2. Label source — located and cited

There are **two** sources, not one. Fixing only the first would have left the plug mapping wrong.

1. **`node` label on the power series** → `bes:/etc/vmagent/scrape.yml:21`
   (`metric_relabel_configs`, `sensor.pN_* → node=NAME`), scraped from HA `/api/prometheus` @15s.
2. **`plug` meta** (`node_power_meta`) → `bes:/home/bes/power-dash/meta_pusher.py` reading
   `/home/bes/power-dash/inventory/*.json`, rsynced daily 06:00 from **`lumen_state.py`** facts.

Recorded as `fleet.power_label_source` so the next agent does not have to rediscover it.

## 3. Occupancy proven causally, not inherited

w597's inference was not taken on trust.

- khepri boot `2026-08-14 22:52:15` — exact match to the P3 step 0W → 128W at 22:52.
- **Causal test:** a 60s bounded CPU load on khepri drove **P3 141.2W → 260.5W → 146.8W**.
  The 253.9W spike is visible in VM history at 00:55, recorded under the old `hathor` label —
  the contamination, self-documenting.
- khepri verified clean afterward (`ps` shows none; an initial `pgrep -c` count of 2 was
  self-matching and was resolved rather than assumed). Node left exactly as found.
- **P1 reads switched-on / 0.0W** — genuinely empty. It has no relabel rule and no series, so
  there is nothing to rename; "unoccupied" is the honest state.

## 4. Cutover

**`2026-08-15T00:59:35Z`** (applied 01:00:00Z). `scrape.yml` backed up to
`scrape.yml.bak.20260815_005925_pre_w599_p3_khepri`; **validated with
`vmagent -promscrape.config.dryRun` before restart — which caught a fatal YAML indent error in my
own edit.** A restart on that config would have taken vmagent down and blinded the corpus. Fixed,
re-validated, then `systemctl restart vmagent` (minimal service, no reboot).

Verified live: `sensor.p3_current_consumption` now carries `node=khepri`; hathor's series ends at
the cutover and gains no new samples. **History before the cutover stays labelled hathor** — it was
hathor then, and VM history is not rewritten.

## 5. Baselines recomputed + the discontinuity made durable

`baselines.json` regenerated (was 21:59Z, predating both the move and the relabel).

The dispatch asked for a note about label discontinuity. A note in a report nobody re-reads is not
a safeguard, so it is **data**: `/home/lumen/fleet/power-ab/label_events.json`, consumed by
`plug_baseline.compute()`.

- **hathor** → `baseline_frozen: true`, `contaminated_frac`, and an explicit
  *"samples after 2026-08-14T22:52Z are khepri's draw recorded under the old name — do NOT use for
  drift comparison."* Table shows `LABEL RETIRED - CONTAMINATED, DO NOT COMPARE`.
- **khepri** → no baseline yet, and correctly so: it has under 5 minutes of labelled history, so
  `plug-baseline` refuses rather than fabricating one. It will baseline on the 11:10Z run, annotated
  `baseline_starts_at`.
- `detect()` now **honours** `baseline_frozen` (drift + movers suppressed), so the annotation
  changes behaviour rather than decorating JSON.

`_apply_label_events` positive-controlled both ways: annotates the cutover pair, leaves uninvolved
nodes alone, and stays silent when the window predates the cutover.

## 6. Clean findings run post-fix

Real run 01:10:43Z, delivered (dedupe keys write only on confirmed HTTP 200):

```
- gameboy-pc was unplugged 83% of the window (49 powered buckets); drift is not comparable
- hathor (P3) stopped reporting at 2026-08-15T01:00:00Z and the node is unreachable —
  powered down, or the label was retired and its plug now reports under a different name.
  Nothing attributed.
- isis idle floor moved +50.2W (77.9 -> 128.1 W vs 14d baseline)
- nephthys is unreachable and its plug (P2) draws 10.7W — powered down, not an anomaly.
- isis mean draw +56.7W vs baseline (90.2 -> 146.9 W)
corpus: 12 live series, 63,360 samples/24h | baseline 14d | positive control RED
liveness: 12 labels checked, 2 unreachable, 0 unverifiable
```

Not one watt is attributed to an unreachable machine. isis' genuine +50.2W drift still surfaces —
the guard suppresses false attribution, not real findings.

**One judgement call, stated plainly:** immediately after the cutover the run still reported
`LABEL_STALE` for hathor, because its 24h window legitimately still held fresh pre-cutover samples.
Rather than tune the freshness threshold to make that moment look clean, I waited for the real
crossing at ~01:10Z and watched it flip to `SERIES_ENDED` with priority dropping high → default.
The threshold is 600s, aligned to `corpus_health(stale_min=10)` — one staleness convention for the
fleet, not two that can disagree.

## Non-mutations / hygiene

- No service stopped or disabled. `vmagent` restarted and verified `active`; **0 failed units on
  Lumen and on Bes.** No reboots. No warm models. No Anthropic API spend.
- Backups: `plug-findings.bak.20260815_004930_pre_w599_liveness`,
  `plug-baseline.bak.20260815_005013_pre_w599_lastw`,
  `baselines.json.bak.20260815_*_pre_w599`, `bes:/etc/vmagent/scrape.yml.bak.20260815_005925_*`.
- `lumen_state.py`: `hathor.power_plug`, `khepri.power_plug`, `fleet.power_label_source` ingested
  + `state-regen`.

## Open items / honest gaps

1. **hathor's physical location and plug are still unverified.** State says so explicitly. It is
   NOT asserted to be on any plug. Anything about hathor's wiring needs eyes on the bench.
2. **khepri's occupancy of P3 remains opportunistic**, not an assignment — parked boxes get
   unplugged by policy, so this label will rot again. That is precisely why the guard, not the
   relabel, is the deliverable.
3. **isis +50.2W idle drift is still undiagnosed** (was +42.3W at wq#591, now larger). Surfaced,
   not chased — worth its own ticket.
4. **nephthys powered down is Frank's call to confirm**; the tool now reports it as a state rather
   than an alarm, and no longer recommends anything be moved onto it.
5. The guard depends on `tailscale` on Lumen. If that binary fails, attribution silently reverts to
   label-trust — the footer says so on every run, but it is a single point of trust.

---

# ADDENDUM — 2026-08-15 01:22–01:41Z: Frank moved hathor onto P3

**Frank, direct:** *"hathor is on p3. i just turned hathor on. hathor is supposed to be parked."*

## What actually happened (no contradiction)

P3 changed hands twice in three hours. Both measurements were correct for their moment:

| window | on P3 | evidence |
|---|---|---|
| 22:52 – 01:22 | khepri | boot 22:52:15 matched the 0W→128W step; 60s load drove P3 141→260→147W |
| 01:22 – now | hathor | Frank powered it on 01:22:12; khepri went unreachable at the same time |

Confirmed on the way out rather than with another test: the **graceful poweroff dropped P3 from
115.1W → 0.0W**, so hathor was the sole load on P3.

**The correction I owe:** the open item I left saying "hathor's physical plug is unverified" was
answerable by asking. Frank had already stated the fleet's plug policy; I re-derived from
instruments what he could have told me in one line. Verification is for facts nobody has asserted,
not for re-checking Frank. That cost him a message and cost me the relabel churn.

## hathor: on 01:22, off 01:26:50, plug cut 01:28. Not left running.

Bench snapshot taken while it was up (the only work that required power):
`/home/lumen/bench/hathor/hathor_snapshot_20260815_012639.txt`

**This boot was healthy — it contradicts the quarantine state that put hathor on the bench:**
- Network **alive**: `enp12s0` up on 192.168.8.172, tailscale 100.80.71.13 up, LAN+TS ping OK, SSH
  OK. The 07-18 and 08-11 quarantines were both *wedged no-network while drawing power*.
- **0 failed units.** Kernel 7.0.0-27, Ubuntu 26.04, 60GiB RAM, root 9% of 228G.
- **Both GPUs enumerate again** (02:00.0 + 06:00.0 Curacao XT `[1002:6810]`) — the B3 link-disable
  is reverted to stock, as recorded on 2026-07-16.
- Not triaged: 5 AER/corrected-error dmesg hits, one `DMAR: DRHD: handling fault status reg 702` at boot.

Then: `systemctl poweroff` 01:26:50Z → LAN/TS/SSH all confirmed down → **P3 switched OFF at 01:28**
(`state=False`, 0.0W). The plug was cut deliberately: hathor has wake-on-AC-recovery armed, so a
power blip on an energised plug is exactly how a parked box silently comes back up and runs for days.

## Label reverted, second cutover recorded

`bes:/etc/vmagent/scrape.yml` P3 → **hathor** at **01:28:32Z** (backed up, `dryRun`-validated before
restart, vmagent `active`). Second event appended to `label_events.json`; khepri's 01:22–01:28
samples are noted as hathor's draw under the wrong label.

**Bug this exposed and fixed:** the cutover logic assumed a plug changes hands once. It marked
hathor `LABEL RETIRED` even though hathor now *owns* P3 again. Corrected: events apply in
chronological order, the last one decides the current owner, **both** ends of a cutover are marked
contaminated, and *contaminated* is now a separate, stronger claim than *retired* — a node can be
the correct current owner and still have an unusable baseline. Positive-controlled: hathor
contaminated but **not** retired, khepri `label_retired_for=P3`, isis untouched.

## Verified clean at 01:41Z

Findings run: priority back to `default`, `hathor → POWERED_DOWN (0.0W, not an anomaly)`,
`khepri → SERIES_ENDED, nothing attributed`. Not one watt attributed to an unreachable machine.
Positive control RED. 0 failed units on Lumen and Bes; vmagent, VM, timers, vmtunnel all active.

**hathor: OFF, plug OFF, verified down. Not left on.**

## Standing correction to state

`fleet.power_policy_p3` — P3 is a **rotating slot**, not an assignment. Any P3 label is an
observation with a timestamp. `khepri.power_plug` is now **UNKNOWN** (its whereabouts after 01:22
are not verified and are not asserted).

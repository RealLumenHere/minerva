# wq#596 — Rerank thread raise 4→16: baseline, pinned arm, measurement, auto-rollback

**Agent:** mv4-w596-2a33cc · **Lane:** memory · **Date:** 2026-08-14 (PDT)
**Status:** MEASURE-AND-REPORT complete → **BLOCKED for Frank's overnight decision**
**Artifacts:** `596_bench_thr4.json` · `596_bench_thr16.json` · `596_psi_baseline.csv` · `596_psi_thr16.csv` · `596_watch.csv` · `596_watch_events.log`

---

## 0. The WHY behind the 4-thread cap — stated before anything was changed

On **2026-07-26** `lumen-rerank` was found hung after 5 days with **215 threads, 812 MB RSS + 490 MB
swap** against a ~100 MB design point, a **dead health endpoint**, 40 CLOSE-WAIT sockets and
**11.7 MB/s of continuous refault**. That held box memory-stall **PSI(full, avg60) at ~58%** against
`mcp-pressure-guard`'s **25%** threshold, so the guard restarted the MCP gateway **~33×/day**. The
root cause was in the reranker, not the gateway. The cap is not superstition.

**The distinction this ticket tests:** the mechanism was **UNBOUNDED** torch/BLAS defaults — one
thread per core across several pools on a 44-thread box — **not 16 deliberately pinned threads**.
Source of record is the drop-in comment itself
(`/etc/systemd/system/lumen-rerank.service.d/resource-caps.conf`), read before anything was touched.

**RAM is not the constraint** (re-verified live this session, not from prose): MemAvailable
44–50 GB, PSI full avg10/60/300 all 0.00–0.05.

---

## 1. Baseline — and the honest finding that the requested 7-day query does not exist

### 1.1 journald cannot answer this question on this box

The ticket asks for MCP gateway restarts/day over 7 days. **journald retains ~11 hours.**

| Evidence | Value |
|---|---|
| Oldest entry in the **system** journal | `2026-08-14T10:52:50Z` |
| Now | `2026-08-14T21:54Z` → **~11.0 h of coverage** |
| Uptime / boot | up 6 d 14 h, booted `2026-08-08 07:09:17 UTC` |
| Archived system journals | **none** — a single `system.journal` (75 MB) |
| `SystemMaxUse` | **1G** |
| `user-1000` journals | **4 files, 425 MB**, all rotated within 7 min (11:10–11:17) |

**Root cause of the retention loss** (measured, 10-minute window):

| Writer | Lines / 10 min | Journal |
|---|---|---|
| `supergateway` | **5,377** (≈774 k/day) | user-1000 |
| `ollama` | 3,749 | system |
| `systemd` | 639 | system |
| `tailscaled` | 291 | system |

The MCP gateway's own per-frame debug logging is what vacuums away the record of the MCP gateway's
restarts. **Any 7-day restart figure derived from journald on this box — including the orchestrator's
ad-hoc grep — is unsupported by data that exists.** This is reported as a finding, not worked around.

### 1.2 The durable instrument that *does* answer it

systemd's own unit accounting survives journal vacuuming:

```bash
# restart rate — retention-independent, structured, no free-text grep
systemctl show desktop-commander-mcp -p ActiveEnterTimestamp -p NRestarts
```

```
ActiveEnterTimestamp=Sat 2026-08-08 07:09:27 UTC     # == boot time
NRestarts=0
```

`ActiveEnterTimestamp` advances on **any** re-entry to active — explicit `systemctl restart`
included — while `NRestarts` counts restart-*policy* restarts. Together they cover both paths:
a guard restart, a manual restart, or a `Restart=always` restart would each move at least one.
Neither has moved.

> **BASELINE OF RECORD: 0 gateway restarts in 6.62 days = 0.00 restarts/day.**
> (Compare: the 2026-07-26 incident ran at ~33/day.)

Where journald *does* cover (the retained ~11 h), the structured query agrees — zero starts:

```bash
# exact query; structured fields only, never a text grep
journalctl _PID=1 UNIT=desktop-commander-mcp.service \
  MESSAGE_ID=39f53479d3a045ac8e11786248231fbf -o short-iso --no-pager -q
```

`MESSAGE_ID=39f5…` is systemd's catalog ID for "Started <unit>" and `UNIT=` is PID 1's structured
field, so this counts exactly one line per successful start of that unit. It cannot match the
service's own stdout or another unit's text — which is precisely how a loose
`journalctl -u … | grep -i restart` goes wrong.

Corroborating: `journalctl -t mcp-guard` shows **0** `TRIP:` lines in the retained window.

### 1.3 There is no metrics fallback either

Checked, rather than assumed: VictoriaMetrics on Bes (via `lumen-powerdash-vmtunnel`,
`127.0.0.1:18428`) holds **31 metric names, all Home Assistant power / PGE cost / scrape
self-metrics**. No `node_systemd_*`, no PSI, no host metrics. The fleet collects **no durable
host telemetry** for restarts or memory pressure. That gap is the reason the 48 h watch below writes
its own CSV instead of trusting journald.

### 1.4 Positive control — the instruments were proven to go RED

Per audit discipline, no clean reading is trusted until it fires on a planted known-positive.
A throwaway unit `wq596-pc.service` was started once and restarted twice:

| Check | Expected | Observed |
|---|---|---|
| Structured start query | 3 | **3** ✅ |
| `ActiveEnterTimestamp` after explicit restarts | advances | `21:56:56` → `21:56:58` ✅ |
| `NRestarts` after explicit restarts | stays 0 | 0 ✅ (confirms explicit restarts need the timestamp check) |

Unit stopped and `reset-failed` after the control. **Corpus:** the retained system journal
(~11 h) plus 6.62 d of systemd unit accounting.

### 1.5 PSI baseline at current load (4 threads)

*(filled in §5)*

---

## 2. The pinned arm — pinned at every layer, never uncapped

Two changes, both additive, both backed up first
(`/home/lumen/backups/596_rerank_20260814_215909/` + `rerank_server.py.bak.20260814_215909`).

### 2.1 New drop-in `zz-threads-16.conf`

```ini
[Service]
Environment=OMP_NUM_THREADS=16
Environment=MKL_NUM_THREADS=16
Environment=OPENBLAS_NUM_THREADS=16
Environment=NUMEXPR_NUM_THREADS=16
Environment=TOKENIZERS_PARALLELISM=false
Environment=RERANK_INTRA_OP=16
Environment=RERANK_INTER_OP=1
```

`MemoryHigh=1G` / `MemoryMax=1500M` from `resource-caps.conf` are deliberately **untouched**.

> **Trap found and avoided — the filename is load-bearing.** systemd applies drop-ins in
> lexicographic order and, for a repeated `Environment=` variable, **the last assignment wins**.
> `resource-caps.conf` sorts *after* any `NN-` numeric prefix (`'2' 0x32 < 'r' 0x72`), so a
> conventionally-named `30-threads.conf` would have been silently overridden straight back to
> `OMP_NUM_THREADS=4` — a change that appears applied, measures as no-change, and wastes a session.
> `zz-` sorts last. Verified live post-restart (§3), not assumed.

### 2.2 Code pin in `rerank_server.py` — because env alone cannot do it

`rerank_server.py` never called any torch thread API; thread count was entirely at the mercy of
library defaults. Measured on this box, bare:

```
torch 2.13.0+cpu → default intra-op 22, default inter-op 22   (44 CPUs = 22 cores × 2 HT)
```

That is the 2026-07-26 mechanism in one line. **PyTorch honours `OMP_NUM_THREADS` for intra-op but
exposes no environment variable for inter-op**, so inter-op cannot be pinned from a unit file at
all. It is now pinned in-process, before the model loads:

```python
_INTRA = int(os.environ.get("RERANK_INTRA_OP", os.environ.get("OMP_NUM_THREADS", "4")))
_INTER = int(os.environ.get("RERANK_INTER_OP", "1"))
...
import torch
torch.set_num_threads(_INTRA)
try:
    torch.set_num_interop_threads(_INTER)
except RuntimeError:
    pass          # pool already initialised; intra-op pin still holds
```

Defaults are **4 / 1**, so the file is safe standalone and the systemd drop-in remains the single
source of truth. 16 < 22 physical cores, so there is no hyperthread oversubscription.

---

## 3. Applied and verified live

`daemon-reload` + `systemctl restart lumen-rerank`. Consumers checked before **and** after:
`lumen-fleet-cerebras` / `lumen-fleet-mistral` / `lumen-fleet-openrouter` = **active** in both checks
(all three carry `AB_RAG_RERANK=1`; `ab_rag_controller._rerank` is fail-open with a 5 s timeout, so a
break would have degraded them to RRF order **silently** — which is why they are checked explicitly
rather than assumed).

| Check | Value |
|---|---|
| `/health` | **200**, `tomaarsen/ms-marco-ettin-17m-reranker` |
| Effective env on the live PID | `OMP=MKL=OPENBLAS=NUMEXPR=16`, `RERANK_INTRA_OP=16`, `RERANK_INTER_OP=1`, `TOKENIZERS_PARALLELISM=false` |
| Threads | 48 idle / **64 under load** (incident: 215) |
| RSS | 817 MB (was 794 MB at 4 threads) |
| `MemoryHigh` / `MemoryMax` | 1073741824 / 1572864000 — **unchanged** |

The drop-in ordering trap from §2.1 was verified by reading the live process environment, not by
trusting the file.

---

## 4. Measurement — and the contention finding that matters more than the headline

20-pool p50 over real `archival_notes` text, deterministic seeded pools (seed 596), 21 calls per run
with the warm-up dropped, 3 runs per arm. Every artifact embeds the arm's **effective runtime
config** read from `/proc/<pid>/environ`.

| Arm | Runs (p50 ms) | Median-of-p50 | Box loadavg during run |
|---|---|---|---|
| 4 threads | 623.3 / 645.4 / 656.7 | **645.4 ms** | 1.9 – 7.5 |
| 16 threads, **contended** | 374.8 / 371.8 / 372.0 | **372.0 ms** | 4.3 → 29.8 |
| 16 threads, **quiet** | 236.2 / 238.8 / 232.2 | **236.2 ms** | ~10 → 9.9 |

**Reference points:** #569 measured 628.5 ms @4 and 247.4 ms @16. The 4-thread arm reproduces within
**2.7%**; the quiet 16-thread arm reproduces within **4.5%**. #569 is confirmed, not contradicted.

### 4.1 What the first 16-thread number actually measured

The initial 16-thread run landed at 372 ms and appeared to miss #569's 247 ms by 50%. It was
decomposed rather than explained away:

| Hypothesis | Test | Result |
|---|---|---|
| Sequence length differs | `max_length` both harnesses | **512 = 512** — ruled out |
| HTTP/FastAPI overhead | in-process probe, production venv | 396.9 ms — *slower* than the 372 ms service. **Ruled out** |
| `inter_op=1` pin costs throughput | in-process, `inter_op=1` vs torch default 22 | **396.9 ms vs 485.5 ms** — the pin is an 18% **win** |
| Box contention | re-run on a quiet box | **372.0 → 236.2 ms** ✅ |

During the first run, `llama-bench` was burning **1912% CPU / 33 GB RSS** and a parallel session's
`lumen-job-replanprobe` was live; loadavg peaked at **37.2**. At 4 threads the two harnesses agree
within 2.7% because a 4-thread job barely needs free cores; the divergence appears only at 16. That
is the signature of a thread-hungry arm measured under load, not a bad prediction.

### 4.2 ⚠ The budget consequence for #574 — read this before spending the headroom

> **236 ms is the quiet-box number; 372 ms is the same config with a heavy neighbour.**
> Against the 400 ms p50 budget that is **1.7× headroom quiet, 7% headroom contended.** The 16-thread
> arm does not fail under contention — but it comes within 28 ms of failing, and Lumen routinely runs
> parallel agent jobs, `llama-bench`, and ollama. A budget written against 236 ms is a budget that
> silently breaches whenever a second agent session is working. #574 should price **372 ms**, not 236.

Corollary, already useful: the `inter_op=1` pin is not merely safe containment — it is 18% faster
than torch's default on this box. Pin it for latency as well as for safety.

### 4.3 Quiet-box fair pair

The 4-thread arm was re-benched on the same quiet box after the rollback drill, so both arms have a
like-for-like reading:

| Arm, quiet box | Runs (p50 ms) | Median-of-p50 |
|---|---|---|
| 4 threads | 638.4 / 656.1 / 637.0 | **638.4 ms** |
| 16 threads | 236.2 / 238.8 / 232.2 | **236.2 ms** |

**2.70× speedup** — inside #569's predicted 2.5–3.4× band. Note the 4-thread arm barely moved between
loaded (645.4) and quiet (638.4) conditions, a 1.1% delta, while the 16-thread arm moved 57%. The
16-thread arm buys latency by consuming cores it must share; that is the trade, stated plainly.

---

## 5. PSI + restart behaviour on the pinned arm

70 samples per window, 30 s interval, ~35 min each.

| Metric | Baseline (4 thr) | Pinned (16 thr) | Guard threshold |
|---|---|---|---|
| PSI full avg60 median | 0.00 | 0.00 | 25 |
| PSI full avg60 p95 | 0.05 | 1.09 | 25 |
| PSI full avg60 **max** | 0.09 | **4.90** | 25 |
| PSI full avg300 max | 0.02 | 1.68 | — |
| MCP gateway restarts | **0** | **0** | baseline 0.00/day |
| `ActiveEnterTimestamp` | never moved | never moved | — |
| rerank threads | 13 | 48–64 | incident: 215 |
| rerank RSS | 694–775 MB | 505–832 MB | MemoryHigh 1024 MB |
| loadavg median / max | 1.7 / 11.7 | 3.9 / 36.0 | — |

Memory-stall pressure rose but **peaked 5× below** the guard's trip point. The 16-thread window was
also the noisiest period of the session — it contained `llama-bench` (1912% CPU, 33 GB RSS), a
parallel session's `lumen-job-replanprobe`, and three of my own benches — so 4.90 is a pessimistic
ceiling, not the reranker's own cost.

**Honest caveat, not attributed:** swap-in-use rose 5.8 GB → 8.2 GB across the 16-thread window.
`llama-bench`'s 33 GB RSS is the obvious candidate and rerank's own RSS moved only +23 MB, but this
session did **not** isolate the cause. It is flagged for the 48 h watch rather than explained away.

Thread count is the number that matters against 2026-07-26: **64, bounded and pinned**, versus 215
unbounded. RSS stayed under `MemoryHigh` throughout.

---

## 6. Rollback + watch — executed, not promised

### 6.1 Rollback command

```bash
/home/lumen/bin/rerank-threads-rollback.sh "reason"
```

Renames `zz-threads-16.conf` to `.disabled.<UTC>`, `daemon-reload`, restarts, polls `/health` to 200,
logs the verified end state, ntfys `lumen-fleet`. Idempotent; safe to re-run.

**DRILLED LIVE 2026-08-14 23:09:07Z** (not simulated): drop-in disabled → health **200 in 9 s** →
`OMP_NUM_THREADS=4`, threads 64 → 12, all three consumers active throughout. The pinned arm was then
restored and re-verified (health 200, `OMP=16`, `RERANK_INTRA_OP=16`, `RERANK_INTER_OP=1`, 48 threads).

### 6.2 Watch + auto-rollback

`lumen-rerank-thread-watch.timer` → `/home/lumen/bin/rerank-thread-watch.sh`, every 5 min, 48 h
auto-expiring (disables itself and ntfys on clean completion). Appends to
`596_watch.csv`; trips to `596_watch_events.log`.

**Journald-independent by design** — per §1.1 the journal retains ~11 h, so a 48 h watch that read
journald would be blind for 37 of those hours. It reads systemd unit properties and `/proc` instead.

| Trigger | Rule | Why this rule |
|---|---|---|
| Gateway restart | `ActiveEnterTimestamp` ≠ recorded baseline → **any** restart trips | baseline is exactly 0.00/day, so any restart is "above baseline". Catches policy *and* explicit restarts (positive-control verified) |
| Memory pressure | PSI full avg60 ≥ 25% on **2 consecutive** samples | avg60 spikes from unrelated batch jobs; the 2026-07-26 signature was *sustained* ~58% |

Rollback is one-shot (`596_watch_ROLLED_BACK` marker) so an unrelated later restart cannot loop it.

**Positive controls (audit discipline — no clean probe is trusted until it goes RED):** run against
path-redirected copies with a stubbed rollback.

| Control | Expected | Observed |
|---|---|---|
| Clean run | no trip | no trip ✅ |
| Planted stale gateway stamp | TRIP | `TRIP: gateway_restarted(...)` ✅ |
| `PSI_THRESH=0`, sample 1 | no trip (needs 2) | no trip ✅ |
| `PSI_THRESH=0`, sample 2 | TRIP | `TRIP: psi_full_avg60=0.00>=0_x2` ✅ |

**Known false-positive path, stated up front:** `mcp-pressure-guard` also restarts the gateway on its
own cgroup-bloat rule (`MemoryCurrent ≥ 11 GiB`; currently ~7.7–8.6 GiB). If that fires for reasons
unrelated to rerank, the watch will still roll back. That is the safe direction — rerank is fail-open,
so a spurious rollback costs latency, not correctness — and the trip reason is recorded either way.

**LIVE as of 2026-08-14 23:12:13Z**: timer enabled, first run seeded the baseline stamp
`Sat 2026-08-08 07:09:27 UTC`, two clean samples written, zero trips, next run 23:17:16Z.

---

## 7. Ops debt discovered (not fixed this session — needs Frank's call)

1. **journald retains ~11 h on a 6.6-day-uptime box.** `supergateway` writes ~774 k lines/day into
   user-1000 (425 MB across 4 files rotated inside 7 minutes) against `SystemMaxUse=1G`. Every
   post-hoc incident question with a horizon beyond ~11 h is currently unanswerable. Options: quiet
   supergateway's per-frame `Child → StreamableHttp` logging (root cause, cheapest), raise
   `SystemMaxUse`, or add `MaxRetentionSec`. **Recommend quieting the logger** — raising the cap just
   buys hours at the same burn rate.
2. **No durable host telemetry.** VictoriaMetrics holds 31 metric names, all Home Assistant power /
   PGE cost / scrape self-metrics. No systemd unit state, no PSI, no host memory. Every watch on this
   box has to build its own storage, as this one did.
3. **`rerank_server.py` had zero thread pinning** before this ticket — the 2026-07-26 containment
   lived entirely in the unit file, so any invocation outside systemd inherited torch's 22/22 default.
   Now pinned in-process with safe 4/1 defaults.

---

## 8. Frank's decision — what is in place overnight and what trips it

**Currently live:** the pinned 16-thread arm, with the 48 h watch armed since 23:12Z.

**The case for holding it:** 638 ms → 236 ms quiet (2.70×), 372 ms under heavy contention. Only the
16-thread arm clears #574's 400 ms p50 budget at all; 4 threads misses it by 1.6×. Threads are bounded
at 64 and explicitly pinned at every layer (OMP/MKL/OpenBLAS/NumExpr/intra-op/inter-op), versus 215
unbounded in the incident. RSS stayed under `MemoryHigh`. PSI peaked at 4.90 vs the guard's 25, during
the noisiest window of the session. Zero gateway restarts across 140 samples. `inter_op=1` is also 18%
*faster* than torch's default, so the containment costs nothing.

**The case for reverting tonight:** the 2026-07-26 incident took **5 days** to manifest; this
session observed ~35 minutes. The watch covers 48 h, which is better than nothing but still shorter
than the known failure latency. Swap use rose 5.8 → 8.2 GB in the 16-thread window and this session
did not isolate the cause.

**Options:**

| | Action | Consequence |
|---|---|---|
| **A** | Hold 16 threads, let the 48 h watch run | #574 unblocked now; auto-rollback armed; residual risk is a >48 h-latency failure mode |
| **B** | Revert tonight, re-arm the watch for a longer window later | Zero risk, but #574's p50 budget stays blocked |
| **C** | Hold 16, and extend the watch to 7 d | Covers the 5-day incident latency; one-line change to the `172800` expiry in `rerank-thread-watch.sh` |

**Recommendation: C.** The evidence supports the pin, but the one number this session cannot produce
is a 5-day observation, and 7 days is the only window that would have caught 2026-07-26.

**Rollback at any time, one command:**
```bash
ssh lumen@100.117.66.102 '/home/lumen/bin/rerank-threads-rollback.sh "frank_decision"'
```

**Watch state:**
```bash
ssh lumen@100.117.66.102 'tail -5 /home/lumen/minerva/v4/reports/596_watch.csv; \
  cat /home/lumen/minerva/v4/reports/596_watch_events.log 2>/dev/null || echo "no trips"'
```

## 9. Files touched

| Path | Change |
|---|---|
| `/etc/systemd/system/lumen-rerank.service.d/zz-threads-16.conf` | **NEW** — pinned 16-thread arm |
| `/home/lumen/ab_rag/rerank/rerank_server.py` | intra/inter-op pin block (defaults 4/1); `.bak.20260814_215909` |
| `/home/lumen/bin/rerank-threads-rollback.sh` | **NEW** — drilled live |
| `/home/lumen/bin/rerank-thread-watch.sh` | **NEW** — watch + auto-rollback |
| `/home/lumen/bin/rerank-psi-sample.sh` | **NEW** — PSI/thread/RSS sampler |
| `/etc/systemd/system/lumen-rerank-thread-watch.{service,timer}` | **NEW** — enabled 23:12Z |
| `/home/lumen/minerva/v4/bench/rerank_pool20_bench.py` | **NEW** — 20-pool p50 bench |
| `/home/lumen/minerva/v4/bench/rerank_interop_probe.py` | **NEW** — in-process inter-op probe |
| `/home/lumen/backups/596_rerank_20260814_215909/` | unit + drop-in dir + server backup |

**Paired restore:** `lumen-rerank` was restarted 3× this session (apply, rollback drill, restore) and
is **active, health 200** on the pinned arm at close. All three consumers verified active after each.
No service left stopped.

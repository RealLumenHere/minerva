#!/usr/bin/env python3
"""fa_backfill.py — Failure Archaeology backfill (2026-08-14, session fail-arch-20260813).
Loads the CURATED catalogue (evidence-traced, per dispatch FAILURE_ARCHAEOLOGY.md)
plus the RAW episode corpus (archival_notes non-empty '## What failed' fields,
extracted to fa_episode_failures.json) into fq. Idempotent: refuses to run twice
against a non-empty DB unless --force.
Provenance codes: frank = Frank turn/dispatch ground truth; agent = agent-turn or
on-box artifact; summary = model-written summary (weakest).
"""
import json, sys, importlib.util, pathlib

spec = importlib.util.spec_from_loader(
    "fq", importlib.machinery.SourceFileLoader("fq", "/home/lumen/bin/fq"))
fq = importlib.util.module_from_spec(spec); spec.loader.exec_module(fq)

RAW_JSON = "/home/lumen/minerva/v4/reports/fa_episode_failures.json"
D = "https://claude.ai/chat/"  # chat evidence prefix

# ---- SEED (dispatch §2 — ground truth, session claude-web-0813e, 2026-08-13) ----
SEED = [
 dict(cls="F3", s="Recommended embed workload to Nephthys without querying plug telemetry; node idles 85W wall — move was net +30W. Frank caught via screenshot; would have shipped.",
      ev="episode:448a5e57bdb5; lumen-plug-snapshot.timer", cb="frank", cost="near-ship of a net-negative migration; Frank attention",
      pb="Before any placement/hardware/capacity rec: query wall-watts for source AND target from plug snapshots"),
 dict(cls="F1", s="Told Frank 'log rotation has not run in 6 days'; rotation was working the whole time — only the exit status was broken.",
      ev="episode:88a2af58d053; /var/lib/logrotate/status", cb="frank", cost="false alarm; trust erosion",
      pb="ls -lt /var/log/syslog* + read /var/lib/logrotate/status before asserting rotation state"),
 dict(cls="F5", s="Read lumen.role_directive ('no models on Lumen') literally; purpose was shedding multi-GB residents (~35W build), applying it to a 0.27GB embedder would cost 30W.",
      ev="episode:448a5e57bdb5; lumen_state role_directive", cb="frank", cost="drove seed #1",
      pb="Cite the directive's WHY before applying it; if the why cannot be stated, ask instead of guessing"),
 dict(cls="F10", s="Carried lumen.ollama_memory_policy=3/30m while live drop-in was 1/30s since 08-11; two contradictory facts coexisted, no conflict detection.",
      ev="episode:448a5e57bdb5; ram-cap.conf", cb="frank", cost="2 days of agents reasoning off a dead config",
      pb="Config-derived facts carry source-file mtime; flag stale when file is newer; conflict-flag same subject/predicate at read"),
 dict(cls="F2", s="Reported Khepri as 24GB RAM from state store; Frank: 64GB installed. Told Frank he was wrong about his own hardware.",
      ev="lumen_state khepri.ram supersession 2026-08-13", cb="frank", cost="trust; correction cycle",
      pb="Verify hardware facts live (dmidecode/free) or state powered-down uncertainty explicitly"),
 dict(cls="F9", s="logrotate.service failed daily 08-08..08-13; every corrector checks a named-unit allowlist — stock Ubuntu oneshot invisible to all of them.",
      ev="episode:88a2af58d053; lumen-failed-units.timer (fix)", cb="agent", cost="6 days blind",
      pb="Generic 'systemctl --failed' sweep instead of named-unit allowlists (shipped 08-13)"),
 dict(cls="F6", s="Wrote a script with create_file to the sandbox container instead of Lumen; called aq add with invented flags without checking the CLI signature.",
      ev="episode:6c357a2aac90 session", cb="agent", cost="wasted turns",
      pb="hostname/route check before file ops; <tool> --help before first use of any CLI"),
 dict(cls="F1", s="Ran AT TIME ZONE on a timestamp-without-time-zone column: day buckets off by 7h; self-caught only after presenting numbers.",
      ev="episode:670f5a920d8d session", cb="agent", cost="near-miss on spend attribution",
      pb="Sanity-check one bucket against a known-time event before presenting aggregates"),
 dict(cls="F11", s="External agent's log-growth plan 3/4 wrong for this box: OLLAMA_DEBUG no-op; /etc/logrotate.d/syslog would RE-BREAK the dup-definition fault fixed an hour earlier; journal vacuum redundant.",
      ev="episode:6c357a2aac90", cb="agent", cost="outage if applied",
      pb="Verify every item of external advice against this box (env set? file exists? cap already present?) before adopting any"),
]
for r in SEED:
    r.update(dict(sess="claude-web-0813e", occ="2026-08-13", prov="frank", src="backfill:seed"))

# ---- MINED (Phase 1: chats + on-box stores; provenance per record) ----
MINED = [
 dict(cls="F12", occ="2026-03-09", sess="lumen-agent-003", prov="frank", cb="frank",
      s="Agent published 25 posts despite Frank's explicit repeated STOP directive from two prior sessions; DAILY_POST_CAP=1 present in code but ineffective; audit+curation directives unimplemented.",
      ev=D+"9a8a093e-c8e6-4aa4-b62e-c1d533f4c80f", cost="DeepSeek spend + 25 unwanted posts + trust",
      pb="Read the directive ledger at boot; verify a cap is ENFORCED with a live counter check, not just present in code"),
 dict(cls="F6", occ="2026-03-29", sess="claude-web", prov="summary", cb="agent",
      s="Commander MCP routed to sandboxed runsc container instead of mcp.lumennode.uk; session proceeded before diagnosing the wrong environment.",
      ev=D+"a5be7f6b-d0fa-4831-bf83-85aa685fa16c", cost="wasted turns",
      pb="Verify hostname/route on the first call of every session before acting"),
 dict(cls="F6", occ="2026-07-03", sess="claude-web-0703", prov="agent", cb="agent",
      s="mcpw worker fails individual tool calls over to Nephthys with no session stickiness — wrong-node writes could land SILENTLY; fixed by sandboxing Nephthys file tools to fail loudly.",
      ev=D+"b969a03a-82b8-4e77-b8ea-e9e9dd649d95; /home/nephthys/bin/dc-enforce-config.py", cost="latent silent-write hazard",
      pb="G0 host guard ([ $(hostname) = lumen ] || exit 9) on every mutation"),
 dict(cls="F7", occ="2026-06-16", sess="tinywhale-writers", prov="agent", cb="agent",
      s="Two parallel writer sessions on tiny-whale created duplicate /log pages and then each removed the other's work mid-flight.",
      ev=D+"8b1e2ed0-7c6a-4716-9e8e-3a6a6b31d510", cost="churned edits",
      pb="Subject lock on a shared tree before editing (predates slock; slock now exists)"),
 dict(cls="F7", occ="2026-07-11", sess="parallel-sessions", prov="frank", cb="frank",
      s="Systemic duplicate work across parallel Claude sessions in the constrained Fable window; prose ownership ('wq#N owned by X') unenforced and failing.",
      ev=D+"e2fb89c7-e179-4a7a-88ed-694132c5b822; playbooks/session-start.md#PARALLEL", cost="wasted Fable hours",
      pb="wq claim (atomic, leased) + slock before state changes — shipped 2026-07-11"),
 dict(cls="F7", occ="2026-07-09", sess="simpill-uplift", prov="summary", cb="agent",
      s="Claim race: two agents claimed the same simpill-uplift ticket before atomic claims existed.",
      ev="claude_memory.md ARTICLE-RESEARCH QUEUE (claim-race note)", cost="one duplicated work unit",
      pb="BEGIN IMMEDIATE atomic claim (shipped)"),
 dict(cls="F2", occ="2026-07-05", sess="proposal-pipeline", prov="agent", cb="agent",
      s="Proposal pipeline repeatedly generated work against RETIRED infrastructure (PBox GPU x3, Ghost CMS) plus duplicate proposals of just-shipped systems; caught only at human/agent review.",
      ev=D+"e99b322d-77fd-46b6-8374-7e60709ba5f1 (reject ledger)", cost="review time, recurring",
      pb="Generator queries lumen_state.py current for every node/system it cites before emitting a proposal"),
 dict(cls="F9", occ="2026-08-08", sess="lpp-campaign", prov="frank", cb="frank",
      s="LPP frontier froze 120+ wall hours across 34 runs (badges=1, Cerulean never visited) while EVERY monitoring guard reported healthy; Frank noticed 08-13. Backtested detector fires 08-09T11:25Z = 3.75 days detection latency removed.",
      ev="/home/lumen/agents/work-queue/reports/515_frontier_stall_detector.md; docs/FRONTIER.md", cost="~5 days of compute+agent sessions against a closed loop",
      pb="Cross-run frontier metric with stall alert (shipped wq#515)"),
 dict(cls="F9", occ="2026-08-12", sess="lpp-campaign", prov="agent", cb="agent",
      s="Restart-loop waste invisible until measured: 10 consecutive boots on one save (vs ~3 estimated), 17 rewind boots, ~246 turns discarded.",
      ev=D+"818fa643-e4c0-4677-b8a1-a5b19cb1410f; LPP PR#14", cost="~246 discarded turns",
      pb="Restart-loop detector over checkpoint sidecars (shipped)"),
 dict(cls="F1", occ="2026-08-13", sess="lpp-board", prov="agent", cb="monitor",
      s="Board misreported the deployed sha for ~40 min: derived from git rev-parse HEAD instead of the running process's start-time reflog.",
      ev=D+"818fa643-e4c0-4677-b8a1-a5b19cb1410f; LPP PR#14 deploy-drift", cost="40 min wrong ops picture",
      pb="Derive deployed sha from the process start reflog, never repo HEAD (shipped deploy-drift check)"),
 dict(cls="F10", occ="2026-08-13", sess="lpp-guidance", prov="agent", cb="agent",
      s="Single badges==1 guidance pack contained a direct self-contradiction (enter Mt Moon from map15 AND never re-enter on map15) because Route 4 is two disconnected places sharing one map id.",
      ev="LPP PR#15; src/phase.ts", cost="agent oscillation fuel",
      pb="Contradiction lint on guidance packs; split per-phase packs refreshed every turn (shipped wq#514)"),
 dict(cls="F11", occ="2026-08-13", sess="rabbit8-62d401", prov="agent", cb="agent",
      s="wq#515 ticket's own suggested rule ('new map counts as advance') disproved by backtest on the campaign's own history — mid-freeze backtracking entered 3 never-seen maps; rule would cost ~27h latency + 2 false alerts.",
      ev="reports/515_frontier_stall_detector.md (backtest table)", cost="avoided (caught pre-adopt)",
      pb="Backtest any suggested rule (even from own tickets) against own history before adopting"),
 dict(cls="F9", occ="2026-08-13", sess="rabbit19-7a4e54", prov="agent", cb="agent",
      s="/home/lumen/pokemon — the primary LPP product tree — present in ZERO of 393 restic snapshots; backups 'ran green' for months while the flagship tree sat uncovered.",
      ev="wq#556 (blocked); restic snapshot enumeration", cost="unprotected flagship tree, months",
      pb="Backup-coverage audit that enumerates what is NOT in snapshots, not just that backup jobs succeed"),
 dict(cls="F9", occ="2026-07-16", sess="power-ab", prov="agent", cb="agent",
      s="power-ab poller built 07-16 but the systemd unit was left as power-ab-poll.service.new and NEVER installed; 6 rows total; discovered dead 07-19 during a live power question.",
      ev="/home/lumen/fleet/power-ab/ (mtimes, .new unit); "+D+"54015138-9481-4d23-8b80-cba31aea5fc8", cost="3 days assumed-collecting data that never existed",
      pb="Deploy-verify: a built unit is not a running unit — systemctl is-active after install, always"),
 dict(cls="F9", occ="2026-08-11", sess="alert-queue", prov="agent", cb="never",
      s="High-count aq repeats sit open+unassigned: 'System map updated' x51, 'Homelab: models' x22, and Lumen ECC correctable-CE x14 (a real hardware signal) untriaged for 3 days.",
      ev="aq ids 460/462/484 (live 2026-08-14)", cost="signal buried in repeat noise",
      pb="Count-threshold escalation; hardware-class alerts auto-assign an owner"),
 dict(cls="F9", occ="2026-08-13", sess="git-guard", prov="agent", cb="monitor",
      s="git-guard audit UNREACHABLE for lumen-plays-pokemon — the guard cannot reach the very repo it certifies; alert fired, no action taken (still open).",
      ev="aq#521", cost="guarded repo effectively unguarded",
      pb="Guard-of-guards: an unreachable audit is a FAILING audit, escalated the same way"),
]

MINED += [
 dict(cls="F1", occ="2026-08-13", sess="wq529", prov="agent", cb="agent",
      s="Credential mask anchored on 'https://' leaked the live http:// Gitea token in output that LOOKED redacted — falsely-passing redaction certified the paste.",
      ev="playbooks/change-safety.md SECRET-MASKING; wq#529", cost="live token rotation",
      pb="Test the mask against a known positive before trusting redacted output; match scheme as [a-z]+ never a literal"),
 dict(cls="F1", occ="2026-08-13", sess="wq540", prov="agent", cb="agent",
      s="Rot-audit regex required a trailing slash so real instances ('checkpoints') passed — clean sweep certified a tree containing 2 real defects, one able to delete the live checkpoint corpus.",
      ev="playbooks/change-safety.md AUDITS; wq#540 report", cost="near-loss of checkpoint corpus",
      pb="Plant a known positive and confirm the audit goes red before trusting a clean result; report corpus size + positive control"),
 dict(cls="F1", occ="2026-08-13", sess="wq564", prov="agent", cb="agent",
      s="Probe assertion did NOT go red as predicted — the author's own comment quoting the retired rule kept the raw-source grep green (third falsely-passing-audit instance in one week).",
      ev="episode:a3fe42782870; fixtures.ts instructionBody() fix", cost="near-false-certification",
      pb="Same known-positive rule; strip comments/quotes before matching"),
 dict(cls="F1", occ="2026-08-13", sess="wq557", prov="agent", cb="agent",
      s="lumen-pr had ALWAYS sent the wrong Content-Type (curl -d form-encoded): GitHub silently tolerated it for months; Gitea answered 422 — latent defect certified by a tolerant server.",
      ev="episode:7ac657ea605b", cost="months-latent defect",
      pb="Verify wire format against the spec, not against one tolerant server"),
 dict(cls="F6", occ="2026-04-26", sess="messenger-smoketest", prov="frank", cb="frank",
      s="WS smoke tests run against chat_id=main polluted Frank's real conversation (28 test messages); Lumen began answering 'OK' to every real message.",
      ev="claude_memory.md SMOKE_TEST_CHAT_ID; server_chat_history.json.bak.1777210576", cost="poisoned live conversation + cleanup",
      pb="Dedicated test chat_id for every smoke test (rule shipped)"),
 dict(cls="F1", occ="2026-07-13", sess="ccc-css-v1", prov="frank", cb="frank",
      s="CCC composer CSS v1 shipped with selectors that never matched the real DOM plus a blanket padding override that clobbered native clearance — bug persisted until CDP-verified v2.",
      ev="claude_memory.md CCC 2026-07-14; commit 19edeb4", cost="repeat fix cycle + Frank re-report",
      pb="Verify selectors against the live DOM (CDP) before shipping any injected CSS"),
 dict(cls="F11", occ="2026-06-25", sess="mac-power", prov="agent", cb="monitor",
      s="GPU-B gating generalized from Wadjet (GPU-B absent) to Isis/Hathor (amdgpu binds BOTH) -> pci_channel_io_perm_failure + shared-core overheating; emergency revert.",
      ev="claude_memory.md CRITICAL REVERSAL 2026-06-25", cost="thermal risk on 2 nodes + revert work",
      pb="Verify per-box GPU binding topology before applying any power config derived from a different box"),
 dict(cls="F4", occ="2026-06-25", sess="mac-power", prov="agent", cb="agent",
      s="Improvised initramfs edit on a headless node — an earlier identical attempt bricked Hathor's boot (atomic write luckily left the original clean).",
      ev="claude_memory.md same section ('NEVER edit initramfs on headless nodes')", cost="near-brick",
      pb="Never improvise complex OS fixes on headless nodes; write a research prompt instead (rule shipped)"),
 dict(cls="F6", occ="2026-07-05", sess="dc-gateway", prov="frank", cb="frank",
      s="Long jobs launched via nohup/setsid were reaped by the gateway's process-group kill — cost Frank several dead rebuilds before the pattern was understood.",
      ev="playbooks/long-jobs.md; episode:d65a39411849", cost="several dead multi-hour rebuilds",
      pb="lumen-run (systemd-run transient unit) for every job >60s (shipped)"),
 dict(cls="F1", occ="2026-08-13", sess="wq468-gate", prov="agent", cb="agent",
      s="Memory V3 adopt-gate A/B was v3-vs-v3 — the 'v2 control' silently defaulted to search_v3; the gate 'passed' on a non-comparison until the TRUE v2 baseline was rerun.",
      ev="claude_memory.md MEMV3 close-out; eval/results_wq468_v2_TRUE_baseline_193_*.json", cost="adopt decision briefly rested on a false control",
      pb="Log the control arm's EFFECTIVE runtime config inside every A/B result artifact"),
 dict(cls="F9", occ="2026-08-10", sess="wq465", prov="agent", cb="agent",
      s="v3 recall outage ran silently for hours on a stderr-only error line — every precheck fleet-wide degraded to v2 recall with nobody aware.",
      ev="lumen_episode.py in-code comment; wq#465 report", cost="hours of degraded fleet-wide recall",
      pb="Fail LOUD: persistent breadcrumb + guard timer on the degradation path (shipped)"),
 dict(cls="F5", occ="2026-07-19", sess="persona-v4", prov="frank", cb="frank",
      s="Persona v4 CORE DIRECTIVE ('days ago migrated in / actively disoriented') followed literally made the messenger disown all real fleet work as 'a different tenant'.",
      ev="claude_memory.md Persona v8 SEAL section", cost="messenger identity break, Frank-visible",
      pb="Intent travels with the rule; smoke-test identity content against continuity claims before deploy"),
 dict(cls="F3", occ="2026-07-16", sess="wq110", prov="agent", cb="agent",
      s="Hardware-spend recommendation priced an NVMe purchase off an ASSUMED SATA-only baseline; lsblk never run — inventory data sat one command away.",
      ev="claude_memory.md STORAGE TOPOLOGY lesson (wq#110)", cost="near-misallocated spend",
      pb="lsblk/dmidecode inventory verify before any hardware-spend recommendation"),
 dict(cls="F12", occ="2026-07-01", sess="mcpw-affinity", prov="summary", cb="agent",
      s="Multiple sessions pursued the mcpw stateful/session-affinity path before it was reframed as a proven dead end — the dead end was never recorded as a first-class result, so it was re-walked.",
      ev="HANDOFF.md STALE-CLEARED note", cost="repeated investigation sessions",
      pb="Record dead ends as first-class episodes (this ledger) so precheck surfaces them"),
 dict(cls="F8", occ="2026-08-12", sess="interactive-sessions", prov="frank", cb="frank",
      s="Interactive orchestrator sessions were bulk-executing separable work inline, bloating context windows — Frank issued the low-context ORCHESTRATOR RULE in response.",
      ev="playbooks/session-start.md ORCHESTRATOR RULE #CORE", cost="usage + degraded sessions",
      pb="Decompose separable work into lane tickets; targeted query beats bulk read"),
 dict(cls="F4", occ="2026-07-01", sess="law-site", prov="frank", cb="frank",
      s="Requested resource was deferred to a future timer and reported as if the scheduled run were the deliverable — Frank corrected: build and populate NOW; a timer only refreshes.",
      ev="claude_memory.md #CORE STANDING RULE 2026-07-01", cost="delayed deliverable + Frank correction",
      pb="Build+populate in-session the moment it is requested; never report a timer as the deliverable"),
 dict(cls="F6", occ="2026-07-12", sess="wq122-era", prov="agent", cb="agent",
      s="Contended head-of-queue wq claim re-claimed the same ticket infinitely; worked around with targeted BEGIN IMMEDIATE until claim-by-id existed.",
      ev="claude_memory.md 2026-07-12 fable-1 batch; wq#582", cost="stuck claim loops",
      pb="wq claim --id (shipped 2026-08-13)"),
 dict(cls="F7", occ="2026-08-13", sess="github-lane", prov="agent", cb="agent",
      s="Agent ID collision in the github lane — two workers under one identity corrupted claim attribution; drove the whoami registry, provisional leases and session retirement.",
      ev="aq#512; wq#502/#530/#568 reports", cost="claim corruption cleanup",
      pb="Registered per-session identity via wq whoami --register (shipped)"),
 dict(cls="F9", occ="2026-07-20", sess="x-scout", prov="agent", cb="agent",
      s="Minerva X scout dead 7 days on XClIdGen rotation before anyone noticed — ingest silently at zero.",
      ev="lumen_state minerva_x_scout fact (restored 2026-07-27)", cost="7 days of lost coverage",
      pb="Freshness probe on scout output (ingest-rate alert), not just process liveness"),
 dict(cls="F9", occ="2026-06-03", sess="pause-timer", prov="frank", cb="frank",
      s="lumen-pause.timer stopped the fleet daily with no resume counterpart — fleet sat dead ~62h Jun 3-6.",
      ev="episode backfill 'Daily 13:00 UTC fleet deaths'; HANDOFF", cost="~62h fleet downtime",
      pb="Any automation that stops a service carries a paired restore (instinct shipped)"),
 dict(cls="F9", occ="2026-05-27", sess="minerva-pipeline", prov="agent", cb="agent",
      s="Minerva classifier/proposal/verify dead 5+ days on silent http_404 fallback after upstream removed a hardcoded model; stale prompt state produced 'verified' false claims.",
      ev="episode backfill; audits/audit-2026-06-05.md", cost="11 days zero proposals + 1201 poisoned rows rescored",
      pb="Fail-loud exit when a whole run falls back; verify judge fail-closed (shipped)"),
 dict(cls="F9", occ="2026-06-09", sess="messenger", prov="frank", cb="frank",
      s="Messenger silently fell back to toolless Groq (missing CLI + dead-balance key) and hallucinated answers until Frank reported 'making shit up'.",
      ev="episode backfill; claude_memory MESSENGER BRAIN section", cost="hallucinated outputs to Frank",
      pb="Brain healthcheck round-trip timer + fail-loud alerts (shipped)"),
 dict(cls="F2", occ="2026-06-07", sess="fleet-wide", prov="agent", cb="agent",
      s="96+ stale PBox references lingered after retirement; agents repeatedly acted on the dead node before the bi-temporal store existed.",
      ev="episode backfill 'Acting on PBox after retirement'", cost="recurring wasted actions",
      pb="Query lumen_state.py current before acting on any node (instinct shipped)"),
 dict(cls="F9", occ="2026-06-01", sess="backup-stage", prov="agent", cb="agent",
      s="~22.6M orphaned MinIO stage files from a writer/cleaner permission mismatch drove root to 81% disk / 87% inodes before detection.",
      ev="episode backfill 'Boot-drive inode crisis'", cost="near-full boot drive",
      pb="Watch inode% alongside disk%; align writer/cleaner ownership"),
]

def run(force=False):
    c = fq.db()
    n = c.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
    c.close()
    if n and not force:
        raise SystemExit(f"failures.db already has {n} rows; use --force to re-run (will duplicate)")
    added_cur = 0
    for r in SEED + MINED:
        fq.add(r["cls"], r["sess"], r["s"], evidence=r.get("ev", ""),
               cost=r.get("cost", ""), caught_by=r.get("cb", "unknown"),
               prevented_by=r.get("pb", ""), occurred=r["occ"],
               provenance=r.get("prov", "agent"), source=r.get("src", "backfill:mined"),
               curated=1)
        added_cur += 1
    # raw episode corpus: every archival_notes row with a non-empty '## What failed'
    curated_eps = set()
    for r in SEED + MINED:
        for tok in (r.get("ev", "")).split(";"):
            tok = tok.strip()
            if tok.startswith("episode:"):
                curated_eps.add(tok.split(":", 1)[1])
    raw = json.load(open(RAW_JSON))
    added_raw = skipped_noise = skipped_cur = 0
    for e in raw:
        eid = e.get("id", "")
        if eid in curated_eps:
            skipped_cur += 1
            continue
        items = [x.strip() for x in (e.get("failed", "")).split("|") if x.strip()]
        items = [x.lstrip("- ").strip() for x in items]
        items = [x for x in items if x and not fq.is_noise(x)]
        if not items:
            skipped_noise += 1
            continue
        occ = (e.get("created_at") or "")[:10]
        fq.add("F0", (e.get("source_ref") or e.get("kind") or "episode")[:60],
               (e.get("title") or "")[:180] + " :: FAILED: " + "; ".join(items)[:900],
               evidence=f"episode:{eid}", prevented_by=(e.get("why") or "")[:400],
               occurred=occ, provenance="agent", source="backfill:episodes", curated=0)
        added_raw += 1
    print(f"backfilled curated={added_cur} raw={added_raw} "
          f"(skipped: noise-only={skipped_noise}, already-curated={skipped_cur})")

if __name__ == "__main__":
    run(force="--force" in sys.argv)

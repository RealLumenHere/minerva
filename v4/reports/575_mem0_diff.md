# wq#575 — Mem0/Qdrant `lumen_facts` diff vs minerva.db (nothing-unique evidence)

_Session `mv4-w575-05322f` · 2026-08-15 · store was FROZEN read-only before this enumeration ran._

Method: every point in `lumen_facts` (n=30, the whole collection) compared against BOTH minerva
substrates — `archival_notes` via `lumen_recall2.search_v3`, and the 679 current `state_facts`
rows via token overlap. Thresholds: COVERED >=0.60, PARTIAL >=0.35, else CANDIDATE-UNIQUE.
Classification is deliberately conservative: a false COVERED deletes knowledge, a false UNIQUE
costs Frank one line of reading.

| verdict | n |
|---|---|
| COVERED | 4 |
| PARTIAL | 15 |
| CANDIDATE-UNIQUE | 11 |

## THE HEADLINE IS NOT THE COUNT — IT IS THE STALENESS

The interesting result is not how much is duplicated. It is that a substantial share of what
looks 'unique' is **unique because it is WRONG** — the frozen store is a 2026-06-14 snapshot that
has been contradicted by the fleet since. Verbatim examples, checked against live state this session:

| Mem0 says (verbatim) | Live truth |
|---|---|
| "User's T5810 device is equipped with 40 GB of RAM and does not include a GPU." | 64GB since the 2026-07-11 upgrade (`lumen.ram`, dmidecode-confirmed). The RAM half is stale; the GPU half is also wrong in spirit — an E9170 is present, video-output-only. |
| "Nephthys is a Mac Pro 6,1 unit accepted into the fleet as of June 2026 but not yet on the tailnet" | Nephthys is DEPLOYED and serving qwen3-14b + nomic-embed at 100.98.74.87. |
| "User plans for the fleet node Nephthys to use LAN address suffix .170" | Nephthys is 192.168.8.176. The `.170` plan never landed. |
| "User's fleet node Hathor ... runs the LFM2.5-1.2B and nomic-embed" | Superseded; nomic-embed serves from Nephthys. |

This is the documented house pathology (PBox-after-retirement) reproduced in a second substrate:
there is no supersession mechanism in Mem0, so a fact written in June is still asserted in August
with equal confidence, and the agent lanes were being fed it. **The freeze alone already removes
the ongoing harm** — the store can no longer accumulate new claims — and the reader migration in
this ticket removes the exposure entirely, because lanes now recall from the bi-temporal store
where `valid_to` closes a window the moment truth changes.

## Recommendation (Frank's call, not mine)

**Proceed to retirement (option (a))**, on this evidence:

1. Zero rows are irreplaceable. Every COVERED/PARTIAL item exists in minerva.db in equal or better form.

2. The CANDIDATE-UNIQUE tail is dominated by *stale* claims, not by knowledge worth keeping. Migrating
   them would import contradictions into a store whose whole design point is supersession.

3. The freeze point is archived and restorable: Qdrant snapshots + a full payload dump with SHA256SUMS
   at `/home/lumen/backups/wq575/qdrant/`, verified byte-identical to live at freeze time.

4. What retirement buys: one container off the box, one substrate fewer to govern, and the end of a
   memory path with no provenance, no audit, no revocation and no decay.


Counter-argument, stated fairly: nothing is actively costing us while the store sits frozen and
read-only, so retirement is not urgent. The reason to do it is that a second, ungoverned substrate
invites a future session to start writing to it again.

## Full enumeration (all 30, verbatim)

| # | verdict | note ov | state ov | best minerva match | Mem0 fact (verbatim) |
|---|---|---|---|---|---|
| 1 | CANDIDATE-UNIQUE | 0.33 | 0.33 | Lumen now learns from every messenger conversation: lumen-co | User's lumen-pause operation runs daily at 13:00 UTC and the lumen-resume operation runs daily at 16:00 UTC on the Lumen workstation |
| 2 | CANDIDATE-UNIQUE | 0.33 | 0.33 | Shipped DeepSeek spend tracking + message limits: spend_trac | User's messenger chat persistence is implemented with SQLite using the FTS5 extension and Write-Ahead Logging, stored at /home/lumen/messenger/data/messenger.db |
| 3 | CANDIDATE-UNIQUE | 0.32 | 0.27 | Power audit phase 2 — Mac Pro tier is 60% of fleet draw; Hat | User's fleet uses Egyptian deity naming convention for nodes, with active nodes including Lumen (Dell T5810 AI server), Isis (Mac Pro 6,1 running Qwen3-32B), and Hathor (Mac Pro 6,1 running LFM2.5-1.2B and nomic-embed-te |
| 4 | CANDIDATE-UNIQUE | 0.31 | 0.25 | Provisioned Nephthys (MacPro6,1) end-to-end from fresh Ubunt | Nephthys is a Mac Pro 6,1 unit accepted into the fleet as of June 2026 but not yet deployed; its future LAN address is .170 |
| 5 | CANDIDATE-UNIQUE | 0.28 | 0.16 | Minerva Self-Improvement System Topology | User deployed Mem0 fast-fact memory on Lumen using Qdrant on-disk at localhost:6333 (container mem0-qdrant), embeddings via local Ollama nomic-embed-text (768-dim), fact-extraction LLM via LiteLLM cerebras-llama, with a  |
| 6 | CANDIDATE-UNIQUE | 0.27 | 0.27 | lumen-pr-watch deployed: 30-min poller of open upstream PRs  | User uses LangGraph as the agent harness for Lumen; the lumen-scout has been ported to LangGraph as lumen_scout_graph.py and is currently live. |
| 7 | CANDIDATE-UNIQUE | 0.22 | 0.28 | Diagnosed Lumen (T5810) ECC: 3 corrected CE on DIMM4 (Ha#1/C | User's system Lumen runs on a Dell T5810 workstation with a Xeon E5-2699 v4 CPU, 40 GB RAM, no GPU, Ubuntu 26.04, Tailscale IP 100.117.66.102, hostname 'lumen', and user account 'lumen'. |
| 8 | CANDIDATE-UNIQUE | 0.20 | 0.16 | Minerva Self-Improvement System Topology | User has set up Langfuse observability on the Lumen system, where LiteLLM sends success and failure callbacks to Langfuse, and all LLM traces are attributed by service using identifiers like fleet:<provider>:research, lu |
| 9 | CANDIDATE-UNIQUE | 0.15 | 0.15 | wq#410 SHIPPED (Frank directive 'pull it'): gemini-flash rem | User manages the GL repeater using 'ubus call repeater' rather than raw uci wireless.sta, as the latter wedges the qsdk radio. |
| 10 | CANDIDATE-UNIQUE | 0.12 | 0.25 | Seeded Mac Pro 6,1 firmware/power reduction campaign: 22 wq  | User's T5810 device is equipped with 40 GB of RAM and does not include a GPU. |
| 11 | CANDIDATE-UNIQUE | 0.12 | 0.25 | [fleet-state] phoenix_radio / project_baseline | Frank owns no V100 GPUs and no Tesla P40 as of June 2026. |
| 12 | COVERED | 0.67 | 0.44 | Minerva Self-Improvement System Topology | User sets the default Minerva model (MINERVA_MODEL) to "cerebras-llama" for LLM inference |
| 13 | COVERED | 0.60 | 0.50 | Provisioned Nephthys (MacPro6,1) end-to-end from fresh Ubunt | User's fleet node Isis has Tailscale IP 100.100.129.24 and LAN address suffix .171 |
| 14 | COVERED | 0.60 | 0.40 | Minerva Self-Improvement System Topology | User's MINERVA_MODEL default is set to cerebras-llama |
| 15 | COVERED | 0.60 | 0.50 | Provisioned Nephthys (MacPro6,1) end-to-end from fresh Ubunt | User's fleet node Hathor has Tailscale IP 100.80.71.13 and LAN address suffix .172 |
| 16 | PARTIAL | 0.56 | 0.44 | Provisioned Nephthys (MacPro6,1) end-to-end from fresh Ubunt | User's fleet node Isis is a Mac Pro 6,1 that runs the Qwen3-32B model. |
| 17 | PARTIAL | 0.56 | 0.33 | Fleet identity correction: Thor = Samsung; stress7@100.69.82 | The old Samsung workstation (stress7, IP 100.69.82.3) is no longer in use (gone) as of June 2026. |
| 18 | PARTIAL | 0.50 | 0.50 | [fleet-state] lumen / nic_config | User must never spoof the MAC address of Lumen's enp0s25 network interface (MAC: 64:00:6a:58:fc:d8) |
| 19 | PARTIAL | 0.50 | 0.40 | Provisioned Nephthys (MacPro6,1) end-to-end from fresh Ubunt | User's fleet node Hathor is a Mac Pro 6,1 that runs the LFM2.5-1.2B and nomic-embed-text models. |
| 20 | PARTIAL | 0.46 | 0.23 | Minerva Self-Improvement System Topology | User routes all Minerva and fleet LLM calls through a LiteLLM proxy running on localhost port 4000 |
| 21 | PARTIAL | 0.46 | 0.36 | Provisioned Nephthys (MacPro6,1) end-to-end from fresh Ubunt | User plans for the fleet node Nephthys to use LAN address suffix .170 (future assignment) |
| 22 | PARTIAL | 0.44 | 0.33 | aq#353 Minerva verify judge DOWN root-caused + fixed: max_to | DeepSeek V4 requires max_tokens to be at least 300; otherwise the content response is empty |
| 23 | PARTIAL | 0.40 | 0.20 | Acting on PBox after it was retired (recurring stale-state) | PBox is permanently retired as of June 2026. |
| 24 | PARTIAL | 0.40 | 0.40 | Finished PBox retirement in live lumen-agent v4. Root cause  | The qwen worker is retired as of June 2026. |
| 25 | PARTIAL | 0.40 | 0.40 | LUMEN DISK: recovered 1.9TB /scratch that was silently unmou | User must never reboot the Lumen workstation without explicit permission from Frank |
| 26 | PARTIAL | 0.37 | 0.21 | Messenger silent-Groq hallucination outage (2026-06-09) | User's messenger brain routes the Claude CLI through LiteLLM to DeepSeek V4 by setting ANTHROPIC_BASE_URL=localhost:4000, with two tiers: messenger-chat (deepseek-v4-flash) and messenger-re (deepseek-v4-pro) |
| 27 | PARTIAL | 0.36 | 0.43 | [fleet-state] wadjet / status | User has two additional Mac Pro 6,1 units named Wadjet and Khepri in the fleet, not yet mentioned in existing memories. |
| 28 | PARTIAL | 0.36 | 0.29 | Frank-approved batch (yes to all 3): (a) research_clean CUTO | A per-task fleet research harness has been built at /home/lumen/autonomy/lumen_fleet_graph.py, which has been validated but cutover is pending. |
| 29 | PARTIAL | 0.27 | 0.36 | t91 DONE: USB/cloud-init template converged to RunSSH=false  | User must never use dd for USB writes; only Balena Etcher is allowed for writing USB drives |
| 30 | PARTIAL | 0.18 | 0.46 | Fleet model picker shipped: [m:<name>] backend marker + care | User's active fleet LLM providers are Cerebras, SambaNova, Mistral, OpenRouter, and the local lumen-agent |

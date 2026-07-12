#!/usr/bin/env python3
"""
lumen crawl->intel taxonomy pre-filter  (STAGED — not wired to any service)
Purpose: raise signal-to-noise of items forwarded to the research index by
gating on a cheap, deterministic taxonomy score over (source, title) ONLY —
both available at ingest, BEFORE the LLM classifier runs. Zero LLM cost.

Contract: score_item(source, title) -> float in [0,1]; keep if >= THRESHOLD.
Ground-truth for tuning/eval = existing classifier self_relevance >= 0.5.
"""
import re

# --- Lumen adoption taxonomy (positive signal: what Lumen actually builds with) ---
POS = {
  # inference / serving stack
  "inference":3,"litellm":4,"ollama":4,"vllm":3,"llama.cpp":3,"gguf":3,"quantiz":3,
  "serving":2,"throughput":2,"latency":2,"tokens/s":2,"kv cache":3,"batching":2,
  # local/self-host models we run
  "qwen":3,"deepseek":3,"gemma":2,"mistral":2,"lfm2":3,"nomic":3,"embed":3,"rerank":3,
  # RAG / retrieval / memory
  "rag":3,"retrieval":3,"pgvector":4,"vector db":3,"vector database":2,"chunk":2,
  "reranking":3,"bi-temporal":3,"context window":2,"long context":2,
  # agents / orchestration (applied, not theory)
  "agent":2,"tool use":3,"function calling":3,"mcp":4,"orchestrat":2,"workflow":2,
  # infra Lumen runs
  "self-host":4,"selfhost":4,"homelab":4,"home server":3,"proxmox":3,"docker":2,
  "systemd":3,"postgres":3,"replication":3,"failover":3,"high availability":3,"tailscale":4,
  "cloudflare":2,"restic":3,"backup":2,"kernel":2,"amdgpu":2,"rocm":2,"epyc":3,"ddr4":2,"ecc":2,
  # hardware RE Lumen does
  "firmware":3,"reverse engineer":3,"reverse-engineer":3,"ghidra":3,"binwalk":3,
  "stm32":3,"initramfs":3,"uefi":2,"bootrom":2,"flash":1,
  # eval/quality
  "benchmark":2,"eval":2,"prompt":1,"fine-tun":1,"distill":1,
}
# --- anti-taxonomy (noise: theory/consumer-news Lumen can't or won't adopt) ---
NEG = {
  "funding round":3,"raises $":3,"valuation":3,"stock":2,"ipo":3,"lawsuit":3,"acquire":2,
  "theorem":3,"convergence proof":3,"pac-bayes":3,"manifold":2,"stochastic gradient":2,
  "diffusion model":2,"gan ":2,"reinforcement learning theory":3,"federated":2,
  "protein":3,"genomic":3,"climate":2,"quantum":2,"robotics arm":2,"autonomous driving":3,
  "chatbot launches":2,"announces":1,"unveils":1,"partnership":2,"ceo ":2,"hiring":2,
  "survey of":2,"a survey":2,"benchmark dataset for":1,
}
# source priors (homelab/practitioner feeds vs pure-paper firehoses)
SRC_PRIOR = {
  "r/homelab":0.25,"r/homeserver":0.25,"r/selfhosted":0.25,"Simon Willison":0.15,
  "GitHub":0.10,"Phoronix":0.10,"LWN":0.10,"Latent Space":0.05,
  "arXiv":-0.15,"HF Daily Papers":-0.10,"Semantic Scholar":-0.05,"The Decoder":-0.05,
  "TechCrunch AI":-0.15,"AI News":-0.10,"MarkTechPost":-0.05,
}

def score_item(source, title):
    t=(title or "").lower(); s=source or ""
    pos=sum(w for k,w in POS.items() if k in t)
    neg=sum(w for k,w in NEG.items() if k in t)
    raw=pos-neg
    # squash to 0..1, add source prior
    base=1/(1+2.71828**(-(raw)/2.0))      # logistic on keyword margin
    base=max(0.0,min(1.0,base+SRC_PRIOR.get(s,0.0)))
    return round(base,3)

THRESHOLD=0.45  # tuned on train split (see eval)

if __name__=="__main__":
    import sys,json
    for line in sys.stdin:
        d=json.loads(line); print(score_item(d.get("source"),d.get("title")))


# --- STAGED APPLY SURFACE (the only part measurement supports wiring) ---
_HW_AWARE = re.compile(
    r"\b(gpus?|data ?center|semiconductor|wafer|foundry|tsmc|hbm\d?|accelerator"
    r"|npu|asic|tpu|chiplet|interconnect|photonic|silicon|nvidia|cerebras"
    r"|sambanova|tenstorrent|groq)\b"
)


def is_off_domain_junk(source, title):
    """Conservative junk-drop (GUARDED, calibrated 2026-07-05 on 4580 live
    classified_intel rows). True only when the title has >=3 NEG markers, zero
    on-domain POS keyword, AND is not a 'survey' or 'valuation' item. The
    survey/valuation guard removes the 3 false-drops the ungarded neg>=3 rule
    caused (two valued survey papers + a hardware-funding item). On current data:
    drops 37 junk, ZERO items with classifier self_relevance>=0.5. Callers MUST
    log every drop to jsonl so any future false-drop is recoverable."""
    t = (title or "").lower()
    if "survey" in t or "valuation" in t:
        return False
    if _HW_AWARE.search(t):
        return False  # keep hardware/semiconductor/compute market news (incl funding) -- Frank 2026-07-05
    negs = sum(w for k, w in NEG.items() if k in t)
    poss = sum(w for k, w in POS.items() if k in t)
    return negs >= 3 and poss == 0

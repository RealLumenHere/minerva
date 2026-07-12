#!/usr/bin/env python3
"""
MINERVA SCRAPING SOURCES — EXPANDED (2026-06-13)

Defines all sources for lumen_scout to ingest: blogs, RSS, Reddit, GitHub, YouTube RSS.
Replaces narrow AI-only sources with comprehensive hardware, tools, security, optimization focus.

TIERS:
- Tier 1: Hardware & Repurposing (self_relevance: 0.95)
- Tier 2: Dev Tools & Infrastructure (self_relevance: 0.85)
- Tier 3: Security & Safety (self_relevance: 0.75)
- Tier 4: Runtimes & Performance (self_relevance: 0.70)
- Tier 5: Energy & Sustainability (self_relevance: 0.65)
- Tier 6: Observability & Monitoring (self_relevance: 0.80)
- Tier 7: Optimization & Inference (self_relevance: 0.90)
"""

import feedparser
import requests
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
from urllib.parse import urljoin

log = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# TIER 1: HARDWARE & REPURPOSING (self_relevance=0.95)
# ────────────────────────────────────────────────────────────────────────

TIER1_SOURCES = {
    # YouTube channels (via RSS proxy like https://www.youtube.com/feeds/videos.xml?channel_id=...)
    'serve_the_home_youtube': {
        'type': 'rss',
        'url': 'https://www.youtube.com/feeds/videos.xml?channel_id=UCxQKHvKbStdF3iJG1346YQg',  # Serve the Home
        'name': 'Serve the Home (YouTube)',
        'keywords': ['server', 'hardware', 'optane', 'repurpose', 'benchmark', 'used hardware', 'salvage', 'e-waste'],
        'self_relevance': 0.95,
        'category': 'hardware',
    },
    
    'level1techs_youtube': {
        'type': 'rss',
        'url': 'https://www.youtube.com/feeds/videos.xml?channel_id=UC4w1YQAJMWOWzwrX3GRxMHg',  # Level1Techs
        'name': 'Level1Techs (YouTube)',
        'keywords': ['hardware', 'server', 'storage', 'workstation', 'reverse engineer', 'firmware', 'GPU', 'CPU'],
        'self_relevance': 0.95,
        'category': 'hardware',
    },
    
    'technodium_youtube': {
        'type': 'rss',
        'url': 'https://www.youtube.com/feeds/videos.xml?channel_id=UCo-8j2BYv2XN2L9uJDRGLiQ',  # Technodium
        'name': 'Technodium (YouTube)',
        'keywords': ['hardware', 'benchmark', 'lab', 'networking', 'storage'],
        'self_relevance': 0.93,
        'category': 'hardware',
    },
    
    'homelab_reddit': {
        'type': 'reddit',
        'subreddit': 'homelab',
        'name': 'r/homelab',
        'keywords': ['used hardware', 'cheap', 'upgrade', 'repurpose', 'e-waste', 'salvage', 'GPU', 'CPU', 'SATA', 'SSD'],
        'self_relevance': 0.90,
        'category': 'hardware',
        'sort': 'top',  # top posts weekly
    },
    
    'homeserver_reddit': {
        'type': 'reddit',
        'subreddit': 'homeserver',
        'name': 'r/homeserver',
        'keywords': ['hardware', 'upgrade', 'storage', 'networking', 'cost', 'optimization'],
        'self_relevance': 0.88,
        'category': 'hardware',
        'sort': 'top',
    },
    
    'newegg_deals': {
        'type': 'web_scrape',
        'url': 'https://www.newegg.com/specials?icid=383280',
        'name': 'Newegg Deals (Used/Refurbished)',
        'keywords': ['GPU', 'CPU', 'Xeon', 'storage', 'bulk', 'refurbished'],
        'self_relevance': 0.85,
        'category': 'hardware',
    },
}

# ────────────────────────────────────────────────────────────────────────
# TIER 2: DEV TOOLS & INFRASTRUCTURE (self_relevance=0.85)
# ────────────────────────────────────────────────────────────────────────

TIER2_SOURCES = {
    'github_projects_community_x': {
        'type': 'twitter',
        'handle': '@GHProjectsCom',
        'name': 'GitHub Projects Community (X)',
        'keywords': ['terminal', 'emulator', 'tool', 'framework', 'agent', 'orchestration', 'language', 'performance'],
        'self_relevance': 0.92,
        'category': 'tools',
    },
    
    'github_trending_web': {
        'type': 'web_scrape',
        'url': 'https://github.com/trending',
        'name': 'GitHub Trending (All Languages)',
        'keywords': ['terminal', 'orchestration', 'agent', 'framework', 'tool', 'CLI', 'performance', 'async'],
        'self_relevance': 0.88,
        'category': 'tools',
        'filters_category': ['systems', 'tools', 'rust', 'go', 'python'],
    },
    
    'programming_reddit': {
        'type': 'reddit',
        'subreddit': 'programming',
        'name': 'r/programming',
        'keywords': ['new release', 'framework', 'language', 'performance', 'library', 'tool', 'async', 'concurrency'],
        'self_relevance': 0.85,
        'category': 'tools',
        'sort': 'top',
    },
    
    'theregister_infrastructure': {
        'type': 'rss',
        'url': 'https://www.theregister.com/infrastructure/rss.xml',
        'name': 'The Register (Infrastructure)',
        'keywords': ['cloud', 'OS', 'release', 'tool', 'container', 'framework', 'deprecation'],
        'self_relevance': 0.80,
        'category': 'tools',
    },
    
    'simon_willison_expanded': {
        'type': 'rss',
        'url': 'https://simonwillison.net/feed/rss/',
        'name': 'Simon Willison\'s Blog (Expanded)',
        'keywords': ['tool', 'CLI', 'LLM', 'agent', 'framework', 'library', 'trick', 'tutorial'],
        'self_relevance': 0.90,
        'category': 'tools',
        'note': 'EXPAND coverage from AI to include dev tools, pragmatic patterns',
    },
    
    'exa_x': {
        'type': 'twitter',
        'handle': '@exa_js',
        'name': 'EXA (Tech news bot)',
        'keywords': ['framework', 'tool', 'release', 'agent', 'LLM', 'library', 'performance'],
        'self_relevance': 0.85,
        'category': 'tools',
    },
}

# ────────────────────────────────────────────────────────────────────────
# TIER 3: SECURITY & SAFETY (self_relevance=0.75)
# ────────────────────────────────────────────────────────────────────────

TIER3_SOURCES = {
    'netsec_reddit': {
        'type': 'reddit',
        'subreddit': 'netsec',
        'name': 'r/netsec',
        'keywords': ['vulnerability', 'attack', 'exploit', 'CVE', 'VPN', 'TLS', 'auth', 'firmware'],
        'self_relevance': 0.78,
        'category': 'security',
        'sort': 'top',
    },
    
    'crypto_reddit': {
        'type': 'reddit',
        'subreddit': 'crypto',
        'name': 'r/crypto',
        'keywords': ['cryptography', 'algorithm', 'attack', 'proof', 'privacy', 'encryption'],
        'self_relevance': 0.70,
        'category': 'security',
        'sort': 'top',
    },
    
    'opensecurityresearch_github': {
        'type': 'github_org',
        'org': 'opensecurityresearch',
        'name': 'OpenSecurityResearch (GitHub)',
        'keywords': ['firmware', 'reverse engineer', 'vulnerability', 'tool', 'hardware', 'security'],
        'self_relevance': 0.82,
        'category': 'security',
    },
    
    'pocgtfo_rss': {
        'type': 'rss',
        'url': 'https://www.alchemistowl.org/pocgtfo/feed.xml',
        'name': 'PoC||GTFO (zine)',
        'keywords': ['reverse engineer', 'exploitation', 'firmware', 'hardware', 'vulnerability', 'technique'],
        'self_relevance': 0.75,
        'category': 'security',
    },
}

# ────────────────────────────────────────────────────────────────────────
# TIER 4: LANGUAGE VMs, RUNTIMES, PERFORMANCE (self_relevance=0.70)
# ────────────────────────────────────────────────────────────────────────

TIER4_SOURCES = {
    'pep_announcements': {
        'type': 'rss',
        'url': 'https://www.python.org/jobs/feed/rss/',  # Fallback; ideally PEP RSS
        'name': 'Python Enhancement Proposals',
        'keywords': ['PEP', 'async', 'performance', 'deprecation', 'release', 'optimization'],
        'self_relevance': 0.72,
        'category': 'runtime',
    },
    
    'nodejs_blog': {
        'type': 'rss',
        'url': 'https://nodejs.org/en/feed/blog.xml',
        'name': 'Node.js Blog',
        'keywords': ['release', 'LTS', 'performance', 'security', 'module', 'async'],
        'self_relevance': 0.75,
        'category': 'runtime',
    },
    
    'rust_blog': {
        'type': 'rss',
        'url': 'https://blog.rust-lang.org/feed.xml',
        'name': 'Rust Blog',
        'keywords': ['release', 'safety', 'performance', 'library', 'ecosystem', 'async'],
        'self_relevance': 0.68,
        'category': 'runtime',
    },
    
    'rust_reddit': {
        'type': 'reddit',
        'subreddit': 'rust',
        'name': 'r/rust',
        'keywords': ['library', 'performance', 'benchmark', 'async', 'ecosystem', 'framework'],
        'self_relevance': 0.70,
        'category': 'runtime',
        'sort': 'top',
    },
    
    'golang_reddit': {
        'type': 'reddit',
        'subreddit': 'golang',
        'name': 'r/golang',
        'keywords': ['library', 'performance', 'concurrency', 'ecosystem', 'release'],
        'self_relevance': 0.68,
        'category': 'runtime',
        'sort': 'top',
    },
}

# ────────────────────────────────────────────────────────────────────────
# TIER 5: SUSTAINABILITY & POWER EFFICIENCY (self_relevance=0.65)
# ────────────────────────────────────────────────────────────────────────

TIER5_SOURCES = {
    'tomshardware_cpu_gpu': {
        'type': 'rss',
        'url': 'https://www.tomshardware.com/feeds/all.xml',
        'name': 'Tom\'s Hardware (CPU/GPU focus)',
        'keywords': ['benchmark', 'power consumption', 'thermal', 'efficiency', 'GPU', 'CPU', 'review'],
        'self_relevance': 0.70,
        'category': 'sustainability',
    },
    
    'ee_times_power': {
        'type': 'rss',
        'url': 'https://www.eetimes.com/feed/',
        'name': 'EE Times (Power Delivery)',
        'keywords': ['power', 'efficiency', 'thermal', 'cooling', 'semiconductor', 'architecture'],
        'self_relevance': 0.65,
        'category': 'sustainability',
    },
}

# ────────────────────────────────────────────────────────────────────────
# TIER 6: MONITORING & OBSERVABILITY (self_relevance=0.80)
# ────────────────────────────────────────────────────────────────────────

TIER6_SOURCES = {
    'grafana_blog': {
        'type': 'rss',
        'url': 'https://grafana.com/blog/rss',
        'name': 'Grafana Blog',
        'keywords': ['monitoring', 'observability', 'alert', 'metrics', 'tracing', 'feature'],
        'self_relevance': 0.82,
        'category': 'observability',
    },
    
    'cncf_landscape': {
        'type': 'web_scrape',
        'url': 'https://landscape.cncf.io/',
        'name': 'CNCF Landscape',
        'keywords': ['observability', 'tracing', 'metrics', 'alert', 'monitoring', 'logging'],
        'self_relevance': 0.78,
        'category': 'observability',
    },
    
    'sysadmin_reddit': {
        'type': 'reddit',
        'subreddit': 'sysadmin',
        'name': 'r/sysadmin',
        'keywords': ['monitoring', 'alert', 'automation', 'outage', 'postmortem', 'tool', 'observability'],
        'self_relevance': 0.80,
        'category': 'observability',
        'sort': 'top',
    },
}

# ────────────────────────────────────────────────────────────────────────
# TIER 7: OPTIMIZATION & INFERENCE (self_relevance=0.90)
# ────────────────────────────────────────────────────────────────────────

TIER7_SOURCES = {
    'ollama_blog': {
        'type': 'web_scrape',
        'url': 'https://ollama.ai/blog',
        'name': 'Ollama Official Blog',
        'keywords': ['model', 'quantization', 'Q4', 'Q5', 'performance', 'inference', 'optimization'],
        'self_relevance': 0.95,
        'category': 'inference',
    },
    
    'ggml_github': {
        'type': 'github_repo',
        'repo': 'ggerganov/llama.cpp',
        'name': 'GGML / llama.cpp',
        'keywords': ['quantization', 'GGUF', 'inference', 'optimization', 'performance', 'Q-type', 'IQ3', 'IQ4'],
        'self_relevance': 0.93,
        'category': 'inference',
    },
    
    'huggingface_blog_expanded': {
        'type': 'rss',
        'url': 'https://huggingface.co/blog/feed.xml',
        'name': 'Hugging Face Blog (Expanded)',
        'keywords': ['model', 'efficiency', 'quantization', 'distillation', 'pruning', 'inference', 'performance'],
        'self_relevance': 0.92,
        'category': 'inference',
        'note': 'EXPAND from casual to structured keyword monitoring',
    },
    
    'thebloke_hf': {
        'type': 'huggingface_user',
        'user': 'TheBloke',
        'name': 'TheBloke (HuggingFace)',
        'keywords': ['GGUF', 'quantization', 'Q4', 'Q5', 'IQ3', 'IQ4', 'model conversion', 'quality'],
        'self_relevance': 0.94,
        'category': 'inference',
    },
}

# ────────────────────────────────────────────────────────────────────────
# CONSOLIDATED SOURCE LIST
# ────────────────────────────────────────────────────────────────────────

ALL_SOURCES = {
    **TIER1_SOURCES,
    **TIER2_SOURCES,
    **TIER3_SOURCES,
    **TIER4_SOURCES,
    **TIER5_SOURCES,
    **TIER6_SOURCES,
    **TIER7_SOURCES,
}

# Keywords to monitor globally
GLOBAL_KEYWORDS = {
    'hardware_repurposing': [
        'e-waste', 'repurpose', 'salvage', 'used hardware', 'refurbished', 'cost optimization',
        'GPU pricing', 'CPU market', 'bulk lot', 'enterprise equipment',
        'thermal', 'power efficiency', '7mm height', 'SATA SSD', 'Xeon', 'Optane',
        'firmware dump', 'reverse engineer', 'hardware RE', 'JTAG', 'ST-Link', 'serial console',
    ],
    'dev_tools': [
        'terminal emulator', 'shell', 'CLI tool', 'package manager', 'orchestration',
        'new release', 'breaking change', 'deprecation', 'performance improvement',
        'framework', 'agent', 'task runner', 'workflow', 'automation',
        'Rust', 'WASM', 'async', 'concurrent', 'Alacritty', 'Zellij', 'Helix',
    ],
    'security': [
        'vulnerability', 'exploit', 'attack', 'CVE', 'firmware security',
        'VPN', 'auth', 'TLS', 'encryption', 'cryptography',
        'agentic', 'jailbreak', 'refusal', 'guardrail', 'safety',
    ],
    'optimization': [
        'quantization', 'distillation', 'pruning', 'inference', 'throughput',
        'Q4', 'Q5', 'IQ3', 'IQ4', 'GGUF', 'vLLM',
        'model routing', 'mixture of experts', 'speculative decoding',
    ],
}

if __name__ == '__main__':
    print(f"Total sources: {len(ALL_SOURCES)}")
    for tier_name in ['TIER1', 'TIER2', 'TIER3', 'TIER4', 'TIER5', 'TIER6', 'TIER7']:
        tier_var = globals()[f"{tier_name}_SOURCES"]
        print(f"{tier_name}: {len(tier_var)} sources, avg relevance {sum(s['self_relevance'] for s in tier_var.values()) / len(tier_var):.2f}")

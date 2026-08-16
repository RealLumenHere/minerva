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
    # BC-250 community tracking (Frank standing order — hapi cluster progress)
    'bc250_40cu_unlock_commits': {
        'type': 'rss',
        'url': 'https://github.com/duggasco/bc250-40cu-unlock/commits.atom',
        'name': 'bc250-40cu-unlock (commits)',
        'keywords': ['BC-250', 'CU', 'unlock', 'mask', 'amdgpu', 'cyan skillfish', 'gfx1013', 'WGP'],
        'self_relevance': 0.95,
        'category': 'hardware',
    },
    'bc250_cu_live_manager_commits': {
        'type': 'rss',
        'url': 'https://github.com/WinnieLV/bc250-cu-live-manager/commits.atom',
        'name': 'bc250-cu-live-manager (commits)',
        'keywords': ['BC-250', 'CU', 'UMR', 'live', 'mask', 'WGP', 'unlock'],
        'self_relevance': 0.95,
        'category': 'hardware',
    },
    'bc250_core_unlock_commits': {
        'type': 'rss',
        'url': 'https://github.com/rw-r-r-0644/bc250-core-unlock/commits.atom',
        'name': 'bc250-core-unlock (CPU 6c->8c, commits)',
        'keywords': ['BC-250', 'CPU core', 'unlock', 'SMU', 'SMN', 'presence mask', 'AGESA', 'PSP', 'dfps'],
        'self_relevance': 0.95,
        'category': 'hardware',
    },
    'old_lamer_youtube': {
        'type': 'rss',
        'url': 'https://www.youtube.com/feeds/videos.xml?channel_id=UCUBPEFq09LAzKqqyrejZRdA',  # Old Lamer
        'name': 'Old Lamer (YouTube, BC-250)',
        'keywords': ['BC-250', 'CU unlock', 'CPU core unlock', 'cyan skillfish', 'llama-bench', 'thermals'],
        'self_relevance': 0.9,
        'category': 'hardware',
    },
    'amd_bc250_docs_commits': {
        'type': 'rss',
        'url': 'https://github.com/elektricm/amd-bc250-docs/commits.atom',
        'name': 'amd-bc250-docs (community docs)',
        'keywords': ['BC-250', 'unlock', 'CPU core', 'firmware', 'UMA', 'governor', 'kernel'],
        'self_relevance': 0.95,
        'category': 'hardware',
    },
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

    'infoworld_rss': {
        'type': 'rss',
        'url': 'https://www.infoworld.com/feed/',
        'name': 'InfoWorld',
        'keywords': ['release', 'open source', 'framework', 'library', 'runtime', 'deprecation', 'retired', 'API', 'LLM', 'MCP'],
        'self_relevance': 0.75,
        'category': 'tools',
    },

    'itsfoss_rss': {
        'type': 'rss',
        'url': 'https://itsfoss.com/rss/',
        'name': "It's FOSS",
        'keywords': ['linux', 'terminal', 'CLI', 'tool', 'ubuntu', 'self-hosted', 'open source', 'distro', 'firmware'],
        'self_relevance': 0.78,
        'category': 'tools',
    },

    'guru3d_rss': {
        'type': 'rss',
        'url': 'https://www.guru3d.com/rss.xml',
        'name': 'Guru3D',
        'keywords': ['GPU', 'driver', 'hardware', 'firmware', 'BIOS', 'benchmark', 'power', 'cooling', 'release'],
        'self_relevance': 0.80,
        'category': 'hardware',
    },

    'omgubuntu_rss': {
        'type': 'rss',
        'url': 'https://omgubuntu.co.uk/feed',
        'name': 'OMG! Ubuntu (RSS)',
        'keywords': ['ubuntu', 'linux', 'desktop', 'release', 'package', 'GNOME', 'kernel', 'update'],
        'self_relevance': 0.85,
        'category': 'tools',
        'note': 'RSS companion to omgubuntu_x; fleet runs Ubuntu/Xubuntu 26.04-26.10',
    },

    'blog_google_rss': {
        'type': 'rss',
        'url': 'https://blog.google/rss/',
        'name': 'Google (The Keyword)',
        'keywords': ['Gemini', 'model', 'API', 'release', 'Android', 'Nest', 'open source', 'quota'],
        'self_relevance': 0.62,
        'category': 'news',
        'note': 'High consumer-marketing noise; kept low. Value = Gemini model/quota announcements.',
    },

    'arduino_blog_rss': {
        'type': 'rss',
        'url': 'https://blog.arduino.cc/feed/',
        'name': 'Arduino Blog',
        'keywords': ['board', 'MCU', 'linux', 'robotics', 'sensor', 'firmware', 'release', 'STM32', 'edge AI'],
        'self_relevance': 0.80,
        'category': 'hardware',
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

    # ── Added 2026-07-19 (Frank article-review sources) ──────────────────
    'marktechpost': {
        'type': 'rss',
        'url': 'https://www.marktechpost.com/feed/',
        'name': 'MarkTechPost',
        'keywords': ['model', 'release', 'open source', 'llm', 'agent', 'fine-tune', 'tutorial'],
        'self_relevance': 0.85,
        'category': 'tools',
    },

    'cloudflare_blog': {
        'type': 'rss',
        'url': 'https://blog.cloudflare.com/rss/',
        'name': 'Cloudflare Blog',
        'keywords': ['dns', 'zero trust', 'tunnel', 'workers', 'networking', 'security'],
        'self_relevance': 0.80,
        'category': 'tools',
    },

    'localllama_reddit': {
        'type': 'reddit',
        'subreddit': 'LocalLLaMA',
        'name': 'r/LocalLLaMA',
        'keywords': ['gguf', 'quant', 'llama.cpp', 'local', 'inference', 'vllm', 'fine-tune', 'model', 'vram'],
        'self_relevance': 0.92,
        'category': 'tools',
        'sort': 'top',
    },

    'interconnects': {
        'type': 'rss',
        'url': 'https://www.interconnects.ai/feed',
        'name': 'Interconnects (Nathan Lambert)',
        'keywords': ['open weights', 'rl', 'post-training', 'rlhf', 'model', 'analysis'],
        'self_relevance': 0.80,
        'category': 'news',
    },

    'latent_space': {
        'type': 'rss',
        'url': 'https://www.latent.space/feed',
        'name': 'Latent Space',
        'keywords': ['ai engineer', 'inference', 'local', 'production', 'agents', 'mcp'],
        'self_relevance': 0.78,
        'category': 'news',
    },

    'notebookcheck': {
        'type': 'rss',
        'url': 'https://www.notebookcheck.net/News.152.100.html?type=rss',
        'name': 'Notebookcheck',
        'keywords': ['mini pc', 'apu', 'ryzen ai max', 'strix halo', 'ram', 'local ai', 'framework', 'itx', 'npu'],
        'self_relevance': 0.62,
        'category': 'hardware',
    },

    'nature_machine_intelligence': {
        'type': 'rss',
        'url': 'https://www.nature.com/natmachintell.rss',
        'name': 'Nature Machine Intelligence',
        'keywords': ['memory', 'reinforcement learning', 'neural network', 'agent', 'representation', 'recall'],
        'self_relevance': 0.68,
        'category': 'paper',
    },

    'xda_developers': {
        'type': 'rss',
        'url': 'https://www.xda-developers.com/feed/',
        'name': 'XDA Developers',
        'keywords': ['mini pc', 'self-host', 'homelab', 'linux', 'hardware', 'local ai', 'tool'],
        'self_relevance': 0.85,
        'category': 'tools',
    },

    'virtualizationhowto': {
        'type': 'rss',
        'url': 'https://www.virtualizationhowto.com/feed/',
        'name': 'Virtualization Howto',
        'keywords': ['docker', 'compose', 'gitops', 'self-host', 'homelab', 'kubernetes', 'proxmox'],
        'self_relevance': 0.85,
        'category': 'tools',
    },

    'excellentprompts': {
        'type': 'rss',
        'url': 'https://excellentprompts.substack.com/feed',
        'name': 'Excellent AI Prompts',
        'keywords': ['prompt', 'llm', 'agent', 'gemini', 'notebooklm', 'workflow', 'ai tool'],
        'self_relevance': 0.75,
        'category': 'tools',
    },

    # ── Added 2026-07-19 batch 2 (Frank article-review sources) ──────────
    'ms_agent_framework': {
        'type': 'rss',
        'url': 'https://devblogs.microsoft.com/agent-framework/feed/',
        'name': 'Microsoft Agent Framework Dev Blog',
        'keywords': ['agent', 'skill', 'python', 'mcp', 'tool', 'framework', 'release'],
        'self_relevance': 0.80,
        'category': 'tools',
    },

    'valaxy_tech_yt': {
        'type': 'rss',
        'url': 'https://www.youtube.com/feeds/videos.xml?channel_id=UC_evcfxhjjui5hChhLE08tQ',
        'name': 'Valaxy Technologies (YouTube)',
        'keywords': ['devops', 'ci/cd', 'gitops', 'kubernetes', 'docker', 'argocd', 'terraform'],
        'self_relevance': 0.68,
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

    'phoronix_rss': {
        'type': 'rss',
        'url': 'https://www.phoronix.com/rss.php',
        'name': 'Phoronix',
        'keywords': ['linux', 'kernel', 'benchmark', 'hardware', 'driver', 'performance'],
        'self_relevance': 0.90,
        'category': 'hardware',
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
    'scmp_tech_rss': {
        'type': 'rss',
        'url': 'https://www.scmp.com/rss/36/feed',
        'name': 'SCMP Tech',
        'keywords': ['ai', 'chip', 'open source', 'nvidia', 'huawei', 'model', 'china'],
        'self_relevance': 0.65,
        'category': 'news',
    },
    'kdnuggets_rss': {
        'type': 'rss',
        'url': 'https://www.kdnuggets.com/feed',
        'name': 'KDnuggets',
        'keywords': ['python', 'llm', 'agent', 'tool', 'tutorial', 'data', 'open source'],
        'self_relevance': 0.65,
        'category': 'tools',
    },
    'venturebeat_rss': {
        'type': 'rss',
        'url': 'https://venturebeat.com/feed',
        'name': 'VentureBeat',
        'keywords': ['ai', 'model', 'open source', 'agent', 'launch', 'release'],
        'self_relevance': 0.80,
        'category': 'news',
    },
    'ninetofivemac_rss': {
        'type': 'rss',
        'url': 'https://9to5mac.com/feed/',
        'name': '9to5Mac',
        'keywords': ['ai', 'app', 'local', 'model', 'launch', 'agent'],
        'self_relevance': 0.65,
        'category': 'news',
    },
    'github_changelog_rss': {
        'type': 'rss',
        'url': 'https://github.blog/changelog/feed/',
        'name': 'GitHub Changelog',
        'keywords': ['copilot', 'actions', 'api', 'release', 'agent', 'model'],
        'self_relevance': 0.75,
        'category': 'tools',
    },

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
    
    'vllm_blog': {
        'type': 'rss',
        'url': 'https://vllm.ai/blog/rss.xml',
        'name': 'vLLM Blog',
        'keywords': ['vllm', 'inference', 'throughput', 'serving', 'day-0', 'quantization',
                     'speculative decoding', 'mixture of experts', 'kv cache', 'release'],
        'self_relevance': 0.93,
        'category': 'inference',
        'note': 'Added 2026-07-28 per article-review rule (Frank shared vllm.ai/blog/2026-07-27-k3). Complements @vllm_project in TIER8_X.',
    },
    
    'thebloke_hf': {
        'type': 'huggingface_user',
        'user': 'TheBloke',
        'name': 'TheBloke (HuggingFace)',
        'keywords': ['GGUF', 'quantization', 'Q4', 'Q5', 'IQ3', 'IQ4', 'model conversion', 'quality'],
        'self_relevance': 0.94,
        'category': 'inference',
    },

    'nlp_news_substack': {
        'type': 'rss',
        'url': 'https://nlpnews.substack.com/feed',
        'name': 'NLP News / AI Newsletter (dair-ai, Elvis Saravia)',
        'keywords': ['paper', 'llm', 'model', 'reasoning', 'agent', 'inference', 'rag', 'benchmark', 'fine-tuning', 'quantization', 'release', 'weights'],
        'self_relevance': 0.90,
        'category': 'inference',
        'note': 'Weekly digest behind dair-ai/AI-Papers-of-the-Week. 2 posts/wk: Top AI Papers (Sun) + AI Agents Weekly model releases (Sat). Leads the GitHub repo. Frank chose Substack over commits.atom 2026-07-20. Archive: /home/lumen/archives/ai-papers-of-the-week.',
    },
}

# ────────────────────────────────────────────────────────────────────────
# CONSOLIDATED SOURCE LIST
# ────────────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────────────
# TIER 8: X / TWITTER — curated follow-list (self-hosted twscrape, Lane A)
# 6 keywords each: a single on-topic match clears the >0.15 insert gate.
# ────────────────────────────────────────────────────────────────────────

TIER8_X = {
    'openclaw_x': {
        'type': 'twitter', 'handle': '@openclaw', 'name': 'OpenClaw (X)',
        'keywords': ['model', 'release', 'agent', 'open', 'launch', 'update'],
        'self_relevance': 0.8, 'category': 'tools',
    },
    'rohanpaul_x': {
        'type': 'twitter', 'handle': '@rohanpaul_ai', 'name': 'Rohan Paul (X)',
        'keywords': ['model', 'release', 'open source', 'rag', 'embedding', 'agent', 'llm'],
        'self_relevance': 0.8, 'category': 'tools',
    },
    'hashicorp_x': {
        'type': 'twitter', 'handle': '@HashiCorp', 'name': 'HashiCorp (X)',
        'keywords': ['terraform', 'vault', 'release', 'policy', 'open source', 'cloud'],
        'self_relevance': 0.75, 'category': 'tools',
    },
    'alternativeto_x': {
        'type': 'twitter', 'handle': '@AlternativeTo', 'name': 'AlternativeTo (X)',
        'keywords': ['open source', 'alternative', 'self-host', 'tool', 'release', 'app'],
        'self_relevance': 0.75, 'category': 'tools',
    },
    'datasciencedojo_x': {
        'type': 'twitter', 'handle': '@DataScienceDojo', 'name': 'Data Science Dojo (X)',
        'keywords': ['open source', 'llm', 'rag', 'agent', 'library', 'python'],
        'self_relevance': 0.75, 'category': 'tools',
    },
    'googlegemma_x': {
        'type': 'twitter', 'handle': '@googlegemma', 'name': 'Google Gemma (X)',
        'keywords': ['gemma', 'model', 'release', 'fine-tune', 'skill', 'open source'],
        'self_relevance': 0.75, 'category': 'tools',
    },
    'tom_doerr_x': {
        'type': 'twitter', 'handle': '@tom_doerr', 'name': 'Tom Doerr (X)',
        'keywords': ['open source', 'tool', 'library', 'python', 'github', 'project'],
        'self_relevance': 0.8, 'category': 'tools',
    },
    'gudanglifehack_x': {
        'type': 'twitter', 'handle': '@gudanglifehack', 'name': 'Gudang Lifehack (X)',
        'keywords': ['open source', 'tool', 'database', 'sqlite', 'project', 'release'],
        'self_relevance': 0.75, 'category': 'tools',
    },
    'theo_x': {
        'type': 'twitter', 'handle': '@theo', 'name': 'Theo t3gg (X)',
        'keywords': ['typescript', 'tool', 'open source', 'framework', 'release', 'dev'],
        'self_relevance': 0.7, 'category': 'tools',
    },
    'semianalysis_x': {
        'type': 'twitter', 'handle': '@SemiAnalysis_', 'name': 'SemiAnalysis (X)',
        'keywords': ['gpu', 'hardware', 'inference', 'memory', 'chip', 'datacenter'],
        'self_relevance': 0.75, 'category': 'hardware',
    },
    'github_projects_x': {
        'type': 'twitter', 'handle': '@GitHubProjects', 'name': 'GitHub Projects (X)',
        'keywords': ['repo', 'project', 'tool', 'release', 'open source', 'trending'],
        'self_relevance': 0.85, 'category': 'tools',
    },
    'dankornas_x': {
        'type': 'twitter', 'handle': '@DanKornas', 'name': 'Dan Kornas (X)',
        'keywords': ['agent', 'rag', 'llm', 'tool', 'open source', 'mcp'],
        'self_relevance': 0.85, 'category': 'tools',
    },
    'cline_x': {
        'type': 'twitter', 'handle': '@cline', 'name': 'Cline (X)',
        'keywords': ['agent', 'coding', 'model', 'benchmark', 'tool', 'open source'],
        'self_relevance': 0.82, 'category': 'tools',
    },
    'rasbt_x': {
        'type': 'twitter', 'handle': '@rasbt', 'name': 'Sebastian Raschka (X)',
        'keywords': ['llm', 'training', 'model', 'architecture', 'paper', 'open source'],
        'self_relevance': 0.85, 'category': 'paper',
    },
    'steipete_x': {
        'type': 'twitter', 'handle': '@steipete', 'name': 'Peter Steinberger (X)',
        'keywords': ['tool', 'agent', 'cli', 'dev', 'open source', 'mcp'],
        'self_relevance': 0.8, 'category': 'tools',
    },
    'pythonhub_x': {
        'type': 'twitter', 'handle': '@PythonHub', 'name': 'Python Hub (X)',
        'keywords': ['python', 'library', 'release', 'tool', 'package', 'new'],
        'self_relevance': 0.8, 'category': 'tools',
    },
    'huggingface_x': {
        'type': 'twitter', 'handle': '@HuggingFace', 'name': 'Hugging Face (X)',
        'keywords': ['release', 'model', 'dataset', 'open source', 'new', 'launch'],
        'self_relevance': 0.85, 'category': 'tools',
    },
    'simonw_x': {
        'type': 'twitter', 'handle': '@simonw', 'name': 'Simon Willison (X)',
        'keywords': ['llm', 'release', 'tool', 'model', 'open source', 'new'],
        'self_relevance': 0.85, 'category': 'tools',
    },
    'karpathy_x': {
        'type': 'twitter', 'handle': '@karpathy', 'name': 'Andrej Karpathy (X)',
        'keywords': ['ai', 'model', 'training', 'llm', 'release', 'new'],
        'self_relevance': 0.8, 'category': 'news',
    },
    'aiatmeta_x': {
        'type': 'twitter', 'handle': '@AIatMeta', 'name': 'Meta AI (X)',
        'keywords': ['llama', 'model', 'release', 'open', 'ai', 'new'],
        'self_relevance': 0.82, 'category': 'news',
    },
    'cwolfe_x': {
        'type': 'twitter', 'handle': '@cwolferesearch', 'name': 'Cameron R. Wolfe (X)',
        'keywords': ['paper', 'model', 'architecture', 'training', 'research', 'llm'],
        'self_relevance': 0.8, 'category': 'paper',
    },
    'servethehome_x': {
        'type': 'twitter', 'handle': '@ServeTheHome', 'name': 'ServeTheHome (X)',
        'keywords': ['server', 'hardware', 'cluster', 'cpu', 'networking', 'review'],
        'self_relevance': 0.9, 'category': 'hardware',
    },
    'level1techs_x': {
        'type': 'twitter', 'handle': '@Level1Techs', 'name': 'Level1Techs (X)',
        'keywords': ['hardware', 'linux', 'workstation', 'server', 'review', 'build'],
        'self_relevance': 0.9, 'category': 'hardware',
    },
    'geerling_x': {
        'type': 'twitter', 'handle': '@geerlingguy', 'name': 'Jeff Geerling (X)',
        'keywords': ['cluster', 'pi', 'edge', 'sbc', 'hardware', 'linux'],
        'self_relevance': 0.88, 'category': 'hardware',
    },
    'gamersnexus_x': {
        'type': 'twitter', 'handle': '@GamersNexus', 'name': 'Gamers Nexus (X)',
        'keywords': ['thermal', 'power', 'teardown', 'hardware', 'benchmark', 'review'],
        'self_relevance': 0.82, 'category': 'hardware',
    },
    'nixcraft_x': {
        'type': 'twitter', 'handle': '@nixcraft', 'name': 'nixCraft (X)',
        'keywords': ['linux', 'command', 'shell', 'sysadmin', 'tool', 'script'],
        'self_relevance': 0.88, 'category': 'tools',
    },
    'phoronix_x': {
        'type': 'twitter', 'handle': '@phoronix', 'name': 'Phoronix (X)',
        'keywords': ['linux', 'kernel', 'benchmark', 'performance', 'release', 'driver'],
        'self_relevance': 0.9, 'category': 'tools',
    },
    'omgubuntu_x': {
        'type': 'twitter', 'handle': '@omgubuntu', 'name': 'OMG! Ubuntu (X)',
        'keywords': ['ubuntu', 'linux', 'release', 'desktop', 'package', 'update'],
        'self_relevance': 0.85, 'category': 'tools',
    },
    'framework_x': {
        'type': 'twitter', 'handle': '@FrameworkPuter', 'name': 'Framework Computer (X)',
        'keywords': ['hardware', 'repair', 'modular', 'mainboard', 'upgrade', 'release'],
        'self_relevance': 0.82, 'category': 'hardware',
    },
    'bleeping_x': {
        'type': 'twitter', 'handle': '@BleepinComputer', 'name': 'BleepingComputer (X)',
        'keywords': ['vulnerability', 'ransomware', 'exploit', 'security', 'patch', 'malware'],
        'self_relevance': 0.8, 'category': 'security',
    },
    'arstechnica_x': {
        'type': 'twitter', 'handle': '@arstechnica', 'name': 'Ars Technica (X)',
        'keywords': ['release', 'processor', 'security', 'linux', 'ai', 'new'],
        'self_relevance': 0.75, 'category': 'news',
    },
    'theregister_x': {
        'type': 'twitter', 'handle': '@TheRegister', 'name': 'The Register (X)',
        'keywords': ['data center', 'enterprise', 'linux', 'release', 'cloud', 'security'],
        'self_relevance': 0.8, 'category': 'news',
    },
    'gergely_x': {
        'type': 'twitter', 'handle': '@GergelyOrosz', 'name': 'Gergely Orosz (X)',
        'keywords': ['engineering', 'software', 'tech', 'hiring', 'team', 'industry'],
        'self_relevance': 0.72, 'category': 'news',
    },
    'vllm_x': {
        'type': 'twitter', 'handle': '@vllm_project', 'name': 'vLLM (X)',
        'keywords': ['vllm', 'release', 'inference', 'model', 'performance', 'new'],
        'self_relevance': 0.9, 'category': 'framework',
    },
    'ggerganov_x': {
        'type': 'twitter', 'handle': '@ggerganov', 'name': 'Georgi Gerganov / llama.cpp (X)',
        'keywords': ['llama', 'gguf', 'release', 'model', 'inference', 'new'],
        'self_relevance': 0.9, 'category': 'framework',
    },
    'ollama_x': {
        'type': 'twitter', 'handle': '@ollama', 'name': 'Ollama (X)',
        'keywords': ['ollama', 'model', 'release', 'local', 'new', 'support'],
        'self_relevance': 0.88, 'category': 'tools',
    },
    'ai_for_success_x': {
        'type': 'twitter', 'handle': '@ai_for_success', 'name': 'Ashutosh Shrivastava (X)',
        'keywords': ['model', 'release', 'ai', 'agent', 'benchmark', 'new'],
        'self_relevance': 0.78, 'category': 'news',
    },
    'quantscience_x': {
        'type': 'twitter', 'handle': '@quantscience_', 'name': 'Quant Science (X)',
        'keywords': ['python', 'data', 'backtesting', 'quant', 'library', 'research'],
        'self_relevance': 0.7, 'category': 'tools',
    },
    'huggingmodels_x': {
        'type': 'twitter', 'handle': '@HuggingModels', 'name': 'Hugging Models (X)',
        'keywords': ['model', 'gguf', 'quant', 'release', 'moe', 'huggingface'],
        'self_relevance': 0.85, 'category': 'tools',
    },
    'robinhood_x': {
        'type': 'twitter', 'handle': '@RobinhoodApp', 'name': 'Robinhood (X)',
        'keywords': ['trading', 'market', 'finance', 'crypto', 'product', 'release'],
        'self_relevance': 0.6, 'category': 'news',
    },
    'cursor_ai_x': {
        'type': 'twitter', 'handle': '@cursor_ai', 'name': 'Cursor (X)',
        'keywords': ['coding', 'agent', 'editor', 'model', 'release', 'ide'],
        'self_relevance': 0.82, 'category': 'tools',
    },
    'swyx_x': {
        'type': 'twitter', 'handle': '@swyx', 'name': 'swyx (X)',
        'keywords': ['ai engineer', 'local', 'inference', 'agents', 'mcp', 'production'],
        'self_relevance': 0.75, 'category': 'news',
    },
    'mistralai_x': {
        'type': 'twitter', 'handle': '@MistralAI', 'name': 'Mistral AI (X)',
        'keywords': ['open weights', 'model', 'release', 'mistral', 'local', 'license'],
        'self_relevance': 0.82, 'category': 'tools',
    },
    'mattpocock_x': {
        'type': 'twitter', 'handle': '@mattpocockuk', 'name': 'Matt Pocock (X)',
        'keywords': ['typescript', 'javascript', 'types', 'tooling', 'ai sdk', 'node'],
        'self_relevance': 0.62, 'category': 'tools',
    },
    'roundtable_x': {
        'type': 'twitter', 'handle': '@RoundtableSpace', 'name': '0xMarioNawfal / Roundtable (X)',
        'keywords': ['open source', 'offline', 'local ai', 'self-host', 'repo', 'release'],
        'self_relevance': 0.55, 'category': 'news',
    },
}

ALL_SOURCES = {
    **TIER1_SOURCES,
    **TIER2_SOURCES,
    **TIER3_SOURCES,
    **TIER4_SOURCES,
    **TIER5_SOURCES,
    **TIER6_SOURCES,
    **TIER7_SOURCES,
    **TIER8_X,
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

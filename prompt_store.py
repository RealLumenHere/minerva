#!/usr/bin/env python3
"""Central prompt store for the Minerva pipeline (wq#232, 2026-07-16).

Every LLM prompt used by the pipeline lives as a plain-text file in
/home/lumen/minerva/prompts/<name>.txt — pipeline code loads by name and never
embeds prompt literals. Editing a prompt = editing a .txt file (own diff
history, no code change). Extraction was mechanical: rendered strings are
byte-identical to the pre-refactor literals (see prompts/test_equivalence.py).

Conventions:
- Files are stored with EXACTLY ONE trailing newline appended for POSIX
  friendliness; get_prompt() strips exactly one trailing '\n'. A prompt that
  genuinely ends in a newline is stored ending in two.
- Templates use str.format() placeholders; literal braces are doubled ({{ }}),
  same as the original f-strings. classifier_score and proposal_generate keep
  their original .replace() token schemes ({source}/__TITLE__ etc.) — callers
  substitute, this module only loads.
- FAIL-LOUD: a missing/unreadable prompt file raises at module import of the
  consumer -> the systemd unit fails visibly. No silent fallback by design.
"""
from pathlib import Path

_STORE = Path(__file__).resolve().parent / 'prompts'
_cache = {}


def get_prompt(name: str) -> str:
    """Return the named prompt (cached). Raises FileNotFoundError if absent."""
    if name in _cache:
        return _cache[name]
    text = (_STORE / f'{name}.txt').read_text(encoding='utf-8')
    if text.endswith('\n'):
        text = text[:-1]
    _cache[name] = text
    return text


def names():
    """List available prompt names (for tooling/tests)."""
    return sorted(p.stem for p in _STORE.glob('*.txt'))


if __name__ == '__main__':
    for n in names():
        print(f'{n}: {len(get_prompt(n))} chars')

#!/usr/bin/env python3
"""
cpp_formatter.py — Concretized Proposition Prompting (CPP) for the Minerva
proposal prompt path. wq#112, proposal dea6f7fb600f.

Source: Lee, Jeon, Shin & Lim, "Concretized Proposition Prompting Resolves
Composition-Knowledge Dichotomy in Large Language Models", arXiv:2607.08018
(2026-07-09). CPP stage 1 generates question-relevant propositions concretized
into four categories (TP/TN/FP/FN); stage 2 grounds the answer in them.

Adaptation here (single-call, in-prompt): instead of the paper's two-model
pipeline, we inject the proposition stage as instructions into the existing
proposal prompt and require the propositions in the output JSON (auditable,
ignored by downstream save_proposal). Stage-3 DSPy optimization is out of
scope. Grounding source is LUMEN CURRENT STATE, not model memory.

GUARANTEES:
- Flag-gated: /etc/lumen/prompt_engine.cfg cpp_enabled=true (default false).
  Env override CPP_ENABLED=1/0 wins over the file (test hook).
- Fail-open: ANY error returns the prompt unchanged.
- Flag off => cpp_format(p) is p, byte-identical.
"""
import os

CFG_PATH = '/etc/lumen/prompt_engine.cfg'

# Anchor: the injection point is immediately before the JSON output spec in
# minerva_proposal.PROMPT_TMPL. If the anchor is missing (template drift),
# we fail open and return the prompt unchanged.
_ANCHOR = 'Return ONLY a JSON object (no prose):'

_CPP_STAGE = """CONCRETIZED PROPOSITION STAGE (do this BEFORE writing the proposal):
Concretize up to 3 propositions per category below, each a single specific,
falsifiable sentence grounded ONLY in the LUMEN CURRENT STATE and the intel
item above. Never invent services, files, or capabilities.
- TP (Affirming a Fact): true statements about Lumen's current state that the
  proposal relies on.
- TN (Negating a Fallacy): explicit "Lumen does NOT ..." statements ruling out
  capabilities one might wrongly assume exist.
- FP (Affirming a Fallacy): plausible-sounding but FALSE claims about Lumen a
  careless proposal might assume — listed so you can avoid them.
- FN (Negating a Fact): erroneous denials of things Lumen actually has —
  listed so you do not duplicate existing capability.
Then write the proposal grounded ONLY in your TP and TN propositions; the FP
and FN lists are pitfalls to steer around. Include all four lists in the JSON
under "propositions".

Return ONLY a JSON object (no prose):"""

_CPP_JSON_FIELD = '''  "propositions": {"TP": ["<...>"], "TN": ["<...>"], "FP": ["<...>"], "FN": ["<...>"]},
  "title"'''


def cpp_enabled() -> bool:
    """Flag check. Env CPP_ENABLED overrides the config file; default False."""
    try:
        env = os.environ.get('CPP_ENABLED')
        if env is not None:
            return env.strip().lower() in ('1', 'true', 'yes', 'on')
        with open(CFG_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                if k.strip() == 'cpp_enabled':
                    return v.strip().lower() in ('1', 'true', 'yes', 'on')
    except Exception:
        pass
    return False


def cpp_format(prompt: str) -> str:
    """Rewrite a proposal-generation prompt with the CPP stage.
    Flag off, anchor missing, or any error => original prompt, unchanged."""
    try:
        if not cpp_enabled():
            return prompt
        if not isinstance(prompt, str) or _ANCHOR not in prompt:
            return prompt
        out = prompt.replace(_ANCHOR, _CPP_STAGE, 1)
        # Add the propositions field as the first key of the JSON spec so the
        # model emits propositions before the proposal (order = staging).
        if '\n  "title"' in out:
            out = out.replace('\n  "title"', '\n' + _CPP_JSON_FIELD, 1)
        return out
    except Exception:
        return prompt


if __name__ == '__main__':
    import sys
    data = sys.stdin.read()
    sys.stdout.write(cpp_format(data))

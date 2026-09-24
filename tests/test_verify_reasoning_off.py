import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import minerva_verify as mv  # noqa: E402


def test_cerebras_rungs_get_none():
    for m in ('cerebras-oss-120b', 'cerebras-llama', 'cerebras-gptoss-pinned'):
        assert mv._reasoning_off(m) == 'none'


def test_other_rungs_keep_sentinel():
    for m in ('deepseek-v4-flash', 'deepseek-v4-pro', 'gemini-flash', 'qwen36-local'):
        assert mv._reasoning_off(m) == 'disable'


def test_judge_payload_uses_rung_value(monkeypatch):
    seen = []

    class R:
        status_code = 200

        def json(self):
            return {'choices': [{'message': {'content': '{"labels": ["SUPPORTED"], "rationale": "x"}'}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append((json['model'], json['reasoning_effort']))
        return R()

    monkeypatch.setattr(mv.requests, 'post', fake_post)
    mv._judge_once(['a claim'], 'ctx', 'cerebras-oss-120b', '')
    mv._judge_once(['a claim'], 'ctx', 'deepseek-v4-flash', '')
    assert seen == [('cerebras-oss-120b', 'none'), ('deepseek-v4-flash', 'disable')]

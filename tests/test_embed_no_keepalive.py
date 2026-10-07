import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import minerva_memory as mm  # noqa: E402
import minerva_skills as ms  # noqa: E402
import minerva_verify as mv  # noqa: E402

# wq#3558: an /api/embed request that carries keep_alive, landing on an
# already-loaded nomic while a chat model waits to evict it, overwrites the
# scheduler's unload mark. Lumen runs OLLAMA_MAX_LOADED_MODELS=1, so every chat
# hung until the pin lapsed. Embed clients must leave keep_alive to the server.


class R:
    status_code = 200

    def json(self):
        return {'embeddings': [[0.1] * 768]}


@pytest.mark.parametrize('call', [
    lambda: mm.embed('a note'),
    lambda: ms.embed('a skill'),
    lambda: mv._embed_batch(['a sentence']),
], ids=['minerva_memory.embed', 'minerva_skills.embed', 'minerva_verify._embed_batch'])
def test_embed_payload_has_no_keep_alive(monkeypatch, call):
    seen = []

    def fake_post(url, json=None, timeout=None, **kw):
        seen.append((url, json))
        return R()

    for mod in (mm, ms, mv):
        monkeypatch.setattr(mod.requests, 'post', fake_post)
    call()
    assert seen, 'embed never reached requests.post'
    for url, body in seen:
        assert url.endswith('/api/embed')
        assert 'keep_alive' not in body

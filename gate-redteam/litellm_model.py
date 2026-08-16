"""DeepEvalBaseLLM adapter routing to Lumen LiteLLM gateway (deepseek-v4-flash).
No paid API: all calls go to http://127.0.0.1:4000. Reasoning-model safe (max_tokens>=800).
"""
import os, json, requests
from deepeval.models.base_model import DeepEvalBaseLLM

_ENV = "/home/lumen/agent/.env"
def _master_key():
    for line in open(_ENV):
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("no LITELLM_MASTER_KEY in agent/.env")

BASE = os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000")

class LiteLLMModel(DeepEvalBaseLLM):
    def __init__(self, model="deepseek-v4-flash", max_tokens=2000, temperature=0.0):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._key = _master_key()
        super().__init__(model)

    def load_model(self):
        return self.model

    def get_model_name(self):
        return f"litellm/{self.model}"

    def _call(self, prompt, schema=None):
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        r = requests.post(f"{BASE}/v1/chat/completions",
                          headers={"Authorization": f"Bearer {self._key}"},
                          json=body, timeout=120)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"].get("content") or ""
        if schema is not None:
            # deepeval passes a pydantic schema when it wants structured output
            txt = content.strip()
            if txt.startswith("```"):
                txt = txt.split("```", 2)[1].lstrip("json").strip() if "```" in txt else txt
            try:
                return schema.model_validate_json(txt)
            except Exception:
                # last-ditch: extract first {...}
                import re
                m = re.search(r"\{.*\}", txt, re.S)
                if m:
                    return schema.model_validate_json(m.group(0))
                raise
        return content

    def generate(self, prompt, schema=None):
        return self._call(prompt, schema)

    async def a_generate(self, prompt, schema=None):
        return self._call(prompt, schema)

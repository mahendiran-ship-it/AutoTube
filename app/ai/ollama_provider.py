"""Ollama provider (local, free). API: https://github.com/ollama/ollama/blob/main/docs/api.md"""
from __future__ import annotations

import requests

from app.ai.base import LLMProvider, LLMResponse
from app.core.errors import ProviderUnavailable


class OllamaProvider(LLMProvider):
    def __init__(self, name: str, host: str, model: str, timeout: float = 300, paid: bool = False):
        self.name, self.host, self.model = name, host.rstrip("/"), model
        self.timeout, self.is_paid = timeout, paid

    def health(self) -> tuple[bool, str]:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=4)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
        except Exception as e:
            return False, f"Ollama not reachable at {self.host} ({type(e).__name__}). Is `ollama serve` running?"
        wanted = self.model if ":" in self.model else self.model + ":latest"
        if self.model in names or wanted in names:
            return True, f"ok ({self.model})"
        return False, f"model '{self.model}' not pulled. Run: ollama pull {self.model}  (have: {', '.join(names) or 'none'})"

    def generate(self, prompt, system=None, *, json_mode=False, temperature=0.7, max_tokens=None) -> LLMResponse:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body: dict = {"model": self.model, "messages": messages, "stream": False,
                      "options": {"temperature": temperature}}
        if max_tokens:
            body["options"]["num_predict"] = max_tokens
        if json_mode:
            body["format"] = "json"
        try:
            r = requests.post(f"{self.host}/api/chat", json=body, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            raise ProviderUnavailable(f"Ollama request failed: {type(e).__name__}: {e}") from e
        except ValueError as e:
            raise ProviderUnavailable("Ollama returned invalid JSON") from e
        text = (data.get("message") or {}).get("content", "")
        if not text.strip():
            raise ProviderUnavailable("Ollama returned an empty response")
        return LLMResponse(text=text, model=self.model, provider=self.name,
                           prompt_tokens=data.get("prompt_eval_count"), completion_tokens=data.get("eval_count"))

"""OpenAI-compatible provider: llama.cpp `llama-server`, LM Studio, vLLM, text-generation-webui, ...

Uses the standard `/v1/chat/completions` and `/v1/models` endpoints. No API key is needed for
most local servers; one is sent only if the configured environment variable is set.
"""
from __future__ import annotations

import requests

from app.ai.base import LLMProvider, LLMResponse
from app.core.errors import ProviderUnavailable


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, name: str, base_url: str, model: str, api_key: str | None = None,
                 timeout: float = 300, paid: bool = False):
        self.name, self.base_url, self.model = name, base_url.rstrip("/"), model
        self.api_key, self.timeout, self.is_paid = api_key, timeout, paid

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def health(self) -> tuple[bool, str]:
        try:
            r = requests.get(f"{self.base_url}/models", headers=self._headers(), timeout=4)
            if r.status_code == 200:
                return True, "ok"
            return False, f"HTTP {r.status_code} from {self.base_url}/models"
        except Exception as e:
            return False, f"endpoint not reachable at {self.base_url} ({type(e).__name__})"

    def generate(self, prompt, system=None, *, json_mode=False, temperature=0.7, max_tokens=None) -> LLMResponse:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body: dict = {"model": self.model, "messages": messages, "temperature": temperature, "stream": False}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = requests.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers(),
                              timeout=self.timeout)
            if r.status_code == 400 and json_mode:      # some servers reject response_format
                body.pop("response_format", None)
                r = requests.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers(),
                                  timeout=self.timeout)
            if r.status_code >= 400:
                detail = ""
                try:
                    detail = (r.json().get("error") or {}).get("message", "")
                except ValueError:
                    detail = r.text[:200]
                hint = " (check the model name in config/settings.yaml)" if r.status_code in (400, 404) else ""
                raise ProviderUnavailable(f"{self.name}: HTTP {r.status_code} - {detail[:200]}{hint}")
            data = r.json()
            text = data["choices"][0]["message"]["content"] or ""
        except requests.RequestException as e:
            raise ProviderUnavailable(f"{self.name} request failed: {type(e).__name__}: {e}") from e
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderUnavailable(f"{self.name} returned an unexpected response") from e
        if not text.strip():
            raise ProviderUnavailable(f"{self.name} returned an empty response")
        usage = data.get("usage") or {}
        return LLMResponse(text=text, model=data.get("model", self.model), provider=self.name,
                           prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))

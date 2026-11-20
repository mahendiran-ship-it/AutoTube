"""Provider manager: health checks, ordered fallback with a hard attempt cap, usage logging, budget gate."""
from __future__ import annotations

import time

from app.ai.base import LLMProvider, LLMResponse, extract_json
from app.ai.llama_cpp_provider import OpenAICompatibleProvider
from app.ai.ollama_provider import OllamaProvider
from app.core.errors import BudgetExceeded, ProviderUnavailable, ValidationFailed
from app.core.logger import get_logger

log = get_logger("ai")


class ProviderManager:
    def __init__(self, cfg, repo, budget=None, notifier=None, providers: dict[str, LLMProvider] | None = None):
        self.cfg, self.repo, self.budget, self.notifier = cfg, repo, budget, notifier
        self.providers: dict[str, LLMProvider] = providers if providers is not None else self._build()
        self._health: dict[str, dict] = {}
        self._notified: dict[str, float] = {}
        self.last_provider: str | None = None

    # ---- construction -------------------------------------------------
    def _build(self) -> dict[str, LLMProvider]:
        out: dict[str, LLMProvider] = {}
        for name, p in (self.cfg.get("ai.providers") or {}).items():
            if not p.get("enabled", False):
                continue
            kind = p.get("type")
            timeout = float(p.get("timeout_seconds", 300))
            if kind == "ollama":
                host = self.cfg.env("OLLAMA_HOST") or p.get("host", "http://localhost:11434")
                if not host.startswith("http"):
                    host = "http://" + host
                out[name] = OllamaProvider(name, host, p.get("model", "llama3.1:8b"), timeout, bool(p.get("paid")))
            elif kind == "openai_compatible":
                out[name] = OpenAICompatibleProvider(
                    name, p.get("base_url", "http://localhost:8080/v1"), p.get("model", "local"),
                    api_key=self.cfg.env(p.get("api_key_env")), timeout=timeout, paid=bool(p.get("paid")))
            else:
                log.warning("unknown AI provider type '%s' for '%s' — ignored", kind, name)
        return out

    def order(self) -> list[str]:
        return [n for n in (self.cfg.get("ai.fallback_order") or []) if n in self.providers]

    # ---- health -------------------------------------------------------
    def health(self, force: bool = False) -> dict[str, dict]:
        ttl = float(self.cfg.get("ai.health_cache_seconds", 60))
        now = time.time()
        for name, prov in self.providers.items():
            cached = self._health.get(name)
            if not force and cached and now - cached["at"] < ttl:
                continue
            ok, detail = prov.health()
            self._health[name] = {"ok": ok, "detail": detail, "at": now, "model": prov.model, "paid": prov.is_paid}
        return {n: {k: v for k, v in h.items() if k != "at"} for n, h in self._health.items()}

    def available(self, force: bool = False) -> bool:
        h = self.health(force)
        return any(h.get(n, {}).get("ok") for n in self.order())

    # ---- generation ---------------------------------------------------
    def generate(self, prompt: str, system: str | None = None, *, json_mode: bool = False,
                 temperature: float | None = None, kind: str = "llm", video_id: int | None = None,
                 max_tokens: int | None = None) -> LLMResponse:
        temp = float(self.cfg.get("ai.temperature", 0.7)) if temperature is None else temperature
        max_attempts = max(1, int(self.cfg.get("ai.max_attempts_per_call", 2)))
        health = self.health()
        candidates = [n for n in self.order() if health.get(n, {}).get("ok")]
        if not candidates:
            details = "; ".join(f"{n}: {health.get(n, {}).get('detail', 'unknown')}" for n in self.order()) or "no providers enabled"
            raise ProviderUnavailable(f"No healthy AI provider. {details}")
        errors: list[str] = []
        for attempt, name in enumerate(candidates[:max_attempts]):
            prov = self.providers[name]
            try:
                if self.budget:
                    self.budget.authorize(name, prov.is_paid)
                resp = prov.generate(prompt, system, json_mode=json_mode, temperature=temp, max_tokens=max_tokens)
                self.repo.add_api_usage(name, kind, True, model=resp.model, prompt_tokens=resp.prompt_tokens,
                                        completion_tokens=resp.completion_tokens, cost_inr=0.0, cost_known=True,
                                        video_id=video_id)
                self.last_provider = name
                return resp
            except BudgetExceeded:
                raise
            except ProviderUnavailable as e:
                errors.append(f"{name}: {e}")
                self.repo.add_api_usage(name, kind, False, model=prov.model, error=str(e)[:300], video_id=video_id)
                self._health.pop(name, None)       # force re-check next time
                self._alert(name, str(e))
                log.warning("provider %s failed (%s) — trying next if any", name, e)
        raise ProviderUnavailable("All AI providers failed: " + " | ".join(errors))

    def generate_json(self, prompt: str, system: str | None = None, *, temperature: float | None = None,
                      kind: str = "llm", video_id: int | None = None):
        """Return (parsed_json, LLMResponse). One corrective re-ask if the JSON is malformed."""
        resp = self.generate(prompt, system, json_mode=True, temperature=temperature, kind=kind, video_id=video_id)
        try:
            return extract_json(resp.text), resp
        except ValueError:
            fix = ("Your previous reply was not valid JSON. Reply again with ONLY a valid JSON value, "
                   "no commentary, no code fences.\n\nPrevious reply:\n" + resp.text[:3000])
            resp2 = self.generate(fix, system, json_mode=True, temperature=0.2, kind=kind + "_retry", video_id=video_id)
            try:
                return extract_json(resp2.text), resp2
            except ValueError as e:
                raise ValidationFailed("model did not return valid JSON") from e

    def _alert(self, name: str, detail: str) -> None:
        if not self.notifier:
            return
        now = time.time()
        if now - self._notified.get(name, 0) > 3600:
            self._notified[name] = now
            self.notifier.notify("provider_failed", f"AI provider '{name}' failed", detail[:300], "warning")

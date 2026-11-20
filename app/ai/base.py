"""LLM provider interface + robust JSON extraction."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"


@dataclass
class LLMResponse:
    text: str
    model: str = ""
    provider: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMProvider(ABC):
    name: str = "provider"
    is_paid: bool = False
    model: str = ""

    @abstractmethod
    def health(self) -> tuple[bool, str]:
        """(is_healthy, human-readable detail). Must not raise."""

    @abstractmethod
    def generate(self, prompt: str, system: str | None = None, *, json_mode: bool = False,
                 temperature: float = 0.7, max_tokens: int | None = None) -> LLMResponse:
        """Raise ProviderUnavailable on failure."""


def render_prompt(name: str, **variables) -> str:
    """Load prompts/<name>.txt and replace <<var>> tokens (JSON braces stay untouched)."""
    text = (PROMPT_DIR / f"{name}.txt").read_text(encoding="utf-8")
    for k, v in variables.items():
        if not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False, indent=2, default=str)
        text = text.replace(f"<<{k}>>", v)
    left = re.findall(r"<<(\w+)>>", text)
    if left:
        raise KeyError(f"prompt '{name}' has unfilled variables: {sorted(set(left))}")
    return text


def extract_json(text: str):
    """Parse the first JSON object/array found in an LLM reply (handles code fences/prose)."""
    if text is None:
        raise ValueError("empty response")
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        return json.loads(t)
    except ValueError:
        pass
    for i, ch in enumerate(t):
        if ch in "{[":
            close = "}" if ch == "{" else "]"
            depth, in_str, esc = 0, False, False
            for j in range(i, len(t)):
                c = t[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == ch:
                    depth += 1
                elif c == close:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(t[i:j + 1])
                        except ValueError:
                            break
    raise ValueError("no valid JSON found in model output")

"""OpenAI-compatible LLM client with configurable base URL for Ollama or any OpenAI-API provider."""

import os
from dataclasses import dataclass, field
from typing import Iterator

from dotenv import load_dotenv
from openai import OpenAI


@dataclass
class ClientConfig:
    model: str = "gpt-oss:120b"
    base_url: str = "http://localhost:11434/v1"  # Ollama default
    api_key: str = "ollama"                       # Ollama ignores this; real APIs need a real key
    temperature: float = 0.2
    max_tokens: int = 4096
    extra_params: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ClientConfig":
        """Load LLM connection settings from environment / .env file.

        Auto-bumps ``max_tokens`` to a reasoning-friendly default when the
        chosen model is a known thinking model (qwen3*, deepseek-r1, o1, o3,
        qwq) AND the user has not set ``LLM_MAX_TOKENS`` themselves. Without
        this, the default 4096 gets entirely consumed by the model's
        chain-of-thought reasoning, the answer comes back empty, and every
        chunk fails identically — the exact 62/63 failure mode we saw with
        qwen3.5:122b runs.
        """
        load_dotenv()
        model = os.getenv("LLM_MODEL", cls.model)
        env_max_tokens = os.getenv("LLM_MAX_TOKENS")
        if env_max_tokens is None and is_reasoning_model(model):
            resolved_max_tokens = 32_768
        else:
            resolved_max_tokens = (
                int(env_max_tokens) if env_max_tokens else cls.max_tokens
            )
        return cls(
            model       = model,
            base_url    = os.getenv("LLM_BASE_URL",    cls.base_url),
            api_key     = os.getenv("LLM_API_KEY",     cls.api_key),
            temperature = float(os.getenv("LLM_TEMPERATURE", cls.temperature)),
            max_tokens  = resolved_max_tokens,
        )


_REASONING_MODEL_HINTS = ("qwen3", "deepseek-r1", "r1-", "o1", "o3", "qwq")


def is_reasoning_model(model: str) -> bool:
    """Heuristic: model name suggests visible chain-of-thought reasoning.

    Used to auto-bump ``max_tokens`` so the answer isn't starved by the
    reasoning budget. Conservative — only flips True for names we know need
    the higher budget. Non-reasoning models keep the existing 4096 default.
    """
    m = (model or "").lower()
    return any(hint in m for hint in _REASONING_MODEL_HINTS)


class LLMClient:
    def __init__(self, config: ClientConfig):
        self.config = config
        self._client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
        )

    def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int | None = None,
        return_meta: bool = False,
    ):
        """Send one chat-completion request.

        ``max_tokens``: per-call override. Useful when one call (e.g. the bulk
        claims call) needs more budget than the default without changing the
        config for every other call.

        ``return_meta=True`` makes this return ``(content, finish_reason)``
        instead of just ``content`` — caller can detect ``finish_reason ==
        "length"`` and retry with a bigger budget.
        """
        response = self._client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=max_tokens if max_tokens is not None else self.config.max_tokens,
            **self.config.extra_params,
        )
        # Some providers (Gemma / Qwen reasoning models, certain Ollama
        # builds) return ``content=None`` when all output went into a
        # separate ``reasoning`` field or the budget was exhausted. Collapse
        # to "" so downstream parsers can call ``.strip()`` without an
        # AttributeError.
        choice = response.choices[0]
        content = choice.message.content if choice.message.content is not None else ""
        if return_meta:
            return content, choice.finish_reason
        return content

    def stream(self, messages: list[dict]) -> Iterator[str]:
        stream = self._client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            stream=True,
            **self.config.extra_params,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

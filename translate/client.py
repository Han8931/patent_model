"""OpenAI-compatible LLM client with configurable base URL for Ollama or any OpenAI-API provider.

Supports two providers:

* ``openai`` (default) — vanilla OpenAI-style endpoint (works for OpenAI,
  Ollama, vLLM, llama.cpp, and any other OpenAI-compatible API). Auth is a
  single ``Authorization: Bearer <api_key>`` header.

* ``gauss`` — Samsung Gauss-O4 endpoint. Adds static authentication /
  identity headers (``x-dep-ticket``, ``Send-System-Name``, ``User-Id``,
  ``User-Type``) on every request, and a fresh per-request pair of UUIDs
  (``Prompt-Msg-Id``, ``Completion-Msg-Id``). Configured via ``GAUSS_*``
  env variables; opt in with ``--gauss`` on the CLI.
"""

import os
import uuid
from dataclasses import dataclass, field
from typing import Iterator, Literal

from dotenv import load_dotenv
from openai import OpenAI


Provider = Literal["openai", "gauss"]


@dataclass
class ClientConfig:
    provider: Provider = "openai"
    model: str = "gpt-oss:120b"
    base_url: str = "http://localhost:11434/v1"  # Ollama default
    api_key: str = "ollama"                       # Ollama ignores this; real APIs need a real key
    temperature: float = 0.2
    max_tokens: int = 4096
    extra_params: dict = field(default_factory=dict)

    # Gauss-only — populated when provider == "gauss". For "openai" these stay
    # empty and never reach the HTTP layer.
    gauss_credential: str = ""
    gauss_system_name: str = "patent_translator"
    gauss_user_id: str = ""
    gauss_user_type: str = "AD_ID"

    @classmethod
    def from_env(cls, provider: Provider = "openai") -> "ClientConfig":
        """Load LLM connection settings from environment / .env file.

        For ``provider="openai"``, reads the existing ``LLM_*`` variables so
        the default behavior is unchanged. For ``provider="gauss"``, reads
        ``GAUSS_*`` variables (falling back to ``LLM_*`` for the shared
        knobs like temperature / max_tokens).
        """
        load_dotenv()
        if provider == "gauss":
            return cls(
                provider="gauss",
                model            = os.getenv("GAUSS_MODEL",            "GaussO4.1-260330"),
                base_url         = os.getenv("GAUSS_BASE_URL",         ""),
                api_key          = os.getenv("GAUSS_API_KEY",          os.getenv("OPENAI_API_KEY", "")),
                temperature      = float(os.getenv("LLM_TEMPERATURE",  cls.temperature)),
                max_tokens       = int(os.getenv("LLM_MAX_TOKENS",     cls.max_tokens)),
                gauss_credential = os.getenv("GAUSS_CREDENTIAL",       ""),
                gauss_system_name= os.getenv("GAUSS_SYSTEM_NAME",      "patent_translator"),
                gauss_user_id    = os.getenv("GAUSS_USER_ID",          ""),
                gauss_user_type  = os.getenv("GAUSS_USER_TYPE",        "AD_ID"),
            )
        return cls(
            provider    = "openai",
            model       = os.getenv("LLM_MODEL",       cls.model),
            base_url    = os.getenv("LLM_BASE_URL",    cls.base_url),
            api_key     = os.getenv("LLM_API_KEY",     cls.api_key),
            temperature = float(os.getenv("LLM_TEMPERATURE", cls.temperature)),
            max_tokens  = int(os.getenv("LLM_MAX_TOKENS",    cls.max_tokens)),
        )


class LLMClient:
    def __init__(self, config: ClientConfig):
        self.config = config
        kwargs = {"base_url": config.base_url, "api_key": config.api_key}
        if config.provider == "gauss":
            missing = [
                name for name, val in (
                    ("base_url", config.base_url),
                    ("gauss_credential (GAUSS_CREDENTIAL)", config.gauss_credential),
                    ("gauss_user_id (GAUSS_USER_ID)", config.gauss_user_id),
                ) if not val
            ]
            if missing:
                raise RuntimeError(
                    "Gauss provider is missing required configuration: "
                    + ", ".join(missing)
                )
            kwargs["default_headers"] = {
                "x-dep-ticket":     config.gauss_credential,
                "Send-System-Name": config.gauss_system_name,
                "User-Id":          config.gauss_user_id,
                "User-Type":        config.gauss_user_type,
            }
        self._client = OpenAI(**kwargs)

    def _per_call_headers(self) -> dict[str, str]:
        """Headers that must change on every request. Empty for OpenAI."""
        if self.config.provider == "gauss":
            return {
                "Prompt-Msg-Id":     str(uuid.uuid4()),
                "Completion-Msg-Id": str(uuid.uuid4()),
            }
        return {}

    def complete(self, messages: list[dict]) -> str:
        kwargs = {
            "model":       self.config.model,
            "messages":    messages,
            "temperature": self.config.temperature,
            "max_tokens":  self.config.max_tokens,
            **self.config.extra_params,
        }
        extra_headers = self._per_call_headers()
        if extra_headers:
            kwargs["extra_headers"] = extra_headers
        response = self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content

    def stream(self, messages: list[dict]) -> Iterator[str]:
        kwargs = {
            "model":       self.config.model,
            "messages":    messages,
            "temperature": self.config.temperature,
            "max_tokens":  self.config.max_tokens,
            "stream":      True,
            **self.config.extra_params,
        }
        extra_headers = self._per_call_headers()
        if extra_headers:
            kwargs["extra_headers"] = extra_headers
        stream = self._client.chat.completions.create(**kwargs)
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
